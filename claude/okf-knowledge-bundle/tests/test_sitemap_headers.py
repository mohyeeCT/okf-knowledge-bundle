"""Exercise native sitemap transport against a local server requiring a user agent."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

from okf_bundle.ingest import discover_sitemap
from okf_bundle.network import direct_fetch


XML_ACCEPT = 'application/xml,text/xml,application/gzip'
USER_AGENT = 'OKF-Knowledge-Bundle/0.2.1'


class SitemapHeadersTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append({'path': self.path, 'headers': dict(self.headers)})
                if self.path == '/robots.txt':
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'User-agent: *\nDisallow:\n')
                    return
                accepted = self.headers.get('Accept') == XML_ACCEPT and self.headers.get('User-Agent') == USER_AGENT
                if not accepted or self.path == '/denied.xml':
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'Forbidden')
                    return
                if self.path == '/sitemap.xml':
                    self.send_response(302)
                    self.send_header('Location', '/sitemap_index.xml')
                    self.end_headers()
                    return
                if self.path == '/sitemap_index.xml':
                    body = (f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                            f'<sitemap><loc>http://example.com:{self.server.server_port}/pages.xml.gz</loc>'
                            '</sitemap></sitemapindex>').encode()
                    content_type = 'application/xml'
                else:
                    body = gzip.compress(b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                         b'<url><loc>https://example.com/service/</loc></url></urlset>')
                    content_type = 'application/gzip'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {'domains': ['example.com'], 'collection_authorized': True}
        self.base = f'http://example.com:{self.server.server_port}'
        # Only this fixture pins to loopback. Native domain, redirect, byte and
        # XML handling still run; production public-address validation is unchanged.
        self.resolver = patch('okf_bundle.network.public_addresses', return_value=['127.0.0.1'])
        self.resolver.start()

    def tearDown(self):
        self.resolver.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_xml_negotiation_with_user_agent_recovers_the_missing_agent_403(self):
        old = direct_fetch(self.base + '/sitemap_index.xml', self.config,
                           headers={'Accept': XML_ACCEPT, 'User-Agent': ''})
        self.assertEqual(old['status'], 403)
        current = direct_fetch(self.base + '/sitemap_index.xml', self.config)
        self.assertEqual(current['status'], 200)
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(self.seen[-1]['headers']['Accept'], XML_ACCEPT)
        self.assertEqual(self.seen[-1]['headers']['Accept-Encoding'], 'identity')

    def test_native_discovery_keeps_profile_on_redirect_and_gzipped_child(self):
        result = discover_sitemap(self.base + '/sitemap.xml', self.config)
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual(next(iter(result['sources'].values()))['normalized_url'], 'https://example.com/service/')
        self.assertEqual(result['warnings'], [])
        self.assertEqual([r['path'] for r in self.seen], ['/robots.txt', '/sitemap.xml', '/sitemap_index.xml', '/pages.xml.gz'])
        self.assertEqual(self.seen[0]['headers']['Accept'], 'text/plain')
        for request in self.seen[1:]:
            self.assertEqual(request['headers']['User-Agent'], USER_AGENT)
            self.assertEqual(request['headers']['Accept'], XML_ACCEPT)
            self.assertEqual(request['headers']['Accept-Encoding'], 'identity')

    def test_real_denial_remains_failed_without_retry_or_sources(self):
        result = discover_sitemap(self.base + '/denied.xml', self.config)
        self.assertEqual(result['sources'], {})
        self.assertEqual(len(result['inventory']), 1)
        self.assertEqual(result['inventory'][0]['state'], 'failed')
        self.assertEqual([r['path'] for r in self.seen], ['/robots.txt', '/denied.xml'])


if __name__ == '__main__':
    unittest.main()
