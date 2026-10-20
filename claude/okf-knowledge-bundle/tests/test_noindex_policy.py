"""Noindex hosting instructions, retained source links and legacy policies."""
import copy
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from okf_bundle.common import digest
from okf_bundle.export import build_candidate, eligibility, verify_candidate
from okf_bundle.publication import verify_publication
from okf_bundle.state import new_state, StateStore
from test_export import fixture_state, NOW


class NoindexPolicyTests(unittest.TestCase):
    def test_new_default_and_explicit_legacy_policy(self):
        self.assertEqual(new_state({'domains':['example.com']})['config']['indexing'], 'noindex-crawlable')
        self.assertEqual(new_state({'domains':['example.com'],'indexing':'indexable'})['config']['indexing'], 'indexable')

    def test_loading_legacy_state_does_not_migrate_policy_or_hash(self):
        state = fixture_state()
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(temp)
            with store.transaction(): store.save(state)
            self.assertEqual(store.load(), state)

    def test_new_candidate_keeps_links_and_private_instructions(self):
        state = fixture_state()
        state['config']['indexing'] = 'noindex-crawlable'
        state['config_hash'] = digest(state['config'])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'candidate'
            manifest = build_candidate(state, root, 'synthetic-noindex', NOW)
            verify_candidate(root)
            text = (root/'DEPLOYMENT.md').read_text(encoding='utf-8')
            self.assertIn('X-Robots-Tag: noindex', text)
            self.assertIn('do not add a robots.txt Disallow', text)
            self.assertIn('Keep the original website pages indexable', text)
            self.assertNotIn('provide an indexable, snippet-eligible entry route', text)
            self.assertIn('https://example.com/training/', ''.join(p.read_text(encoding='utf-8') for p in (root/'okf').rglob('*.md')))
            self.assertEqual(manifest['deployment']['indexing'], 'noindex-crawlable')
            self.assertNotIn('DEPLOYMENT.md', manifest['payload'])

    def test_legacy_handoff_keeps_its_actual_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'candidate'
            build_candidate(fixture_state(), root, 'synthetic-legacy', NOW)
            text = (root/'DEPLOYMENT.md').read_text(encoding='utf-8')
            self.assertIn('Preserve this run\'s explicit indexable policy', text)
            self.assertNotIn('Set `X-Robots-Tag: noindex`', text)

    def test_legacy_direct_fetch_still_requires_owner_decision(self):
        state = fixture_state()
        state['config']['indexing'] = 'direct-fetch-only'
        state['config_hash'] = digest(state['config'])
        self.assertTrue(any(i['rule']=='profile.indexing' for i in eligibility(state, NOW)['issues']))

    def test_verifier_requires_global_noindex_for_every_payload_response(self):
        body = b'# Synthetic source-linked content\n'
        manifest = {'payload':{'index.md':digest(body),'en/service.md':digest(body)},
                    'deployment':{'indexing':'noindex-crawlable'}}
        for header, expected in [('noindex',True),('NOINDEX, follow',True),('',False),
                                 ('googlebot: noindex',False),('index',False)]:
            with self.subTest(header=header):
                def fetch(url,headers):
                    return {'status':200,'body':body,'headers':{'content-type':'text/markdown','X-Robots-Tag':header}}
                result = verify_publication(manifest, 'https://example.com/okf/', fetch)
                self.assertEqual(result['ready'], expected)
                self.assertEqual(len(result['observations']),2)

    def test_archive_normal_download_also_requires_header(self):
        state = fixture_state()
        state['config']['indexing'] = 'noindex-crawlable'
        state['config_hash'] = digest(state['config'])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'candidate'
            manifest = build_candidate(state, root, 'synthetic-headers', NOW)
            def fetch(url,headers):
                path = urlsplit(url).path.removeprefix('/okf/')
                response_headers = {'content-type':'application/gzip' if path.endswith('.gz') else 'text/markdown',
                                    'x-robots-tag':'noindex'}
                if not headers: response_headers.pop('x-robots-tag')
                return {'status':200,'body':(root/'okf'/path).read_bytes(),'headers':response_headers}
            result = verify_publication(manifest, 'https://example.com/okf/', fetch)
            self.assertFalse(result['ready'])
            self.assertTrue(any('Normal archive' in i['message'] for i in result['issues']))


if __name__ == '__main__': unittest.main()
