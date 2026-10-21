"""Private, domain-bound unresolved exclusions carried across fresh runs.

These are holds, never inherited approvals. Selectors are conservative text
guards; assistants must also compare meaning against the retained reasons.
"""
import copy
import re
import unicodedata

from .common import digest, utc_now


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError("Continuity fields need bounded nonempty text")
    return value


def _normalized(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def import_context(state, payload):
    """Merge holds without allowing an empty/newer capture to erase them."""
    if not isinstance(payload, dict) or set(payload) != {"domains", "checked_references", "history_status", "exclusions"}:
        raise ValueError("Continuity needs domains, checked_references, history_status and exclusions")
    if payload["domains"] != sorted(set(state["config"]["domains"])):
        raise ValueError("Continuity domains must exactly match this run's sorted approved hosts")
    if payload["history_status"] not in {"checked", "unavailable"}:
        raise ValueError("History status must be checked or unavailable; absence of access is not an empty search")
    refs = payload["checked_references"]
    rows = payload["exclusions"]
    if not isinstance(refs, list) or not refs or len(refs) > 50 or not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Use bounded continuity references and exclusions")
    for ref in refs:
        _text(ref)
    current = copy.deepcopy(state.get("continuity", {"exclusions": {}, "checks": []}))
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"key", "reason", "source_reference", "selectors"}:
            raise ValueError("An exclusion needs key, reason, source_reference and selectors; approvals cannot be imported")
        for key in ("key", "reason", "source_reference"):
            _text(row[key])
        selectors = row["selectors"]
        if not isinstance(selectors, list) or not 1 <= len(selectors) <= 20:
            raise ValueError("An exclusion needs 1-20 alternative selectors")
        for terms in selectors:
            if not isinstance(terms, list) or not 1 <= len(terms) <= 10:
                raise ValueError("Each selector needs 1-10 terms that must all occur together")
            for term in terms:
                if not _normalized(_text(term)):
                    raise ValueError("Selector terms must contain words")
        old = current["exclusions"].get(row["key"])
        if old and old["material"] != row:
            raise ValueError("Existing exclusion cannot be overwritten; retain it and add a distinct hold if needed")
        if not old:
            current["exclusions"][row["key"]] = {"material": copy.deepcopy(row), "recorded_at": utc_now(), "resolved": None}
    current["checks"].append({"at": utc_now(), "history_status": payload["history_status"],
                              "references": list(refs), "payload_hash": digest(payload)})
    state["continuity"] = current
    return {"recorded": True, "unresolved": sum(not r["resolved"] for r in current["exclusions"].values()),
            "history_status": payload["history_status"], "approvals_imported": 0}


def export_context(state):
    context = state.get("continuity", {})
    if not context.get("checks"):
        raise ValueError("Record a continuity check before exporting")
    return {"domains": sorted(set(state["config"]["domains"])),
            "checked_references": ["Prior run " + state["run_id"]] + context["checks"][-1]["references"],
            "history_status": context["checks"][-1]["history_status"],
            "exclusions": [copy.deepcopy(r["material"]) for r in context["exclusions"].values() if not r["resolved"]]}


def resolve_exclusion(state, key, reason, by, at, channel, reference):
    from .review import reviewer_event
    from .common import parse_time
    row = state.get("continuity", {}).get("exclusions", {}).get(key)
    if not row or row["resolved"]:
        raise ValueError("Select a current unresolved exclusion")
    _text(reason)
    event = reviewer_event(by, at, channel, reference, digest(row["material"]))
    if parse_time(at) < parse_time(row["recorded_at"]):
        raise ValueError("Resolution must follow this hold's import; old content approval is not clarification")
    row["resolved"] = dict(event, reason=reason)
    return {"resolved": key, "content_approved": False}


def selection_issues(state, concept_ids):
    context = state.get("continuity", {})
    if state["config"].get("continuity_required") and not context.get("checks"):
        return [{"rule": "profile.continuity.unchecked", "entity": "run",
                 "message": "Check prior client exclusions and run continuity-import before review or release"}]
    issues = []
    for cid in concept_ids:
        concept = state.get("concepts", {}).get(cid)
        if not concept:
            continue
        # Include public descriptions as well as statements; neither is a bypass.
        texts = [" ".join(str(concept.get(k, "")) for k in ("title", "description"))]
        texts += [texts[0] + " " + state["claims"][key]["public_statement"] for key in concept.get("claim_ids", []) if key in state["claims"]]
        normalized = [" " + _normalized(t) + " " for t in texts]
        for key, row in context.get("exclusions", {}).items():
            if row["resolved"]:
                continue
            if any(all(" " + _normalized(term) + " " in text for term in terms)
                   for text in normalized for terms in row["material"]["selectors"]):
                issues.append({"rule": "profile.continuity.excluded", "entity": cid,
                               "message": "Unresolved exclusion " + key + ": " + row["material"]["reason"]})
    return issues
