"""Publication-profile destination guards; synthetic Markdown, no network calls."""
from pathlib import Path
import shutil
import unittest
import uuid

from okf_bundle.export import ReleaseError, build_candidate
from okf_bundle.validation import validate_bundle
from test_export import NOW, fixture_state, rebind


class MarkdownDestinationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = (Path(__file__).resolve().parents[3] / "work" / "markdown-link-tests").resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.root = self.scratch / uuid.uuid4().hex
        self.root.mkdir()
        build_candidate(fixture_state(), self.root / "candidate", "links-fixture", NOW)
        self.bundle = self.root / "candidate" / "okf"
        self.concept = next((self.bundle / "en" / "services").glob("team*.md"))
        self.original = self.concept.read_text(encoding="utf-8")

    def tearDown(self):
        if self.root.is_symlink() or self.root.resolve().parent != self.scratch:
            raise RuntimeError("Refusing cleanup outside the synthetic test directory")
        shutil.rmtree(self.root)

    def report(self, markup, profile=True):
        self.concept.write_text(self.original + "\n\n" + markup + "\n", encoding="utf-8")
        return validate_bundle(self.bundle, profile=profile, allowed_domains=["example.com"])

    def test_ordinary_allowed_destinations(self):
        cases = [
            "Ordinary prose, [a label], parentheses (a note), and 25% progress.",
            "Comparison: 1 < 2 > 0. Inline code: `value = (3 + 4)`.",
            "An example of a safe link: `[Section](index.md)`.",
            r"Escaped literal syntax: \[label\]\(not-a-link\).",
            "[Details](https://example.com/training/)",
            "[Section](index.md)",
            "[Root](../../index.md#contents)",
            "[Current](#details)",
            "[Current]()",
            "![Logo](https://example.com/logo.png)",
            "[Details][details]\n\n[details]: https://example.com/training/",
            "[Section][section]\n\n[section]: index.md",
            "![Logo][logo]\n\n[logo]: https://example.com/logo.png",
            "[Section][]\n\n[Section]: index.md",
            "[Section]\n\n[Section]: index.md",
            "<https://example.com/training/>",
            "[Details](<https://example.com/training/>)",
            '[Details](https://example.com/training/ "A title")',
            "[Details](https://example.com/training/ 'A title')",
            "[Details](https://example.com/training/ (A title))",
            "[Details]( \n https://example.com/training/\n )",
            "[Details][details]\n\n[details]:\n  <https://example.com/training/> \"Title\"",
            "[Details](https://example.com/a(b)c)",
            r"[Details](https://example.com/a\(b\)c)",
            "[Details](https://example.com/a%20b?q=first&amp;page=2)",
            "[Details](https&#58;//example.com/training/)",
            "[Details](https://example.com/a&lpar;b&rpar;)",
        ]
        for markup in cases:
            with self.subTest(markup=markup):
                report = self.report(markup)
                self.assertTrue(report["profile"]["ready"], report["issues"])

    def test_every_original_bypass_and_inline_controls_block(self):
        cases = [
            "[Details][details]\n\n[details]: ../../../../private-state/state.json",
            "[Details][details]\n\n[details]: javascript:alert(1)",
            "![Preview](../../../../private-state/review.xlsx)",
            "![Preview](//unapproved.example.net/track)",
            "<javascript:alert(1)>",
            "[Details](../../../../private-state/state.json)",
            "[Details](javascript:alert(1))",
        ]
        self.assert_blocked(cases)

    def assert_blocked(self, cases):
        for markup in cases:
            with self.subTest(markup=markup):
                report = self.report(markup)
                self.assertTrue(report["core"]["conformant"], report["issues"])
                self.assertFalse(report["profile"]["ready"], markup)
                self.assertTrue(any(i["rule"].startswith("profile.link.") for i in report["issues"]), report["issues"])

    def test_entities_and_escapes_cannot_hide_schemes_or_credentials(self):
        self.assert_blocked([
            "[x](javascript&colon;alert(1))",
            "![x](&#106;avascript:alert(1))",
            "[x][r]\n\n[r]: &#x6a;avascript:alert(1)",
            "<javascript&colon;alert(1)>",
            r"[x](javascript\:alert\(1\))",
            r"[x][r]" + "\n\n" + r"[r]: javascript\:alert\(1\)",
            "[x](https://example.com/?a=1&amp;token=secret123)",
            "[x](index.md?api_key=secret123)",
            "[x](data:text/plain,hello)",
            "<person@example.com>",
            "![x](https://unapproved.example.net/logo.png)",
        ])

    def test_protocol_relative_and_encoded_navigation_block(self):
        self.assert_blocked([
            "[x][r]\n\n[r]: //example.com/secret",
            "<//example.com/secret>",
            "![x](&sol;&sol;example.com/secret)",
            "[x](%2e%2e/%2e%2e/%2e%2e/private.json)",
            "[x][r]\n\n[r]: %252e%252e%252fprivate.json",
            "![x](%2f%2funapproved.example.net/track)",
            "[x](%5c%5cunapproved.example.net/track)",
            "[x](javascript%3Aalert(1))",
            r"[x](..\..\..\private.json)",
            "[x](https://example.com/%0aevil)",
            "[x](https://example.com/%255cevil)",
            "[x](https://example.com/bad%xx)",
            "[x](index.md?value=%0aevil)",
            "[x](index.md#%255cevil)",
        ])

    def test_whitespace_controls_and_malformed_syntax_block_explicitly(self):
        self.assert_blocked([
            "[x](java&#x09;script:alert(1))",
            "[x](java&#x0A;script:alert(1))",
            "[x](<java\nscript:alert(1)>)",
            "<java\nscript:alert(1)>",
            "[x](https://example.com/a b)",
            "[x](https://example.com/\n javascript:alert(1))",
            "[x](https://example.com/\n\n)",
            "[x](https://example.com/a(b)",
            "[x](https://example.com/",
            "[x](<https://example.com/)",
            "[x](https://example.com/ \"unclosed)",
            "[x][r]\n\n[r]: <javascript:alert(1)",
            "[x][r]\n\n[r]:\n\nhttps://example.com/",
            "[x](https://example.com/\x00evil)",
            "[x][r]\n\n[r]: index.md[^source-1]",
        ])

    def test_reference_definitions_in_lists_quotes_and_multiline_labels_are_checked(self):
        self.assert_blocked([
            "[x][r]\n\n> [r]: javascript:alert(1)",
            "[x][r]\n\n- [r]: javascript:alert(1)",
            "[x][r]\n\n[r]:\n  javascript:alert(1)",
            "[x][long label]\n\n[long\nlabel]: javascript:alert(1)",
            "[r]: javascript:alert(1)",
            "A conservative code example: `[x](javascript:alert(1))`.",
            "```markdown\n[x][r]\n[r]: javascript:alert(1)\n```",
        ])

    def test_parentheses_and_escaped_reference_labels_remain_supported(self):
        (self.concept.parent / "safe(name).md").write_text(self.original, encoding="utf-8")
        for markup in ("[x](safe(name).md)", r"[x](safe\(name\).md)",
                       "[x][r]\n\n[r]: safe(name).md", r"[x][r\]]" + "\n\n" + r"[r\]]: index.md"):
            with self.subTest(markup=markup):
                report = self.report(markup)
                self.assertTrue(report["profile"]["ready"], report["issues"])

    def test_candidate_export_blocks_profile_bypass(self):
        state = fixture_state()
        state["claims"]["claim-1"]["public_statement"] += "\n\n![Preview](//unapproved.example.net/track)"
        rebind(state)  # Synthetic reviews, not real approvals.
        with self.assertRaises(ReleaseError):
            build_candidate(state, self.root / "blocked", "blocked-fixture", NOW)
        self.assertFalse((self.root / "blocked").exists())

    def test_core_stays_permissive_without_publication_profile(self):
        report = self.report("[x][r]\n\n[r]: javascript:alert(1)\n\n![x](../../../../private.json)", profile=False)
        self.assertTrue(report["core"]["conformant"], report["issues"])
        self.assertIsNone(report["profile"]["ready"])
        self.assertFalse(any(i["rule"].startswith("profile.link.") for i in report["issues"]))


if __name__ == "__main__":
    unittest.main()
