"""Offline release gates plus a localhost-only publication harness.

These tests check structural controls, not an LLM's semantic accuracy.
"""
import copy
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
import tarfile
import unittest
import shutil
import uuid
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from okf_bundle import __version__
from okf_bundle.common import digest
from okf_bundle.availability import check_availability
from okf_bundle.extraction import EXTRACTION_CONTRACT, extract_html
from okf_bundle.export import (ReleaseError, build_candidate, concept_path, deployment_fingerprint,
                               eligibility, load_manifest, manifest_diff, manifest_hash,
                               package_release, rebuild_historical, verify_candidate)
from okf_bundle.publication import verify_publication
from okf_bundle.review import claim_revision, concept_revision
from okf_bundle.validation import SPEC_COMMIT, parse_document, public_url_issue, safe_relative_path, validate_bundle

NOW = "2026-10-15T12:00:00Z"
REVIEWED = "2026-10-01T12:00:00Z"


def rebind(state):
    for claim in state["claims"].values():
        claim["revision"] = claim_revision(claim)
        for event in claim["reviews"]:
            event["revision"] = claim["revision"]
    for concept in state["concepts"].values():
        concept["revision"] = concept_revision(concept, state["claims"])
        concept["review"]["revision"] = concept["revision"]
    return state


def fixture_state():
    config = {"domains": ["example.com"], "locale": "en", "business_profiles": ["b2b"],
              "model": "recorded-host-model", "effort": "medium", "publication_owner": "human:client-owner",
              "public_base_path": "/okf/", "indexing": "indexable", "availability_mode": "offline"}
    text = "Our team provides on-site training for businesses."
    version = {"id": "version-1", "representation": "sf-rendered", "settings": {"extractor": EXTRACTION_CONTRACT}, "locale": "en", "observed_language": "en",
               "content_hash": digest(text.encode()), "text": text, "evidence": {"body": text, "metadata": {}, "schema": []},
               "headings": ["Team training"], "links": [], "imported_at": REVIEWED, "capture_at": None, "warnings": []}
    source = {"id": "source-1", "requested_url": "https://example.com/training/", "normalized_url": "https://example.com/training/",
              "citation_url": "https://example.com/training/", "identity_basis": "analyst-confirmed", "identity_confirmed": True,
              "confirmed_identity_url": "https://example.com/training/",
              "scope": "included", "scope_reason": "Selected service", "classification": "service", "availability": "unknown",
              "availability_acceptance": {"owner": config["publication_owner"], "at": REVIEWED, "reason": "Offline capture-age uncertainty accepted",
                                          "urls": ["https://example.com/training/"], "version_id": "version-1"},
              "captured_status": 200, "canonical": None, "og_url": None, "active_version": "version-1", "versions": {"version-1": version}}
    claim = {"id": "claim-1", "revision": "claim-revision-1", "subject": "Training", "predicate": "delivery", "value": "on-site",
             "material_revision_created_at": REVIEWED,
             "qualifiers": {"audience": "businesses"}, "public_statement": text,
             "evidence": [{"source_id": "source-1", "version_id": "version-1", "locator": "body", "quote": text}],
             "risk_categories": [], "evidence_review": "accepted", "business_confirmation": "not_required", "publication": "approved",
             "conflict": "none", "expires_at": None, "reviews": []}
    for dimension, decision in (("evidence_review", "accepted"), ("publication", "approved")):
        claim["reviews"].append({"dimension": dimension, "decision": decision, "by": "human:analyst@example-agency.com",
                                 "at": REVIEWED, "channel": "workbook", "reference": "signed-import-hash", "revision": claim["revision"]})
    concept = {"id": "concept-1", "key": "team-training", "type": "Service", "title": "Team training",
               "material_revision_created_at": REVIEWED,
               "description": "Training options for businesses.", "resource": "https://example.com/training/", "tags": ["training"],
               "locale": "en", "claim_ids": ["claim-1"], "revision": "concept-revision-1", "status": "stable", "replacement": None,
               "generated_at": REVIEWED, "review": {"decision": "accepted", "by": "human:analyst@example-agency.com", "at": REVIEWED,
                                                     "channel": "workbook", "reference": "concept-import-hash", "revision": "concept-revision-1"}}
    state = {"schema_version": 1, "tool_version": __version__, "run_id": "run-fixture", "config": config, "config_hash": digest(config),
            "inventory": [], "sources": {"source-1": source}, "claims": {"claim-1": claim}, "concepts": {"concept-1": concept},
            "review_history": [], "candidates": {}, "release_approvals": {}, "created_at": REVIEWED}
    for event in claim["reviews"]:
        event["by"] = "human:analyst"
    concept["review"]["by"] = "human:analyst"
    return rebind(state)


def approve(state, manifest):
    key = manifest_hash(manifest)
    state["release_approvals"][key] = {"manifest_hash": key, "owner": state["config"]["publication_owner"], "at": NOW,
                                      "config_hash": state["config_hash"], "deployment_fingerprint": deployment_fingerprint(state),
                                      "channel": "client-message", "reference": "explicit-release-approval"}


def observed_live(source):
    source["availability"] = "live"
    source["availability_check"] = {"status": 200, "at": NOW, "url": source["citation_url"],
                                    "version_id": source["active_version"], "quotes_supported": True,
                                    "supersedes_captured_failure": True, "requires_source_refresh": False,
                                    "response_hash": digest(b"synthetic observed response"),
                                    "observed_final_url": source["citation_url"]}


class ExportTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[3] / "work" / "export-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.scratch = scratch.resolve()
        self.root = scratch / uuid.uuid4().hex
        self.root.mkdir()
        self.state = fixture_state()
        self.candidate_paths = {}

    def tearDown(self):
        if self.root.resolve().parent != self.scratch or self.root.is_symlink():
            raise RuntimeError("Refusing test cleanup outside scratch directory")
        shutil.rmtree(self.root)

    def build(self, name="candidate", release_id="release-001", previous=None):
        path = self.root / name
        manifest = build_candidate(self.state, path, release_id, NOW, previous,
                                   previous_candidate=self.candidate_paths.get(digest(previous)) if previous else None)
        self.candidate_paths[digest(manifest)] = path
        return path, manifest

    def probe_source(self, representation="sf-original", old_extra=""):
        source = self.state["sources"]["source-1"]
        quote = self.state["claims"]["claim-1"]["evidence"][0]["quote"]
        raw = ('<html lang="en"><head><title>Training</title></head><body><main><p>' + quote + '</p></main></body></html>').encode()
        version = source["versions"][source["active_version"]]
        version.update(representation=representation, evidence=extract_html(raw, source["citation_url"])["evidence"])
        if old_extra:
            version["evidence"]["body"] += "\n" + old_extra
        return source, raw

    def probe(self, source, raw, final=None):
        with patch("okf_bundle.availability.utc_now", return_value=NOW):
            return check_availability(self.state, [source["id"]], fetch=lambda url: {"status": 200, "body": raw, "url": final or url})

    def test_core_accepts_minimal_unknown_type_extension_and_broken_link(self):
        (self.root / "thing.md").write_text("---\ntype: Unregistered Type\ncustom: {anything: true}\n---\n[future](missing.md)\n", encoding="utf-8")
        report = validate_bundle(self.root, profile=False)
        self.assertTrue(report["core"]["conformant"])
        self.assertEqual(report["semantic_review"], "not_checked")
        self.assertFalse(validate_bundle(self.root)["profile"]["ready"])

    def test_nonstring_empty_type_and_duplicate_yaml_rejected(self):
        for raw in ("type: []", "type: {}", "type: ''", "type: Service\ntype: Product"):
            with self.subTest(raw=raw):
                (self.root / "thing.md").write_text("---\n" + raw + "\n---\nBody", encoding="utf-8")
                self.assertFalse(validate_bundle(self.root, False)["core"]["conformant"])

    def test_reserved_index_and_invalid_calendar_log(self):
        (self.root / "index.md").write_text("---\nokf_version: [\n---\n# Index", encoding="utf-8")
        self.assertFalse(validate_bundle(self.root, False)["core"]["conformant"])
        (self.root / "index.md").write_text("# Index", encoding="utf-8")
        (self.root / "log.md").write_text("# History\n## 2026-02-30\n- Change", encoding="utf-8")
        self.assertFalse(validate_bundle(self.root, False)["core"]["conformant"])

    def test_generated_citation_does_not_modify_reference_definition(self):
        statement = "Our team provides on-site training for businesses. [Details][details]\n\n[details]: index.md"
        self.state["claims"]["claim-1"]["public_statement"] = statement
        rebind(self.state)
        path, manifest = self.build()
        concept = next((path / "okf").rglob("team-training-*.md"))
        _, body = parse_document(concept.read_text(encoding="utf-8"))
        self.assertIn(statement + "\n\n[^", body)
        self.assertIn("[details]: index.md\n", body)
        self.assertNotIn("[details]: index.md[^", body)
        self.assertTrue(validate_bundle(path / "okf", allowed_domains=["example.com"])["profile"]["ready"])

    def test_frozen_upstream_spec_and_ready_fixture(self):
        self.assertEqual(SPEC_COMMIT, "ad30107c31c06aec8a7d5636e0d1058118604e6f")
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_candidate_is_reproducible_and_archive_excludes_itself(self):
        one, first = self.build("one")
        two, second = self.build("two")
        self.assertEqual(first, second)
        self.assertEqual((one / "okf/okf.tar.gz").read_bytes(), (two / "okf/okf.tar.gz").read_bytes())
        archive_bytes = (one / "okf/okf.tar.gz").read_bytes()
        self.assertEqual(archive_bytes[4:8], b"\x00\x00\x00\x00")
        self.assertEqual(archive_bytes[3] & 8, 0)  # No original filename in gzip header.
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            self.assertNotIn("okf.tar.gz", archive.getnames())
            self.assertIn("index.md", archive.getnames())
            self.assertTrue(all(m.mtime == 0 and m.uid == 0 and m.gid == 0 for m in archive.getmembers()))
        self.assertTrue(validate_bundle(one / "okf", allowed_domains=["example.com"])["profile"]["ready"])
        with self.assertRaises(ReleaseError):
            self.build("one")

    def test_public_export_uses_nested_allowlist_and_opaque_earned_actor(self):
        self.state["sources"]["source-1"]["private_analytics"] = {"revenue": "private-only"}
        self.state["sources"]["source-1"]["author"] = "private-email@example.com"
        self.state["claims"]["claim-1"]["internal_comment"] = "do-not-export"
        root, _ = self.build()
        text = next((root / "okf/en/services").glob("team*.md")).read_text(encoding="utf-8")
        meta, _ = parse_document(text)
        self.assertEqual(set(meta["sources"][0]), {"id", "resource"})
        self.assertEqual(set(meta["generated"]), {"by", "at"})
        self.assertEqual(set(meta["verified"][0]), {"by", "at"})
        self.assertTrue(meta["verified"][0]["by"].startswith("human:reviewer-"))
        for private in ("example-agency.com", "private-only", "private-email", "do-not-export"):
            self.assertNotIn(private, text)

    def test_model_or_publication_approval_does_not_earn_human_verification(self):
        self.state["claims"]["claim-1"]["reviews"][0]["by"] = "model/sonnet"
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        with self.assertRaises(ReleaseError):
            self.build()

    def test_distributed_claim_review_is_eligible_without_invented_verifier(self):
        self.state["concepts"]["concept-1"]["review"]["by"] = "human:different-reviewer"
        self.assertTrue(eligibility(self.state, NOW)["ready"])
        root, _ = self.build()
        path = next((root / "okf/en/services").glob("team*.md"))
        meta, _ = parse_document(path.read_text(encoding="utf-8"))
        self.assertNotIn("verified", meta)

    def test_material_tampering_with_stale_revision_is_rejected(self):
        self.state["claims"]["claim-1"]["public_statement"] = "A changed, unreviewed claim."
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        self.state = fixture_state()
        self.state["concepts"]["concept-1"]["title"] = "Unreviewed title"
        self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_quote_rechecked_at_release_gate(self):
        self.state["claims"]["claim-1"]["evidence"][0]["quote"] = "Invented evidence"
        report = eligibility(self.state, NOW)
        self.assertFalse(report["ready"])
        self.assertTrue(any("quotation" in issue for issue in report["claims"]["claim-1"]["issues"]))

    def test_citation_cannot_relabel_captured_evidence(self):
        source = self.state["sources"]["source-1"]
        source["citation_url"] = "https://example.com/unrelated-page/"
        source["availability_acceptance"]["urls"] = [source["citation_url"]]
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        source.pop("confirmed_identity_url")
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        source.update(identity_basis="supplied URL", citation_url=source["normalized_url"])
        source["availability_acceptance"]["urls"] = [source["citation_url"]]
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_representation_version_change_invalidates_eligibility(self):
        source = self.state["sources"]["source-1"]
        source["versions"]["version-2"] = copy.deepcopy(source["versions"]["version-1"])
        source["active_version"] = "version-2"
        self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_unknown_availability_acceptance_does_not_waive_failure(self):
        source = self.state["sources"]["source-1"]
        for availability in ("failed", "nonpublic"):
            source["availability"] = availability
            self.assertFalse(eligibility(self.state, NOW)["ready"])
        source["availability"] = "unknown"
        source["availability_acceptance"]["version_id"] = "other"
        self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_known_failed_capture_cannot_be_waived_as_unknown(self):
        source = self.state["sources"]["source-1"]
        for status in (401, 403, 404, 410, 429, 500, 502):
            with self.subTest(status=status):
                source["captured_status"] = status
                self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_partial_release_requires_explicit_exact_scope_acceptance(self):
        other = copy.deepcopy(self.state["concepts"]["concept-1"])
        other.update(id="concept-2", key="other-training")
        other["review"]["decision"] = "pending"
        self.state["concepts"]["concept-2"] = other
        rebind(self.state)
        target = self.root / "partial"
        with self.assertRaises(ReleaseError):
            build_candidate(self.state, target, "partial", NOW, allow_partial=True)
        self.state["config"]["partial_release_acceptance"] = {
            "owner": "human:client-owner", "at": NOW, "reason": "Publish reviewed training scope only",
            "concept_ids": ["concept-1"]}
        self.state["config_hash"] = digest(self.state["config"])
        with self.assertRaises(ReleaseError):
            build_candidate(self.state, target, "partial", NOW)
        manifest = build_candidate(self.state, target, "partial", NOW, allow_partial=True)
        self.assertTrue(manifest["partial"])
        self.assertEqual(manifest["withheld_concepts"], ["concept-2"])
        approve(self.state, manifest)
        package_release(self.state, target, self.root / "partial.zip", NOW)

    def test_captured_status_is_not_checked_live(self):
        source = self.state["sources"]["source-1"]
        source["availability"] = "live"
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        observed_live(source)
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_redirected_identical_content_needs_identity_review_and_cannot_be_waived(self):
        source, raw = self.probe_source()
        original = source["citation_url"]
        self.probe(source, raw, "https://example.com/new-training/")
        self.assertTrue(source["availability_check"]["redirected"])
        self.assertTrue(source["redirect_review_required"])
        self.assertEqual(source["availability"], "unknown")
        self.assertEqual(source["citation_url"], original)
        source["availability_acceptance"] = {"owner": self.state["config"]["publication_owner"], "at": NOW,
                                             "reason": "Cannot waive a known redirect", "urls": [original], "version_id": "version-1"}
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        # Actual same-URL recovery, rather than an enum/acceptance change, clears it.
        self.probe(source, raw)
        self.assertNotIn("redirect_review_required", source)
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_directly_forged_live_enum_does_not_hide_redirected_final_url(self):
        source = self.state["sources"]["source-1"]
        observed_live(source)
        source["availability_check"]["observed_final_url"] = "https://example.com/new-training/"
        self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_cross_representation_drift_preserves_reviews_but_needs_actual_comparison(self):
        for representation in ("sf-rendered", "jina-dom"):
            with self.subTest(representation=representation):
                self.state = fixture_state()
                source, raw = self.probe_source(representation, "Rendered-only widget text.")
                before = copy.deepcopy(self.state["claims"]["claim-1"]["reviews"])
                self.probe(source, raw)
                check = source["availability_check"]
                self.assertTrue(check["quotes_supported"])
                self.assertTrue(check["comparison_inconclusive"])
                self.assertFalse(check["requires_source_refresh"])
                self.assertIsNone(check["current_body_changed"])
                self.assertEqual(self.state["claims"]["claim-1"]["reviews"], before)
                self.assertFalse(eligibility(self.state, NOW)["ready"])
                source["availability_comparison_review"] = {
                    "by": "human:analyst", "at": NOW, "channel": "synthetic-review", "reference": "reviewed-comparison",
                    "reason": "Synthetic body difference is a presentation-only widget; no changed applicability",
                    "version_id": source["active_version"], "response_hash": check["response_hash"], "decision": "accepted"}
                self.assertTrue(eligibility(self.state, NOW)["ready"])
                with patch("okf_bundle.availability.utc_now", return_value="2026-10-15T13:00:00Z"):
                    check_availability(self.state, [source["id"]], fetch=lambda url: {"status": 200, "body": raw, "url": url})
                self.assertEqual(source["availability_check"]["first_observed_at"], NOW)
                self.assertTrue(eligibility(self.state, "2026-10-15T13:00:00Z")["ready"])
                source["availability_comparison_review"]["response_hash"] = digest(b"different response")
                self.assertFalse(eligibility(self.state, NOW)["ready"])
                source["availability"] = "unknown"
                source["availability_acceptance"] = {"owner": self.state["config"]["publication_owner"], "at": NOW,
                                                     "reason": "Unknown availability", "urls": [source["citation_url"]], "version_id": "version-1"}
                self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_cross_representation_missing_quote_or_metadata_change_invalidates_reviews(self):
        for changed in (b"Training no longer offered for business teams and replacement evidence is required.",
                        b"Our team provides on-site training for businesses.</p><p>A new condition applies."):
            with self.subTest(changed=changed):
                self.state = fixture_state()
                source, raw = self.probe_source("sf-rendered")
                if changed.startswith(b"Our"):
                    raw = raw.replace(b"<title>Training</title>", b"<title>New policy</title>")
                else:
                    raw = raw.replace(b"Our team provides on-site training for businesses.", changed)
                self.probe(source, raw)
                self.assertTrue(source["availability_check"]["requires_source_refresh"])
                self.assertEqual(self.state["claims"]["claim-1"]["evidence_review"], "pending")
                self.assertEqual(self.state["claims"]["claim-1"]["reviews"], [])

    def test_comparable_raw_body_change_requires_refresh_even_if_quote_survives(self):
        source, raw = self.probe_source()
        raw = raw.replace(b"</main>", b"<p>However, new customers are excluded.</p></main>")
        self.probe(source, raw)
        self.assertTrue(source["availability_check"]["quotes_supported"])
        self.assertTrue(source["availability_check"]["current_body_changed"])
        self.assertTrue(source["availability_check"]["requires_source_refresh"])
        self.assertEqual(self.state["claims"]["claim-1"]["evidence_review"], "pending")

    def test_pending_collection_blocks_source_and_alias_target(self):
        source = self.state["sources"]["source-1"]
        source["collection_state"] = "pending"
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        source["collection_state"] = "complete"
        alias = copy.deepcopy(source)
        alias.update(id="alias-target", collection_state="pending")
        self.state["sources"]["alias-target"] = alias
        source["alias_of"] = "alias-target"
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        alias["collection_state"] = "complete"
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_old_extraction_contract_blocks_current_release_and_alias_but_not_historical_copy(self):
        candidate, _ = self.build("extraction-current")
        source = self.state["sources"]["source-1"]
        version = source["versions"][source["active_version"]]
        for settings in ({}, None, ["html-v2"], {"extractor": "html-v1"}):
            with self.subTest(settings=settings):
                version["settings"] = settings
                self.assertFalse(eligibility(self.state, NOW)["ready"])
        result = rebuild_historical(candidate, self.root / "extraction-history")
        self.assertTrue(result["historical_only"])
        self.assertEqual((candidate / "okf/okf.tar.gz").read_bytes(),
                         (self.root / "extraction-history/okf/okf.tar.gz").read_bytes())
        version["settings"] = {"extractor": EXTRACTION_CONTRACT}
        alias = copy.deepcopy(source)
        alias["id"] = "old-alias"
        alias["versions"][alias["active_version"]]["settings"] = {}
        self.state["sources"][alias["id"]] = alias
        source["alias_of"] = alias["id"]
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        alias["versions"][alias["active_version"]]["settings"] = {"extractor": EXTRACTION_CONTRACT}
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_ambiguous_companion_csv_cannot_be_waived_by_scope_inclusion(self):
        source = self.state["sources"]["source-1"]
        source["csv_metadata_review_required"] = True
        source["inclusion_decision"] = {"by": "human:analyst", "at": NOW, "channel": "synthetic-review",
                                        "reference": "source-scope", "reason": "A scope decision cannot bind an ambiguous HTTP result",
                                        "version_id": source["active_version"], "decision": "included"}
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        source["csv_metadata_review_required"] = False
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_reviews_cannot_predate_material_revision_or_missing_legacy_boundary(self):
        for category, record_id in (("claims", "claim-1"), ("concepts", "concept-1")):
            for boundary in (None, "2026-10-02T00:00:00Z", "bad-time", "2027-01-01T00:00:00Z"):
                with self.subTest(category=category, boundary=boundary):
                    self.state = fixture_state()
                    self.state[category][record_id]["material_revision_created_at"] = boundary
                    self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_resolved_conflict_needs_current_human_resolution_rationale(self):
        claim = self.state["claims"]["claim-1"]
        claim["conflict"] = "resolved"
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        event = {"dimension": "conflict", "decision": "resolved", "by": "human:analyst", "at": NOW,
                 "channel": "synthetic-review", "reference": "conflict-evidence", "revision": claim["revision"],
                 "reason": "The two statements concern separate audiences; the claim preserves its business-only qualifier"}
        claim["reviews"].append(event)
        self.assertTrue(eligibility(self.state, NOW)["ready"])
        for key, value in (("reason", ""), ("reason", "A reason\nwith a hidden extra line"), ("revision", "old"), ("at", "2026-09-01T00:00:00Z")):
            saved = event[key]
            event[key] = value
            self.assertFalse(eligibility(self.state, NOW)["ready"])
            event[key] = saved

    def test_failed_capture_needs_fresh_observed_recovery_not_a_waiver(self):
        source = self.state["sources"]["source-1"]
        source["captured_status"] = 503
        observed_live(source)
        report = eligibility(self.state, NOW)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["availability_mode"], "checked-live")
        for key, value in (("quotes_supported", False), ("supersedes_captured_failure", False),
                           ("at", REVIEWED), ("version_id", "other-version"), ("url", "https://example.com/elsewhere/"),
                           ("observed_final_url", "http://127.0.0.1/"), ("response_hash", "")):
            with self.subTest(key=key):
                observed_live(source)
                source["availability_check"][key] = value
                self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_current_probe_change_requires_retained_new_source_version(self):
        source = self.state["sources"]["source-1"]
        observed_live(source)
        source["availability_check"]["requires_source_refresh"] = True
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        source["availability"] = "unknown"
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        source["versions"]["version-2"] = {**source["versions"]["version-1"], "id": "version-2"}
        source["active_version"] = "version-2"
        source["availability_acceptance"]["version_id"] = "version-2"
        self.state["claims"]["claim-1"]["evidence"][0]["version_id"] = "version-2"
        rebind(self.state)
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_noindex_and_canonical_away_need_explicit_current_inclusion(self):
        for field, value in (("robots", "noindex, follow"), ("robots", "none"),
                             ("csv_indexability", "Non-Indexable"),
                             ("reader_warning_review_required", True),
                             ("canonical", "https://example.com/other/")):
            with self.subTest(field=field, value=value):
                self.state = fixture_state()
                source = self.state["sources"]["source-1"]
                source[field] = value
                self.assertFalse(eligibility(self.state, NOW)["ready"])
                source["inclusion_decision"] = {"by": "human:analyst", "at": NOW, "channel": "workbook",
                                                "reference": "source-scope-review", "reason": "Relevant public evidence despite indexing preference",
                                                "version_id": "version-1", "decision": "included"}
                self.assertTrue(eligibility(self.state, NOW)["ready"])
                source["inclusion_decision"]["version_id"] = "old-version"
                self.assertFalse(eligibility(self.state, NOW)["ready"])
        self.state = fixture_state()
        self.state["sources"]["source-1"]["canonical"] = "/training/"
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_availability_mode_uses_included_observations_not_configuration_label(self):
        second = copy.deepcopy(self.state["sources"]["source-1"])
        second.update(id="source-2", requested_url="https://example.com/other/", normalized_url="https://example.com/other/",
                      citation_url="https://example.com/other/", confirmed_identity_url="https://example.com/other/")
        second["availability_acceptance"]["urls"] = [second["citation_url"]]
        self.state["sources"]["source-2"] = second
        claim = copy.deepcopy(self.state["claims"]["claim-1"])
        claim["id"] = "claim-2"
        claim["evidence"][0]["source_id"] = "source-2"
        self.state["claims"]["claim-2"] = claim
        self.state["concepts"]["concept-1"]["claim_ids"].append("claim-2")
        rebind(self.state)
        observed_live(self.state["sources"]["source-1"])
        report = eligibility(self.state, NOW)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["availability_mode"], "mixed-live/offline-with-availability-acceptance")
        observed_live(second)
        self.assertEqual(eligibility(self.state, NOW)["availability_mode"], "checked-live")

    def test_freshness_uses_earliest_claim_deadline_not_build_time(self):
        second = copy.deepcopy(self.state["claims"]["claim-1"])
        second["id"] = "claim-2"
        second["expires_at"] = "2026-10-16T00:00:00Z"
        for event in second["reviews"]:
            event["at"] = "2026-10-14T12:00:00Z"
        self.state["claims"]["claim-2"] = second
        self.state["concepts"]["concept-1"]["claim_ids"].append("claim-2")
        rebind(self.state)
        report = eligibility(self.state, NOW)
        self.assertEqual(report["concepts"]["concept-1"]["stale_after"], "2026-10-16T00:00:00Z")
        self.assertFalse(eligibility(self.state, "2026-10-16T00:00:00Z")["ready"])

    def test_business_confirmation_deadline_can_be_earlier_than_evidence(self):
        claim = self.state["claims"]["claim-1"]
        claim["business_confirmation"] = "confirmed"
        claim["risk_categories"] = ["credential"]
        claim["reviews"].append({"dimension": "business_confirmation", "decision": "confirmed", "by": "human:client-owner",
                                  "at": "2026-06-01T12:00:00Z", "channel": "email", "reference": "confirmation", "revision": claim["revision"]})
        self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_volatile_claim_needs_current_maintenance_contract(self):
        claim = self.state["claims"]["claim-1"]
        claim.update(risk_categories=["volatile"], business_confirmation="confirmed")
        claim["reviews"].append({"dimension": "business_confirmation", "decision": "confirmed", "by": "human:client-owner",
                                  "at": REVIEWED, "channel": "message", "reference": "client-current-fact", "revision": claim["revision"]})
        rebind(self.state)
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        policy = {"owner": "human:maintenance-owner", "method": "Review source and client inventory each month",
                  "interval_days": 30, "reason": "Agreed refresh process", "revision": claim["revision"]}
        self.state["config"]["claim_maintenance"] = {"claim-1": policy}
        self.state["config_hash"] = digest(self.state["config"])
        report = eligibility(self.state, NOW)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["claims"]["claim-1"]["stale_after"], "2026-10-31T12:00:00Z")
        policy["interval_days"] = 7
        self.state["config_hash"] = digest(self.state["config"])
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        policy.update(interval_days=30, revision="superseded-revision")
        self.state["config_hash"] = digest(self.state["config"])
        self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_fixed_price_requires_currency_and_offer_context(self):
        claim = self.state["claims"]["claim-1"]
        claim.update(risk_categories=["price"], business_confirmation="confirmed", value=100)
        claim["reviews"].append({"dimension": "business_confirmation", "decision": "confirmed", "by": "human:client-owner",
                                  "at": REVIEWED, "channel": "message", "reference": "client-price", "revision": claim["revision"]})
        rebind(self.state)
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        claim["qualifiers"]["currency"] = "USD"
        rebind(self.state)
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        claim["qualifiers"]["offer_context"] = "Per team session; public standard offer"
        rebind(self.state)
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_price_keyword_without_amount_does_not_invent_currency(self):
        claim = self.state["claims"]["claim-1"]
        claim.update(risk_categories=["price"], business_confirmation="confirmed")
        claim["reviews"].append({"dimension": "business_confirmation", "decision": "confirmed", "by": "human:client-owner",
                                  "at": REVIEWED, "channel": "message", "reference": "client-price-wording", "revision": claim["revision"]})
        for text in ("Contact us to request a price.", "There is no fee to reschedule."):
            with self.subTest(text=text):
                claim.update(value=text, public_statement=text)
                rebind(self.state)
                self.assertTrue(eligibility(self.state, NOW)["ready"])
        claim.update(value="USD 100", public_statement="Training is USD 100 per team.")
        rebind(self.state)
        self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_business_confirmation_requires_designated_client_reviewer(self):
        claim = self.state["claims"]["claim-1"]
        claim["business_confirmation"] = "confirmed"
        claim["reviews"].append({"dimension": "business_confirmation", "decision": "confirmed", "by": "human:other-client",
                                  "at": REVIEWED, "channel": "message", "reference": "actual-message", "revision": claim["revision"]})
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        self.state["config"]["business_reviewers"] = ["human:other-client"]
        self.state["config_hash"] = digest(self.state["config"])
        self.assertTrue(eligibility(self.state, NOW)["ready"])

    def test_language_mismatch_blocks_and_unobserved_language_is_reported(self):
        version = self.state["sources"]["source-1"]["versions"]["version-1"]
        version["observed_language"] = "es"
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        version["observed_language"] = None
        report = eligibility(self.state, NOW)
        self.assertTrue(report["ready"])
        self.assertTrue(any("unobserved" in i["message"] for i in report["warnings"]))
        version["observed_language"] = "en-US"
        report = eligibility(self.state, NOW)
        self.assertTrue(report["ready"])
        self.assertTrue(any("regional" in i["message"] for i in report["warnings"]))

    def test_conflict_or_private_correction_is_not_publishable(self):
        claim = self.state["claims"]["claim-1"]
        claim["conflict"] = "open"
        self.assertFalse(eligibility(self.state, NOW)["ready"])
        claim["conflict"] = "resolved"
        claim["evidence"] = []
        self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_package_requires_exact_manifest_owner_approval(self):
        root, manifest = self.build()
        with self.assertRaises(ReleaseError):
            package_release(self.state, root, self.root / "release.zip", NOW)
        approve(self.state, manifest)
        result = package_release(self.state, root, self.root / "release.zip", NOW)
        self.assertEqual(result["manifest_hash"], manifest_hash(manifest))
        second = package_release(self.state, root, self.root / "release2.zip", NOW)
        self.assertEqual(result["sha256"], second["sha256"])

    def test_any_candidate_byte_change_prevents_packaging(self):
        root, manifest = self.build()
        approve(self.state, manifest)
        with (root / "okf/log.md").open("a", encoding="utf-8") as handle:
            handle.write("Unreviewed addition\n")
        with self.assertRaises(ReleaseError):
            package_release(self.state, root, self.root / "release.zip", NOW)

    def test_packaging_and_historical_copy_cannot_mutate_candidate(self):
        root, manifest = self.build()
        approve(self.state, manifest)
        with self.assertRaises(ReleaseError):
            package_release(self.state, root, root / "release.zip", NOW)
        with self.assertRaises(ReleaseError):
            rebuild_historical(root, root / "history")
        self.assertEqual(verify_candidate(root), manifest)

    def test_manifest_cannot_add_private_handoff_attachment(self):
        root, manifest = self.build()
        (root / "private-state.json").write_bytes(b"private")
        manifest["handoff_files"]["private-state.json"] = digest(b"private")
        (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        approve(self.state, manifest)
        with self.assertRaises(ReleaseError):
            package_release(self.state, root, self.root / "release.zip", NOW)

    def test_policy_or_source_change_invalidates_approval(self):
        root, manifest = self.build()
        approve(self.state, manifest)
        self.state["config"]["public_base_path"] = "/knowledge/"
        self.state["config_hash"] = digest(self.state["config"])
        with self.assertRaises(ReleaseError):
            package_release(self.state, root, self.root / "release.zip", NOW)

    def test_historical_rebuild_is_identical_but_expired_current_package_fails(self):
        root, manifest = self.build()
        approve(self.state, manifest)
        result = rebuild_historical(root, self.root / "history")
        self.assertTrue(result["historical_only"])
        self.assertEqual(load_manifest(self.root / "history"), manifest)
        self.assertEqual((root / "okf/okf.tar.gz").read_bytes(), (self.root / "history/okf/okf.tar.gz").read_bytes())
        with self.assertRaises(ReleaseError):
            package_release(self.state, root, self.root / "expired.zip", "2027-12-01T00:00:00Z")

    def test_nested_allowlist_and_leading_slash_profile_checks(self):
        root, _ = self.build()
        path = next((root / "okf/en/services").glob("team*.md"))
        text = path.read_text(encoding="utf-8").replace("sources:\n", "sources:\n")
        text = text.replace("  resource: https://example.com/training/", "  resource: https://example.com/training/\n  usage_count: 100")
        text += "\n[Wrong root](/en/services/not-real.md)\n"
        path.write_text(text, encoding="utf-8")
        report = validate_bundle(root / "okf")
        self.assertTrue(report["core"]["conformant"])
        rules = {i["rule"] for i in report["issues"]}
        self.assertIn("profile.sources.allowlist", rules)
        self.assertIn("profile.link.absolute", rules)

    def test_stable_key_survives_title_edits_and_reserved_key_fails(self):
        concept = self.state["concepts"]["concept-1"]
        initial = concept_path(concept, self.state["config"])
        concept["title"] = "New reviewed name"
        self.assertEqual(initial, concept_path(concept, self.state["config"]))
        concept["key"] = "CON"
        with self.assertRaises(ValueError):
            concept_path(concept, self.state["config"])

    def test_manifest_paths_reject_encoded_traversal_and_url_ambiguity(self):
        for value in (".", "en//services/a.md", "en/./a.md", "../a.md", "%2e%2e/private.md",
                      "en/a.md#fragment", "en/a.md?token=private", "en/a\n.md", "en/CON.md"):
            with self.subTest(value=value):
                self.assertFalse(safe_relative_path(value))
        self.assertTrue(safe_relative_path("en/services/team-1234.md"))

    def test_secret_and_unapproved_urls_are_not_public_sources(self):
        for url in ("https://example.com/?token=private", "https://user:pass@example.com/", "http://127.0.0.1/", "https://staging.example.com/"):
            with self.subTest(url=url):
                self.assertIsNotNone(public_url_issue(url, ["example.com"]))

    def test_profile_rejects_bare_private_url_and_malformed_optional_shapes(self):
        root, _ = self.build()
        path = next((root / "okf/en/services").glob("team*.md"))
        original = path.read_text(encoding="utf-8")
        for extra in ("\nSee <http://127.0.0.1/private>.\n", "\nhttps://example.com/?token=private\n"):
            path.write_text(original + extra, encoding="utf-8")
            report = validate_bundle(root / "okf", allowed_domains=["example.com"])
            self.assertTrue(report["core"]["conformant"])
            self.assertIn("profile.url", {i["rule"] for i in report["issues"]})
        path.write_text(original.replace("status: stable", "status: []"), encoding="utf-8")
        report = validate_bundle(root / "okf")
        self.assertTrue(report["core"]["conformant"])
        self.assertFalse(report["profile"]["ready"])

    def test_profile_rejects_html_event_handlers_without_rejecting_url_autolinks(self):
        root, _ = self.build()
        path = next((root / "okf/en/services").glob("team*.md"))
        original = path.read_text(encoding="utf-8")
        path.write_text(original + '\n<img src=x onerror="alert(1)">\n', encoding="utf-8")
        self.assertIn("profile.html", {i["rule"] for i in validate_bundle(root / "okf")["issues"]})
        path.write_text(original + "\n<https://example.com/training/>\n", encoding="utf-8")
        self.assertTrue(validate_bundle(root / "okf", allowed_domains=["example.com"])["profile"]["ready"])

    def test_log_records_linked_concept_creation_update_and_deprecation(self):
        root, old = self.build("created")
        self.assertIn("**Creation**: [Team training](en/services/", (root / "okf/log.md").read_text(encoding="utf-8"))
        self.state["concepts"]["concept-1"]["title"] = "Reviewed team training"
        rebind(self.state)
        root, changed = self.build("updated", previous=old)
        self.assertIn("**Update**: [Reviewed team training]", (root / "okf/log.md").read_text(encoding="utf-8"))
        self.state["concepts"]["concept-1"].update(status="deprecated", claim_ids=[])
        rebind(self.state)
        root, _ = self.build("deprecated", previous=changed)
        self.assertIn("**Deprecation**: [Reviewed team training]", (root / "okf/log.md").read_text(encoding="utf-8"))
        history = (root / "okf/log.md").read_text(encoding="utf-8")
        self.assertIn("**Creation**: [Team training]", history)
        self.assertIn("**Update**: [Reviewed team training]", history)
        self.assertEqual(history.count("## 2026-10-15"), 1)

    def test_previous_candidate_preserves_date_groups_and_rebuild_is_deterministic(self):
        previous = self.root / "history-before"
        before = build_candidate(self.state, previous, "release-before", "2026-10-14T12:00:00Z")
        first, second = self.root / "history-one", self.root / "history-two"
        one = build_candidate(self.state, first, "release-after", NOW, previous_candidate=previous)
        two = build_candidate(self.state, second, "release-after", NOW, previous_manifest=before, previous_candidate=previous)
        self.assertEqual(one, two)
        history = (first / "okf/log.md").read_text(encoding="utf-8")
        self.assertLess(history.index("## 2026-10-15"), history.index("## 2026-10-14"))
        self.assertIn("Release: release-before.", history)
        self.assertIn("Release: release-after.", history)
        self.assertEqual((first / "okf/okf.tar.gz").read_bytes(), (second / "okf/okf.tar.gz").read_bytes())

    def test_history_requires_matching_verified_candidate_and_never_silently_truncates(self):
        previous, manifest = self.build("prior-history")
        with self.assertRaisesRegex(ReleaseError, "cannot reconstruct"):
            build_candidate(self.state, self.root / "manifest-only", "new", NOW, previous_manifest=manifest)
        wrong = copy.deepcopy(manifest)
        wrong["release_id"] = "different"
        with self.assertRaisesRegex(ReleaseError, "does not match"):
            build_candidate(self.state, self.root / "mismatch", "new", NOW, previous_manifest=wrong, previous_candidate=previous)
        old_length = len((previous / "okf/log.md").read_bytes())
        with patch("okf_bundle.export.MAX_PUBLIC_LOG_BYTES", old_length + 5):
            with self.assertRaisesRegex(ReleaseError, "never silent truncation"):
                build_candidate(self.state, self.root / "bounded", "new", NOW, previous_candidate=previous)
        (previous / "okf/log.md").write_text("# Bundle change log\n\n## 2026-10-15\n\nForged history", encoding="utf-8")
        with self.assertRaises(ReleaseError):
            build_candidate(self.state, self.root / "tampered", "new", NOW, previous_candidate=previous)

    def test_generated_titles_with_brackets_are_literal_in_indexes_and_history(self):
        self.state["concepts"]["concept-1"]["title"] = "Training [business teams]"
        rebind(self.state)
        root, _ = self.build("bracket-title")
        for name in ("okf/en/services/index.md", "okf/log.md"):
            self.assertIn(r"Training \[business teams\]", (root / name).read_text(encoding="utf-8"))
        self.assertTrue(validate_bundle(root / "okf")["profile"]["ready"])

    def test_reserved_log_has_no_frontmatter_and_newest_dates_first(self):
        path = self.root / "log.md"
        for raw in ("---\ntype: Service\n---\n# Log\n## 2026-01-01\n- Change", "# Log\n## 2026-01-01\n- Old\n## 2026-02-01\n- New"):
            path.write_text(raw, encoding="utf-8")
            self.assertFalse(validate_bundle(self.root, profile=False)["core"]["conformant"])

    def test_removed_paths_are_explicit_in_handoff_and_diff(self):
        retired = copy.deepcopy(self.state["concepts"]["concept-1"])
        retired.update(id="concept-retired", key="retired", title="Retired training")
        self.state["concepts"][retired["id"]] = retired
        rebind(self.state)
        retired_path = concept_path(retired, self.state["config"])
        _, old = self.build("old")
        del self.state["concepts"][retired["id"]]
        root, new = self.build("new", previous=old)
        self.assertIn(retired_path, new["changes"]["removed"])
        self.assertIn(retired_path, (root / "DEPLOYMENT.md").read_text(encoding="utf-8"))
        self.assertEqual(manifest_diff(old, new)["removed"], [retired_path])
        history = (root / "okf/log.md").read_text(encoding="utf-8")
        self.assertIn("**Creation**: Retired training", history)
        self.assertIn(f"(`{retired_path}`; retired)", history)
        self.assertNotIn(f"]({retired_path})", history)

    def test_draft_and_deprecated_claims_block_release(self):
        concept = self.state["concepts"]["concept-1"]
        for status in ("draft", "deprecated"):
            concept["status"] = status
            self.assertFalse(eligibility(self.state, NOW)["ready"])

    def test_deprecated_marker_removes_withdrawn_claims(self):
        concept = self.state["concepts"]["concept-1"]
        concept["status"] = "deprecated"
        concept["claim_ids"] = []
        rebind(self.state)
        root, manifest = self.build()
        path = root / "okf" / manifest["deprecated_paths"][0]
        self.assertNotIn("provides on-site training", path.read_text(encoding="utf-8"))

    def test_publication_injected_fetch_rejects_app_shell_gzip_and_retired_files(self):
        root, manifest = self.build()
        def fetch(url, headers):
            path = url.split("/okf/", 1)[1]
            if path == "okf.tar.gz":
                return {"status": 200, "headers": {"Content-Type": "application/gzip", "Content-Encoding": "gzip"},
                        "body": (root / "okf" / path).read_bytes()}
            return {"status": 200, "headers": {"Content-Type": "text/html"}, "body": b"<!doctype html><html>App</html>"}
        report = verify_publication(manifest, "https://example.com/okf/", fetch, ["retired.md"])
        rules = {issue["rule"] for issue in report["issues"]}
        self.assertIn("publication.app_shell", rules)
        self.assertIn("publication.encoding", rules)
        self.assertIn("publication.retired_path", rules)

    def test_publication_checks_snippet_zero_and_conflicting_charset(self):
        root, manifest = self.build()
        def fetch(url, headers):
            path = url.split("/okf/", 1)[1]
            result_headers = {"Content-Type": "application/gzip" if path.endswith(".gz") else "text/markdown; charset=iso-8859-1"}
            if path == "index.md":
                result_headers["X-Robots-Tag"] = "max-snippet: 0"
            return {"status": 200, "headers": result_headers, "body": (root / "okf" / path).read_bytes()}
        report = verify_publication(manifest, "https://example.com/okf/", fetch)
        rules = {i["rule"] for i in report["issues"]}
        self.assertIn("publication.indexing", rules)
        self.assertIn("publication.charset", rules)

    def test_localhost_http_fixture_matches_payload_and_absent_removed_path(self):
        root, manifest = self.build()
        public = root / "okf"
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                relative = self.path.removeprefix("/okf/")
                path = public / relative
                if not path.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip" if relative.endswith(".gz") else "text/markdown; charset=utf-8")
                self.end_headers()
                self.wfile.write(path.read_bytes())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def fetch(url, headers):
            request = Request(url, headers=headers)
            try:
                response = urlopen(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return {"status": response.status, "headers": dict(response.headers), "body": response.read(), "url": response.url}
        try:
            result = verify_publication(manifest, f"http://127.0.0.1:{server.server_port}/okf/", fetch, ["retired.md"])
            self.assertTrue(result["ready"], result["issues"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
