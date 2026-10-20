"""Host-managed MCP receipts and guarded direct HTML; no provider credentials."""
import copy
from collections import Counter
import hashlib
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .common import digest, normalize_url, parse_time, public_url, stable_id, utc_now
from .extraction import classify, extract_html
from .ingest import add_version
from .network import (HTML_REQUEST_HEADERS, direct_fetch, pending_attempts, _attempt_descriptor,
                      _collection_indexes, _invalidate_source_reviews, _retry_closed)

SECRET = re.compile(r'(?:jina_|fc-)[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9_.-]{12,}', re.I)
PAGE_METADATA = {'title','description','language','canonicalURL','robots','keywords','author',
                 'ogUrl','ogTitle','ogDescription','ogImage','ogLocale','ogSiteName','ogType',
                 'twitterTitle','twitterDescription','twitterImage','twitterCard'}


def check_config(config):
    """The managed workflow never accepts analyst-supplied credentials."""
    encoded = json.dumps(config)
    if SECRET.search(encoded) or re.search(r'"(?:api[_-]?key|jina_api_key|firecrawl_api_key|authorization|access_token)"\s*:', encoded, re.I):
        raise ValueError('Credentials do not belong in run configuration; use an existing account connection')


def _allowed(state, url):
    if not isinstance(url, str) or not public_url(url, state['config']['domains']):
        raise ValueError('Observed URL is unsafe or outside approved domains')
    return normalize_url(url)


def _invalidate(state, sid, reason):
    _, claims, aliases = _collection_indexes(state)
    _invalidate_source_reviews(state, sid, reason, claims, aliases)


def _rows(state, sid, scope, reason):
    for row in state['inventory']:
        if row.get('source_id') == sid and row.get('state') != 'duplicate':
            row.update(state=scope, reason=reason)


def extract_markdown(text, metadata):
    """Keep the exact Markdown for evidence; missing HTML is never invented."""
    if not isinstance(text, str) or not isinstance(metadata, dict):
        raise ValueError('Markdown capture needs text and a metadata mapping')
    heads = [{'level':len(m[1]), 'text':m[2].strip()} for m in re.finditer(r'(?m)^(#{1,6})\s+(.+)$', text)]
    title = metadata.get('title', '')
    if not isinstance(title, str):
        raise ValueError('Metadata title is malformed')
    lower = (title + ' ' + text[:700]).lower()
    quality = 'empty_or_thin' if len(text.strip()) < 40 else 'usable'
    if any(x in lower for x in ('verify you are human', 'checking your browser', 'just a moment', 'access denied')):
        quality = 'challenge'
    elif any(x in lower for x in ('sign in to continue', 'log in to continue', 'enter your password')):
        quality = 'login'
    warnings = ['Markdown representation: HTML structure and JSON-LD not captured in this version; main-content filtering can omit material facts']
    if 'noindex' in str(metadata.get('robots','')).lower():
        warnings.append('Noindex source requires explicit inclusion review')
    if metadata.get('canonicalURL') and metadata.get('sourceURL') and normalize_url(metadata['canonicalURL']) != normalize_url(metadata['sourceURL']):
        warnings.append('Canonicalized-away source requires explicit inclusion review')
    if re.search(r'https?://(?:www\.)?(?:youtube\.com|youtu\.be)/',text,re.I):
        warnings.append('Embedded media/listing evidence: titles and player controls do not establish a spoken transcript')
    page_metadata = {key:value for key,value in metadata.items() if key in PAGE_METADATA}
    return {'text':text, 'evidence':{'body':text, 'metadata':json.dumps(page_metadata, ensure_ascii=False, sort_keys=True), 'schema':''},
        'headings':heads, 'links':[], 'language':metadata.get('language'), 'canonical':metadata.get('canonicalURL'),
        'og_url':metadata.get('ogUrl'), 'robots':metadata.get('robots'), 'quality':quality,
        'classification':classify(metadata.get('sourceURL'), title, heads, []),
        'warnings':warnings}


def _capture(state, sid, raw, extracted, representation, settings, observations, invalidate=True):
    source = state['sources'][sid]
    source['render_fallback_eligible'] = False
    for field in ('sourceURL', 'url', 'finalURL', 'canonicalURL', 'ogUrl'):
        value = observations.get(field)
        if value:
            observed = _allowed(state, value)
            if field in ('sourceURL', 'url', 'finalURL') and observed != source['normalized_url']:
                raise ValueError('Observed destination differs from requested identity; retain for alias review')
    prior = source.get('active_version')
    vid = add_version(source, raw, extracted, representation, state['config'], settings)
    version = source['versions'][vid]
    version.update(retrieved_at=observations['retrieved_at'], collector=observations['collector'],
        quality=extracted['quality'],
        capture_date_basis=observations.get('capture_date_basis','unknown; retrieval is not origin capture time'),
        completeness='complete returned representation; page coverage depends on format/filters',
        transport_artifact_sha256=observations.get('artifact_sha256'),
        metadata_observations=copy.deepcopy(observations))
    status = observations.get('statusCode')
    source['captured_status'] = status if type(status) is int else None
    source['captured_status_origin'] = observations['status_origin']
    source['availability'] = 'live' if observations.get('direct_observation') and status == 200 else 'unknown'
    if invalidate and prior != vid:
        _invalidate(state, sid, 'Source representation changed; repeat evidence/business/publication reviews')
    source['collection_state'] = 'complete'
    quality = extracted['quality']
    scope = 'included'
    reason = 'Captured evidence; human review remains required'
    if quality != 'usable' or any('requires' in w.lower() for w in extracted.get('warnings', [])):
        scope, reason = 'pending', 'Source quality/relevance review: ' + quality
    if quality in ('login', 'challenge'):
        source['availability'] = 'nonpublic'
        _invalidate(state, sid, 'Nonpublic or challenge source')
    source.update(scope=scope, scope_reason=reason)
    blocks = Counter(line.strip() for line in extracted['text'].splitlines() if len(line.strip()) > 80)
    if any(count > 1 for count in blocks.values()):
        version['warnings'] = list(dict.fromkeys(version['warnings'] + ['Repeated content blocks detected; review carousel clones before drafting unique facts']))
    _rows(state, sid, scope, reason)
    return vid


def budget_status(state):
    requests = state.get('managed_collection', [])
    if any(r.get('credits_used') is not None and not _valid_credits(r['credits_used']) for r in requests):
        raise ValueError('Malformed recorded provider credit telemetry; reconcile before dispatch')
    return {'requests_reserved':sum(len(r['source_ids']) for r in requests),
        'credits_reported':sum(r.get('credits_used', 0) for r in requests if type(r.get('credits_used')) in (int,float)),
        'credits_reserved_unknown':sum(r['credit_reservation'] for r in requests if r.get('credits_used') is None),
        'unit':'Firecrawl credits; not Jina units or host model tokens'}


def _valid_credits(value):
    return (type(value) is int and value >= 0
            or type(value) is float and math.isfinite(value) and value >= 0)


def _robots_checks(state, sources, fetch):
    """A fresh public robots check precedes every managed dispatch reservation."""
    policies, checks = {}, {}
    for source in sources:
        url = source['normalized_url']
        origin = urlsplit(url)
        host = origin.scheme+'://'+origin.netloc
        if host not in policies:
            response = fetch(host+'/robots.txt',state['config'],max_bytes=256*1024,
                             headers={'User-Agent':'OKF-Knowledge-Bundle/0.2.1'})
            _allowed(state,response.get('url',host+'/robots.txt'))
            status, body = response.get('status'), response.get('body')
            if type(status) is not int or not isinstance(body,bytes) or len(body) > 256*1024:
                raise ValueError('Malformed robots response; no managed request reserved')
            parser = None
            if status == 200:
                parser = RobotFileParser()
                parser.parse(body.decode('utf-8',errors='replace').splitlines())
            elif status not in (404,410):
                raise ValueError('Robots policy could not be checked; no managed request reserved')
            policies[host] = parser, {'at':utc_now(),'status':status,'response_hash':digest(body),
                                     'observed_url':response.get('url',host+'/robots.txt')}
        parser, observation = policies[host]
        if parser and not parser.can_fetch('OKF-Knowledge-Bundle',url):
            raise ValueError('Robots policy disallows the URL; do not bypass through managed rendering')
        checks[source['id']] = {**observation,'url':url,'allowed':True}
    return checks


def prepare_mcp(state, source_ids, settings, reference, robots_fetch=None):
    check_config(state['config'])
    if not state['config'].get('collection_authorized') or not reference:
        raise ValueError('Target/scope/budget authorization and its reference are required')
    if not source_ids or len(set(source_ids)) != len(source_ids):
        raise ValueError('Select a nonempty unique source set')
    defaults = {'formats':['markdown'],'onlyMainContent':True,'maxAge':0,'timeout':45000,'waitFor':0,
        'maxConcurrency':1,'proxy':'basic','skipTlsVerification':False,'ignoreInvalidURLs':False}
    settings = {**defaults,**settings}
    allowed_settings = {'formats','onlyMainContent','maxAge','timeout','waitFor','maxConcurrency','proxy',
                        'skipTlsVerification','ignoreInvalidURLs','removeBase64Images','storeInCache'}
    if set(settings) - allowed_settings or SECRET.search(json.dumps(settings)):
        raise ValueError('Unsupported or credential-bearing MCP collection settings')
    formats = settings.get('formats', ['markdown'])
    if not isinstance(formats,list) or not formats or set(formats) - {'markdown','html','rawHtml'} or len(set(formats)) != len(formats):
        raise ValueError('Use evidence formats only; summaries, agent and LLM extraction are not collection')
    if settings.get('skipTlsVerification', False) is not False or settings.get('proxy', 'basic') != 'basic':
        raise ValueError('Use TLS verification and the bounded basic scrape policy')
    if type(settings.get('maxAge',0)) is not int or settings.get('maxAge',0) < 0:
        raise ValueError('Cache policy must be a nonnegative millisecond value')
    if (type(settings['onlyMainContent']) is not bool or settings['ignoreInvalidURLs'] is not False
            or type(settings['timeout']) is not int or not 1000 <= settings['timeout'] <= 120000
            or type(settings['waitFor']) is not int or not 0 <= settings['waitFor'] <= 10000
            or type(settings['maxConcurrency']) is not int or not 1 <= settings['maxConcurrency'] <= 3):
        raise ValueError('Use bounded, explicit batch options; invalid URLs cannot be silently dropped')
    if any(key in settings and type(settings[key]) is not bool for key in ('removeBase64Images','storeInCache')):
        raise ValueError('Optional batch switches must be booleans')
    budget = state['config'].get('managed_budget', {})
    for field in ('max_requests','max_credits','per_page_credit_reservation','max_response_bytes'):
        if type(budget.get(field)) is not int or budget[field] < 1:
            raise ValueError('Configure positive managed request/credit/byte ceilings')
    status = budget_status(state)
    reservation = len(source_ids) * budget['per_page_credit_reservation']
    if status['requests_reserved'] + len(source_ids) > budget['max_requests'] or status['credits_reported'] + status['credits_reserved_unknown'] + reservation > budget['max_credits']:
        raise ValueError('Managed request/credit ceiling reached; do not dispatch')
    sources = [state['sources'][sid] for sid in source_ids]
    for source in sources:
        _allowed(state, source['normalized_url'])
        eligible_scope = source['scope'] == 'included' or (source['scope'] == 'pending' and source.get('render_fallback_eligible') is True)
        if (not eligible_scope or _retry_closed(source)
                or source['availability'] in ('failed','nonpublic') or not source['identity_confirmed'] or pending_attempts(source)):
            raise ValueError('Source scope/identity or an interrupted request requires review')
    robots_checks = _robots_checks(state,sources,robots_fetch or direct_fetch)
    now = utc_now()
    request = {'id':stable_id('mcp', [state['run_id'],now,len(state.get('managed_collection',[]))]),
        'provider':'firecrawl', 'tool':'FIRECRAWL_BATCH_SCRAPE', 'created_at':now,
        'source_ids':list(source_ids), 'settings':copy.deepcopy(settings), 'authorization_reference':reference,
        'credit_reservation':reservation, 'credits_used':None, 'status':'pending', 'attempts':[]}
    for source in sources:
        source.update(robots_check=robots_checks[source['id']],robots_disallowed=False)
        attempt = {'requested_at':now, 'completed_at':None, 'outcome':'pending', 'provider_status':None,
            'source_url':source['normalized_url'], 'version_id_at_request':source.get('active_version'),
            'settings':copy.deepcopy(settings), 'reserved_tokens':0, 'usage_tokens':None,
            'reserved_credits':budget['per_page_credit_reservation'], 'reason':'MCP request reserved before host dispatch',
            'managed_request_id':request['id']}
        attempt['attempt_id'] = stable_id('attempt',[request['id'],source['id']])
        source.setdefault('collection_attempts',[]).append(attempt)
        request['attempts'].append(_attempt_descriptor(source, len(source['collection_attempts'])-1, attempt))
        source['collection_state'] = 'pending'
    request['request_hash'] = digest(request)
    state.setdefault('managed_collection',[]).append(request)
    return {**copy.deepcopy(request), 'arguments':{'urls':[s['normalized_url'] for s in sources], **settings},
        'next_step':'Persist this plan, then call the connected host MCP. Import the complete saved result; never retype evidence.'}


def _unwrap(payload):
    """Known Composio envelopes only; no preview fallback or recursive guessing."""
    node = payload
    for _ in range(8):
        if not isinstance(node, dict):
            break
        if node.get('successful') is False or node.get('success') is False or node.get('error'):
            raise ValueError('Provider/tool failure; prior evidence reviews preserved')
        if node.get('status') in ('completed','failed','scraping','processing') and isinstance(node.get('data'),list):
            return node
        if 'data_preview' in node and 'data' not in node:
            raise ValueError('Preview is incomplete; persist the full tool result')
        if 'results' in node:
            if (not isinstance(node['results'],list) or len(node['results']) != 1
                    or not isinstance(node['results'][0],dict)):
                raise ValueError('Import exactly one batch job result per receipt')
            node = node['results'][0].get('response')
        elif isinstance(node.get('data'),dict):
            node = node['data']
        else:
            break
    raise ValueError('Unrecognized complete batch result envelope')


def import_mcp(state, receipt, base):
    """Atomic local import of an exact persisted host tool artifact."""
    check_config(state['config'])
    fields = {'request_id','request_hash','artifact','sha256','completed_at','job_id','complete'}
    if not isinstance(receipt,dict) or set(receipt) != fields or receipt['complete'] is not True:
        raise ValueError('Receipt must describe a complete persisted tool artifact')
    if not isinstance(receipt['job_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',receipt['job_id']):
        raise ValueError('Receipt needs the observed batch job identifier')
    request = next((r for r in state.get('managed_collection',[]) if r['id'] == receipt['request_id']),None)
    if not request or request['request_hash'] != receipt['request_hash'] or request['status'] != 'pending':
        raise ValueError('Unknown, changed or already imported request; do not replay')
    received = parse_time(receipt['completed_at'])
    if received < parse_time(request['created_at']) or received > parse_time(utc_now()):
        raise ValueError('Receipt time predates request or is in the future')
    path = Path(receipt['artifact'])
    path = (Path(base)/path).resolve() if not path.is_absolute() else path.resolve()
    if path.stat().st_size > state['config']['managed_budget']['max_response_bytes']:
        raise ValueError('Saved MCP response exceeds byte ceiling')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != receipt['sha256']:
        raise ValueError('Saved MCP artifact hash mismatch')
    if any(r.get('status') == 'complete' and
           (r.get('artifact_sha256') == receipt['sha256'] or r.get('job_id') == receipt['job_id'])
           for r in state.get('managed_collection',[]) if r['id'] != request['id']):
        raise ValueError('Saved artifact or provider job already consumed by another request; do not replay')
    batch = _unwrap(json.loads(raw))
    if batch['status'] != 'completed' or batch.get('next'):
        raise ValueError('Batch is pending, failed or paginated; full evidence not available')
    if batch.get('id') is not None and batch['id'] != receipt['job_id']:
        raise ValueError('Provider job identifier differs from receipt')
    times = {key:parse_time(batch[key]) for key in ('createdAt','completedAt') if key in batch}
    if any(value < parse_time(request['created_at']) or value > received for value in times.values()):
        raise ValueError('Provider job timestamp predates reservation or follows result receipt')
    if times.get('createdAt') and times.get('completedAt') and times['completedAt'] < times['createdAt']:
        raise ValueError('Provider job timestamps are out of order')
    docs = batch['data']
    if batch.get('total') != len(request['source_ids']) or batch.get('completed') != len(docs) or len(docs) != len(request['source_ids']):
        raise ValueError('Batch coverage is incomplete; reconcile failed/missing pages before importing')
    by_url = {}
    for doc in docs:
        if not isinstance(doc,dict) or not isinstance(doc.get('metadata'),dict) or doc.get('truncated') or doc.get('isTruncated'):
            raise ValueError('Malformed or truncated page result')
        url = _allowed(state,doc['metadata'].get('sourceURL'))
        if url in by_url:
            raise ValueError('Duplicate page in response')
        by_url[url] = doc
    expected = {state['sources'][sid]['normalized_url'] for sid in request['source_ids']}
    if set(by_url) != expected:
        raise ValueError('Returned URLs differ from reserved scope')
    credits = batch.get('creditsUsed')
    if credits is not None and not _valid_credits(credits):
        raise ValueError('Malformed provider credit telemetry')
    working = copy.deepcopy(state)
    work_request = next(r for r in working['managed_collection'] if r['id'] == request['id'])
    outcomes = []
    for sid, descriptor in zip(request['source_ids'],request['attempts']):
        source = working['sources'][sid]
        old_primary = source.get('active_version')
        old_signals = source.get('managed_capture_signals',[])
        index = descriptor['attempt_index']
        attempt = source['collection_attempts'][index]
        if (attempt['outcome'] != 'pending' or attempt.get('reconciliation') is not None
                or _attempt_descriptor(source,index,attempt)['attempt_hash'] != descriptor['attempt_hash']
                or source.get('active_version') != descriptor['current_version_id']):
            raise ValueError('Attempt/source changed since request reservation')
        doc = by_url[source['normalized_url']]
        meta = doc['metadata']
        status = meta.get('statusCode')
        if type(status) is not int:
            raise ValueError('Target status was not reported')
        attempt.update(completed_at=receipt['completed_at'], target_status=status,
            response_hash=receipt['sha256'], job_id=receipt['job_id'])
        observed_destination = next((meta[field] for field in ('url','finalURL')
            if isinstance(meta.get(field),str) and meta[field] and
            (not public_url(meta[field],working['config']['domains']) or normalize_url(meta[field]) != source['normalized_url'])),None)
        if observed_destination and 200 <= status < 300:
            source.update(availability='unknown',captured_status=status,
                captured_status_origin='Firecrawl metadata.statusCode; destination held for review',
                scope='pending',scope_reason='Observed destination differs from requested identity; review retained artifact',
                collection_state='failed',render_fallback_eligible=False)
            source['redirect_review_required']={'from_url':source['normalized_url'],'to_url':observed_destination,
                'version_id':source.get('active_version'),'at':receipt['completed_at'],'response_hash':receipt['sha256']}
            _invalidate(working,sid,source['scope_reason'])
            _rows(working,sid,'pending',source['scope_reason'])
            attempt.update(outcome='failed',reason=source['scope_reason'])
            outcomes.append({'source_id':sid,'quality':'identity-held','versions':[]})
            continue
        if not 200 <= status < 300:
            source.update(availability='nonpublic' if status in (401,403) else 'failed',captured_status=status)
            _invalidate(working,sid,'Provider explicitly reported failing target status')
            source.update(scope='pending',scope_reason='Target HTTP '+str(status),collection_state='failed')
            _rows(working,sid,'pending',source['scope_reason'])
            attempt.update(outcome='failed',reason='Target HTTP '+str(status))
            outcomes.append({'source_id':sid,'quality':'target-error','versions':[]})
            continue
        obs = {**copy.deepcopy(meta),'retrieved_at':receipt['completed_at'],'collector':'firecrawl-mcp',
            'artifact_sha256':receipt['sha256'],'status_origin':'Firecrawl metadata.statusCode; not direct origin verification'}
        versions = []
        signals = []
        for field in ('rawHtml','html','markdown'):
            if field not in request['settings'].get('formats',['markdown']):
                continue
            value = doc.get(field)
            if not isinstance(value,str) or (field != 'markdown' and not value.strip()):
                raise ValueError('Requested evidence format missing or empty')
            if field == 'markdown':
                extracted = extract_markdown(value,meta)
                if request['settings']['formats'] == ['markdown'] and not meta.get('canonicalURL'):
                    extracted['warnings'].append('Markdown-only canonical/HTML indexing signals are unknown; source requires explicit inclusion review')
            else:
                if '<' not in value or '>' not in value:
                    raise ValueError('Requested HTML format is not HTML')
                extracted = extract_html(value,source['normalized_url'])
            if doc.get('warning') or (status == 200 and meta.get('error')):
                extracted['warnings'].append('Provider warning requires source review')
            representation = 'firecrawl-'+field.lower()
            signals.append({'representation':representation,'canonical':extracted.get('canonical'),
                            'robots':extracted.get('robots'),
                            'review_required':any('requires' in w.lower() for w in extracted['warnings'])})
            version_settings = {**request['settings'],'format':field,
                'extractor':'markdown-verbatim-v2' if field == 'markdown' else 'html-v2'}
            if field == 'markdown':
                version_settings['page_metadata_sha256'] = digest(extracted['evidence']['metadata'])
            vid = _capture(working,sid,value,extracted,representation,version_settings,obs,invalidate=False)
            versions.append(vid)
        if old_primary != source.get('active_version'):
            _invalidate(working,sid,'Source representation changed; repeat evidence/business/publication reviews')
        source['managed_capture_signals'] = signals
        source['managed_signal_review_required'] = any(s['review_required'] for s in signals)
        if old_signals != signals:
            source['inclusion_decision'] = None
            if source['managed_signal_review_required']:
                _invalidate(working,sid,'Cross-format source restrictions changed; repeat inclusion/publication review')
        for signal in signals:
            if signal['canonical'] and normalize_url(signal['canonical']) != source['normalized_url']:
                source['canonical'] = signal['canonical']
            if re.search(r'\b(?:noindex|none)\b',str(signal['robots'] or ''),re.I):
                source['robots'] = signal['robots']
        if any(source['versions'][v]['quality'] != 'usable' or any('requires' in w.lower() for w in source['versions'][v]['warnings']) for v in versions):
            source.update(scope='pending',scope_reason='One or more captured formats require source/quality review')
            _rows(working,sid,'pending',source['scope_reason'])
        attempt.update(outcome='success',reason='Complete saved MCP representation imported; human evidence review pending')
        source['mcp_raw_html_versions'] = versions[:-1] if 'markdown' in request['settings'].get('formats',[]) else versions
        source['versions'][source['active_version']]['evidence_formats_available'] = list(request['settings'].get('formats',['markdown']))
        outcomes.append({'source_id':sid,'quality':extracted['quality'],'versions':versions})
    work_request.update(status='complete',completed_at=receipt['completed_at'],artifact_sha256=receipt['sha256'],
        job_id=receipt['job_id'],credits_used=credits,
        budget_exceeded=credits is not None and credits > request['credit_reservation'])
    state.clear()
    state.update(working)
    return {'request_id':request['id'],'outcomes':outcomes,'budget':budget_status(state),
        'publication':'not authorized by collection/import','capture_date':'unknown unless explicitly reported by provider'}


def collect_direct(state, fetch=None, max_pages=25, refresh=False, checkpoint=None, *, clock=None, sleep=None):
    check_config(state['config'])
    if not state['config'].get('collection_authorized') or type(max_pages) is not int or max_pages < 1:
        raise ValueError('Direct collection needs authorization and a positive page ceiling')
    from .politeness import DirectFetcher, AvailabilityPolicyError, RobotsDisallowedError
    pacing = DirectFetcher(state, direct_fetch, clock=clock, sleep=sleep) if fetch is None else None
    interval = state['config'].get('direct_interval_seconds',0.5)
    if type(interval) not in (int,float) or not math.isfinite(interval) or not 0 <= interval <= 30:
        raise ValueError('Direct request interval must be bounded between zero and thirty seconds')
    summary = {'attempted':0,'captured':0,'fallback_needed':0,'provider_requests':0,'provider_credits':0}
    robots = {}
    for sid, source in state['sources'].items():
        if source['scope'] != 'included' or not source['identity_confirmed'] or pending_attempts(source):
            continue
        if source.get('active_version') and not refresh:
            continue
        if summary['attempted'] >= max_pages:
            break
        url = _allowed(state,source['normalized_url'])
        origin = urlsplit(url)
        if pacing is None and state['config'].get('respect_robots',True):
            host = origin.scheme+'://'+origin.netloc
            if host not in robots:
                response = fetch(host+'/robots.txt',state['config'],max_bytes=256*1024)
                parser = RobotFileParser()
                if response['status'] == 200:
                    parser.parse(response['body'].decode('utf-8',errors='replace').splitlines())
                    robots[host] = parser
                elif response['status'] in (404,410):
                    robots[host] = None
                else:
                    raise ValueError('Robots policy could not be checked; no page request sent')
            if robots[host] and not robots[host].can_fetch('OKF-Knowledge-Bundle',url):
                source['render_fallback_eligible'] = False
                source['robots_disallowed'] = True
                source['fallback_reason'] = 'Robots policy disallows this URL; do not bypass through another provider'
                source.update(scope='pending',scope_reason=source['fallback_reason'])
                _rows(state,sid,'pending',source['scope_reason'])
                continue
            source['robots_disallowed'] = False
        summary['attempted'] += 1
        if pacing is None and summary['attempted'] > 1 and interval:
            time.sleep(interval)
        try:
            if pacing is not None:
                response = pacing(url,max_bytes=state['config'].get('direct_max_bytes',2*1024**2))
                source['robots_disallowed'] = False
            else:
                response = fetch(url,state['config'],max_bytes=state['config'].get('direct_max_bytes',2*1024**2),headers=HTML_REQUEST_HEADERS)
        except AvailabilityPolicyError as error:
            source.update(render_fallback_eligible=False, robots_disallowed=isinstance(error, RobotsDisallowedError),
                          fallback_reason=str(error), scope='pending', scope_reason=str(error))
            _rows(state,sid,'pending',source['scope_reason'])
            if checkpoint:
                checkpoint(state)
            continue
        except (OSError,TimeoutError,ValueError):
            source['fallback_reason'] = 'Direct transport failed; prior evidence retained, rendered fallback needs explicit bounded authorization'
            summary['fallback_needed'] += 1
            if checkpoint:
                checkpoint(state)
            continue
        if response['status'] != 200:
            source.update(availability='nonpublic' if response['status'] in (401,403) else 'failed',captured_status=response['status'])
            _invalidate(state,sid,'Direct origin returned failing HTTP status')
            source.update(scope='pending',scope_reason='Direct target HTTP '+str(response['status']))
            _rows(state,sid,'pending',source['scope_reason'])
        else:
            _allowed(state,response['url'])
            if normalize_url(response['url']) != url:
                source.update(availability='unknown',scope='pending',render_fallback_eligible=False,
                              scope_reason='Direct redirect changes identity; explicit alias/source review required')
                source['redirect_review_required'] = {'from_url':url,'to_url':response['url'],
                    'version_id':source.get('active_version'),'at':utc_now(),'response_hash':digest(response['body'])}
                _invalidate(state,sid,source['scope_reason'])
                _rows(state,sid,'pending',source['scope_reason'])
                if checkpoint:
                    checkpoint(state)
                continue
            body = response['body']
            media = response.get('headers',{}).get('content-type','text/html').split(';')[0].lower()
            if media not in ('text/html','application/xhtml+xml') or b'<' not in body or b'>' not in body:
                source['fallback_reason'] = 'Direct response is not HTML; no evidence substituted'
                summary['fallback_needed'] += 1
                if checkpoint:
                    checkpoint(state)
                continue
            extracted = extract_html(body,url)
            http_robots = response.get('headers',{}).get('x-robots-tag')
            if http_robots != source.get('http_robots'):
                source['inclusion_decision'] = None
                _invalidate(state,sid,'Direct HTTP robots policy changed; repeat source/publication review')
            source.update(http_robots=http_robots,x_robots_tag=http_robots)
            if re.search(r'\b(?:noindex|none)\b',str(http_robots or ''),re.I):
                extracted['warnings'].append('HTTP robots noindex source requires explicit inclusion review')
            obs = {'sourceURL':url,'statusCode':200,'status_origin':'Guarded direct origin GET',
                'retrieved_at':utc_now(),'collector':'direct-http','direct_observation':True,
                'http_robots':http_robots,
                'capture_date_basis':'Direct origin retrieved at recorded time; no historical origin date inferred'}
            _capture(state,sid,response['body'],extracted,'direct-html',{'format':'html','engine':'direct'},obs)
            summary['captured'] += 1
            if extracted['quality'] != 'usable':
                source['fallback_reason'] = 'Direct content is '+extracted['quality']+'; review source before authorizing rendering'
                source['render_fallback_eligible'] = extracted['quality'] in ('empty_or_thin','placeholder')
                summary['fallback_needed'] += 1
        if checkpoint:
            checkpoint(state)
    summary['pacing'] = pacing.receipt() if pacing else {'mode':'injected fixture transport; production pacing not evaluated'}
    state.setdefault('direct_collection_history',[]).append({**summary,'at':utc_now()})
    return summary
