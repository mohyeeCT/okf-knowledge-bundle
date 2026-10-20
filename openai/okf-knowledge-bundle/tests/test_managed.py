import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from okf_bundle.common import utc_now
from okf_bundle.ingest import discover_csv
from okf_bundle.managed import (budget_status, check_config, collect_direct,
                                extract_markdown, import_mcp, prepare_mcp)
from okf_bundle.network import pending_attempts
from okf_bundle.review import validate_evidence
from okf_bundle.state import new_state
from okf_bundle.workflow import merge_import
from okf_bundle.cli import execute, parser
from okf_bundle.export import _source_issues
from okf_bundle.common import parse_time
from okf_bundle.availability import check_availability

HTML = '<html lang="en"><head><title>Training</title><link rel="canonical" href="https://example.com/a/"><script type="application/ld+json">{"@type":"Course","name":"Food training"}</script></head><body><h1>Training</h1><table><tr><td>Review</td><td>4 hours; excludes exam time</td></tr></table><p>Private on-site instruction needs at least eight people.</p></body></html>'
MD = '# Training\n\n| Option | Duration |\n|---|---|\n| Review | 4 hours; excludes exam time |\n\nPrivate on-site instruction needs at least eight people.'


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'domains':['example.com'],'collection_authorized':True,
            'managed_budget':{'max_requests':6,'max_credits':12,'per_page_credit_reservation':2,'max_response_bytes':2*1024**2}}
        self.state = new_state(self.config)
        csv = self.root/'urls.csv'
        csv.write_text('URL\nhttps://example.com/a/\n',encoding='utf-8')
        merge_import(self.state,discover_csv(csv,self.config))
        self.sid = next(iter(self.state['sources']))

    def plan(self, settings=None):
        return prepare_mcp(self.state,[self.sid],settings or {'formats':['markdown','rawHtml']},'current user target/scope/budget instruction',
                           robots_fetch=lambda url,cfg,**kw:{'status':404,'body':b'','url':url})

    def receipt(self, plan, edit=None):
        doc = {'metadata':{'sourceURL':'https://example.com/a/','statusCode':200,'title':'Training','language':'en'},'markdown':MD,'rawHtml':HTML}
        batch = {'id':'job-'+plan['id'],'status':'completed','total':1,'completed':1,'creditsUsed':1,'data':[doc]}
        if edit:
            edit(batch)
        data = json.dumps({'successful':True,'data':{'results':[{'response':{'successful':True,'data':batch}}]}}).encode()
        path = self.root/'result.json'
        path.write_bytes(data)
        return {'request_id':plan['id'],'request_hash':plan['request_hash'],'artifact':path.name,
            'sha256':hashlib.sha256(data).hexdigest(),'completed_at':utc_now(),'job_id':'job-'+plan['id'],'complete':True}

    def test_reservation_precedes_host_dispatch_and_blocks_replay(self):
        plan = self.plan()
        self.assertEqual(1,len(pending_attempts(self.state['sources'][self.sid])))
        self.assertEqual(2,budget_status(self.state)['credits_reserved_unknown'])
        self.assertEqual(0,plan['settings']['maxAge'])
        result = import_mcp(self.state,self.receipt(plan),self.root)
        self.assertEqual(1,result['budget']['credits_reported'])
        self.assertEqual(0,result['budget']['credits_reserved_unknown'])
        self.assertFalse(pending_attempts(self.state['sources'][self.sid]))
        with self.assertRaises(ValueError):
            import_mcp(self.state,self.receipt(plan),self.root)

    def test_markdown_and_html_versions_have_separate_evidence(self):
        plan = self.plan()
        import_mcp(self.state,self.receipt(plan),self.root)
        source = self.state['sources'][self.sid]
        active = source['versions'][source['active_version']]
        self.assertEqual('firecrawl-markdown',active['representation'])
        self.assertEqual(MD,active['evidence']['body'])
        self.assertEqual('',active['evidence']['schema'])
        raw = source['versions'][source['mcp_raw_html_versions'][0]]
        self.assertIn('Food training',raw['evidence']['schema'])
        self.assertEqual('unknown',source['availability'])
        validate_evidence(self.state,[{'source_id':self.sid,'version_id':active['id'],'locator':'body','quote':'4 hours; excludes exam time'}])
        with self.assertRaises(ValueError):
            validate_evidence(self.state,[{'source_id':self.sid,'version_id':active['id'],'locator':'schema','quote':'Food training'}])

    def test_tampered_missing_partial_and_wrong_scope_leave_state_unchanged(self):
        edits = [lambda b:b.update(next='https://api.firecrawl.dev/next'),
            lambda b:b.update(total=2),
            lambda b:b['data'][0]['metadata'].update(sourceURL='https://evil.example/a/'),
            lambda b:b['data'][0]['metadata'].update(sourceURL='https://example.com/other/'),
            lambda b:b['data'][0].update(isTruncated=True),
            lambda b:b['data'][0].pop('rawHtml')]
        for edit in edits:
            with self.subTest(edit=edit):
                plan = self.plan()
                receipt = self.receipt(plan,edit)
                before = copy.deepcopy(self.state)
                with self.assertRaises(ValueError):
                    import_mcp(self.state,receipt,self.root)
                self.assertEqual(before,self.state)
                self.setUp()

    def test_bad_hash_future_time_and_noncomplete_receipt_refused(self):
        plan = self.plan()
        receipt = self.receipt(plan)
        for change in ({'sha256':'0'*64},{'complete':False},
            {'completed_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}):
            before = copy.deepcopy(self.state)
            with self.assertRaises(ValueError):
                import_mcp(self.state,{**receipt,**change},self.root)
            self.assertEqual(before,self.state)

    def test_unknown_usage_stays_reserved_and_budget_stops_next_dispatch(self):
        self.state['config']['managed_budget']['max_credits'] = 2
        plan = self.plan()
        import_mcp(self.state,self.receipt(plan,lambda b:b.pop('creditsUsed')),self.root)
        self.assertEqual(2,budget_status(self.state)['credits_reserved_unknown'])
        with self.assertRaises(ValueError):
            self.plan()

    def test_provider_failure_preserves_prior_reviews_and_pending_reservation(self):
        plan = self.plan()
        receipt = self.receipt(plan)
        artifact = self.root/receipt['artifact']
        artifact.write_text(json.dumps({'successful':False,'error':'provider access denial'}))
        receipt['sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.state['release_approvals'] = {'old':'record'}
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            import_mcp(self.state,receipt,self.root)
        self.assertEqual(before,self.state)
        self.assertEqual(2,budget_status(self.state)['credits_reserved_unknown'])

    def test_explicit_target_failure_invalidates_and_does_not_become_public(self):
        plan = self.plan()
        self.state['release_approvals'] = {'old':'record'}
        import_mcp(self.state,self.receipt(plan,lambda b:b['data'][0]['metadata'].update(statusCode=404)),self.root)
        self.assertEqual('failed',self.state['sources'][self.sid]['availability'])
        self.assertFalse(self.state['release_approvals'])

    def test_soft_challenge_thin_noindex_and_duplicates_are_flagged(self):
        for body,quality in [('Verify you are human. Checking your browser. Please wait for access.','challenge'),('Videos','empty_or_thin')]:
            extracted = extract_markdown(body,{'title':'Training'})
            self.assertEqual(quality,extracted['quality'])
        plan = self.plan()
        receipt = self.receipt(plan,lambda b:b['data'][0].update(rawHtml=HTML.replace('<head>','<head><meta name="robots" content="noindex">')))
        import_mcp(self.state,receipt,self.root)
        self.assertEqual('pending',self.state['sources'][self.sid]['scope'])

    def test_unchanged_recapture_preserves_approvals(self):
        plan = self.plan()
        import_mcp(self.state,self.receipt(plan),self.root)
        self.state['release_approvals'] = {'old':'record'}
        self.state['sources'][self.sid]['availability_acceptance'] = {'old':'acceptance'}
        plan = self.plan()
        import_mcp(self.state,self.receipt(plan),self.root)
        self.assertEqual({'old':'record'},self.state['release_approvals'])
        self.assertEqual({'old':'acceptance'},self.state['sources'][self.sid]['availability_acceptance'])

    def test_unsafe_settings_and_credentials_never_reserve(self):
        for settings in ({'formats':['summary']},{'maxConcurrency':20},{'skipTlsVerification':True},{'proxy':'auto'},{'maxAge':True},
                         {'storeInCache':'false'},{'removeBase64Images':'false'}):
            before = copy.deepcopy(self.state)
            with self.assertRaises(ValueError):
                self.plan(settings)
            self.assertEqual(before,self.state)
        for config in ({'api_key':'placeholder'},{'some':'Bearer '+ 'z'*30}):
            with self.assertRaises(ValueError):
                check_config(config)

    def test_direct_html_needs_no_provider_and_shell_gets_fallback_reason(self):
        calls = []
        def fetch(url,config,**kwargs):
            calls.append(url)
            return {'status':404,'body':b'','url':url} if url.endswith('robots.txt') else {'status':200,'body':HTML.encode(),'url':url}
        result = collect_direct(self.state,fetch,max_pages=1)
        self.assertEqual(1,result['captured'])
        self.assertEqual(0,result['provider_requests'])
        source = self.state['sources'][self.sid]
        self.assertEqual('direct-html',source['versions'][source['active_version']]['representation'])
        self.assertFalse(self.state.get('managed_collection'))
        self.setUp()
        def shell(url,config,**kwargs):
            return {'status':404,'body':b'','url':url} if url.endswith('robots.txt') else {'status':200,'body':b'<html><body><div id="root">Loading...</div></body></html>','url':url}
        result = collect_direct(self.state,shell,max_pages=1)
        self.assertEqual(1,result['fallback_needed'])
        self.assertTrue(self.state['sources'][self.sid]['render_fallback_eligible'])
        self.plan()

    def test_direct_robots_block_is_not_bypassed(self):
        calls = []
        def fetch(url,config,**kwargs):
            calls.append(url)
            return {'status':200,'body':b'User-agent: *\nDisallow: /a/\n','url':url}
        result = collect_direct(self.state,fetch,max_pages=1)
        self.assertEqual(0,result['attempted'])
        self.assertEqual(['https://example.com/robots.txt'],calls)
        with self.assertRaises(ValueError):
            self.plan()

    def test_cli_managed_roundtrip_preserves_unapproved_release_gate(self):
        run = self.root/'cli-run'
        config = self.root/'config.json'
        config.write_text(json.dumps(self.config),encoding='utf-8')
        def cli(*args):
            return execute(parser().parse_args(list(args)))
        cli('init','--run',str(run),'--config',str(config))
        cli('discover-csv','--run',str(run),'--csv',str(self.root/'urls.csv'))
        settings = self.root/'settings.json'
        settings.write_text(json.dumps({'formats':['markdown','rawHtml']}))
        with patch('okf_bundle.managed.direct_fetch',side_effect=lambda url,cfg,**kw:{'status':404,'body':b'','url':url}):
            plan = cli('mcp-prepare','--run',str(run),'--ids',self.sid,'--settings',str(settings),'--reference','actual synthetic fixture authorization')
        receipt = self.receipt(plan)
        path = self.root/'receipt.json'
        path.write_text(json.dumps(receipt))
        cli('mcp-import','--run',str(run),'--receipt',str(path))
        self.assertFalse(cli('readiness','--run',str(run))['ready'])
        self.assertEqual(0,len(cli('status','--run',str(run))['unresolved_collection_attempts']))
        packet = cli('packets','--run',str(run),'--max-chars','1000')
        self.assertGreater(len(packet['packet_files']),0)

    def test_cli_sitemap_and_sf_routes_still_work_without_provider_secrets(self):
        config = self.root/'config.json'
        config.write_text(json.dumps(self.config))
        sitemap = self.root/'sitemap.xml'
        sitemap.write_text('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/a/</loc></url></urlset>')
        archive = self.root/'sf.zip'
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('original_https_example.com_a_.html',HTML)
        def cli(*args):
            return execute(parser().parse_args(list(args)))
        for route,command,arg,value in [('sitemap','discover-sitemap','--sitemap',sitemap),('sf','import-sf','--zip',archive)]:
            run = self.root/route
            cli('init','--run',str(run),'--config',str(config))
            cli(command,'--run',str(run),arg,str(value))
            if route == 'sitemap':
                def fetch(url,cfg,**kwargs):
                    return {'status':404,'body':b'','url':url} if url.endswith('robots.txt') else {'status':200,'body':HTML.encode(),'url':url}
                with patch('okf_bundle.managed.direct_fetch',side_effect=fetch):
                    result = cli('collect-direct','--run',str(run),'--max-pages','1')
                self.assertEqual(0,result['provider_requests'])
            self.assertGreater(len(cli('packets','--run',str(run),'--max-chars','1000')['packet_files']),0)
            self.assertFalse(cli('readiness','--run',str(run))['ready'])

    def test_markdown_contract_is_current_but_schema_stays_uncaptured(self):
        plan = self.plan({'formats':['markdown']})
        import_mcp(self.state,self.receipt(plan),self.root)
        source = self.state['sources'][self.sid]
        evidence = {'source_id':self.sid,'version_id':source['active_version'],'locator':'body','quote':'4 hours; excludes exam time'}
        issues = _source_issues(self.state,evidence,parse_time(utc_now()))
        self.assertFalse(any('extraction contract' in issue for issue in issues))
        source['versions'][source['active_version']]['settings']['extractor'] = 'unknown-markdown'
        self.assertTrue(any('extraction contract' in issue for issue in _source_issues(self.state,evidence,parse_time(utc_now()))))

    def test_direct_check_of_markdown_requires_human_comparison_not_recrawl_loop(self):
        plan = self.plan({'formats':['markdown']})
        import_mcp(self.state,self.receipt(plan),self.root)
        result = check_availability(self.state,[self.sid],lambda url:{'status':200,'body':HTML.encode(),'url':url})
        observation = result['observations'][0]
        self.assertTrue(observation['comparison_inconclusive'])
        self.assertFalse(observation['requires_source_refresh'])
        self.assertIn('explicit human',observation['quote_comparison_scope'])
        self.assertEqual('checked-live-comparison-required',observation['outcome'])


if __name__ == '__main__':
    unittest.main()
