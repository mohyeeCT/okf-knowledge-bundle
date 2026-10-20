"""Demonstrations cannot create approvals or silently include held material."""
import copy
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from okf_bundle.common import digest
from okf_bundle.demonstration import build_demonstration
from okf_bundle.export import _deployment_text
from okf_bundle.review import import_drafts
from okf_bundle.validation import parse_document, validate_bundle
from okf_bundle.workflow import packets
from test_review_state import draft_state


class DemonstrationTests(unittest.TestCase):
    def fixture(self):
        state, source, payload = draft_state()
        self.assertTrue(import_drafts(state, payload)["accepted"])
        source = state["sources"][source["id"]]
        concept = next(iter(state["concepts"].values()))
        claim = next(iter(state["claims"].values()))
        return state, source, concept, claim

    def test_pending_draft_exports_without_approvals_and_keeps_wording(self):
        state, source, concept, claim = self.fixture()
        before = digest(state)
        with tempfile.TemporaryDirectory() as temp, patch("socket.create_connection", side_effect=AssertionError("No network")):
            out = Path(temp) / "demo"
            result = build_demonstration(state, out, [concept["id"]])
            root = Path(result["handoff"])
            report = validate_bundle(root / "okf", allowed_domains=["example.com"])
            self.assertTrue(report["core"]["conformant"])
            self.assertEqual([i["rule"] for i in report["issues"]], ["profile.lifecycle"])
            doc = next(p for p in (root / "okf").rglob("*.md") if p.name != "index.md" and p.name != "log.md")
            meta, body = parse_document(doc.read_text(encoding="utf-8"))
            self.assertEqual(meta["status"], "draft")
            self.assertNotIn("verified", meta)
            self.assertNotIn("stale_after", meta)
            self.assertIn(claim["public_statement"], body)
            self.assertEqual(meta["description"], concept["description"])
            self.assertIn("assumed", body)
            self.assertIn("selected draft wording", (root / "okf/log.md").read_text())
            self.assertNotIn("reviewed wording", (root / "okf/log.md").read_text())
            self.assertIn("first publication", (root / "DEPLOYMENT.md").read_text())
            self.assertIn("pre-existing files", (root / "DEPLOYMENT.md").read_text())
            manifest = json.loads((root / "MANIFEST.json").read_text())
            self.assertFalse(manifest["production_ready"])
            for name, expected in manifest["payload"].items():
                self.assertEqual(digest((root / "okf" / name).read_bytes()), expected)
            for name, expected in manifest["private_handoff_files"].items():
                self.assertEqual(digest((root / name).read_bytes()), expected)
            with tarfile.open(root / "okf/okf.tar.gz", "r:gz") as tar:
                members = tar.getmembers()
                markdown = {p.relative_to(root / "okf").as_posix():p.read_bytes() for p in (root / "okf").rglob("*.md")}
                self.assertEqual({m.name for m in members}, set(markdown))
                self.assertTrue(all(m.isfile() and tar.extractfile(m).read() == markdown[m.name] for m in members))
            with zipfile.ZipFile(result["zip"]) as z:
                expected = {"Developer-Handoff-DEMO/"+p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()}
                self.assertEqual(set(z.namelist()),set(expected))
                self.assertTrue(all(z.read(p)==b for p,b in expected.items()))
                self.assertFalse(any("private-state" in n or "provider" in n for n in z.namelist()))
            self.assertEqual(digest(state), before)
            self.assertFalse(state["candidates"])
            self.assertFalse(state["release_approvals"])
            with self.assertRaisesRegex(ValueError, "already exists"):
                build_demonstration(state, out, [concept["id"]])

    def test_excluded_or_conflicting_claims_fail_before_output(self):
        for field, value in [("publication", "withheld"), ("evidence_review", "rejected"),
                             ("business_confirmation", "disputed"), ("conflict", "open")]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                state, source, concept, claim = self.fixture()
                claim[field] = value
                before = digest(state)
                out = Path(temp) / "demo"
                with self.assertRaisesRegex(ValueError, "Excluded or unresolved"):
                    build_demonstration(state, out, [concept["id"]])
                self.assertFalse(out.exists())
                self.assertEqual(digest(state), before)

    def test_explicit_selection_omits_other_languages_and_rejected_concepts(self):
        state, source, concept, claim = self.fixture()
        excluded = copy.deepcopy(concept)
        excluded.update(id="excluded-spanish", locale="es", review={"decision":"rejected"})
        state["concepts"][excluded["id"]] = excluded
        with tempfile.TemporaryDirectory() as temp:
            result = build_demonstration(state, Path(temp) / "demo", [concept["id"]])
            self.assertEqual(result["concepts"], 1)
            self.assertFalse((Path(result["handoff"]) / "okf/es").exists())
            with self.assertRaisesRegex(ValueError, "excluded"):
                build_demonstration(state, Path(temp) / "bad", [excluded["id"]])

    def test_active_identity_scope_quotes_status_and_domains_are_required(self):
        mutations = [lambda s,c: s.update(scope="excluded"), lambda s,c: s.update(identity_confirmed=False),
                     lambda s,c: s.update(availability="failed"), lambda s,c: s.update(collection_state="pending"),
                     lambda s,c: s.update(citation_url="https://outside.example.org/"),
                     lambda s,c: c["evidence"][0].update(quote="Absent quotation"),
                     lambda s,c: c["evidence"][0].update(version_id="old-version"),
                     lambda s,c: s["versions"][s["active_version"]].update(observed_language="es")]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                state, source, concept, claim = self.fixture()
                mutate(source,claim)
                out = Path(temp) / "demo"
                with self.assertRaises(ValueError):build_demonstration(state,out,[concept["id"]])
                self.assertFalse(out.exists())

    def test_invalid_public_link_fails_atomically(self):
        state, source, concept, claim = self.fixture()
        claim["public_statement"] += " [Other](https://outside.example.org/)"
        before = digest(state)
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "demo"
            with self.assertRaisesRegex(ValueError, "scoped format"):
                build_demonstration(state,out,[concept["id"]])
            self.assertFalse(out.exists())
            self.assertEqual(list(Path(temp).iterdir()),[])
        self.assertEqual(digest(state),before)

    def test_empty_duplicate_or_unknown_selection_is_refused(self):
        state, source, concept, claim = self.fixture()
        for ids in ([], [concept["id"],concept["id"]], ["unknown"]):
            with self.subTest(ids=ids), tempfile.TemporaryDirectory() as temp:
                out=Path(temp)/"demo"
                with self.assertRaises(ValueError):build_demonstration(state,out,ids)
                self.assertFalse(out.exists())

    def test_demo_manifest_cannot_be_used_as_a_release_candidate(self):
        from okf_bundle.export import verify_candidate, ReleaseError
        state, source, concept, claim = self.fixture()
        with tempfile.TemporaryDirectory() as temp:
            result=build_demonstration(state,Path(temp)/"demo",[concept["id"]])
            root=Path(result["handoff"])
            self.assertFalse((root/"release-manifest.json").exists())
            # Even relabeling the inventory does not create a supported candidate.
            (root/"release-manifest.json").write_bytes((root/"MANIFEST.json").read_bytes())
            with self.assertRaisesRegex(ReleaseError,"Unsupported candidate manifest"):
                verify_candidate(root)

    def test_packets_prompt_semantic_checks_without_rewriting_source(self):
        state, source, concept, claim = self.fixture()
        with tempfile.TemporaryDirectory() as temp:
            result = packets(state,temp)
            packet=json.loads((Path(temp)/result["packet_files"][0]).read_text())
            self.assertIn("titles, descriptions and tags",packet["instructions"])
            self.assertIn("total session durations",packet["instructions"])
            self.assertIn("do not infer",packet["instructions"])

    def test_production_instructions_keep_both_rollback_cases(self):
        text=_deployment_text({"release_id":"fixture", "changes":{"removed":[]},
            "deployment":{"public_base_path":"/okf/","indexing":"indexable"}, "availability_mode":"offline"}, "").decode()
        self.assertIn("restore the complete prior tree",text)
        self.assertIn("first publication",text)
        self.assertIn("pre-existing files",text)


if __name__ == "__main__":
    unittest.main()
