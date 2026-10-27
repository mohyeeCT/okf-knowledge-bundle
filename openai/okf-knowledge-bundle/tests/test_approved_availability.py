"""Fictional approved/excluded workflow and safe failures; no live sites."""
import copy
import json
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from okf_bundle.availability import check_approved_availability, source_check_plan
from okf_bundle.cli import execute, parser
from okf_bundle.common import digest, utc_now
from okf_bundle.export import build_candidate, eligibility, verify_candidate
from okf_bundle.extraction import extract_html
from okf_bundle.politeness import AvailabilityPolicyError, RobotsDisallowedError
from okf_bundle.review import accept_availability, claim_revision, concept_revision
from okf_bundle.state import StateStore
from test_availability_headers import HTML
from test_bundle_selection import approve_selection, selection_fixture


def approved_state():
    state, _ = selection_fixture()
    source = state['sources']['source-1']
    source['versions']['version-1'].update(representation='sf-original', evidence=extract_html(HTML, source['citation_url'])['evidence'])
    # The unresolved fact shares the source but has no usable current quote.
    excluded = state['claims']['claim-excluded']
    excluded['evidence'][0]['quote'] = 'An unsupported excluded fee.'
    excluded['revision'] = claim_revision(excluded)
    from okf_bundle.batch_review import review_preview
    approve_selection(state, review_preview(state, ['concept-1']))
    return state


def live(url):
    return {'status': 200, 'url': url, 'body': HTML}


class ApprovedAvailabilityTests(unittest.TestCase):
    def test_plan_is_read_only_and_excludes_unrelated_failed_source(self):
        state = approved_state()
        state['sources']['excluded-only'] = {'availability': 'failed'}
        before = copy.deepcopy(state)
        result = source_check_plan(state)
        self.assertEqual(result['source_ids'], ['source-1'])
        self.assertEqual(result['network_requests'], 0)
        self.assertEqual(state, before)

    def test_no_approval_fails_before_network(self):
        state = approved_state()
        del state['bundle_selection']
        fetch = Mock()
        with self.assertRaises(ValueError):
            check_approved_availability(state, fetch=fetch)
        fetch.assert_not_called()

    def test_tampered_approval_fails_before_network(self):
        state = approved_state()
        state['bundle_selection']['preview_hash'] = 'tampered'
        fetch = Mock()
        with self.assertRaises(ValueError):
            check_approved_availability(state, fetch=fetch)
        fetch.assert_not_called()

    def test_explicit_batch_cannot_add_excluded_source(self):
        state = approved_state()
        fetch = Mock()
        with self.assertRaises(ValueError):
            check_approved_availability(state, ['excluded-only'], fetch)
        fetch.assert_not_called()

    def test_shared_source_excluded_quote_does_not_delay_candidate(self):
        state = approved_state()
        excluded = copy.deepcopy(state['claims']['claim-excluded'])
        fetch = Mock(spec=live, side_effect=live)
        result = check_approved_availability(state, fetch=fetch)
        self.assertEqual(result['observations'][0]['outcome'], 'checked-live')
        fetch.assert_called_once_with(state['sources']['source-1']['citation_url'])
        self.assertEqual(state['claims']['claim-excluded'], excluded)
        self.assertTrue(eligibility(state, utc_now())['ready'])
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'candidate'
            manifest = build_candidate(state, target, 'fictional-test', utc_now())
            self.assertEqual(verify_candidate(target), manifest)
            self.assertEqual(manifest['withheld_concepts'], ['concept-excluded'])
            self.assertFalse(state['release_approvals'])

    def test_checked_quotes_cannot_cover_newly_selected_evidence(self):
        state = approved_state()
        check_approved_availability(state, fetch=live)
        claim = state['claims']['claim-1']
        claim['evidence'][0]['quote'] = 'provides on-site training'
        claim['revision'] = claim_revision(claim)
        state['concepts']['concept-1']['revision'] = concept_revision(state['concepts']['concept-1'], state['claims'])
        from okf_bundle.batch_review import review_preview
        approve_selection(state, review_preview(state, ['concept-1']))
        report = eligibility(state, utc_now())
        self.assertFalse(report['ready'])
        self.assertIn('outside the last approved-source check', json.dumps(report))
        check_approved_availability(state, fetch=live)
        self.assertTrue(eligibility(state, utc_now())['ready'])

    def test_comparable_page_change_still_invalidates_reviews(self):
        state = approved_state()
        body = HTML.replace(b'</body>', b'<p>New conditions apply to training bookings.</p></body>')
        check_approved_availability(state, fetch=lambda url: {**live(url), 'body': body})
        self.assertFalse(state['claims']['claim-1']['reviews'])
        self.assertFalse(eligibility(state, utc_now())['ready'])

    def test_selected_http_failure_cannot_be_waived(self):
        for status in (403, 404, 503):
            with self.subTest(status=status):
                state = approved_state()
                result = check_approved_availability(state, fetch=lambda url: {**live(url), 'status': status})
                self.assertIn(str(status), result['observations'][0]['diagnostic']['summary'])
                self.assertFalse(eligibility(state, utc_now())['ready'])
                with self.assertRaises(ValueError):
                    accept_availability(state, ['source-1'], state['config']['publication_owner'], utc_now(), 'Cannot waive', 'fixture', 'test')

    def test_transport_failures_are_specific_and_do_not_echo_secrets(self):
        for error, code in ((socket.gaierror('SECRET'), 'dns'), (TimeoutError('SECRET'), 'timeout'),
                            (ssl.SSLError('SECRET'), 'tls'), (PermissionError('SECRET'), 'permission'),
                            (ConnectionError('SECRET'), 'connection'), (OSError('SECRET'), 'unclassified'),
                            (URLError(socket.gaierror('SECRET')), 'dns')):
            with self.subTest(code=code):
                state = approved_state()
                fetch = Mock(spec=live, side_effect=error)
                result = check_approved_availability(state, fetch=fetch)
                observation = result['observations'][0]
                self.assertEqual(observation['diagnostic']['code'], code)
                self.assertNotIn('SECRET', json.dumps(result))
                self.assertIn('only this included URL', observation['diagnostic']['next_step'])
                self.assertEqual(state['sources']['source-1']['availability'], 'unknown')
                self.assertFalse(eligibility(state, utc_now())['ready'])
                fetch.assert_called_once()

    def test_known_failure_is_not_downgraded_by_timeout(self):
        state = approved_state()
        state['sources']['source-1']['availability'] = 'failed'
        result = check_approved_availability(state, fetch=Mock(spec=live, side_effect=TimeoutError()))
        self.assertIn('cannot waive', result['observations'][0]['diagnostic']['next_step'])
        self.assertEqual(state['sources']['source-1']['availability'], 'failed')

    def test_robots_denial_and_pacing_have_distinct_actions(self):
        for error, code, phrase in ((RobotsDisallowedError('Robots policy disallows this URL; no request sent'), 'robots-disallowed', 'Do not bypass'),
                                    (AvailabilityPolicyError('Robots pacing requires a later check; no request sent'), 'pacing-deferred', 'do not shorten')):
            state = approved_state()
            fetch = Mock(spec=live, side_effect=error)
            result = check_approved_availability(state, fetch=fetch)
            detail = result['observations'][0]['diagnostic']
            self.assertEqual(detail['code'], code)
            self.assertIn(phrase, detail['next_step'])
            fetch.assert_called_once()

    def test_robots_fetch_failure_preserves_stage_and_safe_cause(self):
        state = approved_state()
        error = AvailabilityPolicyError('Robots policy could not be checked; no page request sent')
        error.__cause__ = TimeoutError('SECRET')
        result = check_approved_availability(state, fetch=Mock(spec=live, side_effect=error))
        diagnostic = result['observations'][0]['diagnostic']
        self.assertEqual(diagnostic['code'], 'robots-timeout')
        self.assertIn('no page request sent', diagnostic['summary'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_cli_plan_and_approved_check_authorization(self):
        with tempfile.TemporaryDirectory() as folder:
            store = StateStore(folder)
            with store.transaction():
                store.save(approved_state())
            plan = execute(parser().parse_args(['source-check-plan', '--run', folder]))
            self.assertEqual(plan['source_ids'], ['source-1'])
            with patch('okf_bundle.availability.check_approved_availability') as check:
                with self.assertRaises(ValueError):
                    execute(parser().parse_args(['availability-check', '--run', folder, '--approved']))
                check.assert_not_called()
                check.return_value = {'fixture': True}
                self.assertEqual(execute(parser().parse_args(['availability-check', '--run', folder, '--approved', '--authorized'])), {'fixture': True})
                check.assert_called_once()

    def test_cli_no_scope_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            store = StateStore(folder)
            with store.transaction():
                store.save(approved_state())
            with self.assertRaises(ValueError):
                execute(parser().parse_args(['availability-check', '--run', folder, '--authorized']))


if __name__ == '__main__':
    unittest.main()
