"""One exact-content human decision; no inferred business or release approval."""
import copy
from .common import digest
from .review import (CLAIM_MATERIAL, CONCEPT_MATERIAL, apply_review,
                     claim_revision, concept_revision, needs_business_confirmation)


def review_preview(state, concept_ids):
    ids = sorted(concept_ids)
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Select a nonempty unique concept set")
    from .continuity import selection_issues
    issues = selection_issues(state, ids)
    if issues:
        raise ValueError("; ".join(issue["message"] for issue in issues))
    concepts, claims, sources = {}, {}, {}
    for cid in ids:
        concept = state["concepts"].get(cid)
        if not concept or concept.get("status") != "stable" or concept.get("review", {}).get("decision") == "rejected":
            raise ValueError("Select current, non-rejected concepts only")
        if concept.get("revision") != concept_revision(concept, state["claims"]):
            raise ValueError("Concept changed without a new draft revision")
        concepts[cid] = {k: copy.deepcopy(concept.get(k)) for k in (*CONCEPT_MATERIAL, "revision")}
        for claim_id in concept.get("claim_ids", []):
            claim = state["claims"][claim_id]
            if claim.get("revision") != claim_revision(claim):
                raise ValueError("Claim changed without a new draft revision")
            if (claim.get("conflict") == "open" or claim.get("business_confirmation") in {"required", "disputed"}
                    or needs_business_confirmation(state, claim) and claim.get("business_confirmation") != "confirmed"):
                raise ValueError("Resolve or exclude pending critical/client-confirmation facts before batch review")
            if claim.get("publication") == "withheld" or claim.get("evidence_review") == "rejected":
                raise ValueError("Withheld or rejected facts cannot receive batch acceptance")
            claims[claim_id] = {k: copy.deepcopy(claim.get(k)) for k in (*CLAIM_MATERIAL, "confirmation_reason", "revision")}
            for evidence in claim.get("evidence", []):
                sid = evidence["source_id"]
                source = state["sources"][sid]
                sources[sid] = {"url": source.get("citation_url"), "source_hash": digest(source)}
    return {"contract": "okf-content-review/v1", "run_id": state["run_id"],
            "config_hash": digest(state["config"]), "concepts": concepts, "claims": claims,
            "sources": sources, "approval_scope": ["evidence_review", "publication_inclusion", "concept_fields"],
            "excluded_concept_ids": sorted(set(state["concepts"]) - set(ids))}


def approve_preview(state, preview, preview_hash, by, at, channel, reference):
    if not isinstance(preview, dict) or digest(preview) != preview_hash:
        raise ValueError("Preview hash does not match the shown content")
    expected = review_preview(state, list(preview.get("concepts", {})))
    if preview != expected:
        raise ValueError("Preview is stale or edited; show a new exact-content preview")
    working = copy.deepcopy(state)
    for cid, claim in preview["claims"].items():
        apply_review(working, "claim", cid, claim["revision"],
                     {"evidence_review": "accepted", "publication": "approved"}, by, at, channel, reference)
    for cid, concept in preview["concepts"].items():
        apply_review(working, "concept", cid, concept["revision"], {"decision": "accepted"}, by, at, channel, reference)
    working.setdefault("review_history", []).append({"type": "content-batch", "preview_hash": preview_hash,
        "by": by, "at": at, "channel": channel, "reference": reference,
        "claim_ids": sorted(preview["claims"]), "concept_ids": sorted(preview["concepts"])})
    bind_approved_bundle(working, preview, preview_hash)
    state.update(working)
    return {"content_review_recorded": True, "claims": len(preview["claims"]),
            "concepts": len(preview["concepts"]), "preview_hash": preview_hash,
            "business_confirmations_recorded": 0, "release_approvals_recorded": 0}


def bind_approved_bundle(state, preview, preview_hash):
    """Bind the existing human content decision; never create another one.

    The old preview can predate initial owner setup or renewed availability.
    Its selected material must still match, and all original review events
    must remain attached to those exact revisions.
    """
    if not isinstance(preview, dict) or digest(preview) != preview_hash or preview.get("run_id") != state["run_id"]:
        raise ValueError("Use the exact approved preview from this run")
    batches = [e for e in state.get("review_history", []) if e.get("type") == "content-batch"]
    batch = batches[-1] if batches else {}
    if (batch.get("preview_hash") != preview_hash or batch.get("concept_ids") != sorted(preview.get("concepts", {}))
            or batch.get("claim_ids") != sorted(preview.get("claims", {}))):
        raise ValueError("The preview must match the latest actual content-batch approval")
    current = review_preview(state, list(preview["concepts"]))
    if current["concepts"] != preview["concepts"] or current["claims"] != preview["claims"]:
        raise ValueError("Selected wording or revisions changed; obtain approval of the new preview")
    def matches(event, revision):
        return (event.get("revision") == revision
                and all(event.get(k) == batch.get(k) for k in ("by", "at", "channel", "reference")))
    for cid, claim in preview["claims"].items():
        events = state["claims"][cid].get("reviews", [])
        for dimension, decision in (("evidence_review", "accepted"), ("publication", "approved")):
            if not any(matches(e, claim["revision"]) and e.get("dimension") == dimension and e.get("decision") == decision for e in events):
                raise ValueError("The original current-revision content review is missing")
    for cid, concept in preview["concepts"].items():
        event = state["concepts"][cid].get("review", {})
        if event.get("decision") != "accepted" or not matches(event, concept["revision"]):
            raise ValueError("The original current-revision concept review is missing")
    selection = {"preview_hash": preview_hash, "content_batch_hash": digest(batch),
                 "concept_revisions": {cid: c["revision"] for cid, c in preview["concepts"].items()},
                 "claim_revisions": {cid: c["revision"] for cid, c in preview["claims"].items()}}
    state["bundle_selection"] = selection
    return {"bundle_selection_recorded": True, "preview_hash": preview_hash,
            "concepts": len(selection["concept_revisions"]), "statements": len(selection["claim_revisions"]),
            "new_human_decisions": 0, "release_approvals_recorded": 0}
