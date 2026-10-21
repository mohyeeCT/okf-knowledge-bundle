"""Reproduce rewritten final reviews and partial-capture exclusion regressions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from okf_bundle.batch_review import review_preview, approve_preview
from okf_bundle.cli import execute, parser
from okf_bundle.common import digest, utc_now
from okf_bundle.continuity import import_context, export_context, resolve_exclusion, selection_issues
from okf_bundle.delivery import seal_review, verify_delivery
from okf_bundle.export import eligibility, state_fingerprint
from okf_bundle.review import import_drafts
from okf_bundle.state import StateStore, new_state
from test_review_state import draft_state


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review = self.root / 'review.md'
        self.preview = self.root / 'preview.content.html'
        self.preview.write_text('<p>Unchanged proposed wording</p>')
        self.review.write_bytes(b'Reviewed 3 pages. One question remains. Please approve the proposed content.\n')
        self.out = self.root / 'sealed'

    def seal(self):
        return seal_review(self.review, self.preview, self.out, 'sandbox:/mnt/data/review')

    def test_exact_attachment_and_note_are_counted_together(self):
        result = self.seal()
        self.assertTrue(verify_delivery(self.out)['valid'])
        self.assertEqual((self.out/'analyst-review.md').read_bytes(), self.review.read_bytes())
        self.assertEqual((self.out/'proposed-content.html').read_bytes(), self.preview.read_bytes())
        self.assertGreater(result['word_count'], 13)
        self.assertLessEqual(result['word_count'], 150)

    def test_previously_valid_draft_is_not_enough_after_rewrite(self):
        self.seal()
        actual = self.root/'outgoing.md'
        actual.write_text('Rewritten review with extra explanations.')
        result = verify_delivery(self.out, actual)
        self.assertFalse(result['valid'])
        self.assertFalse(result['message_matches'])

    def test_exact_final_note_matches(self):
        result = self.seal()
        actual = self.root/'outgoing.md'
        actual.write_text(result['delivery_text'])
        self.assertTrue(verify_delivery(self.out, actual)['valid'])

    def test_mutated_attachment_or_preview_fails(self):
        self.seal()
        for name in ('analyst-review.md', 'proposed-content.html', 'delivery.md'):
            p = self.out/name
            old = p.read_bytes()
            p.write_bytes(old + b' edited')
            self.assertFalse(verify_delivery(self.out)['valid'])
            p.write_bytes(old)

    def test_overflow_including_delivery_note_refuses_without_output(self):
        self.review.write_text(' '.join(['word']*149))
        with self.assertRaisesRegex(ValueError, 'plus delivery'):
            self.seal()
        self.assertFalse(self.out.exists())

    def test_collision_cannot_rewrite_sealed_review(self):
        self.seal()
        before = (self.out/'receipt.json').read_bytes()
        with self.assertRaises(ValueError):
            self.seal()
        self.assertEqual(before, (self.out/'receipt.json').read_bytes())

    def test_malformed_receipt_cannot_escape_directory(self):
        self.seal()
        p = self.out/'receipt.json'
        data = json.loads(p.read_text())
        data['files']['../secret'] = 'x'
        p.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            verify_delivery(self.out)


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.state, _, self.draft = draft_state()
        self.assertTrue(import_drafts(self.state, self.draft)['accepted'])
        self.payload = {'domains': ['example.com'], 'checked_references': ['Prior client review'],
            'history_status': 'checked', 'exclusions': [{'key': 'delivery-conflict',
            'reason': 'On-site delivery remains unresolved.', 'source_reference': 'Prior review question 1',
            'selectors': [['on site']]}]}
        import_context(self.state, self.payload)

    def test_fresh_run_import_carries_holds_not_approvals(self):
        fresh, _, draft = draft_state()
        self.assertTrue(import_drafts(fresh, draft)['accepted'])
        result = import_context(fresh, export_context(self.state))
        self.assertEqual(result['unresolved'], 1)
        self.assertEqual(result['approvals_imported'], 0)
        with self.assertRaisesRegex(ValueError, 'Unresolved exclusion'):
            review_preview(fresh, list(fresh['concepts']))

    def test_empty_import_cannot_remove_existing_hold(self):
        empty = dict(self.payload, exclusions=[])
        import_context(self.state, empty)
        self.assertEqual(len(export_context(self.state)['exclusions']), 1)

    def test_changed_hold_cannot_silently_weaken_selector(self):
        changed = copy.deepcopy(self.payload)
        changed['exclusions'][0]['selectors'] = [['does not occur']]
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            import_context(self.state, changed)
        self.assertEqual(before, self.state)

    def test_foreign_client_rejected_atomically(self):
        foreign = dict(self.payload, domains=['other.example.com'])
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            import_context(self.state, foreign)
        self.assertEqual(before, self.state)

    def test_approval_fields_cannot_be_smuggled_into_hold(self):
        payload = copy.deepcopy(self.payload)
        payload['exclusions'][0]['approved'] = True
        with self.assertRaises(ValueError):
            import_context(self.state, payload)

    def test_description_is_checked_even_if_statement_avoids_phrase(self):
        draft = copy.deepcopy(self.draft)
        draft['claims'][0]['public_statement'] = 'Training is available.'
        draft['concepts'][0]['description'] = 'Training delivered ON-SITE.'
        self.assertTrue(import_drafts(self.state, draft)['accepted'])
        with self.assertRaisesRegex(ValueError, 'Unresolved exclusion'):
            review_preview(self.state, list(self.state['concepts']))

    def test_title_and_statement_joint_context_match(self):
        payload = copy.deepcopy(self.payload)
        payload['exclusions'][0].update(key='scoped', selectors=[['training', 'on site']])
        import_context(self.state, payload)
        self.assertTrue(selection_issues(self.state, list(self.state['concepts'])))

    def test_renamed_claim_is_still_excluded(self):
        self.draft['claims'][0]['key'] = 'renamed'
        self.draft['concepts'][0]['claim_keys'] = ['renamed']
        self.assertTrue(import_drafts(self.state, self.draft)['accepted'])
        with self.assertRaisesRegex(ValueError, 'Unresolved exclusion'):
            review_preview(self.state, list(self.state['concepts']))

    def test_unrelated_selected_topic_proceeds_in_same_run(self):
        draft = copy.deepcopy(self.draft)
        draft['claims'][0].update(key='other', public_statement='Training is available.')
        draft['concepts'][0].update(key='other', title='Training', description='Training.', claim_keys=['other'])
        self.assertTrue(import_drafts(self.state, draft)['accepted'])
        selected = [c['id'] for c in self.state['concepts'].values() if c['key'] == 'other']
        self.assertEqual(len(review_preview(self.state, selected)['concepts']), 1)
        preview = review_preview(self.state, selected)
        approve_preview(self.state, preview, digest(preview), 'human:analyst', utc_now(), 'fixture', 'fixture approval')
        self.assertFalse(any(i['rule'].startswith('profile.continuity') for i in eligibility(self.state, utc_now())['issues']))
        self.assertEqual(len(export_context(self.state)['exclusions']), 1)

    def test_new_hold_invalidates_candidate_state_fingerprint(self):
        before = state_fingerprint(self.state)
        extra = copy.deepcopy(self.payload)
        extra['exclusions'][0]['key'] = 'second-hold'
        import_context(self.state, extra)
        self.assertNotEqual(before, state_fingerprint(self.state))

    def test_readiness_also_catches_holds(self):
        result = eligibility(self.state, utc_now())
        self.assertTrue(any(i['rule'] == 'profile.continuity.excluded' for i in result['issues']))

    def test_resolution_needs_human_and_current_provenance(self):
        with self.assertRaises(ValueError):
            resolve_exclusion(self.state, 'delivery-conflict', 'Captured page says so', 'assistant:test', utc_now(), 'chat', 'test')
        with self.assertRaises(ValueError):
            resolve_exclusion(self.state, 'delivery-conflict', 'Old approval', 'human:analyst', '2000-01-01T00:00:00Z', 'chat', 'old')
        resolve_exclusion(self.state, 'delivery-conflict', 'Actual clarification', 'human:analyst', utc_now(), 'chat', 'new clarification')
        self.assertEqual(selection_issues(self.state, list(self.state['concepts'])), [])
        self.assertEqual(export_context(self.state)['exclusions'], [])

    def test_unavailable_history_is_explicit_not_fabricated_empty_search(self):
        fresh = new_state({'domains': ['example.com'], 'continuity_required': True})
        import_context(fresh, dict(self.payload, history_status='unavailable', exclusions=[]))
        self.assertEqual(export_context(fresh)['history_status'], 'unavailable')

    def test_cli_new_run_requires_context_even_when_config_disables_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root/'config.json'
            config.write_text(json.dumps({'domains': ['example.com'], 'continuity_required': False}))
            execute(parser().parse_args(['init', '--run', str(root/'run'), '--config', str(config)]))
            state = StateStore(root/'run').load()
            self.assertTrue(state['config']['continuity_required'])
            self.assertEqual(selection_issues(state, [])[0]['rule'], 'profile.continuity.unchecked')

    def test_legacy_state_not_silently_migrated(self):
        legacy, _, _ = draft_state()
        before = copy.deepcopy(legacy)
        self.assertEqual(selection_issues(legacy, list(legacy['concepts'])), [])
        self.assertEqual(before, legacy)
