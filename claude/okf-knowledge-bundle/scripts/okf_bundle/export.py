"""Evidence-gated deterministic candidates and separately approved delivery ZIPs."""
from __future__ import annotations

from datetime import timedelta
import gzip
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import uuid
import unicodedata
import zipfile
from urllib.parse import urljoin

import yaml

from .common import canonical_json, digest, normalize_url, safe_slug
from .extraction import EXTRACTION_CONTRACT
from .review import claim_revision, concept_revision, normalize_quote, needs_business_confirmation
from .validation import (SPEC_COMMIT, SPEC_URL, iso_instant, parse_instant,
                         public_url_issue, safe_relative_path, sensitive_text_issue,
                         validate_bundle)

FOLDERS = {"Organization": "organization", "Service": "services", "Product": "products",
           "Product Group": "products", "Product Category": "categories", "Location": "locations",
           "Venue": "venues", "Service Area": "service-areas", "Person": "people", "Policy": "policies",
           "FAQ": "faqs", "Guide": "guides", "Case Study": "guides"}
DEFAULT_DAYS = {"Organization": 180, "Person": 180, "Guide": 180, "Case Study": 180, "Venue": 30}
DECISIONS = {"evidence_review": "accepted", "business_confirmation": "confirmed", "publication": "approved"}
MANIFEST_FIELDS = {"manifest_version", "tool_version", "okf_version", "spec_commit", "release_id", "release_as_of",
                   "config_hash", "deployment_fingerprint", "state_fingerprint", "deployment", "payload",
                   "included_concepts", "withheld_concepts", "deprecated_paths", "availability_mode", "partial",
                   "earliest_stale_after", "changes", "handoff_files"}
MAX_PUBLIC_LOG_BYTES = 1024 * 1024


class ReleaseError(ValueError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


def deployment_fingerprint(state):
    config = state["config"]
    return digest({key: config.get(key) for key in ("domains", "public_base_path", "indexing", "publication_owner",
                  "availability_mode", "indexing_decision", "deployment_target", "crawler_policy")})


def state_fingerprint(state):
    """Exclude bookkeeping that necessarily changes when a candidate is approved."""
    material = {key: state.get(key) for key in ("schema_version", "tool_version", "config", "sources", "claims", "concepts")}
    # Absence retains historical candidate fingerprints exactly.
    if "bundle_selection" in state:
        material["bundle_selection"] = state["bundle_selection"]
    return digest(material)


def _approved_bundle_ids(state, now):
    """None means legacy whole-run readiness; malformed selection fails closed."""
    if "bundle_selection" not in state:
        return None
    selection = state["bundle_selection"]
    if (not isinstance(selection, dict) or set(selection) != {"preview_hash", "content_batch_hash", "concept_revisions", "claim_revisions"}
            or not isinstance(selection["concept_revisions"], dict) or not selection["concept_revisions"]
            or not isinstance(selection["claim_revisions"], dict) or not selection["claim_revisions"]):
        raise ValueError("Invalid approved bundle selection")
    batches = [e for e in state.get("review_history", []) if e.get("type") == "content-batch"]
    batch = batches[-1] if batches else {}
    key = digest(batch)
    if (selection["content_batch_hash"] != key or selection["preview_hash"] != batch.get("preview_hash")
            or sorted(selection["concept_revisions"]) != batch.get("concept_ids")
            or sorted(selection["claim_revisions"]) != batch.get("claim_ids")
            or not _review_valid({**batch, "revision": key}, key, now)):
        raise ValueError("Bundle selection lacks its latest actual human content approval")
    claims = set()
    for cid, revision in selection["concept_revisions"].items():
        concept = state.get("concepts", {}).get(cid)
        if not concept or concept.get("revision") != revision:
            raise ValueError("Selected topic changed or is missing; review the current preview")
        claims.update(concept.get("claim_ids", []))
    if claims != set(selection["claim_revisions"]):
        raise ValueError("Selected topics and statements no longer match")
    for cid, revision in selection["claim_revisions"].items():
        claim = state.get("claims", {}).get(cid, {})
        if claim.get("revision") != revision:
            raise ValueError("Selected statement changed or is missing; review the current preview")
        for dimension, decision in (("evidence_review", "accepted"), ("publication", "approved")):
            if not any(e.get("dimension") == dimension and e.get("decision") == decision
                       and e.get("revision") == revision
                       and all(e.get(k) == batch.get(k) for k in ("by", "at", "channel", "reference"))
                       for e in claim.get("reviews", [])):
                raise ValueError("Selected statement lacks its original content-batch review")
    for cid, revision in selection["concept_revisions"].items():
        event = state["concepts"][cid].get("review", {})
        if (event.get("decision") != "accepted" or event.get("revision") != revision
                or any(event.get(k) != batch.get(k) for k in ("by", "at", "channel", "reference"))):
            raise ValueError("Selected topic lacks its original content-batch review")
    return set(selection["concept_revisions"])


def manifest_hash(manifest):
    return digest(manifest)


def _normal_text(value):
    return normalize_quote(value)


def _review_valid(event, revision, now, decision=None, not_before=None):
    if not isinstance(event, dict) or event.get("revision") != revision:
        return False
    if decision is not None and event.get("decision") != decision:
        return False
    if not re.fullmatch(r"human:[a-zA-Z0-9][a-zA-Z0-9_.-]{1,80}", str(event.get("by", ""))):
        return False
    if not all(isinstance(event.get(k), str) and event[k].strip() for k in ("channel", "reference", "at")):
        return False
    try:
        at = parse_instant(event["at"])
        return at <= now and (not_before is None or at >= parse_instant(not_before))
    except (ValueError, TypeError):
        return False


def _events(claim, dimension, now):
    if not _material_time_valid(claim, now):
        return []
    return [event for event in claim.get("reviews", [])
            if event.get("dimension") == dimension and _review_valid(event, claim.get("revision"), now, DECISIONS[dimension], claim["material_revision_created_at"])]


def _material_time_valid(record, now):
    try:
        return parse_instant(record.get("material_revision_created_at")) <= now
    except (TypeError, ValueError):
        return False


def _inclusion_issues(source, version_id, now):
    robots = str(source.get("robots") or "") + " " + str(source.get("x_robots_tag") or "")
    needs_decision = bool(re.search(r"\b(?:noindex|none)\b", robots, re.I))
    needs_decision = needs_decision or bool(re.fullmatch(r"non[- ]?indexable", str(source.get("csv_indexability", "")).strip(), re.I))
    needs_decision = needs_decision or source.get("reader_warning_review_required")
    needs_decision = needs_decision or source.get("managed_signal_review_required")
    if source.get("canonical"):
        try:
            needs_decision = needs_decision or normalize_url(urljoin(source["citation_url"], source["canonical"])) != normalize_url(source["citation_url"])
        except (TypeError, ValueError):
            needs_decision = True
    if not needs_decision:
        return []
    decision = source.get("inclusion_decision") or {}
    event = {**decision, "revision": decision.get("version_id")}
    if (not _review_valid(event, version_id, now, "included")
            or not isinstance(decision.get("reason"), str) or not decision["reason"].strip()):
        return ["Noindex/canonical-away/non-indexable or capture-warning source needs a recorded human inclusion decision for the current representation"]
    return []


def _live_check_issues(state, source, version_id, now):
    check, reasons = source.get("availability_check") or {}, []
    from .availability import markdown_comparison_required
    quote_support = (check.get("quotes_supported") is True or
                     markdown_comparison_required(source,check) and not _comparison_issues(source,version_id,now))
    if (check.get("version_id") != version_id or check.get("url") != source.get("citation_url")
            or check.get("status") not in range(200, 300) or not quote_support
            or not re.fullmatch(r"[a-f0-9]{64}", str(check.get("response_hash", "")))
            or public_url_issue(check.get("observed_final_url"), state["config"].get("domains", []))):
        reasons.append("Live availability needs observed usable public evidence for this exact URL/version")
    try:
        if check.get("redirected") or normalize_url(check.get("observed_final_url")) != normalize_url(source.get("citation_url")):
            reasons.append("Redirected availability cannot establish the captured citation identity")
    except (TypeError, ValueError):
        reasons.append("Live availability needs the confirmed citation destination")
    try:
        checked = parse_instant(check.get("at"))
        hours = float(state["config"].get("availability_max_age_hours", 24))
        max_age = timedelta(hours=hours)
        if hours <= 0 or checked > now or now - checked > max_age:
            reasons.append("Live availability observation is stale or future-dated")
    except (ValueError, TypeError, OverflowError):
        reasons.append("Live availability observation needs a valid time/window")
    return reasons


def _comparison_issues(source, version_id, now):
    check = source.get("availability_check") or {}
    if check.get("version_id") != version_id or not check.get("comparison_inconclusive"):
        return []
    review = source.get("availability_comparison_review") or {}
    event = {**review, "revision": review.get("version_id")}
    observed_at = check.get("first_observed_at") or check.get("at")
    from .availability import markdown_comparison_required
    comparable = check.get("quotes_supported") is True or markdown_comparison_required(source,check)
    if (check.get("redirected") or check.get("requires_source_refresh") or not comparable
            or not isinstance(observed_at, str)
            or review.get("response_hash") != check.get("response_hash")
            or not _review_valid(event, version_id, now, "accepted", observed_at)
            or not isinstance(review.get("reason"), str) or not review["reason"].strip()
            or any(c in review["reason"] for c in "\r\n")):
        return ["Different source representations need a recorded human comparison of this version/observed response; availability acceptance cannot waive it"]
    return []


def _supported_extraction(version):
    settings = version.get("settings") or {}
    expected = "markdown-verbatim-v2" if version.get("representation") == "firecrawl-markdown" else EXTRACTION_CONTRACT
    if not isinstance(settings, dict) or settings.get("extractor") != expected:
        return False
    if version.get("representation") == "firecrawl-markdown":
        return settings.get("page_metadata_sha256") == digest(version.get("evidence", {}).get("metadata"))
    return True


def _source_issues(state, evidence, now):
    reasons = []
    source = state.get("sources", {}).get(evidence.get("source_id"))
    if not isinstance(source, dict):
        return ["Missing evidence source"]
    version_id = evidence.get("version_id")
    version = source.get("versions", {}).get(version_id)
    if not isinstance(version, dict) or version_id != source.get("active_version"):
        return ["Evidence version is missing or no longer the active source version"]
    if not source.get("identity_confirmed"):
        reasons.append("Source identity is unconfirmed")
    alias_target = state.get("sources", {}).get(source.get("alias_of"), {})
    settings = version.get("settings") or {}
    if not _supported_extraction(version):
        reasons.append("Retained evidence uses an old or unknown extraction contract; explicitly recapture and review before a new release")
    if source.get("alias_of"):
        alias_version = alias_target.get("versions", {}).get(alias_target.get("active_version"), {})
        alias_settings = alias_version.get("settings") or {}
        if not _supported_extraction(alias_version):
            reasons.append("Approved alias target uses an old or unknown extraction contract; explicitly migrate and review the alias evidence")
    from .network import awaiting_retry_results, pending_attempts
    if (source.get("collection_state") == "pending" or alias_target.get("collection_state") == "pending"
            or pending_attempts(source) or pending_attempts(alias_target)):
        reasons.append("Source or approved alias target has an unfinished collection attempt")
    if awaiting_retry_results(source) or awaiting_retry_results(alias_target):
        reasons.append("Source or approved alias target is awaiting a committed retry result")
    if source.get("redirect_review_required"):
        reasons.append("Known citation redirect needs newly retained and confirmed destination evidence; unknown availability acceptance cannot waive it")
    if source.get("csv_metadata_review_required"):
        reasons.append("Companion CSV metadata is not bound to a unique confirmed capture identity; correct the mapping and reimport before release")
    if source.get("scope") not in (True, "include", "included", "in_scope"):
        reasons.append("Source is outside the selected publication scope")
    url = source.get("citation_url")
    reason = public_url_issue(url, state["config"].get("domains", []))
    if reason:
        reasons.append(reason)
    confirmed_url = source.get("confirmed_identity_url")
    if confirmed_url:
        if url != confirmed_url:
            reasons.append("Citation differs from the explicitly confirmed captured identity")
    elif source.get("identity_basis") == "analyst-confirmed" or url != source.get("normalized_url"):
        reasons.append("Citation needs an explicit binding to the confirmed captured identity; replacement destinations require new evidence")
    reasons.extend(_inclusion_issues(source, version_id, now))
    reasons.extend(_comparison_issues(source, version_id, now))
    locator = evidence.get("locator")
    quote = evidence.get("quote")
    retained = version.get("evidence", {})
    if locator not in {"body", "metadata", "schema"} or not isinstance(retained, dict) or locator not in retained:
        reasons.append("Evidence locator is missing from the exact captured representation")
    elif not isinstance(quote, str) or not _normal_text(quote) or _normal_text(quote) not in _normal_text(retained[locator]):
        reasons.append("Verbatim quotation does not match retained evidence")
    availability = source.get("availability", "unknown")
    captured_status = source.get("captured_status")
    check = source.get("availability_check") or {}
    live_issues = _live_check_issues(state, source, version_id, now) if availability == "live" else []
    if check.get("version_id") == version_id and check.get("requires_source_refresh") is True:
        reasons.append("Current source evidence changed; retain and review the new representation before release")
    superseded = (availability == "live" and not live_issues and check.get("supersedes_captured_failure") is True
                  and check.get("requires_source_refresh") is not True)
    if availability in {"failed", "nonpublic"} or (isinstance(captured_status, int) and captured_status >= 400 and not superseded):
        reasons.append("Known unavailable/nonpublic source cannot be waived into a release")
    elif availability == "unknown":
        acceptance = source.get("availability_acceptance") or {}
        if (state["config"].get("availability_mode") != "offline"
                or acceptance.get("owner") != state["config"].get("publication_owner")
                or url not in acceptance.get("urls", [])
                or acceptance.get("version_id") != version_id or not acceptance.get("reason")):
            reasons.append("Unknown availability needs current owner acceptance for this URL/version")
        try:
            if parse_instant(acceptance.get("at")) > now:
                reasons.append("Availability acceptance is in the future")
        except (TypeError, ValueError):
            reasons.append("Availability acceptance needs a valid time")
    elif availability == "live":
        # A captured 200 or a manually changed enum alone is never checked-live.
        reasons.extend(live_issues)
    else:
        reasons.append("Unknown availability enum")
    return reasons


def _has_monetary_amount(claim):
    """A risk keyword alone does not establish a fixed monetary proposition."""
    value = claim.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return True
    text = str(value) + " " + str(claim.get("public_statement", ""))
    codes = r"(?:USD|EUR|GBP|CAD|AUD|NZD|JPY|CNY|INR|AED|SAR|EGP|CHF|ZAR|SGD|HKD)"
    return bool(re.search(r"[$€£¥₹₩₽]\s*\d|\b" + codes + r"\s*\d|\d[\d,.]*\s*(?:"
                          + codes + r"|dollars?|euros?|yen|rupees?)\b", text, re.I))


def _claim_result(state, claim, interval_days, now):
    reasons = []
    if not _material_time_valid(claim, now):
        reasons.append("Claim needs a recorded, nonfuture material revision creation time")
    if claim.get("revision") != claim_revision(claim):
        reasons.append("Claim material changed without a new revision/review")
    for field, allowed in (("evidence_review", {"accepted"}), ("business_confirmation", {"not_required", "confirmed"}),
                           ("publication", {"approved"}), ("conflict", {"none", "resolved"})):
        if claim.get(field) not in allowed:
            reasons.append(f"{field} is not publishable")
    if claim.get("conflict") == "resolved":
        resolutions = [event for event in claim.get("reviews", [])
                       if event.get("dimension") == "conflict" and _material_time_valid(claim, now)
                       and _review_valid(event, claim.get("revision"), now, "resolved", claim["material_revision_created_at"])
                       and isinstance(event.get("reason"), str) and event["reason"].strip()
                       and not any(c in event["reason"] for c in "\r\n")]
        if not resolutions:
            reasons.append("Resolved conflict needs a current human resolution event and rationale")
    if not isinstance(claim.get("public_statement"), str) or not claim["public_statement"].strip():
        reasons.append("Missing approved public statement")
    else:
        reason = sensitive_text_issue(claim["public_statement"])
        if reason:
            reasons.append(reason)
    risks = claim.get("risk_categories", [])
    maintenance = state["config"].get("claim_maintenance", {}).get(claim.get("id"))
    if "volatile" in risks or maintenance is not None:
        if not isinstance(maintenance, dict):
            reasons.append("Volatile claim needs an agreed refresh owner, method and interval")
        else:
            if (maintenance.get("revision") != claim.get("revision")
                    or not re.fullmatch(r"human:[a-zA-Z0-9][a-zA-Z0-9_.-]{1,80}", str(maintenance.get("owner", "")))
                    or any(not isinstance(maintenance.get(k), str) or not maintenance[k].strip() for k in ("method", "reason"))):
                reasons.append("Maintenance policy needs current revision, owner, method and reason")
            try:
                days = float(maintenance.get("interval_days"))
                if not 0 < days <= 3650:
                    raise ValueError()
                interval_days = min(interval_days, days)
            except (TypeError, ValueError):
                reasons.append("Maintenance policy needs a valid refresh interval")
    if "price" in risks and _has_monetary_amount(claim):
        qualifiers = claim.get("qualifiers", {})
        if not re.fullmatch(r"[A-Z]{3}", str(qualifiers.get("currency", ""))):
            reasons.append("Price needs an explicit three-letter currency qualifier")
        if not isinstance(qualifiers.get("offer_context"), str) or not qualifiers["offer_context"].strip():
            reasons.append("Price needs an explicit offer/applicability context qualifier")
    if needs_business_confirmation(state, claim) and claim.get("business_confirmation") != "confirmed":
        override = state["config"].get("confirmation_overrides", {}).get(claim.get("id"), {})
        if not (override.get("owner") == state["config"].get("publication_owner") and override.get("reason")
                and override.get("revision") == claim.get("revision")):
            reasons.append("Risk policy requires separate business confirmation")
    evidence_events = _events(claim, "evidence_review", now)
    publication_events = _events(claim, "publication", now)
    if not evidence_events:
        reasons.append("No actual human evidence review for the current claim revision")
    if not publication_events:
        reasons.append("No recorded publication inclusion decision for the current claim revision")
    deadlines = []
    if evidence_events:
        deadlines.append(max(parse_instant(e["at"]) for e in evidence_events) + timedelta(days=interval_days))
    if claim.get("business_confirmation") == "confirmed":
        designated = state["config"].get("business_reviewers", [state["config"].get("publication_owner")])
        confirmations = [event for event in _events(claim, "business_confirmation", now)
                         if isinstance(designated, list) and event["by"] in designated]
        if not confirmations:
            reasons.append("No current business confirmation from a designated client reviewer")
        else:
            deadlines.append(max(parse_instant(e["at"]) for e in confirmations) + timedelta(days=interval_days))
    if claim.get("expires_at"):
        try:
            deadlines.append(parse_instant(claim["expires_at"]))
        except (TypeError, ValueError):
            reasons.append("Invalid known expiry")
    evidence = claim.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        reasons.append("Claim has no public supporting evidence")
    else:
        for passage in evidence:
            if not isinstance(passage, dict):
                reasons.append("Malformed evidence record")
            else:
                reasons.extend(_source_issues(state, passage, now))
    stale_after = min(deadlines) if deadlines else None
    if stale_after is None or now >= stale_after:
        reasons.append("Claim has no current freshness window")
    return {"eligible": not reasons, "stale_after": stale_after.isoformat().replace("+00:00", "Z") if stale_after else None,
            "issues": sorted(set(reasons)), "evidence_reviewers": sorted({e["by"] for e in evidence_events})}


def concept_path(concept, config):
    locale = concept.get("locale", config.get("locale", "en"))
    if not isinstance(locale, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", locale):
        raise ReleaseError("Locale must be a reviewed lowercase language tag")
    folder = FOLDERS.get(concept.get("type"))
    if folder is None:
        mapping = config.get("custom_type_folders", {}).get(concept.get("type"), {})
        if not isinstance(mapping, dict) or not mapping.get("owner") or not mapping.get("reason"):
            raise ReleaseError("Custom type requires an explicitly reviewed folder mapping")
        folder = mapping.get("folder")
        if not isinstance(folder, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", folder):
            raise ReleaseError("Unsafe custom type folder")
    key = safe_slug(concept.get("key", ""))
    suffix = digest(str(concept.get("id", "")))[:12]
    relative = f"{locale}/{folder}/{key}-{suffix}.md"
    if not safe_relative_path(relative):
        raise ReleaseError("Unsafe concept path")
    return relative


def eligibility(state, now):
    instant = parse_instant(now)
    config = state.get("config", {})
    issues, warnings = [], []
    def add(rule, entity, message):
        issues.append({"rule": rule, "entity": entity, "message": message})
    if state.get("config_hash") != digest(config):
        add("profile.config.integrity", "run", "Configuration hash does not match")
    if not config.get("publication_owner") or not config.get("domains"):
        add("profile.owner", "run", "Publication owner and exact approved domains are required; use owner-set for an actual initial owner designation")
    base = config.get("public_base_path", "/okf/")
    if not isinstance(base, str) or not base.startswith("/") or not base.endswith("/") or ".." in base or "\\" in base or "?" in base or "#" in base:
        add("profile.deployment.path", "run", "Public base path must be a safe absolute directory route")
    if config.get("indexing") not in {"indexable", "direct-fetch-only"}:
        add("profile.indexing", "run", "Unknown indexing policy")
    if config.get("indexing") != "indexable":
        decision = config.get("indexing_decision", {})
        if decision.get("owner") != config.get("publication_owner") or not decision.get("reason"):
            add("profile.indexing", "run", "Alternative indexing policy needs an owner decision")
    concepts, claims = state.get("concepts", {}), state.get("claims", {})
    try:
        selection = _approved_bundle_ids(state, instant)
    except ValueError as exc:
        add("profile.bundle.selection", "run", str(exc))
        selection = set()
    scoped_concepts = concepts if selection is None else {cid: concepts[cid] for cid in selection}
    intervals = {}
    for concept in scoped_concepts.values():
        days = config.get("freshness_days", {}).get(concept.get("type"), DEFAULT_DAYS.get(concept.get("type"), 90))
        try:
            days = float(days)
            if not 0 < days <= 3650:
                raise ValueError()
        except (ValueError, TypeError):
            add("profile.freshness.policy", concept.get("id"), "Invalid configured review interval")
            days = 0
        for claim_id in concept.get("claim_ids", []):
            intervals[claim_id] = min(intervals.get(claim_id, days), days)
    claim_results = {cid: _claim_result(state, claim, intervals.get(cid, 90), instant) for cid, claim in claims.items()
                     if selection is None or cid in intervals}
    concept_results, paths = {}, {}
    for cid, concept in concepts.items():
        if cid not in scoped_concepts:
            concept_results[cid] = {"eligible": False, "excluded_from_bundle": True, "issues": [],
                                    "path": None, "stale_after": None, "whole_concept_evidence_reviewers": []}
            continue
        reasons, deadlines = [], []
        try:
            if concept.get("revision") != concept_revision(concept, claims):
                reasons.append("Concept material/claim revisions changed without new review")
        except KeyError:
            reasons.append("Concept references missing claims")
        review = concept.get("review", {})
        if not _material_time_valid(concept, instant):
            reasons.append("Concept needs a recorded, nonfuture material revision creation time")
        if not _review_valid(review, concept.get("revision"), instant, "accepted", concept.get("material_revision_created_at")):
            reasons.append("Concept public fields need actual human review of the current revision")
        try:
            generated = parse_instant(concept.get("generated_at"))
            if generated > instant:
                reasons.append("Generated time is in the future")
        except (ValueError, TypeError):
            reasons.append("Missing actual generated time")
        if any(not isinstance(concept.get(k), str) or not concept[k].strip() for k in ("key", "type", "title", "description")):
            reasons.append("Missing required reviewed concept fields")
        if any(any(c in str(concept.get(k, "")) for c in "\r\n") for k in ("key", "type", "title", "description")):
            reasons.append("Concept identifiers and display fields must be single-line text")
        if concept.get("resource"):
            reason = public_url_issue(concept["resource"], config.get("domains"))
            if reason:
                reasons.append(reason)
        try:
            path = concept_path(concept, config)
            folded = unicodedata.normalize("NFC", path).casefold()
            if folded in paths:
                reasons.append("Persistent concept path collision")
            paths[folded] = cid
        except (ValueError, TypeError) as exc:
            reasons.append(str(exc))
            path = None
        claim_ids = concept.get("claim_ids", [])
        reviewers = None
        if concept.get("status") == "deprecated":
            if claim_ids:
                reasons.append("Deprecated markers must not retain withdrawn claims")
            replacement = concept.get("replacement")
            if replacement and replacement not in concepts:
                reasons.append("Missing replacement concept")
        elif concept.get("status") != "stable" or not claim_ids:
            reasons.append("Current release concepts must be stable with material claims")
        else:
            for claim_id in claim_ids:
                result = claim_results.get(claim_id)
                if not result:
                    reasons.append(f"Missing claim {claim_id}")
                    continue
                if not result["eligible"]:
                    reasons.append(f"Blocked claim {claim_id}")
                if result["stale_after"]:
                    deadlines.append(parse_instant(result["stale_after"]))
                covered = set(result["evidence_reviewers"])
                reviewers = covered if reviewers is None else reviewers & covered
                for evidence in claims[claim_id].get("evidence", []):
                    source = state.get("sources", {}).get(evidence.get("source_id"), {})
                    version = source.get("versions", {}).get(evidence.get("version_id"), {})
                    # Intake-selected locale is not proof of observed source language.
                    observed = version.get("observed_language")
                    if isinstance(observed, str) and re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*", observed):
                        source_language = observed.lower().replace("_", "-")
                        target_language = str(concept.get("locale", "")).lower()
                        if source_language.split("-")[0] != target_language.split("-")[0]:
                            reasons.append("Known source language differs from concept locale; v1 does not translate claims")
                        elif source_language != target_language:
                            warnings.append({"rule": "profile.locale.review", "entity": cid,
                                             "message": "Source and concept regional language tags differ; confirm applicability in human review"})
                    else:
                        warnings.append({"rule": "profile.locale.review", "entity": cid,
                                         "message": "Source language is unobserved; human review must confirm language preservation"})
        concept_results[cid] = {"eligible": not reasons, "issues": reasons, "path": path,
                                "stale_after": min(deadlines).isoformat().replace("+00:00", "Z") if deadlines else None,
                                "whole_concept_evidence_reviewers": sorted(reviewers or [])}
        for reason in reasons:
            add("profile.concept.readiness", cid, reason)
    for cid, result in claim_results.items():
        if cid in intervals:
            for reason in result["issues"]:
                add("profile.claim.readiness", cid, reason)
    included = sorted(cid for cid, value in concept_results.items() if value["eligible"])
    excluded = sorted(set(concept_results) - set(included))
    if not included:
        add("profile.empty", "run", "No eligible concepts")
    included_sources = {e["source_id"] for cid in included for claim_id in concepts[cid].get("claim_ids", [])
                        for e in claims[claim_id].get("evidence", [])}
    source_modes = {state["sources"][sid].get("availability", "unknown") for sid in included_sources}
    if source_modes == {"live"}:
        availability_mode = "checked-live"
    elif "live" in source_modes:
        availability_mode = "mixed-live/offline-with-availability-acceptance"
    elif source_modes:
        availability_mode = "offline-with-availability-acceptance"
    else:
        availability_mode = "no-current-source-claims" if included else "not-ready"
    return {"ready": not issues, "issues": issues, "warnings": warnings, "claims": claim_results, "concepts": concept_results,
            "included_concept_ids": included, "excluded_concept_ids": excluded,
            "availability_mode": availability_mode,
            "checked_at": iso_instant(now), "semantic_review": "human_review_records_required_not_machine_certified"}


def _public_actor(actor):
    return "human:reviewer-" + digest(actor)[:16]


def _markdown_label(value):
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _render_concept(state, concept, result, path_map):
    meta = {"type": concept["type"], "title": concept["title"], "description": concept["description"],
            "tags": list(concept.get("tags", [])), "status": concept["status"],
            "generated": {"by": "okf-knowledge-bundle/" + state["tool_version"], "at": iso_instant(concept["generated_at"])}}
    if concept.get("resource"):
        meta["resource"] = concept["resource"]
    body = ["# " + _markdown_label(concept["title"]), ""]
    if concept["status"] == "deprecated":
        body += ["This record is deprecated and contains no current factual claims.", ""]
        if concept.get("replacement"):
            replacement = path_map[concept["replacement"]]
            relative = os.path.relpath(replacement, str(Path(result["path"]).parent)).replace("\\", "/")
            body += [f"See the [replacement record]({relative}).", ""]
    else:
        review = concept["review"]
        # Distributed claim review can make the release eligible without any
        # individual having earned a whole-concept verification event.
        if review["by"] in result["whole_concept_evidence_reviewers"]:
            coverage = [[e for e in _events(state["claims"][cid], "evidence_review", parse_instant(review["at"]))
                         if e["by"] == review["by"]] for cid in concept["claim_ids"]]
            if all(coverage):
                meta["verified"] = [{"by": _public_actor(review["by"]), "at": iso_instant(review["at"])}]
        meta["stale_after"] = result["stale_after"]
        source_ids = sorted({e["source_id"] for cid in concept["claim_ids"] for e in state["claims"][cid]["evidence"]})
        footnotes = {sid: "source-" + digest(sid)[:16] for sid in source_ids}
        meta["sources"] = [{"id": footnotes[sid], "resource": state["sources"][sid]["citation_url"]} for sid in source_ids]
        for claim_id in concept["claim_ids"]:
            claim = state["claims"][claim_id]
            citations = "".join(f"[^{footnotes[sid]}]" for sid in sorted({e["source_id"] for e in claim["evidence"]}))
            # A reference definition or table may end the reviewed statement.
            # Keep citations in their own paragraph so rendering cannot append
            # a source marker to a destination or alter the reviewed Markdown.
            body += [claim["public_statement"], "", citations, ""]
        body += [f"[^{footnotes[sid]}]: {state['sources'][sid]['citation_url']}" for sid in source_ids]
    header = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000)
    return ("---\n" + header + "---\n" + "\n".join(body).rstrip() + "\n").encode("utf-8")


def _tar_bytes(payload):
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in sorted(payload.items()):
            item = tarfile.TarInfo(name)
            item.size, item.mode, item.mtime = len(content), 0o644, 0
            item.uid = item.gid = 0
            item.uname = item.gname = ""
            archive.addfile(item, io.BytesIO(content))
    result = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=result, mtime=0, compresslevel=9) as stream:
        stream.write(tar_buffer.getvalue())
    return result.getvalue()


def manifest_diff(previous, current):
    before = (previous or {}).get("payload", {})
    after = current.get("payload", {})
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
            "changed": sorted(path for path in set(before) & set(after) if before[path] != after[path]),
            "unchanged": sorted(path for path in set(before) & set(after) if before[path] == after[path]),
            "retained_deprecated": sorted(current.get("deprecated_paths", []))}


def _previous_release(previous_candidate, previous_manifest, release_as_of):
    if previous_candidate is None:
        if previous_manifest is not None:
            raise ReleaseError("A previous manifest cannot reconstruct history; supply the verified previous candidate")
        return None, ""
    candidate = Path(previous_candidate)
    log_path = candidate / "okf" / "log.md"
    if log_path.is_symlink() or not log_path.is_file() or log_path.stat().st_size > MAX_PUBLIC_LOG_BYTES:
        raise ReleaseError("Previous public log is missing, unsafe or exceeds the retained-history bound")
    verified = verify_candidate(candidate)
    if previous_manifest is not None and digest(previous_manifest) != digest(verified):
        raise ReleaseError("Previous manifest does not match the verified previous candidate")
    if parse_instant(verified["release_as_of"]) > parse_instant(release_as_of):
        raise ReleaseError("A new release cannot precede its verified previous release")
    log = log_path.read_text(encoding="utf-8")
    if not log.startswith("# Bundle change log\n\n## "):
        raise ReleaseError("Unsupported previous log structure; preserve and review history explicitly")
    return verified, log[len("# Bundle change log\n\n"):].rstrip()


def _change_log(state, included, path_map, payload, previous_manifest, previous_log, release_id, release_as_of):
    previous = (previous_manifest or {}).get("payload", {})
    entries = []
    for cid in included:
        concept, path = state["concepts"][cid], path_map[cid]
        if path in previous and previous[path] == digest(payload[path]):
            continue
        if concept["status"] == "deprecated":
            label = "Deprecation"
        elif path not in previous:
            label = "Creation"
        elif previous[path] != digest(payload[path]):
            label = "Update"
        entries.append(f"- **{label}**: [{_markdown_label(concept['title'])}]({path}).")
    for path in sorted(set(previous) - set(payload)):
        if path.endswith(".md") and Path(path).name not in {"index.md", "log.md"}:
            if not safe_relative_path(path):
                raise ReleaseError("Previous manifest contains an unsafe retired path")
            # A removed file cannot be a resolvable link in the current bundle.
            entries.append(f"- **Removal**: Retired `{path}`; absent from this release.")
            # Keep the actual historical entry, but use a plain path identifier
            # when the former target no longer exists in this release.
            previous_log = re.sub(r"\[([^\n]*?)\]\(" + re.escape(path) + r"\)",
                                  lambda match: match[1] + f" (`{path}`; retired)", previous_log)
    if not entries:
        entries.append("- **Release**: No concept content changed.")
    date_heading = f"## {iso_instant(release_as_of)[:10]}"
    if previous_log.startswith(date_heading + "\n"):
        previous_log = previous_log[len(date_heading):].lstrip("\n")
    text = "# Bundle change log\n\n" + date_heading + "\n\n" + "\n".join(entries) + f"\n\nRelease: {release_id}.\n"
    if previous_log:
        text += "\n" + previous_log + "\n"
    content = text.encode("utf-8")
    if len(content) > MAX_PUBLIC_LOG_BYTES:
        raise ReleaseError("Public log history exceeds its bound; explicit archive/retention design is required, never silent truncation")
    return content


def first_release_rollback():
    return ("For a first publication with no previous release, roll back the newly published public payload and any entry link added with that release, "
            "then purge those cached URLs. If the destination already contained other files, follow the agreed rollback plan and preserve those pre-existing files.")


def _handoff_freshness(state, included, manifest):
    source_ids = {evidence["source_id"] for cid in included
                  for claim_id in state["concepts"][cid]["claim_ids"]
                  for evidence in state["claims"][claim_id]["evidence"]}
    live_sources = [state["sources"][sid] for sid in source_ids
                    if state["sources"][sid].get("availability") == "live"]
    if live_sources:
        hours = float(state["config"].get("availability_max_age_hours", 24))
        deadline = min(parse_instant(source["availability_check"]["at"]) + timedelta(hours=hours)
                       for source in live_sources)
        availability = (f"Checked-live availability: configured maximum age is {hours:g} hours "
                        f"(default 24). The earliest included observation is valid through `{iso_instant(deadline.isoformat())}`. "
                        "After that instant, this candidate is ineligible for release approval or packaging. "
                        "Fresh availability checks change the bound run state: build a new candidate and obtain approval "
                        "of its new manifest hash, even if no page content changed. ")
    else:
        availability = ("No checked-live availability deadline applies to this offline candidate. "
                        "Unknown availability relies on the recorded owner's acceptance for the exact URLs/versions "
                        "and its capture-age limitation; it is not a live check. ")
    claim_deadline = manifest["earliest_stale_after"] or "not established"
    return ("## Freshness and release eligibility\n\n" + availability +
            f"The separate earliest claim-review deadline is `{claim_deadline}`. "
            "An unexpired claim-review deadline does not extend availability eligibility. "
            "Re-run readiness before release; release approval and packaging each recheck current eligibility "
            "and the unchanged state/policy fingerprints at the time they run. Neither build time nor a historical "
            "validation result renews either deadline.\n\n")


def _deployment_text(manifest, freshness_text):
    change = manifest["changes"]
    removed = "\n".join("- `" + path + "`" for path in change["removed"]) or "- None."
    return (f"# Developer handoff: {manifest['release_id']}\n\n"
            "## Release hold\n\n"
            "Creating this candidate or receiving this folder does not grant release approval or deployment authorization. "
            "Use it for developer review and deployment planning only. Before deployment to any host, public or staging, "
            "the designated publication owner must approve the exact manifest hash and the operator must have separate "
            "authorization for the deployment target, path and timing. Until then, do not deploy, add site entry links, "
            "or change robots, sitemaps or llms.txt.\n\n"
            + freshness_text +
            f"Publish only the `okf/` directory at `{manifest['deployment']['public_base_path']}`. "
            "Do not publish this document, release-manifest.json, analyst workbooks or private state.\n\n"
            "Replace the full release atomically where supported. Otherwise perform an exact sync, remove retired files listed below, "
            "and purge affected CDN/application caches. Copying over existing files alone leaves retired facts accessible. "
            "Keep the complete previous release and manifest privately for rollback; restore the complete prior tree, never a mixture. "
            + first_release_rollback() + "\n\n"
            "## Retired paths to remove\n\n" + removed + "\n\n"
            "Retired paths must return 404/410, or a specifically approved redirect to a replacement. "
            "Retained deprecated markers remain present and must match this manifest.\n\n"
            "## Serving and discovery\n\n"
            "Serve UTF-8 Markdown as `text/markdown; charset=utf-8` and `okf.tar.gz` as `application/gzip`. "
            "The tarball is already gzipped: do not add an accidental `Content-Encoding: gzip`. "
            "Do not rewrite file URLs to an application shell or login page. Preserve case, paths, relative links and bytes. "
            "Configure static-file routing in WordPress/static hosts; closed CMSs need a verified route/proxy/storage plan.\n\n"
            f"Approved indexing strategy: `{manifest['deployment']['indexing']}`. "
            "Do not impose blanket noindex or canonicalize every concept to its source. Where search discovery is intended, "
            "provide an indexable, snippet-eligible entry route. Add an ordinary website link to that entry or the bundle index; "
            "a download link may target okf.tar.gz. /okf/ is our convention, not a Google requirement. "
            "Optional llms.txt, sitemap, CORS or crawler-policy changes require separate reviewed decisions. "
            "Preserve the client's existing AI-bot policy and report access conflicts.\n\n"
            "## Verification and rollback\n\n"
            "With authorization for the exact deployment target, check status, content type, entity hashes, relative links, "
            "archive contents and retired paths. Test both identity-transfer bytes and a normal-client download. "
            "Do not interpret an HTTP 200, crawler request or bundle publication as proof of indexing, citation or SEO benefit. "
            "If a check fails, restore the complete previous manifest and clear caches.\n\n"
            f"Evidence availability mode: `{manifest['availability_mode']}`. "
            "Human verification records source checking of the covered material claims; client confirmation establishes business applicability "
            "where required. Neither is an unconditional accuracy guarantee. Freshness is bound to actual claim review dates, not build time.\n"
            ).encode("utf-8")


def build_candidate(state, destination, release_id, release_as_of, previous_manifest=None, allow_partial=False, previous_candidate=None):
    destination = Path(destination)
    if destination.exists():
        raise ReleaseError("Candidate directory already exists; immutable candidates are never overwritten")
    if not isinstance(release_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", release_id):
        raise ReleaseError("Unsafe release ID")
    previous_manifest, previous_log = _previous_release(previous_candidate, previous_manifest, release_as_of)
    report = eligibility(state, release_as_of)
    included = report["included_concept_ids"]
    if not report["ready"]:
        if "bundle_selection" in state:
            raise ReleaseError("Approved bundle selection failed readiness; do not drop selected topics", report)
        acceptance = state["config"].get("partial_release_acceptance", {})
        only_concept_issues = all(i["rule"] in {"profile.concept.readiness", "profile.claim.readiness"} for i in report["issues"])
        partial_ok = (allow_partial and included and only_concept_issues and acceptance.get("owner") == state["config"].get("publication_owner")
                      and acceptance.get("reason") and sorted(acceptance.get("concept_ids", [])) == included)
        try:
            partial_ok = partial_ok and parse_instant(acceptance.get("at")) <= parse_instant(release_as_of)
        except (TypeError, ValueError):
            partial_ok = False
        if not partial_ok:
            raise ReleaseError("Release eligibility failed", report)
    path_map = {cid: report["concepts"][cid]["path"] for cid in included}
    for cid in included:
        replacement = state["concepts"][cid].get("replacement")
        if replacement and replacement not in path_map:
            raise ReleaseError("Deprecated marker replacement is not included")
    payload = {path_map[cid]: _render_concept(state, state["concepts"][cid], report["concepts"][cid], path_map) for cid in included}
    sections = {}
    for cid in included:
        path = path_map[cid]
        sections.setdefault(str(Path(path).parent).replace("\\", "/"), []).append(cid)
    locales = sorted({section.split("/")[0] for section in sections})
    payload["index.md"] = ("---\nokf_version: '0.2'\n---\n# Public knowledge bundle\n\n" + "\n".join(f"- [{locale}]({locale}/index.md) - {locale} language scope." for locale in locales) + "\n").encode()
    for locale in locales:
        listings = [f"- [{section.split('/')[1]}]({section.split('/')[1]}/index.md) - Reviewed concepts." for section in sorted(sections) if section.startswith(locale + "/")]
        payload[f"{locale}/index.md"] = (f"# {locale} language scope\n\n" + "\n".join(listings) + "\n").encode()
    for section, cids in sections.items():
        listings = [f"- [{_markdown_label(state['concepts'][cid]['title'])}]({Path(path_map[cid]).name}) - {_markdown_label(state['concepts'][cid]['description'])}" for cid in sorted(cids)]
        payload[section + "/index.md"] = ("# Reviewed concepts\n\n" + "\n".join(listings) + "\n").encode("utf-8")
    payload["log.md"] = _change_log(state, included, path_map, payload, previous_manifest, previous_log, release_id, release_as_of)
    payload["okf.tar.gz"] = _tar_bytes(payload)
    manifest = {"manifest_version": 1, "tool_version": state["tool_version"], "okf_version": "0.2", "spec_commit": SPEC_COMMIT,
                "release_id": release_id, "release_as_of": iso_instant(release_as_of), "config_hash": state["config_hash"],
                "deployment_fingerprint": deployment_fingerprint(state), "state_fingerprint": state_fingerprint(state),
                "deployment": {"public_base_path": state["config"].get("public_base_path", "/okf/"), "indexing": state["config"].get("indexing", "indexable"),
                               "domains": state["config"].get("domains", [])},
                "payload": {name: digest(content) for name, content in sorted(payload.items())},
                "included_concepts": {cid: state["concepts"][cid]["revision"] for cid in included},
                "withheld_concepts": report["excluded_concept_ids"],
                "deprecated_paths": sorted(path_map[cid] for cid in included if state["concepts"][cid]["status"] == "deprecated"),
                "availability_mode": report["availability_mode"], "partial": bool(report["excluded_concept_ids"]),
                "earliest_stale_after": min((report["concepts"][cid]["stale_after"] for cid in included if report["concepts"][cid]["stale_after"]), default=None)}
    manifest["changes"] = manifest_diff(previous_manifest, manifest)
    deployment_bytes = _deployment_text(manifest, _handoff_freshness(state, included, manifest))
    manifest["handoff_files"] = {"DEPLOYMENT.md": digest(deployment_bytes)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / ("candidate-staging-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        for name, content in payload.items():
            target = staging / "okf" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        validation = validate_bundle(staging / "okf", allowed_domains=state["config"].get("domains"))
        if not validation["profile"]["ready"]:
            raise ReleaseError("Candidate failed publication validation", validation)
        (staging / "DEPLOYMENT.md").write_bytes(deployment_bytes)
        (staging / "release-manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8", newline="\n")
        verify_candidate(staging)
        staging.rename(destination)
    except BaseException:
        # The only removed path is this locally created staging directory.
        if staging.exists():
            if staging.is_symlink() or staging.resolve().parent != destination.parent.resolve() or not staging.name.startswith("candidate-staging-"):
                raise ReleaseError("Refusing cleanup outside the verified candidate staging directory")
            shutil.rmtree(staging)
        raise
    return manifest


def load_manifest(candidate_dir):
    return json.loads((Path(candidate_dir) / "release-manifest.json").read_text(encoding="utf-8"))


def verify_candidate(candidate_dir):
    root = Path(candidate_dir)
    if root.is_symlink():
        raise ReleaseError("Candidate cannot be a symbolic link")
    manifest = load_manifest(root)
    if (not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS or manifest.get("manifest_version") != 1
            or manifest.get("okf_version") != "0.2" or manifest.get("spec_commit") != SPEC_COMMIT):
        raise ReleaseError("Unsupported candidate manifest")
    if (not isinstance(manifest.get("handoff_files"), dict) or set(manifest["handoff_files"]) != {"DEPLOYMENT.md"}
            or not isinstance(manifest.get("payload"), dict) or "okf.tar.gz" not in manifest["payload"]
            or not isinstance(manifest.get("deployment"), dict)
            or set(manifest["deployment"]) != {"public_base_path", "indexing", "domains"}):
        raise ReleaseError("Candidate manifest has an invalid public/handoff contract")
    expected = {"okf/" + key: value for key, value in manifest.get("payload", {}).items()}
    expected.update(manifest.get("handoff_files", {}))
    if any(not safe_relative_path(path) for path in expected):
        raise ReleaseError("Unsafe manifest path")
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in expected.values()):
        raise ReleaseError("Invalid payload hash")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseError("Candidate contains a symbolic link")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative != "release-manifest.json":
                actual.add(relative)
    if actual != set(expected):
        raise ReleaseError("Candidate file set differs from manifest")
    for relative, expected_hash in expected.items():
        if digest((root / relative).read_bytes()) != expected_hash:
            raise ReleaseError(f"Candidate byte hash mismatch: {relative}")
    markdown = {path: value for path, value in manifest["payload"].items() if path.endswith(".md")}
    archive_data = (root / "okf" / "okf.tar.gz").read_bytes()
    seen, expanded = set(), 0
    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r|gz") as archive:
        for member in archive:
            if (not member.isfile() or not safe_relative_path(member.name) or member.name in seen
                    or member.name not in markdown or member.size > 20 * 1024 * 1024):
                raise ReleaseError("Unsafe archive member")
            expanded += member.size
            if expanded > 512 * 1024 * 1024:
                raise ReleaseError("Archive exceeds expanded verification bound")
            seen.add(member.name)
            content = archive.extractfile(member).read(member.size + 1)
            if len(content) != member.size or digest(content) != markdown[member.name]:
                raise ReleaseError("Archive payload parity mismatch")
    if seen != set(markdown):
        raise ReleaseError("Archive contains missing entries")
    report = validate_bundle(root / "okf", allowed_domains=manifest["deployment"]["domains"])
    if not report["profile"]["ready"]:
        raise ReleaseError("Candidate no longer passes structural/publication validation", report)
    return manifest


def current_candidate_eligibility(state, manifest, now):
    """Shared approval/package gate: a historical candidate may be stale now."""
    if (manifest["config_hash"] != state["config_hash"] or manifest["deployment_fingerprint"] != deployment_fingerprint(state)
            or manifest["state_fingerprint"] != state_fingerprint(state)):
        raise ReleaseError("Evidence, content or bound policy changed since the approved candidate")
    expected_deployment = {"public_base_path": state["config"].get("public_base_path", "/okf/"),
                           "indexing": state["config"].get("indexing", "indexable"),
                           "domains": state["config"].get("domains", [])}
    if manifest["deployment"] != expected_deployment:
        raise ReleaseError("Manifest deployment data differs from the approved policy")
    report = eligibility(state, now)
    included = manifest["included_concepts"]
    if any(not report["concepts"].get(cid, {}).get("eligible") for cid in included):
        raise ReleaseError("Approved candidate is no longer currently eligible", report)
    if any(i["entity"] == "run" for i in report["issues"]):
        raise ReleaseError("Current release policy is not eligible", report)
    return report


def package_release(state, candidate_dir, output_zip, now):
    if Path(output_zip).resolve().is_relative_to(Path(candidate_dir).resolve()):
        raise ReleaseError("Package output must be outside the immutable candidate directory")
    manifest = verify_candidate(candidate_dir)
    key = manifest_hash(manifest)
    approval = state.get("release_approvals", {}).get(key, {})
    if (approval.get("manifest_hash") != key or approval.get("owner") != state["config"].get("publication_owner")
            or approval.get("config_hash") != state.get("config_hash")
            or approval.get("deployment_fingerprint") != deployment_fingerprint(state)
            or not approval.get("channel") or not approval.get("reference")):
        raise ReleaseError("Exact manifest and deployment policy need recorded owner approval")
    try:
        approved_at = parse_instant(approval.get("at"))
        if not parse_instant(manifest["release_as_of"]) <= approved_at <= parse_instant(now):
            raise ValueError()
    except (ValueError, TypeError):
        raise ReleaseError("Release approval time is invalid") from None
    report = current_candidate_eligibility(state, manifest, now)
    output = Path(output_zip)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file in sorted(Path(candidate_dir).rglob("*")):
            if not file.is_file():
                continue
            info = zipfile.ZipInfo(file.relative_to(candidate_dir).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, file.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return {"path": str(output.resolve()), "sha256": digest(output.read_bytes()), "manifest_hash": key,
            "readiness_checked_at": iso_instant(now), "semantic_review": report["semantic_review"]}


def rebuild_historical(candidate_dir, destination):
    """Copy frozen verified bytes, explicitly without asserting current readiness."""
    manifest = verify_candidate(candidate_dir)
    destination = Path(destination)
    if destination.resolve().is_relative_to(Path(candidate_dir).resolve()):
        raise ReleaseError("Historical copy must be outside the frozen candidate directory")
    if destination.exists():
        raise ReleaseError("Historical destination already exists")
    shutil.copytree(candidate_dir, destination)
    verify_candidate(destination)
    return {"manifest_hash": manifest_hash(manifest), "path": str(destination.resolve()), "historical_only": True,
            "current_eligibility": "not_checked"}
