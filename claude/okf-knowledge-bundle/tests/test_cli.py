"""End-to-end authoring/review/handoff with synthetic humans and mocked Reader."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from okf_bundle.cli import parser, execute
from okf_bundle.common import utc_now
from okf_bundle.state import StateStore

HTML = '<html lang="en"><head><title>Team training</title><link rel="canonical" href="https://example.com/training/"></head><body><main><h1>Team training</h1><p>Training is offered on site for business teams.</p><p>Teams can request a session through the contact page.</p></main></body></html>'


class CLITests(unittest.TestCase):
    def run_route(self, route):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "run"
            config = root / "config.json"
            config.write_text(json.dumps({"domains": ["example.com"], "publication_owner": "human:fixture-client", "collection_authorized": route != "sf", "retry_base_delay": 0}), encoding="utf-8")
            def call(*args):
                return execute(parser().parse_args([*args, "--run", str(run)]))
            call("init", "--config", str(config))
            if route == "sf":
                archive = root / "sf.zip"
                with zipfile.ZipFile(archive, "w") as z:
                    z.writestr("original_https_example.com_training_.html", HTML)
                call("import-sf", "--zip", str(archive))
            elif route == "csv":
                csv = root / "urls.csv"
                csv.write_text("Address\nhttps://example.com/training/\n", encoding="utf-8-sig")
                call("discover-csv", "--csv", str(csv))
            else:
                xml = root / "sitemap.xml"
                xml.write_text('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/training/</loc></url></urlset>', encoding="utf-8")
                call("discover-sitemap", "--sitemap", str(xml))
            if route != "sf":
                def reader(url, settings):
                    return {"provider_status": 200, "data": {"content": HTML, "url": url, "usage": {"tokens": 100}}}
                with patch.dict(os.environ, {"JINA_API_KEY": "fixture-no-network"}), patch("okf_bundle.network.jina_request", reader):
                    call("collect", "--max-pages", "1")
            state = StateStore(run).load()
            source = next(iter(state["sources"].values()))
            by = "human:fixture-analyst"
            at = utc_now()
            provenance = ["--by", by, "--at", at, "--channel", "synthetic-test", "--reference", "fixture-only-not-real-approval"]
            call("source-review", "--id", source["id"], "--url", "https://example.com/training/", "--scope", "included", "--reason", "Synthetic captured identity", *provenance)
            payload = {"batch_id": "fixture-batch", "claims": [{"key": "onsite-delivery", "subject": "Training", "predicate": "delivery", "value": "on site", "qualifiers": {"audience": "business teams"}, "public_statement": "Training is offered on site for business teams.", "evidence": [{"source_id": source["id"], "version_id": source["active_version"], "locator": "body", "quote": "Training is offered on site for business teams."}]}], "concepts": [{"key": "training", "type": "Service", "title": "Team training", "description": "Documented team training delivery.", "locale": "en", "claim_keys": ["onsite-delivery"]}]}
            draft = root / "draft.json"
            draft.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(call("draft-import", "--draft", str(draft))["accepted"])
            self.assertFalse(call("readiness")["ready"])
            state = StateStore(run).load()
            claim = next(iter(state["claims"].values()))
            pointer_before = (run / "private-state/current.json").read_bytes()
            concept_id = next(iter(state["concepts"]))
            demonstration = call("demonstration", "--concept-ids", concept_id, "--out", str(root / "demo"))
            self.assertEqual(demonstration["status"], "unpublished-demonstration")
            self.assertFalse(demonstration["production_ready"])
            self.assertEqual(pointer_before, (run / "private-state/current.json").read_bytes())
            self.assertFalse(StateStore(run).load()["candidates"])
            # This synthetic decision occurs after importing its material revision.
            provenance[provenance.index("--at") + 1] = utc_now()
            preview = call("review-preview", "--concept-ids", concept_id, "--out", str(root / "content-preview.json"))
            with self.assertRaises(ValueError):
                call("review-preview", "--concept-ids", concept_id, "--out", str(root / "content-preview.json"))
            result = call("review-batch", "--preview", preview["preview"], "--preview-hash", preview["preview_hash"], *provenance)
            self.assertEqual(result["business_confirmations_recorded"], 0)
            self.assertEqual(result["release_approvals_recorded"], 0)
            call("availability-accept", "--ids", source["id"], "--reason", "Synthetic offline test acceptance", "--by", "human:fixture-client", "--at", utc_now(), "--channel", "synthetic-test", "--reference", "fixture-only")
            self.assertTrue(call("readiness")["ready"])
            review_files = call("review-export")
            self.assertTrue(Path(review_files["review"]).exists())
            candidate = call("candidate", "--release-id", "fixture-release")
            with self.assertRaises(ValueError):
                call("package", "--candidate", candidate["candidate"], "--out", str(root / "not-approved.zip"))
            call("release-approve", "--candidate", candidate["candidate"], "--manifest-hash", candidate["manifest_hash"], "--by", "human:fixture-client", "--at", utc_now(), "--channel", "synthetic-test", "--reference", "fixture-only")
            result = call("package", "--candidate", candidate["candidate"], "--out", str(root / "handoff.zip"))
            self.assertEqual(result["manifest_hash"], candidate["manifest_hash"])
            with zipfile.ZipFile(root / "handoff.zip") as archive:
                self.assertIn("okf/index.md", archive.namelist())
                self.assertIn("DEPLOYMENT.md", archive.namelist())
                self.assertNotIn("review.xlsx", archive.namelist())
            with self.assertRaises(ValueError):
                call("source-review", "--id", source["id"], "--url", "https://example.com/unrelated/", "--scope", "included", "--reason", "Must not relabel retained evidence", *provenance)

    def test_sf_route_to_reviewed_handoff_offline(self):
        self.run_route("sf")

    def test_csv_route_to_reviewed_handoff_mocked_jina(self):
        self.run_route("csv")

    def test_sitemap_route_to_reviewed_handoff_mocked_jina(self):
        self.run_route("sitemap")


if __name__ == "__main__":
    unittest.main()
