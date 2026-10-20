"""Critical-only policy and one revision-bound content decision, offline."""
import copy
import unittest
from okf_bundle.batch_review import review_preview, approve_preview
from okf_bundle.common import digest, utc_now
from okf_bundle.review import import_drafts, claim_revision, needs_business_confirmation
from okf_bundle.state import new_state
from okf_bundle.export import _claim_result
from test_review_state import draft_state
from test_export import fixture_state, NOW


class CompactReviewTests(unittest.TestCase):
    def make_state(self):
        state, source, payload = draft_state()
        state['config']['confirmation_policy'] = 'critical-only'
        payload['claims'][0].update(subject='Legal services', public_statement='Training is offered on site.')
        self.assertTrue(import_drafts(state, payload)['accepted'])
        return state, source, payload

    def test_new_runs_default_critical_only_and_invalid_policy_fails(self):
        self.assertEqual(new_state({'domains':['example.com']})['config']['confirmation_policy'], 'critical-only')
        with self.assertRaises(ValueError):
            new_state({'domains':['example.com'], 'confirmation_policy':'anything'})

    def test_ordinary_sector_person_price_coverage_hints_do_not_require_client(self):
        state, _, payload = self.make_state()
        payload['claims'][0]['risk_categories'] = ['price','credential','regulated','coverage_hours','person','statistic_award']
        self.assertTrue(import_drafts(state, payload)['accepted'])
        claim = next(iter(state['claims'].values()))
        self.assertEqual(claim['business_confirmation'], 'not_required')
        self.assertTrue(claim['risk_categories'])
        self.assertFalse(needs_business_confirmation(state, claim))

    def test_explicit_critical_reason_disputed_and_volatile_stay_required(self):
        for extra in ({'confirmation_reason':'Contact and schema list incompatible office addresses.'},
                      {'risk_categories':['disputed']}, {'risk_categories':['volatile']}):
            with self.subTest(extra=extra):
                state, _, payload = self.make_state()
                payload['claims'][0].update(extra)
                self.assertTrue(import_drafts(state, payload)['accepted'])
                claim = next(iter(state['claims'].values()))
                self.assertEqual(claim['business_confirmation'], 'required')
                with self.assertRaises(ValueError):
                    review_preview(state, list(state['concepts']))

    def test_legacy_missing_policy_preserves_all_risk_and_hash(self):
        state, _, payload = self.make_state()
        state['config'].pop('confirmation_policy')
        state['claims'].clear()
        state['concepts'].clear()
        self.assertTrue(import_drafts(state, payload)['accepted'])
        claim = next(iter(state['claims'].values()))
        self.assertEqual(claim['business_confirmation'], 'required')
        absent = claim_revision(claim)
        claim['confirmation_reason'] = None
        self.assertEqual(absent, claim_revision(claim))

    def test_confirmation_reason_changes_revision_and_resets_decisions(self):
        state, _, payload = self.make_state()
        claim = next(iter(state['claims'].values()))
        old = claim['revision']
        payload['claims'][0]['confirmation_reason'] = 'Two pages disagree on whether this consultation is free.'
        self.assertTrue(import_drafts(state, payload)['accepted'])
        claim = next(iter(state['claims'].values()))
        self.assertNotEqual(old, claim['revision'])
        self.assertEqual(claim['business_confirmation'], 'required')
        self.assertEqual(claim['publication'], 'pending')

    def test_one_actual_content_decision_binds_all_fields_without_client_or_release(self):
        state, _, _ = self.make_state()
        preview = review_preview(state, list(state['concepts']))
        result = approve_preview(state, preview, digest(preview), 'human:analyst', utc_now(), 'synthetic-test', 'fixture-approval-only')
        self.assertTrue(result['content_review_recorded'])
        claim = next(iter(state['claims'].values()))
        self.assertEqual((claim['evidence_review'], claim['publication'], claim['business_confirmation']),
                         ('accepted','approved','not_required'))
        self.assertEqual(next(iter(state['concepts'].values()))['review']['decision'], 'accepted')
        self.assertEqual(state['release_approvals'], {})
        self.assertEqual(len([event for event in state['review_history'] if event.get('type') == 'content-batch']), 1)

    def test_tampered_preview_stale_source_or_config_fails_without_mutation(self):
        for change in ('preview','source','config','draft'):
            with self.subTest(change=change):
                state, source, payload = self.make_state()
                preview = review_preview(state, list(state['concepts']))
                original_hash = digest(preview)
                if change == 'preview': preview['approval_scope'].append('business_confirmation')
                if change == 'source': state['sources'][source['id']]['scope'] = 'excluded'
                if change == 'config': state['config']['public_base_path'] = '/new/'
                if change == 'draft':
                    payload['claims'][0]['public_statement'] = 'On-site training is offered.'
                    import_drafts(state, payload)
                before = copy.deepcopy(state)
                with self.assertRaises(ValueError):
                    approve_preview(state, preview, original_hash, 'human:analyst', utc_now(), 'synthetic-test', 'fixture')
                self.assertEqual(before, state)

    def test_nonhuman_or_future_decision_fails_atomically(self):
        for by, at in [('model:reviewer',utc_now()), ('human:analyst','2999-01-01T00:00:00Z')]:
            state, _, _ = self.make_state()
            preview = review_preview(state, list(state['concepts']))
            before = copy.deepcopy(state)
            with self.assertRaises(ValueError):
                approve_preview(state, preview, digest(preview), by, at, 'synthetic-test', 'fixture')
            self.assertEqual(before, state)

    def test_no_batch_revival_of_withheld_or_rejected_facts(self):
        for key, value in [('publication','withheld'), ('evidence_review','rejected'), ('conflict','open')]:
            state, _, _ = self.make_state()
            next(iter(state['claims'].values()))[key] = value
            with self.assertRaises(ValueError):
                review_preview(state, list(state['concepts']))

    def test_export_uses_policy_but_does_not_waive_critical_confirmation(self):
        state = fixture_state()
        state['config']['confirmation_policy'] = 'critical-only'
        claim = state['claims']['claim-1']
        claim['risk_categories'] = ['regulated']
        claim['revision'] = claim_revision(claim)
        for event in claim['reviews']: event['revision'] = claim['revision']
        from okf_bundle.validation import parse_instant
        ordinary = _claim_result(state, claim, 180, parse_instant(NOW))
        self.assertNotIn('Risk policy requires separate business confirmation', str(ordinary))
        claim['confirmation_reason'] = 'Conflicting fee terms.'
        critical = _claim_result(state, claim, 180, parse_instant(NOW))
        self.assertIn('Risk policy requires separate business confirmation', str(critical))


if __name__ == '__main__': unittest.main()
