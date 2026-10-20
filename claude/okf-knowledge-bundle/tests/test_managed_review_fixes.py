import copy
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import patch
import unittest

import test_managed
from okf_bundle.managed import budget_status, collect_direct, import_mcp, prepare_mcp, extract_markdown
from okf_bundle.network import reconcile_attempt
from okf_bundle.common import utc_now
from okf_bundle.export import _inclusion_issues
from okf_bundle.common import parse_time


class ManagedReviewFixTests(unittest.TestCase):
    setUp = test_managed.ManagedTests.setUp
    plan = test_managed.ManagedTests.plan
    receipt = test_managed.ManagedTests.receipt

    def test_nonfinite_or_invalid_usage_is_atomic_and_keeps_reservation(self):
        plan = self.plan()
        for bad in (float('nan'), float('inf'), float('-inf'), -1, True, '1'):
            with self.subTest(value=bad):
                receipt = self.receipt(plan, lambda b: b.update(creditsUsed=bad))
                before = copy.deepcopy(self.state)
                with self.assertRaises(ValueError):
                    import_mcp(self.state, receipt, self.root)
                self.assertEqual(before, self.state)
                self.assertEqual(2, budget_status(self.state)['credits_reserved_unknown'])

    def test_preexisting_invalid_usage_blocks_future_dispatch(self):
        plan = self.plan()
        import_mcp(self.state, self.receipt(plan), self.root)
        self.state['managed_collection'][0]['credits_used'] = float('nan')
        with self.assertRaises(ValueError):
            self.plan()

    def test_robots_denial_clears_prior_shell_fallback_permission(self):
        def shell(url, config, **kwargs):
            return ({'status':404, 'body':b'', 'url':url} if url.endswith('robots.txt') else
                    {'status':200, 'body':b'<html><body><div id="root">Loading...</div></body></html>', 'url':url})
        collect_direct(self.state, shell, max_pages=1)
        source = self.state['sources'][self.sid]
        self.assertTrue(source['render_fallback_eligible'])
        source['scope'] = 'included'  # analyst includes the shell for an explicit refresh
        calls = []
        def denied(url, config, **kwargs):
            calls.append(url)
            return {'status':200, 'body':b'User-agent: *\nDisallow: /a/\n', 'url':url}
        collect_direct(self.state, denied, max_pages=1, refresh=True)
        self.assertEqual(['https://example.com/robots.txt'], calls)
        self.assertFalse(source['render_fallback_eligible'])
        with self.assertRaises(ValueError):
            self.plan()
        source['scope'] = 'included'
        with self.assertRaises(ValueError):
            prepare_mcp(self.state,[self.sid],{},'fixture',robots_fetch=denied)

    def test_direct_http_noindex_is_retained_and_requires_new_inclusion(self):
        def fetch(url, config, **kwargs):
            return ({'status':404, 'body':b'', 'url':url} if url.endswith('robots.txt') else
                    {'status':200, 'body':test_managed.HTML.encode(), 'url':url,
                     'headers':{'content-type':'text/html','x-robots-tag':'noindex'}})
        collect_direct(self.state, fetch, max_pages=1)
        source = self.state['sources'][self.sid]
        self.assertEqual('noindex', source['x_robots_tag'])
        self.assertEqual('pending', source['scope'])
        self.assertFalse(source['render_fallback_eligible'])
        version = source['versions'][source['active_version']]
        self.assertEqual('noindex', version['metadata_observations']['http_robots'])
        source['scope'] = 'included'
        source['inclusion_decision'] = {'old':'decision'}
        self.state['release_approvals'] = {'old':'approval'}
        def changed(url, config, **kwargs):
            result = fetch(url, config, **kwargs)
            if not url.endswith('robots.txt'):
                result['headers']['x-robots-tag'] = 'none'
            return result
        collect_direct(self.state, changed, max_pages=1, refresh=True)
        self.assertEqual(version['id'], source['active_version'])
        self.assertIsNone(source['inclusion_decision'])
        self.assertFalse(self.state['release_approvals'])

    def test_managed_preflight_checks_robots_without_prior_direct_collection(self):
        calls = []
        def denied(url, config, **kwargs):
            calls.append(url)
            return {'status':200,'body':b'User-agent: *\nDisallow: /a/\n','url':url}
        with self.assertRaises(ValueError):
            prepare_mcp(self.state,[self.sid],{},'fixture',robots_fetch=denied)
        self.assertEqual(['https://example.com/robots.txt'],calls)
        self.assertFalse(self.state.get('managed_collection'))

    def test_reconciled_attempt_refuses_late_import_and_closed_retry(self):
        for action in ('allow-retry','close-without-retry'):
            with self.subTest(action=action):
                self.setUp()
                self.state['config']['collection_reviewers']=['human:fixture-operator']
                plan = self.plan()
                receipt = self.receipt(plan)
                descriptor = plan['attempts'][0]
                reconcile_attempt(self.state,self.sid,0,action,'fixture unknown outcome',
                                  'human:fixture-operator',utc_now(),'fixture','fixture decision',
                                  expected_attempt_hash=descriptor['attempt_hash'],expected_version=None)
                before = copy.deepcopy(self.state)
                with self.assertRaises(ValueError):
                    import_mcp(self.state,receipt,self.root)
                self.assertEqual(before,self.state)
                if action == 'close-without-retry':
                    with self.assertRaises(ValueError):self.plan()

    def test_artifact_and_job_cannot_satisfy_a_different_request(self):
        first = self.plan()
        receipt = self.receipt(first)
        import_mcp(self.state,receipt,self.root)
        second = self.plan()
        for job in (receipt['job_id'],'different-job-label'):
            replay = {**receipt,'request_id':second['id'],'request_hash':second['request_hash'],'job_id':job,'completed_at':utc_now()}
            before = copy.deepcopy(self.state)
            with self.assertRaises(ValueError):import_mcp(self.state,replay,self.root)
            self.assertEqual(before,self.state)

    def test_stale_provider_job_time_and_mismatching_job_id_refused(self):
        plan = self.plan()
        for fields in ({'createdAt':'2025-01-01T00:00:00Z'},
                       {'completedAt':'2099-01-01T00:00:00Z'},{'id':'another-job'}):
            with self.subTest(fields=fields):
                receipt = self.receipt(plan,lambda b:b.update(fields))
                before = copy.deepcopy(self.state)
                with self.assertRaises(ValueError):import_mcp(self.state,receipt,self.root)
                self.assertEqual(before,self.state)

    def test_operational_metadata_is_provenance_not_page_evidence(self):
        metadata={'title':'Training','language':'en','proxyUsed':'basic','statusCode':200,'creditsUsed':1,'scrapeId':'fixture'}
        extracted=extract_markdown(test_managed.MD,metadata)
        self.assertIn('Training',extracted['evidence']['metadata'])
        for key in ('proxyUsed','statusCode','creditsUsed','scrapeId'):
            self.assertNotIn(key,extracted['evidence']['metadata'])

    def test_sidecar_canonical_away_survives_and_needs_inclusion(self):
        plan=self.plan()
        receipt=self.receipt(plan,lambda b:b['data'][0].update(rawHtml=test_managed.HTML.replace('https://example.com/a/','https://example.com/other/')))
        import_mcp(self.state,receipt,self.root)
        source=self.state['sources'][self.sid]
        self.assertEqual('https://example.com/other/',source['canonical'])
        self.assertTrue(source['managed_signal_review_required'])
        source['scope']='included'
        self.assertTrue(_inclusion_issues(source,source['active_version'],parse_time(utc_now())))

    def test_sidecar_body_only_changes_keep_primary_approvals(self):
        plan=self.plan()
        import_mcp(self.state,self.receipt(plan),self.root)
        source=self.state['sources'][self.sid]
        primary=source['active_version']
        self.state['release_approvals']={'fixture':'approval'}
        plan=self.plan()
        receipt=self.receipt(plan,lambda b:b['data'][0].update(rawHtml=test_managed.HTML.replace('</body>','<p>Fresh carousel UI</p></body>')))
        import_mcp(self.state,receipt,self.root)
        self.assertEqual(primary,self.state['sources'][self.sid]['active_version'])
        self.assertEqual({'fixture':'approval'},self.state['release_approvals'])

    def test_direct_redirect_holds_source_and_continues_other_pages(self):
        from okf_bundle.ingest import discover_csv
        from okf_bundle.workflow import merge_import
        file=self.root/'more.csv'
        file.write_text('URL\nhttps://example.com/b/\n',encoding='utf-8')
        merge_import(self.state,discover_csv(file,self.config))
        self.state['config']['direct_interval_seconds']=0
        def fetch(url,cfg,**kw):
            if url.endswith('robots.txt'):return {'status':404,'body':b'','url':url}
            final='https://example.com/destination/' if url.endswith('/a/') else url
            return {'status':200,'body':test_managed.HTML.encode(),'url':final}
        result=collect_direct(self.state,fetch,max_pages=2)
        self.assertEqual(1,result['captured'])
        self.assertIn('redirect_review_required',self.state['sources'][self.sid])
        self.assertEqual('pending',self.state['sources'][self.sid]['scope'])

    def test_page_metadata_change_creates_version_and_invalidates_reviews(self):
        plan=self.plan()
        import_mcp(self.state,self.receipt(plan),self.root)
        source=self.state['sources'][self.sid]
        primary=source['active_version']
        self.state['release_approvals']={'fixture':'approval'}
        plan=self.plan()
        receipt=self.receipt(plan,lambda b:b['data'][0]['metadata'].update(title='Changed page title'))
        import_mcp(self.state,receipt,self.root)
        source=self.state['sources'][self.sid]
        self.assertNotEqual(primary,source['active_version'])
        self.assertIn('Changed page title',source['versions'][source['active_version']]['evidence']['metadata'])
        self.assertFalse(self.state['release_approvals'])

    def test_operational_metadata_change_does_not_change_evidence_version(self):
        plan=self.plan()
        import_mcp(self.state,self.receipt(plan),self.root)
        primary=self.state['sources'][self.sid]['active_version']
        self.state['release_approvals']={'fixture':'approval'}
        plan=self.plan()
        receipt=self.receipt(plan,lambda b:b['data'][0]['metadata'].update(scrapeId='another-scrape',proxyUsed='basic'))
        import_mcp(self.state,receipt,self.root)
        self.assertEqual(primary,self.state['sources'][self.sid]['active_version'])
        self.assertEqual({'fixture':'approval'},self.state['release_approvals'])

    def test_malformed_results_envelope_is_atomic_value_error(self):
        plan=self.plan()
        receipt=self.receipt(plan)
        artifact=self.root/receipt['artifact']
        artifact.write_text(json.dumps({'results':{'response':{}}}),encoding='utf-8')
        receipt['sha256']=hashlib.sha256(artifact.read_bytes()).hexdigest()
        before=copy.deepcopy(self.state)
        with self.assertRaises(ValueError):import_mcp(self.state,receipt,self.root)
        self.assertEqual(before,self.state)

    def test_cli_packages_valid_fractional_approval_in_same_second(self):
        import test_export
        from okf_bundle.export import build_candidate,manifest_hash,ReleaseError
        from okf_bundle.state import StateStore
        from okf_bundle.cli import execute,parser
        state=test_export.fixture_state()  # fictional approved fixture, never client state
        candidate=self.root/'candidate'
        manifest=build_candidate(state,candidate,'fractional-fixture',test_export.NOW)
        test_export.approve(state,manifest)
        key=manifest_hash(manifest)
        state['release_approvals'][key]['at']='2026-10-15T18:00:00.500000Z'
        run=self.root/'clock-run'
        store=StateStore(run)
        with store.transaction():store.save(state)
        now=datetime(2026,10,15,18,0,0,750000,tzinfo=timezone.utc)
        with patch('okf_bundle.cli.utc_now',return_value='2026-10-15T18:00:00Z'),patch('okf_bundle.cli.datetime') as clock:
            clock.now.return_value=now
            result=execute(parser().parse_args(['package','--run',str(run),'--candidate',str(candidate),'--out',str(self.root/'package.zip')]))
        self.assertTrue((self.root/'package.zip').exists())
        self.assertIn('.750000',result['readiness_checked_at'])
        store=StateStore(run)
        with store.transaction():
            future=store.load()
            future['release_approvals'][key]['at']='2026-10-15T18:00:00.900000Z'
            store.save(future)
        with patch('okf_bundle.cli.datetime') as clock:
            clock.now.return_value=now
            with self.assertRaises(ReleaseError):
                execute(parser().parse_args(['package','--run',str(run),'--candidate',str(candidate),'--out',str(self.root/'future.zip')]))
        self.assertFalse((self.root/'future.zip').exists())

    def test_markdown_comparison_records_unverified_support_and_requires_human(self):
        from okf_bundle.availability import check_availability
        from okf_bundle.export import _comparison_issues,_live_check_issues
        plan=self.plan({'formats':['markdown']})
        import_mcp(self.state,self.receipt(plan),self.root)
        source=self.state['sources'][self.sid]
        self.state['claims']['fixture']={'id':'fixture','evidence':[{
            'source_id':self.sid,'version_id':source['active_version'],'locator':'body',
            'quote':'Private on-site instruction needs at least eight people.'}]}
        changed=test_managed.HTML.replace('Private on-site instruction needs at least eight people.',
                                         'This direct representation contains a different public statement.')
        check_availability(self.state,[self.sid],lambda url:{'status':200,'body':changed.encode(),'url':url})
        check=source['availability_check']
        self.assertIsNone(check['quotes_supported'])
        self.assertFalse(check['automatic_quotes_supported'])
        self.assertFalse(check['requires_source_refresh'])
        now=parse_time(utc_now())
        self.assertTrue(_comparison_issues(source,source['active_version'],now))
        self.assertTrue(_live_check_issues(self.state,source,source['active_version'],now))
        source['availability_comparison_review']={
            'version_id':source['active_version'],'response_hash':check['response_hash'],
            'by':'human:fixture-reviewer','at':utc_now(),'channel':'synthetic fixture',
            'reference':'fixture only, no real human decision','decision':'accepted',
            'reason':'Synthetic gate acceptance; does not assert semantic correctness'}
        self.assertFalse(_comparison_issues(source,source['active_version'],now))
        self.assertFalse(_live_check_issues(self.state,source,source['active_version'],now))
        self.assertIsNone(check['quotes_supported'])

    def test_direct_shell_keeps_rendered_availability_unknown(self):
        from okf_bundle.availability import check_availability
        from okf_bundle.review import accept_availability
        self.state['config']['publication_owner']='human:fixture-owner'
        plan=self.plan()
        import_mcp(self.state,self.receipt(plan),self.root)
        check_availability(self.state,[self.sid],lambda url:{'status':200,
            'body':b'<html><body><div id="root">Loading...</div></body></html>','url':url})
        source=self.state['sources'][self.sid]
        self.assertEqual('unknown',source['availability'])
        self.assertEqual('direct-shell-for-rendered-capture',source['availability_check']['outcome'])
        accept_availability(self.state,[self.sid],'human:fixture-owner',utc_now(),
                            'Synthetic acceptance of uncertainty','fixture','fixture only')
        self.assertEqual('human:fixture-owner',source['availability_acceptance']['owner'])

    def test_markdown_unknown_canonical_requires_actual_inclusion_decision(self):
        plan=self.plan({'formats':['markdown']})
        import_mcp(self.state,self.receipt(plan),self.root)
        source=self.state['sources'][self.sid]
        self.assertIsNone(source['canonical'])
        self.assertEqual('pending',source['scope'])
        source['scope']='included'
        now=parse_time(utc_now())
        self.assertTrue(_inclusion_issues(source,source['active_version'],now))
        source['inclusion_decision']={'version_id':source['active_version'],'decision':'included',
            'by':'human:fixture-reviewer','at':utc_now(),'channel':'synthetic','reference':'fixture only',
            'reason':'Synthetic explicit acceptance of unknown head signals'}
        self.assertFalse(_inclusion_issues(source,source['active_version'],now))

    def test_redirected_paid_page_is_held_while_other_page_imports(self):
        from okf_bundle.ingest import discover_csv
        from okf_bundle.workflow import merge_import
        for destination in ('https://example.com/moved/','https://outside.example/moved/'):
            with self.subTest(destination=destination):
                self.setUp()
                file=self.root/'second.csv'
                file.write_text('URL\nhttps://example.com/b/\n',encoding='utf-8')
                merge_import(self.state,discover_csv(file,self.config))
                other=next(sid for sid,s in self.state['sources'].items() if s['normalized_url'].endswith('/b/'))
                plan=prepare_mcp(self.state,[self.sid,other],{},'fixture only',
                    robots_fetch=lambda url,cfg,**kw:{'status':404,'body':b'','url':url})
                receipt=self.receipt(plan)
                batch={'id':receipt['job_id'],'status':'completed','total':2,'completed':2,'creditsUsed':2,'data':[
                    {'metadata':{'sourceURL':'https://example.com/a/','statusCode':200,'finalURL':destination},'markdown':test_managed.MD},
                    {'metadata':{'sourceURL':'https://example.com/b/','statusCode':200,'canonicalURL':'https://example.com/b/'},'markdown':test_managed.MD}]}
                artifact=self.root/receipt['artifact']
                artifact.write_text(json.dumps(batch),encoding='utf-8')
                receipt['sha256']=hashlib.sha256(artifact.read_bytes()).hexdigest()
                result=import_mcp(self.state,receipt,self.root)
                bad,good=self.state['sources'][self.sid],self.state['sources'][other]
                self.assertIsNone(bad['active_version'])
                self.assertEqual('pending',bad['scope'])
                self.assertIn('redirect_review_required',bad)
                self.assertIsNotNone(good['active_version'])
                self.assertEqual('included',good['scope'])
                self.assertEqual(2,result['budget']['credits_reported'])
