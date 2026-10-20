"""Approved selection stays in one run; synthetic decisions, no live requests."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from okf_bundle.batch_review import approve_preview, review_preview
from okf_bundle.cli import execute, parser
from okf_bundle.common import digest, utc_now
from okf_bundle.export import ReleaseError, build_candidate, eligibility, package_release, state_fingerprint, verify_candidate
from okf_bundle.review import claim_revision, concept_revision
from okf_bundle.state import StateStore
from okf_bundle.owner_setup import set_publication_owner
from test_export import fixture_state


def selection_fixture():
    state = fixture_state()
    excluded_claim = copy.deepcopy(state['claims']['claim-1'])
    excluded_claim.update(id='claim-excluded', public_statement='An unresolved consultation claim.',
                          evidence_review='pending', publication='pending', business_confirmation='required',
                          conflict='open', reviews=[], confirmation_reason='Conflicting free and paid terms.')
    excluded_claim['revision'] = claim_revision(excluded_claim)
    state['claims'][excluded_claim['id']] = excluded_claim
    excluded_concept = copy.deepcopy(state['concepts']['concept-1'])
    excluded_concept.update(id='concept-excluded', key='unresolved-consultation', title='Unresolved terms',
                            claim_ids=[excluded_claim['id']], review={'decision':'pending'})
    excluded_concept['revision'] = concept_revision(excluded_concept, state['claims'])
    state['concepts'][excluded_concept['id']] = excluded_concept
    preview = review_preview(state, ['concept-1'])
    return state, preview


def approve_selection(state, preview):
    return approve_preview(state, preview, digest(preview), 'human:analyst', utc_now(), 'synthetic-test', 'fixture-only approval')


class BundleSelectionTests(unittest.TestCase):
    def bind(self, state, preview):
        from okf_bundle.batch_review import bind_approved_bundle
        return bind_approved_bundle(state, preview, digest(preview))

    def test_excluded_critical_topic_does_not_block_same_run_candidate(self):
        state, preview = selection_fixture()
        excluded_before = copy.deepcopy((state['claims']['claim-excluded'], state['concepts']['concept-excluded']))
        run_id = state['run_id']
        approve_selection(state, preview)
        report = eligibility(state, utc_now())
        self.assertTrue(report['ready'], report['issues'])
        self.assertEqual(report['included_concept_ids'], ['concept-1'])
        self.assertEqual(report['excluded_concept_ids'], ['concept-excluded'])
        self.assertEqual(excluded_before, (state['claims']['claim-excluded'], state['concepts']['concept-excluded']))
        self.assertEqual(state['run_id'], run_id)
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / 'candidate'
            manifest = build_candidate(state, candidate, 'fixture-selected', utc_now())
            self.assertEqual(set(manifest['included_concepts']), {'concept-1'})
            self.assertEqual(manifest['withheld_concepts'], ['concept-excluded'])
            self.assertEqual(verify_candidate(candidate), manifest)
            public = '\n'.join(p.read_text() for p in (candidate/'okf').rglob('*.md'))
            self.assertNotIn('An unresolved consultation claim.', public)
            with self.assertRaises(ReleaseError):
                package_release(state, candidate, Path(temp)/'unapproved.zip', utc_now())

    def test_unselected_previously_eligible_topic_is_not_silently_published(self):
        state, preview = selection_fixture()
        other_claim = copy.deepcopy(state['claims']['claim-1'])
        other_claim['id'] = 'claim-other'
        state['claims']['claim-other'] = other_claim
        other = copy.deepcopy(state['concepts']['concept-1'])
        other.update(id='concept-other', key='another-topic', claim_ids=['claim-other'])
        other['revision'] = concept_revision(other, state['claims'])
        other['review']['revision'] = other['revision']
        state['concepts']['concept-other'] = other
        preview = review_preview(state, ['concept-1'])
        approve_selection(state, preview)
        self.assertEqual(eligibility(state, utc_now())['included_concept_ids'], ['concept-1'])

    def test_selected_conflict_or_failed_source_still_blocks(self):
        for failure in ('conflict', 'source'):
            with self.subTest(failure=failure):
                state, preview = selection_fixture()
                approve_selection(state, preview)
                if failure == 'conflict':
                    state['claims']['claim-1']['conflict'] = 'open'
                else:
                    state['sources']['source-1']['captured_status'] = 404
                self.assertFalse(eligibility(state, utc_now())['ready'])

    def test_selected_revision_change_cannot_reduce_selection_to_ready_subset(self):
        state, preview = selection_fixture()
        approve_selection(state, preview)
        claim = state['claims']['claim-1']
        claim['public_statement'] = 'Changed wording.'
        claim['revision'] = claim_revision(claim)
        self.assertFalse(eligibility(state, utc_now())['ready'])

    def test_unselected_changes_do_not_invalidate_selected_content_approval(self):
        state, preview = selection_fixture()
        approve_selection(state, preview)
        state['concepts']['concept-excluded']['description'] = 'Still unresolved and omitted.'
        self.assertTrue(eligibility(state, utc_now())['ready'])

    def test_cli_same_run_approval_owner_setup_and_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            state, preview = selection_fixture()
            run = Path(temp)/'run'
            store = StateStore(run)
            with store.transaction():
                store.save(state)
            preview_file = Path(temp)/'preview.json'
            preview_file.write_text(json.dumps(preview))
            def call(command, *args):
                return execute(parser().parse_args([command, '--run', str(run), *args]))
            call('review-batch', '--preview', str(preview_file), '--preview-hash', digest(preview),
                 '--by', 'human:analyst', '--at', utc_now(), '--channel', 'synthetic-test', '--reference', 'fixture-only')
            self.assertTrue(call('readiness')['ready'])
            result = call('candidate', '--release-id', 'fixture-selected')
            self.assertEqual(result['status'], 'awaiting-release-owner-approval')
            after = StateStore(run).load()
            self.assertEqual(after['run_id'], state['run_id'])
            self.assertEqual(len(after['concepts']), 2)
            self.assertEqual(after['release_approvals'], {})

    def test_legacy_run_without_selection_keeps_whole_run_gate_and_fingerprint(self):
        state, preview = selection_fixture()
        legacy_hash = digest({k:state.get(k) for k in ('schema_version','tool_version','config','sources','claims','concepts')})
        self.assertEqual(state_fingerprint(state), legacy_hash)
        self.assertFalse(eligibility(state, utc_now())['ready'])

    def test_existing_batch_can_bind_without_new_reviews_or_run(self):
        state, preview = selection_fixture()
        approve_selection(state, preview)
        state.pop('bundle_selection')
        before = copy.deepcopy(state)
        self.assertFalse(eligibility(state, utc_now())['ready'])
        result = self.bind(state, preview)
        self.assertEqual(result['new_human_decisions'], 0)
        self.assertTrue(eligibility(state, utc_now())['ready'])
        for key in before:
            self.assertEqual(state[key], before[key], key)

    def test_existing_batch_binding_after_initial_owner_setup(self):
        state, preview = selection_fixture()
        state['config']['publication_owner'] = None
        state['config_hash'] = digest(state['config'])
        preview = review_preview(state, ['concept-1'])
        approve_selection(state, preview)
        state.pop('bundle_selection')
        set_publication_owner(state, 'human:client-owner', 'human:analyst', utc_now(), 'synthetic-test', 'fixture owner')
        self.bind(state, preview)
        self.assertTrue(eligibility(state, utc_now())['ready'])

    def test_binding_refuses_unapproved_or_edited_preview_atomically(self):
        for mode in ('unapproved', 'edited', 'missing-review', 'changed-claim'):
            with self.subTest(mode=mode):
                state, preview = selection_fixture()
                if mode != 'unapproved':
                    approve_selection(state, preview)
                    state.pop('bundle_selection')
                if mode == 'edited':
                    preview['concepts']['concept-1']['title'] = 'Edited preview'
                if mode == 'missing-review':
                    state['claims']['claim-1']['reviews'] = []
                if mode == 'changed-claim':
                    state['claims']['claim-1']['public_statement'] = 'Unseen wording'
                before = copy.deepcopy(state)
                with self.assertRaises(ValueError):
                    self.bind(state, preview)
                self.assertEqual(state, before)

    def test_malformed_or_tampered_selection_fails_closed(self):
        for value in (None, {}, [], {'concept_revisions':{}}, 'bad'):
            state, preview = selection_fixture()
            approve_selection(state, preview)
            state['bundle_selection'] = value
            self.assertFalse(eligibility(state, utc_now())['ready'])
        state, preview = selection_fixture()
        approve_selection(state, preview)
        state['bundle_selection']['concept_revisions']['concept-excluded'] = state['concepts']['concept-excluded']['revision']
        self.assertFalse(eligibility(state, utc_now())['ready'])

    def test_new_draft_does_not_expand_selection_and_new_batch_replaces_it(self):
        state, preview = selection_fixture()
        approve_selection(state, preview)
        before = copy.deepcopy(state['bundle_selection'])
        claim = copy.deepcopy(state['claims']['claim-1'])
        claim['id'] = 'claim-new'
        state['claims']['claim-new'] = claim
        concept = copy.deepcopy(state['concepts']['concept-1'])
        concept.update(id='concept-new', key='new-topic', claim_ids=['claim-new'], review={'decision':'pending'})
        concept['revision'] = concept_revision(concept, state['claims'])
        state['concepts']['concept-new'] = concept
        self.assertEqual(eligibility(state, utc_now())['included_concept_ids'], ['concept-1'])
        next_preview = review_preview(state, ['concept-new'])
        approve_selection(state, next_preview)
        self.assertNotEqual(state['bundle_selection'], before)
        self.assertEqual(eligibility(state, utc_now())['included_concept_ids'], ['concept-new'])
        with self.assertRaises(ValueError):
            self.bind(state, preview)

    def test_selection_is_bound_into_candidate_fingerprint(self):
        state, preview = selection_fixture()
        before = state_fingerprint(state)
        approve_selection(state, preview)
        selected = state_fingerprint(state)
        self.assertNotEqual(selected, before)
        state['bundle_selection']['preview_hash'] = '0'*64
        self.assertNotEqual(state_fingerprint(state), selected)

    def test_selected_availability_expiry_still_blocks(self):
        state, preview = selection_fixture()
        approve_selection(state, preview)
        source = state['sources']['source-1']
        source['availability'] = 'live'
        source['availability_acceptance'] = None
        source['availability_check'] = {'url': source['citation_url'], 'version_id':'version-1',
            'status':200, 'at':'2026-10-01T12:00:00Z', 'observed_final_url':source['citation_url'],
            'quotes_supported':True, 'response_hash':digest(b'fixture-response'),
            'supersedes_captured_failure':True, 'requires_source_refresh':False}
        self.assertFalse(eligibility(state, utc_now())['ready'])

    def test_cli_legacy_bundle_select_reuses_existing_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            state, preview = selection_fixture()
            approve_selection(state, preview)
            state.pop('bundle_selection')
            run = Path(temp)/'run'
            store = StateStore(run)
            with store.transaction():
                store.save(state)
            path = Path(temp)/'preview.json'
            path.write_text(json.dumps(preview))
            result = execute(parser().parse_args(['bundle-select','--run',str(run),'--preview',str(path),'--preview-hash',digest(preview)]))
            after = StateStore(run).load()
            self.assertEqual(result['new_human_decisions'], 0)
            self.assertEqual(after['review_history'], state['review_history'])
            self.assertTrue(eligibility(after, utc_now())['ready'])

    def test_partial_flag_cannot_drop_an_approved_but_failed_topic(self):
        state, preview = selection_fixture()
        other = copy.deepcopy(state['concepts']['concept-1'])
        other.update(id='concept-other', key='other-topic')
        other['revision'] = concept_revision(other, state['claims'])
        state['concepts']['concept-other'] = other
        preview = review_preview(state, ['concept-1','concept-other'])
        approve_selection(state, preview)
        state['concepts']['concept-other']['description'] = ''
        state['config']['partial_release_acceptance'] = {'owner':'human:client-owner',
            'at':utc_now(), 'reason':'synthetic partial acceptance', 'concept_ids':['concept-1']}
        state['config_hash'] = digest(state['config'])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ReleaseError):
                build_candidate(state, Path(temp)/'candidate', 'fixture-failed-selected', utc_now(), allow_partial=True)


if __name__ == '__main__':
    unittest.main()
