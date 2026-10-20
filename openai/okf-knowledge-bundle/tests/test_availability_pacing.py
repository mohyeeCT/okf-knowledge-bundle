"""Fake-clock production-path checks; no client, provider or actual waits."""
import copy
import unittest
from unittest.mock import patch

from okf_bundle.availability import check_availability
from okf_bundle.common import digest
from okf_bundle.extraction import extract_html
from okf_bundle.network import direct_fetch
from test_availability_headers import HTML
from test_export import fixture_state


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class AvailabilityPacingTests(unittest.TestCase):
    def setUp(self):
        self.state = fixture_state()
        self.state['config']['direct_interval_seconds'] = 3
        self.clock = Clock()
        self.sent = []
        self.robots = b'User-agent: *\nDisallow:\n'
        self.robots_status = 200
        self.redirect = None
        source = self.state['sources']['source-1']
        source['versions']['version-1'].update(representation='direct-html', evidence=extract_html(HTML, source['citation_url'])['evidence'])

    def transport(self, url, config, **options):
        options['before_request'](url)
        self.sent.append((url, self.clock()))
        if url.endswith('/robots.txt'):
            return {'status':self.robots_status, 'body':self.robots, 'url':url}
        if self.redirect:
            url = self.redirect
            options['before_request'](url)
            self.sent.append((url, self.clock()))
        return {'status':200, 'body':HTML, 'url':url, 'headers':{}}

    def check(self, ids=None):
        with patch('okf_bundle.network.direct_fetch', side_effect=self.transport):
            return check_availability(self.state, ids or list(self.state['sources']), clock=self.clock, sleep=self.clock.sleep)

    def add_source(self, sid, host='example.com'):
        source = copy.deepcopy(self.state['sources']['source-1'])
        source.update(id=sid, citation_url=f'https://{host}/{sid}/')
        self.state['sources'][sid] = source
        if host not in self.state['config']['domains']:
            self.state['config']['domains'].append(host)

    def test_17_pages_use_configured_interval_including_robots_to_page(self):
        for index in range(2,18):
            self.add_source(f'source-{index}')
        result = self.check()
        self.assertEqual(len(self.sent), 18)
        self.assertEqual([b[1]-a[1] for a,b in zip(self.sent,self.sent[1:])], [3]*17)
        self.assertEqual(len(result['observations']), 17)
        self.assertEqual(result['paid_provider_calls'], 0)
        self.assertEqual(len(result['pacing']['requests']), 18)

    def test_stricter_crawl_delay_retained_and_applied(self):
        self.robots = b'User-agent: *\nCrawl-delay: 10\nDisallow:\n'
        self.add_source('source-2')
        result = self.check()
        self.assertEqual([at for _,at in self.sent], [1000,1010,1020])
        record = result['pacing']['robots']['https://example.com']
        self.assertEqual(record['response_hash'], digest(self.robots))
        self.assertEqual(record['body'].encode(), self.robots)
        self.assertEqual(record['effective_interval_seconds'], 10)

    def test_fractional_delay_is_not_silently_ignored(self):
        self.robots = b'User-agent: *\nCrawl-delay: 3.5\nDisallow:\n'
        self.check()
        self.assertEqual(self.clock.waits, [3.5])

    def test_disallowed_redirect_destination_is_never_requested(self):
        self.robots = b'User-agent: *\nDisallow: /other/\n'
        self.redirect = 'https://example.com/other/'
        result = self.check()
        self.assertEqual([url for url,_ in self.sent], ['https://example.com/robots.txt','https://example.com/training/'])
        self.assertEqual(result['observations'][0]['outcome'], 'check-failed')

    def test_failed_new_check_replaces_previous_success_without_erasing_content_reviews(self):
        self.check()
        self.assertEqual(self.state['sources']['source-1']['availability_check']['status'], 200)
        reviews = copy.deepcopy(self.state['claims']['claim-1']['reviews'])
        self.robots_status = 503
        self.check()
        self.assertIsNone(self.state['sources']['source-1']['availability_check']['status'])
        self.assertEqual(self.state['claims']['claim-1']['reviews'], reviews)

    def test_request_rate_and_bot_specific_policy(self):
        self.robots = b'User-agent: OKF-Knowledge-Bundle\nRequest-rate: 2/20\nDisallow:\n\nUser-agent: *\nCrawl-delay: 1\n'
        self.check()
        self.assertEqual(self.clock.waits, [10])

    def test_disallow_and_unavailable_robots_never_fetch_page_or_retry(self):
        for status, body in [(200,b'User-agent: *\nDisallow: /\n'),(403,b'denied'),(503,b'error'),(200,b'<html>challenge</html>')]:
            with self.subTest(status=status,body=body):
                self.setUp()
                self.add_source('source-2')
                self.robots_status, self.robots = status, body
                result = self.check()
                self.assertEqual(len(self.sent), 1)
                self.assertTrue(all(x['outcome']=='check-failed' for x in result['observations']))
                self.assertEqual(self.state['sources']['source-1']['availability_check']['status'], None)

    def test_missing_robots_is_recorded_and_allows_paced_check(self):
        self.robots_status = 404
        result = self.check()
        self.assertEqual(result['observations'][0]['outcome'], 'checked-live')
        self.assertEqual(self.clock.waits, [3])

    def test_redirect_each_hop_checks_policy_and_waits(self):
        self.redirect = 'https://example.com/other/'
        result = self.check()
        self.assertEqual([at for _,at in self.sent], [1000,1003,1006])
        self.assertEqual(result['observations'][0]['outcome'], 'redirect-needs-identity-review')

    def test_different_origins_and_repeated_batch_have_separate_persistent_clocks(self):
        self.add_source('source-2','other.example.com')
        self.check()
        self.assertEqual([at for _,at in self.sent], [1000,1003,1003,1006])
        self.check(['source-2'])
        self.assertEqual([at for _,at in self.sent[-2:]], [1009,1012])

    def test_delay_over_60_seconds_stops_instead_of_long_sleep(self):
        self.robots = b'User-agent: *\nCrawl-delay: 120\nDisallow:\n'
        result = self.check()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.clock.waits, [])
        self.assertIn('later check', result['observations'][0]['reason'])

    def test_invalid_interval_stops_before_transport(self):
        for value in (-1,31,float('inf'),True):
            self.state['config']['direct_interval_seconds'] = value
            with self.assertRaises(ValueError):
                self.check()
        self.assertEqual(self.sent, [])

    def test_injected_fixture_transport_never_claims_production_pacing(self):
        result = check_availability(self.state,['source-1'],fetch=lambda url:{'status':200,'body':HTML,'url':url})
        self.assertIn('not evaluated', result['pacing']['mode'])

    def test_guarded_transport_hook_runs_before_each_actual_redirect_request(self):
        calls = []
        class Response:
            def __init__(self, status): self.status = status
            def getheaders(self): return [('Location','/other/')] if self.status==302 else []
            def getheader(self, key): return None
            def read(self, size): return b'OK'
        class Connection:
            def __init__(self, *args): pass
            def request(self, method, path, headers): calls.append(('request',path))
            def getresponse(self): return Response(302 if len([x for x in calls if x[0]=='request'])==1 else 200)
            def close(self): pass
        with patch('okf_bundle.network.public_addresses',return_value=['93.184.216.34']), patch('okf_bundle.network._PinnedHTTPS',Connection):
            direct_fetch('https://example.com/start/', {'domains':['example.com'],'collection_authorized':True},before_request=lambda url:calls.append(('guard',url)))
        self.assertEqual([kind for kind,_ in calls], ['guard','request','guard','request'])
