"""Generated instruction deadlines agree with actual release eligibility."""
import copy
from pathlib import Path
import re
import shutil
import tarfile
import unittest
import uuid

from okf_bundle.common import digest
from okf_bundle.export import (ReleaseError, build_candidate, eligibility,
                               package_release, verify_candidate)
from test_export import NOW, approve, fixture_state, observed_live, rebind


class HandoffInstructionTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(__file__).resolve().parents[3] / "work" / "handoff-instruction-tests"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.root = self.scratch / uuid.uuid4().hex
        self.root.mkdir()
        self.state = fixture_state()

    def tearDown(self):
        if self.root.is_symlink() or self.root.resolve().parent != self.scratch.resolve():
            raise RuntimeError("Refusing test cleanup outside scratch directory")
        shutil.rmtree(self.root)

    def build(self):
        candidate = self.root / "candidate"
        manifest = build_candidate(self.state, candidate, "synthetic-handoff", NOW)
        text = (candidate / "DEPLOYMENT.md").read_text(encoding="utf-8")
        return candidate, manifest, text

    def test_default_live_deadline_matches_packaging_expiry_not_claim_deadline(self):
        observed_live(self.state["sources"]["source-1"])
        candidate, manifest, text = self.build()
        deadline = re.search(r"valid through `([^`]+)`", text).group(1)
        self.assertEqual(deadline, "2026-10-16T12:00:00Z")
        self.assertNotEqual(deadline, manifest["earliest_stale_after"])
        self.assertTrue(eligibility(self.state, deadline)["ready"])
        later = "2026-10-16T12:00:01Z"
        self.assertFalse(eligibility(self.state, later)["ready"])
        approve(self.state, manifest)
        with self.assertRaises(ReleaseError):
            package_release(self.state, candidate, self.root / "expired.zip", later)
        self.assertFalse((self.root / "expired.zip").exists())

    def test_custom_window_uses_earliest_included_source_and_ignores_uncited_source(self):
        self.state["config"]["availability_max_age_hours"] = 6
        self.state["config_hash"] = digest(self.state["config"])
        first = self.state["sources"]["source-1"]
        observed_live(first)
        second = copy.deepcopy(first)
        second["id"] = "source-2"
        second["availability_check"]["at"] = "2026-10-15T10:00:00Z"
        self.state["sources"]["source-2"] = second
        claim = copy.deepcopy(self.state["claims"]["claim-1"])
        claim["id"] = "claim-2"
        claim["evidence"][0]["source_id"] = "source-2"
        self.state["claims"]["claim-2"] = claim
        self.state["concepts"]["concept-1"]["claim_ids"].append("claim-2")
        unused = copy.deepcopy(first)
        unused["id"] = "source-unused"
        unused["availability_check"]["at"] = "2026-10-15T08:00:00Z"
        self.state["sources"]["source-unused"] = unused
        rebind(self.state)
        _, _, text = self.build()
        self.assertIn("maximum age is 6 hours", text)
        self.assertIn("valid through `2026-10-15T16:00:00Z`", text)
        self.assertTrue(eligibility(self.state, "2026-10-15T16:00:00Z")["ready"])
        self.assertFalse(eligibility(self.state, "2026-10-15T16:00:01Z")["ready"])

    def test_mixed_offline_and_live_preserves_live_deadline(self):
        first = self.state["sources"]["source-1"]
        second = copy.deepcopy(first)
        second["id"] = "source-2"
        observed_live(second)
        self.state["sources"]["source-2"] = second
        claim = copy.deepcopy(self.state["claims"]["claim-1"])
        claim["id"] = "claim-2"
        claim["evidence"][0]["source_id"] = "source-2"
        self.state["claims"]["claim-2"] = claim
        self.state["concepts"]["concept-1"]["claim_ids"].append("claim-2")
        rebind(self.state)
        _, manifest, text = self.build()
        self.assertEqual(manifest["availability_mode"], "mixed-live/offline-with-availability-acceptance")
        self.assertIn("valid through `2026-10-16T12:00:00Z`", text)

    def test_offline_handoff_does_not_invent_live_check_or_change_package_gate(self):
        candidate, manifest, text = self.build()
        self.assertIn("No checked-live availability deadline", text)
        self.assertNotIn("valid through `", text)
        self.assertIn(manifest["earliest_stale_after"], text)
        with self.assertRaises(ReleaseError):
            package_release(self.state, candidate, self.root / "unapproved.zip", NOW)
        self.assertFalse((self.root / "unapproved.zip").exists())

    def test_instructions_are_private_hash_bound_and_explicit_about_staging(self):
        candidate, manifest, text = self.build()
        self.assertIn("Before deployment to any host, public or staging", text)
        self.assertIn("separate authorization", text)
        self.assertEqual(manifest["handoff_files"]["DEPLOYMENT.md"], digest(text.encode()))
        with tarfile.open(candidate / "okf/okf.tar.gz", "r:gz") as archive:
            self.assertTrue(all(name.endswith(".md") for name in archive.getnames()))
            self.assertNotIn("DEPLOYMENT.md", archive.getnames())
        (candidate / "DEPLOYMENT.md").write_text("Deploy immediately", encoding="utf-8")
        with self.assertRaises(ReleaseError):
            verify_candidate(candidate)


if __name__ == "__main__":
    unittest.main()
