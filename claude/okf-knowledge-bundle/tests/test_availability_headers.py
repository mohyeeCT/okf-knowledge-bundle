"""A local HTML server rejects the former XML-only request; no external crawl."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from okf_bundle.availability import check_availability
from okf_bundle.extraction import extract_html
from test_export import fixture_state


HTML = b'<html lang="en"><head><title>Team training</title></head><body><h1>Team training</h1><p>Our team provides on-site training for businesses.</p></body></html>'


class AvailabilityHeadersTests(unittest.TestCase):
    def test_default_availability_reaches_html_and_preserves_approved_evidence(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                if self.path == '/robots.txt':
                    self.send_response(404)
                    self.end_headers()
                    return
                ok = self.headers.get('Accept') == 'text/html' and self.headers.get('User-Agent') == 'OKF-Knowledge-Bundle/0.2.1'
                self.send_response(200 if ok else 403)
                self.send_header('Content-Type','text/html')
                self.end_headers()
                self.wfile.write(HTML if ok else b'Blocked: expected HTML request')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # Only the test's transport is loopback. Production uses guarded direct_fetch.
            def local_transport(url, config, **options):
                options['before_request'](url)
                headers = options.get('headers') or {'Accept':'application/xml,text/xml,application/gzip'}
                path = '/robots.txt' if url.endswith('/robots.txt') else '/page'
                request = Request(f'http://127.0.0.1:{server.server_port}{path}', headers=headers)
                try:
                    response = urlopen(request, timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    return {'status':response.status, 'url':url, 'body':response.read(), 'headers':dict(response.headers)}
            state = fixture_state()
            source = state['sources']['source-1']
            source['versions']['version-1'].update(representation='direct-html', evidence=extract_html(HTML,source['citation_url'])['evidence'])
            original_reviews = list(state['claims']['claim-1']['reviews'])
            with patch('okf_bundle.network.direct_fetch', side_effect=local_transport):
                result = check_availability(state, ['source-1'])
            self.assertEqual(result['observations'][0]['status'], 200)
            self.assertEqual(result['observations'][0]['outcome'], 'checked-live')
            self.assertTrue(result['observations'][0]['quotes_supported'])
            self.assertEqual(state['claims']['claim-1']['reviews'], original_reviews)
            self.assertEqual(len(seen), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_default_availability_keeps_real_403_blocking(self):
        state = fixture_state()
        def transport(url, config, **options):
            options['before_request'](url)
            return {'status':404 if url.endswith('/robots.txt') else 403,'body':b'denied','url':url}
        with patch('okf_bundle.network.direct_fetch', side_effect=transport) as fetch:
            result = check_availability(state, ['source-1'])
        self.assertEqual(result['observations'][0]['outcome'], 'known-unavailable')
        self.assertEqual(state['sources']['source-1']['availability'], 'nonpublic')
        self.assertEqual(fetch.call_count, 2)
