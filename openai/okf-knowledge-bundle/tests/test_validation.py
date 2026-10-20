"""Implicit GFM destinations use the same strict publication policy as links.

Synthetic file/candidate tests only. GFM examples 622-635 define the relevant
recognition rules; no renderer, network call or real approval is involved.
"""
import json
from pathlib import Path
import shutil
import unittest
import uuid

from okf_bundle.common import digest
from okf_bundle.export import ReleaseError, build_candidate, package_release, verify_candidate
from okf_bundle.validation import _markdown_destinations, validate_bundle
from test_export import NOW, approve, fixture_state, rebind


class ImplicitDestinationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = (Path(__file__).resolve().parents[3] / "work" / "validation-tests").resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.root = self.scratch / uuid.uuid4().hex
        self.root.mkdir()
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        (self.bundle / "index.md").write_text("---\nokf_version: '0.2'\n---\n# Index\n\n[Training](concept.md)\n", encoding="utf-8")
        (self.bundle / "log.md").write_text("# Log\n\n## 2026-10-15\n\n- Created [Training](concept.md).\n", encoding="utf-8")
        meta = {"type": "Service", "title": "Training", "description": "Reviewed training service.",
                "status": "stable", "generated": {"by": "synthetic-fixture", "at": NOW},
                "stale_after": "2027-01-01T00:00:00Z", "sources": [{"id": "source", "resource": "https://example.com/evidence/"}]}
        self.document = "---\n" + json.dumps(meta) + "\n---\nTraining is available.[^source]\n\n[^source]: https://example.com/evidence/\n"
        self.domains = ["example.com", "www.example.com"]

    def tearDown(self):
        if self.root.is_symlink() or self.root.resolve().parent != self.scratch:
            raise RuntimeError("Refusing test cleanup outside the synthetic scratch directory")
        shutil.rmtree(self.root)

    def report(self, snippet, *, profile=True, domains=None):
        (self.bundle / "concept.md").write_text(self.document + "\n" + snippet + "\n", encoding="utf-8")
        return validate_bundle(self.bundle, profile=profile, allowed_domains=self.domains if domains is None else domains)

    def test_www_punctuation_parentheses_and_entity_tail_follow_gfm_boundaries(self):
        cases = [
            ("Visit www.example.com.", "http://www.example.com"),
            ("*www.example.com/path*", "http://www.example.com/path"),
            ("_www.example.com/a.b._", "http://www.example.com/a.b"),
            ("(www.example.com/search?q=topic+(business)))", "http://www.example.com/search?q=topic+(business)"),
            ("www.example.com/search?q=(business))+ok", "http://www.example.com/search?q=(business))+ok"),
            ("www.example.com/search?q=topic&hl;", "http://www.example.com/search?q=topic"),
            ("www.example.com/he<lp", "http://www.example.com/he"),
            ("www.example.com/Hello%20there", "http://www.example.com/Hello%20there"),
            ("www.example.com/?a=1&amp;page=2", "http://www.example.com/?a=1&page=2"),
        ]
        for snippet, destination in cases:
            with self.subTest(snippet=snippet):
                self.assertIn(destination, _markdown_destinations(snippet)[0])
                report = self.report(snippet)
                self.assertTrue(report["profile"]["ready"], report["issues"])

    def test_plain_nonlinks_and_invalid_email_endings_are_not_partial_matches(self):
        cases = [
            "The values are 1.23, 45.6% and 10:30; 3 @ 4.5 is a numeric annotation.",
            "A name@example label lacks a dotted domain.",
            "prefixwww.evil.invalid is a plain token here.",
            "www. and www..example.com have no valid dotted domain.",
            "www.exam_ple.com has an underscore in a final domain segment.",
            "hello@mail+xyz.example is not an extended email link.",
            "a.b-c_d@a.b- and a.b-c_d@a.b_ must not be shortened to valid prefixes.",
            "mailto:a.b-c_d@a.b- xmpp:a.b-c_d@a.b_",
            "a@b..c has an empty domain segment.",
        ]
        for snippet in cases:
            with self.subTest(snippet=snippet):
                self.assertEqual(_markdown_destinations(snippet), ([], []))
                report = self.report(snippet)
                self.assertTrue(report["profile"]["ready"], report["issues"])

    def test_bare_email_and_extended_protocols_are_recognized_as_non_http_destinations(self):
        cases = [
            ("person@example.com", "mailto:person@example.com"),
            ("Contact person@evil.invalid.", "mailto:person@evil.invalid"),
            ("hello+xyz@mail.example", "mailto:hello+xyz@mail.example"),
            ("a.b-c_d@a.b.", "mailto:a.b-c_d@a.b"),
            ("mailto:person@example.com/", "mailto:person@example.com"),
            ("xmpp:person@example.com/txt@bin.com", "xmpp:person@example.com/txt@bin.com"),
            ("xmpp:person@example.com/txt/bin", "xmpp:person@example.com/txt"),
        ]
        for snippet, destination in cases:
            with self.subTest(snippet=snippet):
                self.assertEqual(_markdown_destinations(snippet), ([destination], []))
                report = self.report(snippet)
                self.assertTrue(report["core"]["conformant"])
                self.assertFalse(report["profile"]["ready"])
                self.assertIn("profile.link.external", {item["rule"] for item in report["issues"]})

    def test_angle_email_is_mailto_even_when_same_spelling_names_a_bundle_file(self):
        # Previously this could be mistaken for a relative file destination.
        (self.bundle / "person@example.md").write_text(self.document, encoding="utf-8")
        for snippet in ("<person@example.md>", "<person@localhost>", "<foo+special@Bar.baz-bar0.com>"):
            with self.subTest(snippet=snippet):
                destinations, errors = _markdown_destinations(snippet)
                self.assertEqual(errors, [])
                self.assertEqual(destinations, ["mailto:" + snippet[1:-1]])
                report = self.report(snippet)
                self.assertFalse(report["profile"]["ready"])
                self.assertIn("profile.link.external", {item["rule"] for item in report["issues"]})

    def test_private_encoded_and_out_of_scope_implicit_destinations_fail(self):
        cases = [
            "www.evil.invalid", "www.intranet.local", "www.outside.example.org",
            "www.example.com.evil.invalid", "www.example.com@evil.invalid",
            "www.evil&#46;invalid", "person&#64;evil.invalid", "xmpp&#58;person@evil.invalid",
            "www.example.com/%0aheader", "www.example.com/%255cpath",
            "www.example.com/?api_key=hidden-value", "www.example.com/?a=1&amp;token=secret-value",
            "www.example.com%2f@evil.invalid", "www.example.com/%broken",
        ]
        for snippet in cases:
            with self.subTest(snippet=snippet):
                self.assertTrue(_markdown_destinations(snippet)[0], snippet)
                report = self.report(snippet)
                self.assertTrue(report["core"]["conformant"])
                self.assertFalse(report["profile"]["ready"], snippet)
                self.assertTrue(any(item["rule"].startswith("profile.link.") for item in report["issues"]))
        # Apex approval does not implicitly approve www.
        self.assertFalse(self.report("www.example.com", domains=["example.com"])["profile"]["ready"])

    def test_explicit_allowed_links_are_not_reinterpreted_as_nested_email_links(self):
        (self.bundle / "person@example.md").write_text(self.document, encoding="utf-8")
        cases = [
            "[Training](https://example.com/training/)",
            "![Logo](https://example.com/logo.png)",
            "[Index](index.md)",
            "[Contact](https://example.com/contact/person@example.com)",
            "<https://example.com/contact/person@example.com>",
            "https://example.com/contact/person@example.com",
            "[Reference][r]\n\n[r]: https://example.com/contact/person@example.com",
        ]
        for snippet in cases:
            with self.subTest(snippet=snippet):
                report = self.report(snippet)
                self.assertTrue(report["profile"]["ready"], report["issues"])

    def test_conservative_raw_scan_does_not_exempt_labels_code_or_renderer_variant_boundaries(self):
        for snippet in ('[person@example.com](https://example.com/contact/)',
                        '[person@example.com][r]\n\n[r]: https://example.com/contact/',
                        '[Local document](<person@example.md>)',
                        '`person@example.com`', '"www.evil.invalid"', 'path/www.evil.invalid',
                        'https&#58;//example.com/contact/person@example.com',
                        r'https\://example.com/contact/person@evil.example'):
            with self.subTest(snippet=snippet):
                self.assertFalse(self.report(snippet)["profile"]["ready"])

    def test_core_conformance_is_independent_of_implicit_link_publication_policy(self):
        snippet = "www.evil.invalid person@evil.invalid <person@example.md> xmpp:user@example.com"
        core = self.report(snippet, profile=False)
        self.assertTrue(core["core"]["conformant"])
        self.assertIsNone(core["profile"]["ready"])
        self.assertFalse(any(item["rule"].startswith("profile.link.") for item in core["issues"]))
        self.assertFalse(self.report(snippet)["profile"]["ready"])

    def test_implicit_bypasses_cannot_build_and_safe_www_candidate_still_packages(self):
        for index, snippet in enumerate(("Visit www.evil.invalid.", "Contact person@evil.invalid.")):
            with self.subTest(snippet=snippet):
                state = fixture_state()
                state["claims"]["claim-1"]["public_statement"] += "\n\n" + snippet
                rebind(state)  # Synthetic fixture reviewers only.
                candidate = self.root / ("blocked-" + str(index))
                with self.assertRaises(ReleaseError):
                    build_candidate(state, candidate, "blocked", NOW)
                self.assertFalse(candidate.exists())
        state = fixture_state()
        state["config"]["domains"].append("www.example.com")
        state["config_hash"] = digest(state["config"])
        state["claims"]["claim-1"]["public_statement"] += "\n\nVisit www.example.com."
        rebind(state)
        candidate = self.root / "allowed"
        manifest = build_candidate(state, candidate, "allowed", NOW)
        self.assertEqual(verify_candidate(candidate), manifest)
        approve(state, manifest)
        package_release(state, candidate, self.root / "allowed.zip", NOW)
        self.assertTrue((self.root / "allowed.zip").is_file())


if __name__ == "__main__":
    unittest.main()
