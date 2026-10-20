"""Draft validation and explicit human review; no LLM or provider calls."""
import copy
from datetime import datetime, timezone
import re
import unicodedata
from .common import digest, stable_id, parse_time, public_url, utc_now

DIMENSIONS = {
    "evidence_review": {"pending", "accepted", "rejected"},
    "business_confirmation": {"not_required", "required", "confirmed", "disputed"},
    "publication": {"pending", "approved", "withheld"},
    "conflict": {"none", "open", "resolved"},
}
RISK_PATTERNS = {
    "price": r"\b(price|fee|cost|usd|eur|gbp|dollars?|euros?)\b|[$€£]",
    "credential": r"\b(certif\w*|licen[cs]\w*|accredit\w*|credential\w*)\b",
    "regulated": r"\b(regulat\w*|medical|legal|financial|safety|required by law|compliance)\b",
    "guarantee": r"\b(guarantee\w*|warrant\w*)\b",
    "coverage_hours": r"\b(service area|hours|opening|coverage|serves|open daily)\b",
    "person": r"\b(staff|director|manager|founder|employee|person)\b",
    "statistic_award": r"\b(award\w*|best|leading|largest|percent|statistics?)\b|\d\s*%",
    "volatile": r"\b(in stock|out of stock|stock levels?|stock quantity|available now|current availability|live availability|current price|limited offer)\b|\bstock\s*[:=]?\s*\d",
    "disputed": r"\b(disputed|contradict\w*|conflict\w*)\b",
}
CLAIM_FIELDS = {"confirmation_reason", "key", "subject", "predicate", "value", "qualifiers", "public_statement", "evidence", "risk_categories", "expires_at"}
CONCEPT_FIELDS = {"key", "type", "title", "description", "resource", "tags", "locale", "claim_keys", "status", "replacement"}
CLAIM_MATERIAL = ("subject", "predicate", "value", "qualifiers", "public_statement", "evidence", "risk_categories", "expires_at")
CONCEPT_MATERIAL = ("key", "type", "title", "description", "resource", "tags", "locale", "claim_ids", "status", "replacement")


def normalize_quote(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def claim_revision(claim):
    material = {k: claim.get(k) for k in CLAIM_MATERIAL}
    # Omitted optional fields retain existing revision hashes.
    if claim.get("confirmation_reason") is not None:
        material["confirmation_reason"] = claim["confirmation_reason"]
    return digest(material)


def needs_business_confirmation(state, claim):
    """Keyword hints alone do not create client tasks in new runs.

    Missing policy preserves legacy all-risk behavior; no implicit migration.
    """
    risks = set(claim.get("risk_categories", []))
    if state["config"].get("confirmation_policy", "all-risk") != "critical-only":
        return bool(risks or claim.get("confirmation_reason"))
    return bool(claim.get("confirmation_reason") or risks & {"disputed", "volatile"})


def concept_revision(concept, claims):
    material = {k: concept.get(k) for k in CONCEPT_MATERIAL}
    material["claim_revisions"] = {key: claims[key]["revision"] for key in concept.get("claim_ids", [])}
    return digest(material)


def reviewer_event(by, at, channel, reference, revision, dimension=None, decision=None):
    if not re.fullmatch(r"human:[a-zA-Z0-9][a-zA-Z0-9_.-]{1,80}", str(by)):
        raise ValueError("Use an opaque human reviewer role ID; no names/emails or fabricated identity")
    if parse_time(at) > datetime.now(timezone.utc):
        raise ValueError("Review time cannot be in the future")
    if not channel or not reference or not revision:
        raise ValueError("Review needs channel, source reference and exact revision")
    result = {"by": by, "at": at, "channel": channel, "reference": reference, "revision": revision}
    if dimension:
        result.update(dimension=dimension, decision=decision)
    return result


def validate_evidence(state, evidence):
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("A claim needs exact retained public evidence")
    for item in evidence:
        if set(item) != {"source_id", "version_id", "locator", "quote"}:
            raise ValueError("Evidence must name source, exact version, locator and verbatim quote")
        source = state["sources"].get(item["source_id"])
        if not source or item["version_id"] not in source["versions"]:
            raise ValueError("Unknown or misattributed source representation")
        if item["locator"] not in {"body", "metadata", "schema"}:
            raise ValueError("Unsupported evidence locator")
        retained = source["versions"][item["version_id"]].get("evidence", {}).get(item["locator"], "")
        quote = normalize_quote(item["quote"])
        if not quote or quote not in normalize_quote(retained):
            raise ValueError("Evidence quote does not occur in the cited retained representation")


def _string(value, name, limit=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(name + " must be a nonempty bounded string")


def _singleline(value, name, limit=2000):
    _string(value, name, limit)
    if any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in value):
        raise ValueError(name + " must be a single line without control characters")


def _reset_legacy_claim(state, claim):
    """Establish today's review boundary without inventing historical creation."""
    result = copy.deepcopy(claim)
    result.setdefault("previous_revisions", []).append({"revision": claim["revision"],
        "reviews": copy.deepcopy(claim.get("reviews", [])), "material_revision_created_at": None,
        "reason": "Legacy revision creation time unknown; prior decisions are historical only"})
    result.update(material_revision_created_at=utc_now(), evidence_review="pending", publication="pending",
                  business_confirmation="required" if needs_business_confirmation(state, result) else "not_required", reviews=[],
                  refresh_reason="Legacy creation boundary established now; human reviews must be repeated")
    return result


def _check_review_time(record, at):
    try:
        created = parse_time(record["material_revision_created_at"])
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("Material revision needs a tool-established creation time before review") from error
    if parse_time(at) < created:
        raise ValueError("Review time predates the current material revision's creation")


def _claim(state, draft):
    if not isinstance(draft, dict) or set(draft) - CLAIM_FIELDS:
        raise ValueError("Unknown claim fields; drafts cannot inject approvals or review state")
    for name in ("key", "subject", "predicate"):
        _singleline(draft.get(name), name)
    _string(draft.get("public_statement"), "public_statement")
    if "value" not in draft or not isinstance(draft.get("qualifiers", {}), dict):
        raise ValueError("Claim needs value and a qualifier mapping")
    validate_evidence(state, draft.get("evidence"))
    risks = draft.get("risk_categories", [])
    if not isinstance(risks, list) or set(risks) - set(RISK_PATTERNS):
        raise ValueError("Unknown confirmation risk category")
    text = " ".join(str(draft.get(k, "")) for k in ("subject", "predicate", "value", "public_statement"))
    risks = sorted(set(risks) | {k for k, pattern in RISK_PATTERNS.items() if re.search(pattern, text, re.I)})
    if draft.get("expires_at"):
        parse_time(draft["expires_at"])
    result = {key: copy.deepcopy(draft.get(key)) for key in CLAIM_MATERIAL}
    result.update(qualifiers=copy.deepcopy(draft.get("qualifiers", {})), risk_categories=risks)
    if draft.get("confirmation_reason") is not None:
        _singleline(draft["confirmation_reason"], "confirmation_reason")
        result["confirmation_reason"] = draft["confirmation_reason"]
    result["id"] = stable_id("claim", draft["key"])
    result["key"] = draft["key"]
    result["revision"] = claim_revision(result)
    old = state["claims"].get(result["id"])
    if old and old["revision"] == result["revision"]:
        return copy.deepcopy(old) if old.get("material_revision_created_at") else _reset_legacy_claim(state, old)
    result.update(material_revision_created_at=utc_now(), evidence_review="pending", business_confirmation="required" if needs_business_confirmation(state, result) else "not_required", publication="pending", conflict="open" if "disputed" in risks else "none", reviews=[])
    if old:
        result["previous_revisions"] = old.get("previous_revisions", []) + [{"revision": old["revision"], "reviews": old.get("reviews", []), "material_revision_created_at": old.get("material_revision_created_at")}]
    return result


def import_drafts(state, payload):
    """All-or-nothing material import. Rejected batches remain an audited pending item."""
    if not isinstance(payload, dict) or set(payload) - {"batch_id", "claims", "concepts"}:
        raise ValueError("Draft envelope accepts batch_id, claims and concepts only")
    batch = payload.get("batch_id")
    _singleline(batch, "batch_id", 200)
    failures = [row for row in state.get("draft_failures", []) if row["batch_id"] == batch]
    if len(failures) >= 3:
        raise ValueError("Two repair attempts exhausted; keep pending and review the model budget")
    working = copy.deepcopy(state)
    try:
        drafts = payload.get("claims", [])
        concepts = payload.get("concepts", [])
        if not isinstance(drafts, list) or not isinstance(concepts, list) or len(drafts) > 500 or len(concepts) > 500:
            raise ValueError("Use bounded batches of at most 500 claims/concepts")
        seen, reset_claims = set(), set()
        keys = {c.get("key", c["id"]): c["id"] for c in working["claims"].values()}
        for draft in drafts:
            claim = _claim(working, draft)
            if claim["id"] in seen:
                raise ValueError("Duplicate claim key in draft batch")
            seen.add(claim["id"])
            old_claim = working["claims"].get(claim["id"])
            if old_claim and old_claim["revision"] == claim["revision"] and not old_claim.get("material_revision_created_at"):
                reset_claims.add(claim["id"])
            keys[draft["key"]] = claim["id"]
            working["claims"][claim["id"]] = claim
        seen = set()
        for draft in concepts:
            if not isinstance(draft, dict) or set(draft) - CONCEPT_FIELDS:
                raise ValueError("Unknown concept fields")
            for key in ("key", "type", "title", "description", "locale"):
                _singleline(draft.get(key), key)
            if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", draft["locale"]):
                raise ValueError("Use a safe explicit locale key")
            if draft.get("resource") is not None:
                _singleline(draft["resource"], "resource")
                if not public_url(draft["resource"], state["config"]["domains"]):
                    raise ValueError("Concept resource is not an approved public client URL")
            tags = draft.get("tags", [])
            if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
                raise ValueError("Tags must be strings")
            for tag in tags:
                _singleline(tag, "tag", 200)
            status = draft.get("status", "stable")
            if status not in ("stable", "deprecated"):
                raise ValueError("Drafts are review state; release lifecycle is stable or deprecated")
            claim_ids = [keys[k] for k in draft.get("claim_keys", [])]
            if status == "stable" and not claim_ids:
                raise ValueError("Current concepts need at least one material claim")
            if status == "deprecated" and claim_ids:
                raise ValueError("Deprecated markers cannot contain withdrawn claims")
            cid = stable_id("concept", [draft["locale"], draft["key"]])
            if cid in seen:
                raise ValueError("Duplicate concept key")
            seen.add(cid)
            old = working["concepts"].get(cid)
            concept = {k: copy.deepcopy(draft.get(k)) for k in CONCEPT_MATERIAL}
            created_at = utc_now()
            concept.update(id=cid, claim_ids=claim_ids, tags=tags, status=status, replacement=draft.get("replacement"), generated_at=created_at, material_revision_created_at=created_at)
            concept["revision"] = concept_revision(concept, working["claims"])
            if old and old["revision"] == concept["revision"] and old.get("material_revision_created_at"):
                concept = old
            else:
                concept["review"] = {"decision": "pending"}
                if old and not old.get("material_revision_created_at"):
                    concept["legacy_review_history"] = old.get("legacy_review_history", []) + [copy.deepcopy(old.get("review", {}))]
                    concept["refresh_reason"] = "Legacy creation boundary established now; concept review must be repeated"
            working["concepts"][cid] = concept
        for concept in working["concepts"].values():
            current = concept_revision(concept, working["claims"])
            if current != concept["revision"] or any(claim_id in reset_claims for claim_id in concept.get("claim_ids", [])):
                concept["revision"] = current
                concept["review"] = {"decision": "pending"}
                concept["generated_at"] = concept["material_revision_created_at"] = utc_now()
        material_changed = working["claims"] != state["claims"] or working["concepts"] != state["concepts"]
        if material_changed:
            working["release_approvals"] = {}
        state.update(working)
        return {"accepted": True, "claims": len(drafts), "concepts": len(concepts), "material_changed": material_changed}
    except (ValueError, KeyError, TypeError) as error:
        state.setdefault("draft_failures", []).append({"batch_id": batch, "at": utc_now(), "payload_hash": digest(payload), "reason": str(error), "status": "pending"})
        return {"accepted": False, "reason": str(error), "repair_attempts_remaining": max(0, 2 - len(failures))}


def apply_review(state, entity, entity_id, revision, decisions, by, at, channel, reference, reason=None):
    if entity == "claim":
        record = state["claims"][entity_id]
        if revision != record["revision"] or revision != claim_revision(record):
            raise ValueError("Stale or modified claim revision")
        _check_review_time(record, at)
        if set(decisions) - set(DIMENSIONS):
            raise ValueError("Unknown review dimension")
        for dimension, decision in decisions.items():
            if decision not in DIMENSIONS[dimension]:
                raise ValueError("Invalid " + dimension + " decision")
            if dimension == "conflict":
                if decision == "none" and record.get("conflict") in {"open", "resolved"}:
                    raise ValueError("A known conflict cannot be cleared to none; record a reasoned resolution")
                if decision == "resolved":
                    if record.get("conflict") not in {"open", "resolved"}:
                        raise ValueError("Only an existing open conflict can receive a resolution")
                    _singleline(reason, "Conflict resolution reason")
            if dimension == "business_confirmation" and decision == "not_required" and needs_business_confirmation(state, record):
                override = state["config"].get("confirmation_overrides", {}).get(entity_id, {})
                if override.get("owner") != state["config"].get("publication_owner") or not override.get("reason") or override.get("revision") != revision:
                    raise ValueError("A risk policy override needs an explicit owner/reason/current-revision configuration before review")
            if dimension == "business_confirmation" and decision in ("confirmed", "disputed"):
                designated = state["config"].get("business_reviewers", [state["config"].get("publication_owner")])
                if by not in designated:
                    raise ValueError("Business confirmation requires a designated client reviewer")
        events = [reviewer_event(by, at, channel, reference, revision, dimension, decision) for dimension, decision in decisions.items()]
        for event in events:
            if event["dimension"] == "conflict" and event["decision"] == "resolved":
                event["reason"] = reason
            record[event["dimension"]] = event["decision"]
            record["reviews"].append(event)
    elif entity == "concept":
        record = state["concepts"][entity_id]
        if revision != record["revision"] or revision != concept_revision(record, state["claims"]):
            raise ValueError("Stale or modified concept revision")
        _check_review_time(record, at)
        if set(decisions) != {"decision"} or decisions["decision"] not in ("accepted", "pending", "rejected"):
            raise ValueError("Invalid concept review")
        event = reviewer_event(by, at, channel, reference, revision)
        event["decision"] = decisions["decision"]
        record["review"] = event
        events = [event]
    else:
        raise ValueError("Review supports claim or concept")
    state["review_history"].append({"entity": entity, "id": entity_id, "revision": revision, "events": events})
    state["release_approvals"] = {}


def approve_alias(state, source_id, target_id, by, at, channel, reference, reason):
    """Explicit equal-evidence alias; keep capture IDs/versions and history intact."""
    event = reviewer_event(by, at, channel, reference, "source-alias")
    if source_id == target_id or not reason:
        raise ValueError("Alias needs two distinct sources and a review reason")
    source, target = state["sources"][source_id], state["sources"][target_id]
    if source.get("alias_of") or target.get("alias_of"):
        raise ValueError("Resolve existing aliases explicitly; no implicit chain/cycle merge")
    if not source.get("identity_confirmed") or not target.get("identity_confirmed"):
        raise ValueError("Both captured identities must be confirmed before alias review")
    a = source["versions"].get(source.get("active_version"), {})
    b = target["versions"].get(target.get("active_version"), {})
    if not a or not b or not normalize_quote(a.get("evidence", {}).get("body", "")) or normalize_quote(a["evidence"]["body"]) != normalize_quote(b.get("evidence", {}).get("body", "")) or a.get("locale") != b.get("locale"):
        raise ValueError("Alias needs reviewed identical active body evidence and locale; divergent views remain distinct")
    source["original_source_key"] = source.get("source_key", source["id"])
    source["source_key"] = target.get("source_key", target["id"])
    source["alias_of"] = target_id
    source["alias_review_required"] = False
    source.setdefault("alias_history", []).append({**event, "source_id": source_id, "target_id": target_id, "source_version": a["id"], "target_version": b["id"], "reason": reason})
    state["release_approvals"] = {}


def accept_availability(state, source_ids, owner, at, reason, channel, reference):
    reviewer_event(owner, at, channel, reference, "availability")
    if owner != state["config"].get("publication_owner") or not reason:
        raise ValueError("Unknown current availability requires the release owner's recorded acceptance")
    for sid in source_ids:
        source = state["sources"][sid]
        if source.get("availability") in ("failed", "nonpublic") or (isinstance(source.get("captured_status"), int) and source["captured_status"] >= 400):
            raise ValueError("Known failed/nonpublic sources cannot be waived")
        if not source.get("identity_confirmed") or not public_url(source.get("citation_url"), state["config"]["domains"]):
            raise ValueError("A citation needs confirmed public identity before acceptance")
    for sid in source_ids:
        source = state["sources"][sid]
        source["availability_acceptance"] = {"owner": owner, "at": at, "reason": reason, "channel": channel, "reference": reference, "urls": [source["citation_url"]], "version_id": source["active_version"]}
    state["release_approvals"] = {}
