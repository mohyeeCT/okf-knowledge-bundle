"""Final independent-review regressions; no provider or public network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from okf_bundle.cli import execute, parser
from okf_bundle.common import digest, utc_now
from okf_bundle.export import ReleaseError, build_candidate, eligibility, manifest_hash, package_release, verify_candidate
from okf_bundle.network import collect, pending_attempts, reconcile_attempt
from okf_bundle.state import StateStore
from okf_bundle.validation import validate_bundle
from test_export import NOW, approve, fixture_state, rebind
import test_network as network_fixtures

LINK_BYPASSES = [
    '`[r]: https://example.com/`\n\nSee [docs www.evil.example][r] now.',
    'Prose [r]: https://example.com/\n\nSee [docs www.evil.example][r] now.',
    '    [r]: https://example.com/\n\nSee [docs www.evil.example][r] now.',
    '```\n[r]: https://example.com/\n```\n\nSee [docs www.evil.example][r] now.',
    '[see www.evil.example `](https://example.com/)` now',
    '[see ops@evil.example `](https://example.com/)` now',
    '[see www.evil.example\n\n](https://example.com/) now',
    'Visit "www.evil.example" now.',
]


class FinalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_renderer_context_bypasses_fail_standalone_candidate_and_package(self):
        for index, text in enumerate(LINK_BYPASSES):
            with self.subTest(text=text):
                state=fixture_state()
                good=self.root/f'valid-{index}'
                manifest=build_candidate(state,good,'valid',NOW)
                concept=next(p for p in (good/'okf/en/services').glob('*.md') if p.name!='index.md')
                concept.write_text(concept.read_text(encoding='utf-8')+'\n\n'+text,encoding='utf-8')
                report=validate_bundle(good/'okf',allowed_domains=['example.com'])
                self.assertFalse(report['profile']['ready'])
                self.assertTrue(report['core']['conformant'])
                approve(state,manifest)
                with self.assertRaises(ReleaseError):
                    package_release(state,good,self.root/f'tampered-{index}.zip',NOW)
                state['claims']['claim-1']['public_statement']+='\n\n'+text
                rebind(state)  # Fictional fixture review actors only.
                candidate=self.root/f'hostile-{index}'
                with self.assertRaises(ReleaseError):
                    build_candidate(state,candidate,'hostile',NOW)
                self.assertFalse(candidate.exists())

    def test_raw_scan_is_conservative_but_https_email_paths_and_safe_www_still_package(self):
        for index,text in enumerate(('Visit www.example.com.', 'https://example.com/contact/ops@example.com',
                                     '[Contact](https://example.com/contact/ops@example.com)')):
            state=fixture_state()
            state['config']['domains'].append('www.example.com')
            state['config_hash']=digest(state['config'])
            state['claims']['claim-1']['public_statement']+='\n\n'+text
            rebind(state)
            candidate=self.root/f'safe-{index}'
            manifest=build_candidate(state,candidate,'safe',NOW)
            approve(state,manifest)
            package_release(state,candidate,self.root/f'safe-{index}.zip',NOW)
            self.assertEqual(verify_candidate(candidate),manifest)

    def test_malformed_json_shapes_preserve_reviewed_capture_through_actual_adapter(self):
        shapes=[[],{}, {'data':{}}, {'data':'bad'}, {'data':{'html':None}}, {'data':{'html':23}}]
        for envelope in shapes:
            with self.subTest(envelope=envelope):
                state,source=network_fixtures.NetworkTests().reviewed()
                before=copy.deepcopy(state)
                connection=MagicMock()
                connection.getresponse.return_value=network_fixtures.FakeResponse(200,json.dumps(envelope).encode())
                with patch.dict('os.environ',{'JINA_API_KEY':'fixture-not-a-real-key'}), patch('okf_bundle.network.http.client.HTTPSConnection',return_value=connection):
                    collect(state,refresh=True)
                self.assertEqual(before['claims'],state['claims'])
                self.assertEqual(before['release_approvals'],state['release_approvals'])
                self.assertEqual(before['sources'][source['id']]['versions'],source['versions'])
                self.assertEqual(before['sources'][source['id']]['availability_acceptance'],source['availability_acceptance'])
                self.assertEqual('provider_error',source['collection_attempts'][-1]['outcome'])
                self.assertEqual(1,connection.request.call_count)
                self.assertIsNotNone(source['collection_attempts'][-1]['response_hash'])

    def test_allow_retry_blocks_release_until_new_committed_result(self):
        state=fixture_state()
        source=state['sources']['source-1']
        state['config']['collection_authorized']=True
        state['config_hash']=digest(state['config'])
        source['collection_attempts']=[{'outcome':'pending','requested_at':'2026-10-01T12:00:00Z','completed_at':None,
            'source_url':source['normalized_url'],'version_id_at_request':source['active_version'], 'reserved_tokens':1000,'usage_tokens':None}]
        descriptor=pending_attempts(source)[0]
        reconcile_attempt(state,source['id'],0,'allow-retry','Fictional operator authorizes one retry',
            state['config']['publication_owner'],utc_now(),'synthetic','fixture-only',
            expected_attempt_hash=descriptor['attempt_hash'],expected_version=source['active_version'])
        self.assertFalse(eligibility(state,utc_now())['ready'])
        self.assertIn('awaiting a committed retry result',json.dumps(eligibility(state,utc_now())))
        collect(state,lambda *_:{'provider_status':402})
        # A committed provider denial restores the prior reviewed capture under N1;
        # no pending retry window remains, and the original unknown usage is retained.
        self.assertTrue(eligibility(state,utc_now())['ready'])
        self.assertEqual('pending',source['collection_attempts'][0]['outcome'])
        self.assertEqual(1000,source['collection_attempts'][0]['reserved_tokens'])

    def test_release_approval_refuses_changed_candidate_before_saving_decision(self):
        state=fixture_state()
        run=self.root/'run'
        candidate=self.root/'candidate'
        manifest=build_candidate(state,candidate,'frozen',utc_now())
        key=manifest_hash(manifest)
        state['candidates'][key]={'path':str(candidate),'manifest_hash':key}
        state['claims']['claim-1']['public_statement']+=' Changed after candidate.'
        rebind(state)
        store=StateStore(run)
        with store.transaction():
            store.save(state)
        command=['release-approve','--run',str(run),'--candidate',str(candidate),'--manifest-hash',key,
            '--by',state['config']['publication_owner'],'--at',utc_now(),'--channel','synthetic','--reference','fixture-only']
        with self.assertRaises(ReleaseError):
            execute(parser().parse_args(command))
        self.assertEqual({},StateStore(run).load()['release_approvals'])

    def test_wildcard_scope_is_rejected_as_invalid_configuration(self):
        with self.assertRaises(ValueError):
            execute(parser().parse_args(['validate','--bundle',str(self.root),'--domains','*.example.com']))


if __name__=='__main__':
    unittest.main()
