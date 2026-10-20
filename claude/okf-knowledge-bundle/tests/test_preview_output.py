"""Separate readable review from audit data without changing approval binding."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from lxml import html
from okf_bundle.batch_review import review_preview, approve_preview
from okf_bundle.cli import execute, parser
from okf_bundle.common import digest, utc_now
from okf_bundle.preview_output import content_html, write_preview
from okf_bundle.review import import_drafts
from okf_bundle.state import StateStore
from test_review_state import draft_state


class PreviewOutputTests(unittest.TestCase):
    def setUp(self):
        self.state, _, draft = draft_state()
        draft['claims'][0]['qualifiers'] = {'audience': 'internal evidence note'}
        self.assertTrue(import_drafts(self.state, draft)['accepted'])
        self.preview = review_preview(self.state, list(self.state['concepts']))

    def test_clean_view_keeps_exact_public_text_and_per_statement_source(self):
        doc = html.fromstring(content_html(self.preview))
        wording = doc.xpath('//*[contains(@class,"wording")]/text()')
        self.assertIn(next(iter(self.preview['concepts'].values()))['description'], wording)
        self.assertIn(next(iter(self.preview['claims'].values()))['public_statement'], wording)
        self.assertIn('https://example.com/training/', doc.xpath('//a/@href'))
        visible = doc.text_content()
        self.assertNotIn('internal evidence note', visible)
        self.assertNotIn(next(iter(self.preview['claims'])), visible)
        self.assertNotIn(digest(self.preview), visible)
        self.assertIn('Topic details', visible)

    def test_markup_cannot_execute_or_hide_reviewed_text(self):
        statement = '<script>alert(1)</script> & <strong>condition</strong>'
        next(iter(self.preview['claims'].values()))['public_statement'] = statement
        next(iter(self.preview['concepts'].values()))['title'] = '<img src=x onerror=alert(1)>'
        next(iter(self.preview['sources'].values()))['url'] = 'javascript:alert(1)'
        doc = html.fromstring(content_html(self.preview))
        self.assertEqual(doc.xpath('//script|//img'), [])
        self.assertIn(statement, doc.text_content())
        self.assertFalse(any(url.startswith('javascript:') for url in doc.xpath('//a/@href')))

    def test_audit_retains_all_quotes_qualifiers_and_original_json_contract(self):
        before = copy.deepcopy(self.state)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = write_preview(self.preview, root/'review.json', root/'private')
            self.assertEqual(json.loads(Path(result['preview']).read_text()), self.preview)
            audit = Path(result['audit_details']).read_text(encoding='utf-8')
            self.assertIn(digest(self.preview), audit)
            self.assertIn('internal evidence note', audit)
            for claim in self.preview['claims'].values():
                for evidence in claim['evidence']:
                    self.assertIn(evidence['quote'], audit)
            self.assertEqual(self.state, before)

    def test_companion_collision_refuses_without_partial_writes(self):
        for suffix in ('.json', '.content.html', '.audit.md'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                existing = root / ('review' + suffix)
                existing.write_text('keep me')
                with self.assertRaises(ValueError):
                    write_preview(self.preview, root/'review.json', root/'private')
                self.assertEqual(list(root.iterdir()), [existing])
                self.assertEqual(existing.read_text(), 'keep me')

    def test_no_files_written_inside_immutable_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                write_preview(self.preview, root/'private/review.json', root/'private')
            self.assertFalse((root/'private').exists())

    def test_native_cli_returns_separate_views_with_no_state_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = StateStore(root/'run')
            with store.transaction(): store.save(self.state)
            before = store.load()
            result = execute(parser().parse_args(['review-preview','--run',str(root/'run'),
                '--concept-ids',*self.state['concepts'],'--out',str(root/'review.json')]))
            self.assertTrue(Path(result['content_preview']).exists())
            self.assertTrue(Path(result['audit_details']).exists())
            self.assertEqual(before, store.load())
            approved = approve_preview(self.state, self.preview, result['preview_hash'],
                'human:fixture-analyst', utc_now(), 'synthetic', 'exact shown content approved')
            self.assertEqual(approved['business_confirmations_recorded'], 0)
            self.assertEqual(approved['release_approvals_recorded'], 0)

    def test_excluded_topics_never_appear_in_clean_content(self):
        omitted = copy.deepcopy(next(iter(self.state['concepts'].values())))
        omitted['title'] = 'Do not include this excluded topic'
        self.state['concepts']['omitted'] = omitted
        preview = review_preview(self.state, list(self.preview['concepts']))
        self.assertIn('omitted', preview['excluded_concept_ids'])
        self.assertNotIn(omitted['title'], content_html(preview))


if __name__ == '__main__': unittest.main()
