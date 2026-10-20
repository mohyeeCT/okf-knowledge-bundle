"""Scope-aware CLI validation and unresolved-submission release gates."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from okf_bundle.cli import execute, main, parser
from okf_bundle.common import digest, utc_now
from okf_bundle.export import build_candidate, eligibility
from okf_bundle.state import StateStore
from test_export import NOW, fixture_state


class CLIPolicyFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = fixture_state()
        self.run = self.root / "run"
        store = StateStore(self.run)
        with store.transaction():
            store.save(self.state)
        self.candidate = self.root / "candidate"
        build_candidate(self.state, self.candidate, "release-fixture", NOW)

    def validate(self, *policy):
        return execute(parser().parse_args(["validate", "--bundle", str(self.candidate / "okf"), *policy]))

    def test_unscoped_validation_cannot_report_publication_ready(self):
        report = self.validate()
        self.assertTrue(report["core"]["conformant"])
        self.assertFalse(report["profile"]["ready"])
        self.assertFalse(report["domain_scope"]["checked"])
        self.assertIn("profile.domain_scope.unchecked", [row["rule"] for row in report["issues"]])

    def test_explicit_domains_and_run_policy_produce_equal_scoped_results(self):
        supplied = self.validate("--domains", "example.com")
        recorded = self.validate("--run", str(self.run))
        self.assertTrue(supplied["profile"]["ready"])
        self.assertTrue(recorded["profile"]["ready"])
        self.assertEqual(supplied["issues"], recorded["issues"])
        self.assertEqual(["example.com"], recorded["domain_scope"]["domains"])

    def test_command_exit_status_distinguishes_unscoped_and_scoped_checks(self):
        arguments = ["validate", "--bundle", str(self.candidate / "okf")]
        with redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(2, main(arguments))
        self.assertFalse(json.loads(captured.getvalue())["profile"]["ready"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, main(arguments + ["--domains", "example.com"]))

    def test_scoped_cli_rejects_explicit_external_destination(self):
        concept = next((self.candidate / "okf").glob("en/services/*.md"))
        if concept.name == "index.md":
            concept = next(p for p in (self.candidate / "okf/en/services").glob("*.md") if p.name != "index.md")
        concept.write_text(concept.read_text() + "\n[Details](https://outside.example.org/public/)\n", encoding="utf-8")
        report = self.validate("--domains", "example.com")
        self.assertTrue(report["core"]["conformant"])
        self.assertFalse(report["profile"]["ready"])
        self.assertIn("profile.link.external", [row["rule"] for row in report["issues"]])

    def test_domain_policy_rejects_private_empty_or_path_entries(self):
        for domain in ("localhost", "127.0.0.1", "example.com/private", "https://example.com/?token=secret", ""):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                self.validate("--domains", domain)

    def test_legacy_pending_then_success_still_blocks_source_and_alias(self):
        source = self.state["sources"]["source-1"]
        source["collection_state"] = "complete"
        source["collection_attempts"] = [{"outcome": "pending", "requested_at": "2026-10-01T12:00:00Z",
            "completed_at": None, "settings": {"token_budget": 1000}, "reserved_tokens": 1000},
            {"outcome": "success", "completed_at": "2026-10-01T12:01:00Z"}]
        report = eligibility(self.state, NOW)
        self.assertFalse(report["ready"])
        self.assertIn("unfinished collection attempt", json.dumps(report))
        target = copy.deepcopy(source)
        target["id"] = "alias-target"
        self.state["sources"][target["id"]] = target
        source["alias_of"] = target["id"]
        source["collection_attempts"] = []
        report = eligibility(self.state, NOW)
        self.assertFalse(report["ready"])
        self.assertIn("unfinished collection attempt", json.dumps(report))

    def test_status_exposes_exact_unresolved_attempt_binding(self):
        source = self.state["sources"]["source-1"]
        source["collection_attempts"] = [{"outcome": "pending", "requested_at": "2026-10-01T12:00:00Z",
            "completed_at": None, "settings": {"token_budget": 1000}, "reserved_tokens": 1000}]
        store = StateStore(self.run)
        with store.transaction():
            store.load()
            store.save(self.state)
        report = execute(parser().parse_args(["status", "--run", str(self.run)]))
        pending = report["unresolved_collection_attempts"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source_id"], source["id"])
        self.assertEqual(pending[0]["attempt_index"], 0)
        self.assertEqual(len(pending[0]["attempt_hash"]), 64)

    def test_reconciliation_cli_requires_current_attempt_and_human_decision(self):
        source = self.state["sources"]["source-1"]
        self.state["config"]["publication_owner"] = "human:client-owner"
        self.state["config"]["collection_authorized"] = True
        self.state["config_hash"] = digest(self.state["config"])
        source["collection_state"] = "pending"
        source["collection_attempts"] = [{"outcome": "pending", "requested_at": "2026-10-01T12:00:00Z",
            "completed_at": None, "settings": {"token_budget": 1000}, "reserved_tokens": 1000}]
        store = StateStore(self.run)
        with store.transaction():
            store.load()
            store.save(self.state)
        pending = execute(parser().parse_args(["status", "--run", str(self.run)]))["unresolved_collection_attempts"][0]
        arguments = ["collection-reconcile", "--run", str(self.run), "--id", source["id"], "--attempt-index", "0",
            "--attempt-hash", pending["attempt_hash"], "--expected-version", source["active_version"],
            "--action", "close-without-retry", "--reason", "Synthetic operator reconciled unknown outcome; no further submission",
            "--by", "human:client-owner", "--at", utc_now(), "--channel", "synthetic-test", "--reference", "fixture-not-real-approval"]
        bad = arguments.copy()
        bad[bad.index("--attempt-hash") + 1] = digest("stale-attempt")
        with self.assertRaises(ValueError):
            execute(parser().parse_args(bad))
        self.assertEqual(source["collection_attempts"], StateStore(self.run).load()["sources"][source["id"]]["collection_attempts"])
        result = execute(parser().parse_args(arguments))
        self.assertEqual("close-without-retry", result["action"])
        current = StateStore(self.run).load()["sources"][source["id"]]
        self.assertEqual(1000, current["collection_attempts"][0]["reserved_tokens"])
        self.assertEqual("pending", current["collection_attempts"][0]["outcome"])
        self.assertEqual([], execute(parser().parse_args(["status", "--run", str(self.run)]))["unresolved_collection_attempts"])


if __name__ == "__main__":
    unittest.main()
