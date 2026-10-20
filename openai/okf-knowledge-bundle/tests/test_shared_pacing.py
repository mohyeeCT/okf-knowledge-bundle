"""Native sitemap/collection paths with a fake clock; no live sites or providers."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okf_bundle.ingest import discover_sitemap
from okf_bundle.managed import collect_direct
from okf_bundle.availability import check_availability
from okf_bundle.state import new_state, StateStore
from okf_bundle.workflow import merge_import
from okf_bundle.cli import execute, parser
from test_availability_pacing import Clock
from test_managed import HTML

INDEX = b'<sitemapindex><sitemap><loc>https://example.com/pages.xml</loc></sitemap></sitemapindex>'
PAGES = b'<urlset><url><loc>https://example.com/a/</loc></url><url><loc>https://example.com/b/</loc></url></urlset>'


class SharedPacingTests(unittest.TestCase):
    def setUp(self):
        self.config = {'domains':['example.com'], 'collection_authorized':True, 'direct_interval_seconds':3}
        self.state = new_state(self.config)
        self.config = self.state["config"]
        self.clock = Clock()
        self.sent = []
        self.robots = b'User-agent: *\nCrawl-delay: 10\nDisallow:\n'
        self.robots_status = 200
        self.redirects = {}

    def transport(self, url, config, **options):
        while True:
            options['before_request'](url)
            self.sent.append({'url':url, 'at':self.clock(), 'headers':options.get('headers')})
            if url not in self.redirects:
                break
            url = self.redirects[url]
        if url.endswith('/robots.txt'):
            return {'status':self.robots_status,'body':self.robots,'url':url}
        body = INDEX if url.endswith('/index.xml') else PAGES if url.endswith('.xml') else HTML.encode()
        return {'status':200,'body':body,'url':url,'headers':{}}

    def discover(self, location='https://example.com/index.xml', **kwargs):
        with patch('okf_bundle.network.direct_fetch',side_effect=self.transport):
            result = discover_sitemap(location,self.config,state=self.state,clock=self.clock,sleep=self.clock.sleep,**kwargs)
        merge_import(self.state,result)
        return result

    def collect(self, **kwargs):
        with patch('okf_bundle.managed.direct_fetch',side_effect=self.transport):
            return collect_direct(self.state,clock=self.clock,sleep=self.clock.sleep,**kwargs)

    def times(self):
        return [r['at'] for r in self.sent]

    def test_index_child_and_robots_to_index_are_paced(self):
        result = self.discover()
        self.assertEqual(self.times(),[1000,1010,1020])
        self.assertEqual(len(result['sources']),2)
        self.assertEqual(result['sitemap_summary']['pacing']['requests'][2]['interval_seconds'],10)
        self.assertEqual(self.sent[1]['headers']['Accept'],'application/xml,text/xml,application/gzip')
        self.assertEqual(self.sent[1]['headers']['User-Agent'],'OKF-Knowledge-Bundle/0.2.1')

    def test_discovery_collection_canary_batch_and_availability_share_saved_clock(self):
        self.discover()
        self.assertEqual(self.collect(max_pages=1)['captured'],1)
        self.assertEqual(self.collect(max_pages=1)['captured'],1)
        with patch('okf_bundle.network.direct_fetch',side_effect=self.transport):
            result = check_availability(self.state,list(self.state['sources']),clock=self.clock,sleep=self.clock.sleep)
        self.assertEqual(self.times(),list(range(1000,1100,10)))
        self.assertEqual(len(result['observations']),2)
        self.assertEqual(len(self.state['direct_collection_history']),2)
        self.assertEqual(len(self.state['sitemap_collection_history']),1)

    def test_17_pages_include_first_page_and_have_no_provider_usage(self):
        data = '<urlset>'+''.join(f'<url><loc>https://example.com/p{i}/</loc></url>' for i in range(17))+'</urlset>'
        merge_import(self.state,discover_sitemap('https://example.com/pages.xml',self.config,fetch=lambda _:data.encode()))
        result = self.collect(max_pages=17)
        self.assertEqual((result['captured'],result['provider_requests'],result['provider_credits']),(17,0,0))
        self.assertEqual(self.times(),list(range(1000,1180,10)))
        self.assertEqual(len(result['pacing']['requests']),18)

    def test_sitemap_redirect_hops_are_paced(self):
        self.redirects['https://example.com/start.xml']='https://example.com/index.xml'
        self.discover('https://example.com/start.xml')
        self.assertEqual(self.times(),[1000,1010,1020,1030])

    def test_collection_redirect_checks_policy_before_each_hop(self):
        self.discover()
        self.redirects['https://example.com/a/']='https://example.com/blocked/'
        self.robots=b'User-agent: *\nCrawl-delay: 10\nDisallow: /blocked/\n'
        result=self.collect(max_pages=1)
        self.assertFalse(any(x['url'].endswith('/blocked/') for x in self.sent))
        source=next(iter(self.state['sources'].values()))
        self.assertTrue(source['robots_disallowed'])
        self.assertFalse(source['render_fallback_eligible'])
        self.assertEqual(result['fallback_needed'],0)

    def test_unknown_robots_no_retry_no_pages_no_provider_fallback(self):
        self.discover()
        self.sent=[]
        self.robots_status=503
        result=self.collect()
        self.assertEqual(len(self.sent),1)
        self.assertEqual(result['captured'],0)
        self.assertEqual(result['fallback_needed'],0)
        self.assertTrue(all(s['scope']=='pending' and not s['render_fallback_eligible'] for s in self.state['sources'].values()))

    def test_sitemap_denied_by_robots_is_partial_without_target_request(self):
        self.robots=b'User-agent: *\nDisallow: /index.xml\n'
        result=self.discover()
        self.assertEqual(len(self.sent),1)
        self.assertTrue(result['sitemap_summary']['partial'])
        self.assertEqual(result['sources'],{})
        self.assertIn('disallows',result['inventory'][0]['reason'])

    def test_fractional_delay_and_request_rate_apply_to_sitemap(self):
        for rule,expected in [('Crawl-delay: 3.5',3.5),('Request-rate: 2/20',10)]:
            with self.subTest(rule=rule):
                self.setUp()
                self.robots=f'User-agent: *\n{rule}\nDisallow:\n'.encode()
                self.discover()
                self.assertEqual(self.times(),[1000,1000+expected,1000+2*expected])

    def test_missing_robots_uses_configured_spacing(self):
        self.robots_status=404
        self.discover()
        self.assertEqual(self.times(),[1000,1003,1006])

    def test_legacy_availability_timestamp_paces_first_sitemap_robots(self):
        self.state['availability_request_times']={'https://example.com':999}
        self.state['availability_robots_checks']={'https://example.com':{'effective_interval_seconds':10}}
        self.discover()
        self.assertEqual(self.times(),[1009,1019,1029])

    def test_origins_have_separate_clocks(self):
        self.config['domains'].append('other.example.com')
        self.redirects['https://example.com/start.xml']='https://other.example.com/pages.xml'
        self.discover('https://example.com/start.xml')
        self.assertEqual(self.times(),[1000,1010,1010,1020])
        self.assertEqual(set(self.state['direct_request_times']),{'https://example.com','https://other.example.com'})

    def test_local_xml_and_injected_fixture_remain_network_free(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'pages.xml'
            path.write_bytes(PAGES)
            with patch('okf_bundle.network.direct_fetch',side_effect=AssertionError('unexpected network')):
                local=discover_sitemap(path,self.config,state=self.state)
                fixture=discover_sitemap('https://example.com/pages.xml',self.config,lambda _:PAGES)
            self.assertEqual(len(local['sources']),2)
            self.assertEqual(len(fixture['sources']),2)
            self.assertNotIn('direct_request_times',self.state)

    def test_uploaded_index_remote_child_uses_native_pacing(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'index.xml'
            path.write_bytes(INDEX)
            result=self.discover(path)
        self.assertEqual(self.times(),[1000,1010])
        self.assertEqual(len(result['sources']),2)

    def test_long_delay_holds_without_early_request(self):
        self.robots=b'User-agent: *\nCrawl-delay: 120\nDisallow:\n'
        result=self.discover()
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.clock.waits,[])
        self.assertTrue(result['sitemap_summary']['partial'])

    def test_bad_interval_stops_before_network(self):
        self.config['direct_interval_seconds']=float('nan')
        result=self.discover()
        self.assertEqual(self.sent,[])
        self.assertTrue(result['sitemap_summary']['partial'])

    def test_robots_transport_error_is_not_rendering_fallback(self):
        self.discover()
        def broken(url, config, **options):
            options['before_request'](url)
            raise OSError('fixture offline')
        with patch('okf_bundle.managed.direct_fetch',side_effect=broken):
            result=collect_direct(self.state,clock=self.clock,sleep=self.clock.sleep)
        self.assertEqual(result['fallback_needed'],0)
        self.assertTrue(all(not s['render_fallback_eligible'] for s in self.state['sources'].values()))

    def test_cli_saves_sitemap_clock_for_later_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); cfg=root/'config.json'; cfg.write_text(json.dumps(self.config))
            def command(*args):return execute(parser().parse_args([args[0],'--run',str(root/'run'),*args[1:]]))
            command('init','--config',str(cfg))
            with patch('okf_bundle.politeness.time.time',side_effect=self.clock),patch('okf_bundle.politeness.time.sleep',side_effect=self.clock.sleep),patch('okf_bundle.network.direct_fetch',side_effect=self.transport),patch('okf_bundle.managed.direct_fetch',side_effect=self.transport):
                result=command('discover-sitemap','--sitemap','https://example.com/index.xml')
                self.assertEqual(len(result['sitemap_summary']['pacing']['requests']),3)
                command('collect-direct','--max-pages','1')
            saved=StateStore(root/'run').load()
            self.assertEqual(saved['direct_request_times']['https://example.com'],1040)
            self.assertEqual(self.times(),[1000,1010,1020,1030,1040])


if __name__=='__main__':
    unittest.main()
