import gzip
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from okf_bundle.extraction import extract_html
from okf_bundle.ingest import bounded_gzip, discover_csv, discover_sitemap, ingest_sf
from okf_bundle.state import new_state
from okf_bundle.workflow import merge_import


CONFIG = {"domains": ["example.com"], "locale": "en"}


def page(url, body=None, schema=None, og=None):
    return '<html lang="en"><head><title>Our training service</title><link rel="canonical" href="' + url + '">' + ('<meta property="og:url" content="' + og + '">' if og else '') + ('<script type="application/ld+json">' + json.dumps(schema) + '</script>' if schema else '') + '</head><body><header>Site chrome</header><nav>Navigation noise</nav>' + (body or '<h1>Food safety training</h1><p>In person instruction with a separate examination for adult learners.</p>') + '<footer>Footer noise</footer></body></html>'


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, entries):
        path = self.root / "input.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return path

    def test_sf_offline_identity_versions_and_unknown_dates(self):
        content = page("https://example.com/")
        result = ingest_sf(self.archive([("original_https_example.com_.html", content), ("rendered_https_example.com_.html", content)]), CONFIG)
        self.assertEqual(2, len(result["inventory"]))
        self.assertEqual(1, len(result["sources"]))
        source = next(iter(result["sources"].values()))
        self.assertTrue(source["identity_confirmed"])
        self.assertEqual("Homepage", source["classification"]["purpose"])
        self.assertEqual("unknown", source["availability"])
        self.assertIsNone(source["captured_status"])
        self.assertEqual({"sf-original", "sf-rendered"}, {v["representation"] for v in source["versions"].values()})
        for version in source["versions"].values():
            self.assertIsNone(version["capture_at"])
            self.assertIn("<html", version["raw_content"])
        again = ingest_sf(self.root / "input.zip", CONFIG)
        self.assertEqual(set(source["versions"]), set(next(iter(again["sources"].values()))["versions"]))

    def test_changed_extraction_contract_requires_new_version_and_claim_review(self):
        archive = self.archive([("original_https_example.com_.html", page("https://example.com/"))])
        state = new_state(CONFIG)
        with patch("okf_bundle.ingest.EXTRACTION_CONTRACT", "html-v1"):
            merge_import(state, ingest_sf(archive, CONFIG))
        source = next(iter(state["sources"].values()))
        old = source["active_version"]
        state["claims"]["claim"] = {"evidence": [{"source_id": source["id"], "version_id": old}],
            "evidence_review": "accepted", "publication": "approved", "business_confirmation": "confirmed"}
        state["release_approvals"] = {"release": "approved"}
        merge_import(state, ingest_sf(archive, CONFIG))
        source = state["sources"][source["id"]]
        self.assertNotEqual(old, source["active_version"])
        self.assertEqual(2, len(source["versions"]))
        self.assertEqual(1, len({v["content_hash"] for v in source["versions"].values()}))
        self.assertEqual("pending", state["claims"]["claim"]["evidence_review"])
        self.assertEqual("pending", state["claims"]["claim"]["publication"])
        self.assertEqual({}, state["release_approvals"])

    def test_unresolved_csv_binding_survives_reimport_without_csv_and_scope_inclusion(self):
        archive = self.archive([("original_https_example.com_.html", page("https://example.com/"))])
        csv = self.root / "internal.csv"
        csv.write_text("Address,Status Code\nhttps://example.com/,200\nhttps://example.com/,404\n")
        state = new_state(CONFIG)
        merge_import(state, ingest_sf(archive, CONFIG, csv))
        source = next(iter(state["sources"].values()))
        self.assertTrue(source["csv_metadata_review_required"])
        metadata = source["input_metadata"]
        source.update(scope="included", identity_confirmed=True, identity_basis="analyst-confirmed")
        merge_import(state, ingest_sf(archive, CONFIG))
        source = state["sources"][source["id"]]
        self.assertTrue(source["csv_metadata_review_required"])
        self.assertEqual(metadata, source["input_metadata"])
        self.assertIsNone(source["captured_status"])

    def test_canonical_does_not_merge_pagination_or_og_url(self):
        result = ingest_sf(self.archive([
            ("original_https_example.com_news_page_2_.html", page("https://example.com/news/", og="https://example.com/author/a/")),
            ("original_https_example.com_news_page_3_.html", page("https://example.com/news/"))]), CONFIG)
        self.assertEqual(2, len(result["sources"]))
        self.assertEqual({"pending"}, {row["state"] for row in result["inventory"]})
        self.assertTrue(all(not s["identity_confirmed"] for s in result["sources"].values()))

    def test_companion_csv_status_binds_to_independently_confirmed_identity_and_never_live(self):
        name = "original_https_example.com_.html"
        archive = self.archive([(name, page("https://example.com/"))])
        csv = self.root / "internal.csv"
        csv.write_text('Address,Status Code\nhttps://example.com/,200\n', encoding="utf-8-sig")
        unjoined = ingest_sf(archive, CONFIG, csv)
        source = next(iter(unjoined["sources"].values()))
        self.assertEqual(200, source["captured_status"])
        self.assertFalse(source["csv_metadata_provenance"]["establishes_identity"])
        self.assertEqual("unknown", source["csv_metadata_provenance"]["temporal_alignment"])
        self.assertEqual(2, len(unjoined["inventory"]))
        self.assertEqual("companion-metadata", unjoined["inventory"][1]["input_ref"]["kind"])
        source = next(iter(ingest_sf(archive, {**CONFIG, "sf_mapping": {name: "https://example.com/"}}, csv)["sources"].values()))
        self.assertEqual(200, source["captured_status"])
        self.assertEqual("unknown", source["availability"])

    def test_many_to_one_same_representation_mapping_requires_review(self):
        result = ingest_sf(self.archive([("one/original_https_example.com_.html", page("https://example.com/")), ("two/original_https_example.com_.html", page("https://example.com/", '<h1>Changed identity</h1><p>Different capture cannot silently become the same active evidence.</p>'))]), CONFIG)
        source = next(iter(result["sources"].values()))
        self.assertFalse(source["identity_confirmed"])
        self.assertIsNone(source["citation_url"])
        self.assertEqual("pending", source["scope"])
        self.assertEqual(2, len(source["versions"]))

    def test_confirmed_csv_failure_status_is_not_offline_unknown(self):
        name = "original_https_example.com_.html"
        archive = self.archive([(name, page("https://example.com/"))])
        csv = self.root / "status.csv"
        for status, expected in ((401, "nonpublic"), (403, "nonpublic"), (404, "failed"), (410, "failed")):
            with self.subTest(status=status):
                csv.write_text(f"Address,Status Code\nhttps://example.com/,{status}\n", encoding="utf-8")
                result = ingest_sf(archive, {**CONFIG, "sf_mapping": {name: "https://example.com/"}}, csv)
                source = next(iter(result["sources"].values()))
                self.assertEqual(status, source["captured_status"])
                self.assertEqual(expected, source["availability"])
                self.assertNotEqual("included", source["scope"])

    def test_companion_csv_failures_and_indexability_without_manual_file_mapping(self):
        name = "original_https_example.com_.html"
        archive = self.archive([(name, page("https://example.com/"))])
        csv = self.root / "internal.csv"
        for status in (404, 503):
            csv.write_text(f"Address,Status Code,Indexability,Indexability Status\nhttps://example.com/,{status},Non-Indexable,Client Error\n")
            source = next(iter(ingest_sf(archive, CONFIG, csv)["sources"].values()))
            self.assertTrue(source["identity_confirmed"])
            self.assertEqual(status, source["captured_status"])
            self.assertEqual("failed", source["availability"])
            self.assertEqual("pending", source["scope"])
            self.assertEqual("Non-Indexable", source["csv_indexability"])
            self.assertEqual("Client Error", source["csv_indexability_status"])
        csv.write_text("Address,Status Code,Indexability,Indexability Status\nhttps://example.com/,200,Non-Indexable,Canonicalised\n")
        source = next(iter(ingest_sf(archive, CONFIG, csv)["sources"].values()))
        self.assertEqual("unknown", source["availability"])
        self.assertEqual("pending", source["scope"])
        self.assertIsNone(source["robots"])

    def test_companion_divergent_or_duplicate_rows_cannot_assign_status(self):
        archive = self.archive([("original_https_example.com_.html", page("https://example.com/"))])
        csv = self.root / "internal.csv"
        for rows in ("https://example.com/other/,404\n", "https://example.com/,200\nhttps://example.com/,404\n"):
            csv.write_text("Address,Status Code\n" + rows)
            source = next(iter(ingest_sf(archive, CONFIG, csv)["sources"].values()))
            self.assertIsNone(source["captured_status"])
            self.assertEqual("pending", source["scope"])
            self.assertTrue(source["csv_metadata_review_required"])
        ambiguous = self.archive([("unknown.html", page("https://example.com/"))])
        csv.write_text("Address,Status Code\nhttps://example.com/,404\n")
        source = next(iter(ingest_sf(ambiguous, CONFIG, csv)["sources"].values()))
        self.assertFalse(source["identity_confirmed"])
        self.assertIsNone(source["captured_status"])

    def test_macos_metadata_is_skipped_but_bounded_and_validated(self):
        entries = [("original_https_example.com_.html", page("https://example.com/")),
                   (".DS_Store", b"metadata"), ("__MACOSX/._original_https_example.com_.html", b"metadata"), ("._capture.html", b"metadata")]
        result = ingest_sf(self.archive(entries), CONFIG)
        self.assertEqual(1, len(result["sources"]))
        self.assertEqual(3, sum(row["reason"].startswith("Bounded macOS metadata") for row in result["inventory"]))
        with self.assertRaisesRegex(ValueError, "\\.DS_Store"):
            ingest_sf(self.archive([(".DS_Store", b"x" * 101)]), {**CONFIG, "limits": {"html_bytes": 100}})
        with self.assertRaisesRegex(ValueError, "Unsafe archive path"):
            ingest_sf(self.archive([("__MACOSX/../private", b"x")]), CONFIG)
        with self.assertRaisesRegex(ValueError, "script.py"):
            ingest_sf(self.archive([("script.py", b"x")]), CONFIG)

    def test_body_fallback_article_header_tables_language_schema(self):
        body = '<article><header><h1>Información de examen</h1></header><p>Solo para adultos en España.</p><table><tr><th>Examen</th><th>Duración</th></tr><tr><td>Estándar</td><td>1,5 horas</td></tr></table><ul><li>Material incluido</li></ul></article>'
        schema = {"@type": ["Dentist", "LocalBusiness"], "name": "Example Clinic"}
        result = extract_html(page("https://example.com/", body, schema).replace('lang="en"', 'lang="es"'), "https://example.com/")
        self.assertIn("# Información de examen", result["text"])
        self.assertIn("Estándar | 1,5 horas |", result["text"])
        self.assertIn("* Material incluido", result["text"])
        self.assertNotIn("Site chrome", result["text"])
        self.assertNotIn("Navigation noise", result["text"])
        self.assertEqual("es", result["language"])
        self.assertEqual(["Dentist", "LocalBusiness"], result["classification"]["entities"][0]["types"])

    def test_unknown_observed_language_is_not_invented_from_assigned_locale(self):
        content = page("https://example.com/").replace(' lang="en"', '')
        result = ingest_sf(self.archive([("original_https_example.com_.html", content)]), CONFIG)
        source = next(iter(result["sources"].values()))
        version = source["versions"][source["active_version"]]
        self.assertEqual("en", version["locale"])
        self.assertIsNone(version["observed_language"])

    def test_nested_schema_is_not_page_purpose_and_url_segments_are_bounded(self):
        schema = {"@type": "Organization", "name": "Example", "offers": {"@type": "Offer", "name": "Training", "itemOffered": {"@type": "Course", "name": "Exam course"}}}
        for path, expected in [("/", "Homepage"), ("/contact/", "Contact Page"), ("/company/", "About Page"), ("/careers/", "Job Posting"), ("/c/widgets/", "Product Category")]:
            result = extract_html(page("https://example.com" + path, schema=schema), "https://example.com" + path)
            self.assertEqual(expected, result["classification"]["purpose"])
        self.assertIn("Course", {t for e in result["classification"]["entities"] for t in e["types"]})

    def test_media_lms_and_material_conflicts_are_retained_for_review(self):
        video = extract_html(page("https://example.com/videos/", '<h1>Training videos</h1><iframe src="https://video.example/embed/1"></iframe>'), "https://example.com/videos/")
        self.assertTrue(any("no transcript inferred" in w for w in video["warnings"]))
        self.assertNotIn("transcript", video["text"])
        lms = extract_html(page("https://example.com/quizzes/", '<h1>SEO quiz</h1><p>Project management demonstration questions, not a food safety offering.</p>'), "https://example.com/quizzes/")
        self.assertTrue(any("relevance review" in w for w in lms["warnings"]))
        one = extract_html(page("https://example.com/faq/", '<h1>Exam</h1><p>The exam lasts 1.5 hours. The guide has 21 pages and 80 questions.</p>'), "https://example.com/faq/")
        two = extract_html(page("https://example.com/service/", '<h1>Exam</h1><p>The exam lasts 2 hours. The guide has 26 pages and 150 questions.</p>'), "https://example.com/service/")
        self.assertIn("1.5 hours", one["evidence"]["body"])
        self.assertIn("2 hours", two["evidence"]["body"])
        # Detection/adjudication of this planted contradiction belongs to semantic review.

    def test_archive_rejects_unsafe_unsupported_collision_and_bounds(self):
        cases = [[("../escape.html", "a")], [("/abs.html", "a")], [("safe.html", "a"), ("SAFE.html", "b")], [("script.py", "x")], [("ads:stream.html", "x")]]
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                ingest_sf(self.archive(entries), CONFIG)
        link = zipfile.ZipInfo("link.html")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            ingest_sf(self.archive([(link, "target")]), CONFIG)
        with self.assertRaises(ValueError):
            ingest_sf(self.archive([("x.html", "a" * 100)]), {**CONFIG, "limits": {"html_bytes": 20}})

    def test_csv_bom_semicolon_quoted_rows_metadata_and_variants(self):
        path = self.root / "urls.csv"
        path.write_text('Address;Note\nhttps://example.com/A/;"quoted; value"\nhttps://example.com/A/;duplicate\nhttps://example.com/a/;case\nhttps://example.com/A;slash\nhttps://example.com/A/?v=1#view;variant\n;blank\nhttps://other.example/x;scope\nfile:///tmp/x;bad\n', encoding="utf-8-sig")
        result = discover_csv(path, CONFIG)
        self.assertEqual(8, len(result["inventory"]))
        self.assertEqual(1, sum(r["state"] == "duplicate" for r in result["inventory"]))
        self.assertEqual(3, sum(r["state"] == "excluded" for r in result["inventory"]))
        first = next(iter(result["sources"].values()))
        self.assertEqual("quoted; value", first["input_metadata"][0]["values"]["Note"])
        self.assertEqual(2, len(first["input_metadata"]))
        self.assertEqual("view", list(result["sources"].values())[3]["fragment"])

    def test_csv_headerless_ambiguous_column_and_bounds(self):
        path = self.root / "urls.csv"
        path.write_text("https://example.com/\nhttps://example.com/about/\n", encoding="utf-8")
        self.assertEqual(2, len(discover_csv(path, CONFIG)["sources"]))
        path.write_text("URL,Address\nhttps://example.com/,https://example.com/about/\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Choose one"):
            discover_csv(path, CONFIG)
        self.assertEqual("https://example.com/about/", discover_csv(path, CONFIG, "Address")["inventory"][0]["url"])
        with self.assertRaises(ValueError):
            discover_csv(path, {**CONFIG, "limits": {"csv_bytes": 3}}, "URL")

    def test_ordinary_analyst_url_headers_preserve_rows_and_variants(self):
        for header in ("Top pages", "URLs", "  TOP PAGES  ", " urls "):
            with self.subTest(header=header):
                path = self.root / "analyst.csv"
                path.write_text(header + ';Clicks\nhttps://example.com/A/;12\n'
                    'https://example.com/A/?v=1#view;4\nhttps://example.com/A/;2\n', encoding="utf-8-sig")
                result = discover_csv(path, CONFIG)
                self.assertEqual(3, len(result["inventory"]))
                self.assertEqual(2, len(result["sources"]))
                self.assertEqual([2, 3, 4], [row["input_ref"]["row"] for row in result["inventory"]])
                self.assertEqual("duplicate", result["inventory"][2]["state"])
                variant = next(source for source in result["sources"].values() if source["fragment"])
                self.assertEqual("https://example.com/A/?v=1#view", variant["requested_url"])
                self.assertEqual("4", variant["input_metadata"][0]["values"]["Clicks"])

    def test_ordinary_url_headers_remain_ambiguous_and_explicit_column_wins(self):
        path = self.root / "analyst.csv"
        for second in ("URLs", "Address"):
            with self.subTest(second=second):
                path.write_text("Top pages," + second + "\nhttps://example.com/a/,https://example.com/b/\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Choose one unambiguous URL column"):
                    discover_csv(path, CONFIG)
                selected = discover_csv(path, CONFIG, second)
                self.assertEqual("https://example.com/b/", selected["inventory"][0]["url"])
        path.write_text("URLs,Custom export\nhttps://example.com/a/,https://example.com/b/\n", encoding="utf-8")
        self.assertEqual("https://example.com/b/", discover_csv(path, CONFIG, "Custom export")["inventory"][0]["url"])
        path.write_text("URLs,URLs\nhttps://example.com/a/,https://example.com/b/\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Choose one unambiguous URL column"):
            discover_csv(path, CONFIG, "URLs")

    def test_nested_sitemap_gzip_cycle_namespaces_lastmod_and_child_failure(self):
        ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        docs = {
            "https://example.com/index.xml": f'<sitemapindex {ns}><sitemap><loc>https://example.com/child.xml.gz</loc></sitemap><sitemap><loc>https://example.com/index.xml</loc></sitemap><sitemap><loc>https://example.com/missing.xml</loc></sitemap></sitemapindex>'.encode(),
            "https://example.com/child.xml.gz": gzip.compress(f'<urlset {ns} xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"><url><loc>https://example.com/a/</loc><lastmod>2024-01-01</lastmod><image:image><image:loc>https://images.example/image.jpg</image:loc></image:image></url><url><loc>https://example.com/a/</loc></url></urlset>'.encode()),
        }
        result = discover_sitemap("https://example.com/index.xml", CONFIG, lambda url: docs.get(url, {"status": 404, "body": b""}))
        self.assertEqual(1, len(result["sources"]))
        self.assertEqual(3, len(result["inventory"]))
        self.assertTrue(result["sitemap_summary"]["partial"])
        self.assertTrue(any("cyclic" in w for w in result["warnings"]))
        source = next(iter(result["sources"].values()))
        self.assertEqual("2024-01-01", source["input_metadata"][0]["values"]["sitemap_lastmod_assertion"])
        self.assertIsNone(source["captured_status"])

    def test_xml_entity_bomb_local_escape_and_limits(self):
        for content in [b'<!DOCTYPE x [<!ENTITY p SYSTEM "file:///private">]><urlset>&p;</urlset>', b'<notasitemap/>']:
            result = discover_sitemap("https://example.com/s.xml", CONFIG, lambda _: content)
            self.assertEqual("failed", result["inventory"][0]["state"])
        with self.assertRaises(ValueError):
            bounded_gzip(gzip.compress(b"x" * 1000), 100)
        path = self.root / "index.xml"
        path.write_text('<sitemapindex><sitemap><loc>../secret.xml</loc></sitemap></sitemapindex>', encoding="utf-8")
        self.assertTrue(discover_sitemap(path, CONFIG)["sitemap_summary"]["partial"])
        result = discover_sitemap("https://example.com/s.xml", {**CONFIG, "limits": {"urls": 1}}, lambda _: b'<urlset><url><loc>https://example.com/a</loc></url><url><loc>https://example.com/b</loc></url></urlset>')
        self.assertEqual(1, len(result["sources"]))
        self.assertTrue(result["sitemap_summary"]["partial"])

    def test_invalid_child_locations_preserve_siblings_and_never_read_local_files(self):
        local = self.root / "private.xml"
        local.write_text('<urlset><url><loc>https://example.com/private-local/</loc></url></urlset>')
        root = "https://example.com/index.xml"
        docs = {root: ('<sitemapindex><sitemap><loc>https://example.com/first.xml</loc></sitemap>'
            '<sitemap><loc>https://example.com/bad child.xml</loc></sitemap>'
            '<sitemap><loc>C:/fixture/private.xml</loc></sitemap>'
            '<sitemap><loc>file:///private.xml</loc></sitemap>'
            '<sitemap><loc>last.xml</loc></sitemap></sitemapindex>').encode(),
            "https://example.com/first.xml": b'<urlset><url><loc>https://example.com/first/</loc></url></urlset>',
            "https://example.com/last.xml": b'<urlset><url><loc>https://example.com/last/</loc></url></urlset>'}
        calls = []
        def fetch(url):
            calls.append(url)
            return docs[url]
        result = discover_sitemap(root, CONFIG, fetch)
        self.assertEqual({"https://example.com/first/", "https://example.com/last/"}, {s["normalized_url"] for s in result["sources"].values()})
        self.assertTrue(result["sitemap_summary"]["partial"])
        self.assertEqual([root, "https://example.com/first.xml", "https://example.com/last.xml"], calls)
        self.assertEqual(3, sum(row["state"] == "failed" for row in result["inventory"]))
        index = self.root / "index.xml"
        index.write_text('<sitemapindex><sitemap><loc>../escape.xml</loc></sitemap><sitemap><loc>private.xml</loc></sitemap></sitemapindex>')
        result = discover_sitemap(index, CONFIG)
        self.assertEqual(1, len(result["sources"]))
        self.assertTrue(result["sitemap_summary"]["partial"])

    def test_remote_discovery_requires_authorization_without_fixture(self):
        with self.assertRaisesRegex(ValueError, "not authorized"):
            discover_sitemap("https://example.com/sitemap.xml", CONFIG)


if __name__ == "__main__":
    unittest.main()
