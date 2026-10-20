"""Initial owner designation preserves actual review decisions, never release approval."""
import copy
import tempfile
import unittest
from pathlib import Path

from okf_bundle.batch_review import approve_preview, review_preview
from okf_bundle.cli import execute, parser
from okf_bundle.common import digest, utc_now
from okf_bundle.export import ReleaseError, eligibility
from okf_bundle.owner_setup import set_publication_owner
from okf_bundle.state import StateStore
from test_export import fixture_state


class OwnerSetupTests(unittest.TestCase):
    def state(self):
        state = fixture_state()
        state['config']['publication_owner'] = None
        state['config_hash'] = digest(state['config'])
        source = state['sources']['source-1']
        source['availability'] = 'live'
        source['availability_acceptance'] = None
        source['availability_check'] = {'url': source['citation_url'], 'version_id': 'version-1',
            'status': 200, 'at': utc_now(), 'observed_final_url': source['citation_url'],
            'quotes_supported': True, 'response_hash': digest(b'fixture-response'),
            'supersedes_captured_failure': True, 'requires_source_refresh': False}
        return state

    def set_owner(self, state, owner='human:client-owner', **changes):
        args = dict(by='human:analyst', at=utc_now(), channel='synthetic-test', reference='actual-fixture-designation')
        args.update(changes)
        return set_publication_owner(state, owner, **args)

    def test_initial_designation_preserves_run_and_all_content_reviews(self):
        state = self.state()
        before = copy.deepcopy(state)
        self.assertFalse(eligibility(state, utc_now())['ready'])
        result = self.set_owner(state)
        self.assertTrue(result['owner_setup_recorded'])
        self.assertEqual(state['config_hash'], digest(state['config']))
        for key in ['run_id','sources','claims','concepts','review_history','release_approvals','candidates']:
            self.assertEqual(state[key], before[key], key)
        self.assertEqual(state['owner_designations'][0]['by'], 'human:analyst')
        self.assertTrue(eligibility(state, utc_now())['ready'])

    def test_cli_setup_in_existing_approved_run_builds_candidate_but_cannot_package(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / 'run'
            store = StateStore(run)
            original = self.state()
            with store.transaction():
                store.save(original)
            def call(*args):
                return execute(parser().parse_args([*args, '--run', str(run)]))
            result = call('owner-set','--owner','human:client-owner','--by','human:analyst',
                          '--at',utc_now(),'--channel','synthetic-test','--reference','fixture-designation')
            self.assertTrue(result['owner_setup_recorded'])
            self.assertEqual(StateStore(run).load()['run_id'], original['run_id'])
            self.assertTrue(call('readiness')['ready'])
            candidate = call('candidate','--release-id','owner-setup-fixture')
            self.assertEqual(candidate['status'], 'awaiting-release-owner-approval')
            with self.assertRaises(ReleaseError):
                call('package','--candidate',candidate['candidate'],'--out',str(Path(temp)/'not-authorized.zip'))
            self.assertFalse(StateStore(run).load()['release_approvals'])

    def test_same_owner_is_idempotent_even_after_candidate(self):
        state = self.state()
        self.set_owner(state)
        state['candidates']['fixture'] = {'path':'immutable-fixture'}
        before = copy.deepcopy(state)
        self.assertFalse(self.set_owner(state)['owner_setup_recorded'])
        self.assertEqual(state, before)

    def test_replacement_owner_is_refused_atomically(self):
        state = self.state()
        self.set_owner(state)
        before = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            self.set_owner(state, 'human:someone-else')
        self.assertEqual(state, before)

    def test_bad_role_nonhuman_missing_provenance_and_bad_time_are_atomic(self):
        for owner, changes in [('model:owner',{}), ('human:owner@example.com',{}),
                               ('human:client-owner',{'by':'model:assistant'}),
                               ('human:client-owner',{'reference':''}),
                               ('human:client-owner',{'channel':'chat\ninjected'}),
                               ('human:client-owner',{'at':'2999-01-01T00:00:00Z'})]:
            with self.subTest(owner=owner, changes=changes):
                state = self.state()
                before = copy.deepcopy(state)
                with self.assertRaises(ValueError):
                    self.set_owner(state, owner, **changes)
                self.assertEqual(state, before)

    def test_missing_owner_with_existing_candidate_or_release_is_refused(self):
        for field in ['candidates','release_approvals']:
            state = self.state()
            state[field]['fixture'] = {'manifest_hash':'old'}
            before = copy.deepcopy(state)
            with self.assertRaises(ValueError):
                self.set_owner(state)
            self.assertEqual(state, before)

    def test_unrecorded_old_preview_stays_stale_after_configuration_change(self):
        state = self.state()
        preview = review_preview(state, list(state['concepts']))
        self.set_owner(state)
        before = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            approve_preview(state, preview, digest(preview), 'human:analyst', utc_now(), 'synthetic-test', 'fixture')
        self.assertEqual(state, before)

    def test_owner_designation_does_not_accept_unknown_availability_or_pending_content(self):
        state = self.state()
        state['sources']['source-1']['availability'] = 'unknown'
        state['sources']['source-1'].pop('availability_check')
        state['claims']['claim-1']['reviews'] = []
        state['claims']['claim-1']['evidence_review'] = 'pending'
        self.set_owner(state)
        self.assertFalse(eligibility(state, utc_now())['ready'])
        self.assertIsNone(state['sources']['source-1']['availability_acceptance'])
        self.assertFalse(state['claims']['claim-1']['reviews'])
        self.assertFalse(state['release_approvals'])

    def test_bad_cli_designation_keeps_the_committed_snapshot_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / 'run')
            with store.transaction():
                store.save(self.state())
            pointer = (store.private / 'current.json').read_bytes()
            with self.assertRaises(ValueError):
                execute(parser().parse_args(['owner-set','--run',str(store.root),
                    '--owner','human:client-owner','--by','model:assistant','--at',utc_now(),
                    '--channel','synthetic-test','--reference','not-human']))
            self.assertEqual((store.private / 'current.json').read_bytes(), pointer)

    def test_corrupt_config_or_designation_history_fails_before_mutation(self):
        for field in ['config_hash','owner_designations']:
            state = self.state()
            state[field] = 'invalid'
            before = copy.deepcopy(state)
            with self.assertRaises(ValueError):
                self.set_owner(state)
            self.assertEqual(state, before)
