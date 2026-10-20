"""Separately authorized read-only checks of enumerated public citations."""
from .common import digest, normalize_url, public_url, utc_now
from .extraction import extract_html
from .review import normalize_quote


def markdown_comparison_required(source, check):
    version = source.get("versions", {}).get(check.get("version_id"), {})
    return (version.get("representation") == "firecrawl-markdown"
            and check.get("retained_representation") == "firecrawl-markdown"
            and check.get("comparison_inconclusive") is True
            and check.get("quotes_supported") is None)


def _invalidate_reviews(state, source_claims):
    affected = {claim["id"] for claim in source_claims}
    for claim in source_claims:
        claim.setdefault("review_history", []).extend(claim.get("reviews", []))
        claim["reviews"] = []
        claim.update(evidence_review="pending", publication="pending")
        if claim["business_confirmation"] != "not_required":
            claim["business_confirmation"] = "required"
    for concept in state["concepts"].values():
        if affected.intersection(concept["claim_ids"]):
            concept["review"] = {"decision": "pending"}


def check_availability(state, source_ids, fetch=None, *, clock=None, sleep=None):
    if not source_ids or len(source_ids) > 500:
        raise ValueError("Plan at most 500 explicit cited-URL checks per batch")
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("Duplicate availability-check source IDs")
    sources = state["sources"]
    for sid in source_ids:
        source = sources[sid]
        if not source.get("identity_confirmed") or not public_url(source.get("citation_url"), state["config"]["domains"]):
            raise ValueError("Only confirmed permitted public citation URLs may be checked")
    if fetch is None:
        from .network import direct_fetch
        from .politeness import AvailabilityFetcher
        fetch = AvailabilityFetcher(state, direct_fetch, clock=clock, sleep=sleep)
    observations = []
    for sid in source_ids:
        source = sources[sid]
        at = utc_now()
        try:
            response = fetch(source["citation_url"])
            status = response.get("status")
            final = response.get("url") or source["citation_url"]
            raw = response.get("body", b"")
            if not public_url(final, state["config"]["domains"]) or not isinstance(raw, bytes) or len(raw) > 20 * 1024 * 1024:
                raise ValueError("Unsafe/oversized observed destination")
            redirected = normalize_url(final) != normalize_url(source["citation_url"])
            check = {"url": source["citation_url"], "version_id": source["active_version"], "status": status, "at": at, "observed_final_url": final, "response_hash": digest(raw), "quotes_supported": False, "supersedes_captured_failure": False, "redirected": redirected, "comparison_inconclusive": False}
            previous = source.get("availability_check") or {}
            same_response = all(previous.get(key) == check[key] for key in ("url", "version_id", "response_hash", "observed_final_url"))
            check["first_observed_at"] = (previous.get("first_observed_at") or previous.get("at") or at) if same_response else at
            if redirected:
                source["redirect_review_required"] = {"from_url": source["citation_url"], "to_url": final,
                                                       "version_id": source["active_version"], "at": at,
                                                       "response_hash": check["response_hash"]}
            if isinstance(status, int) and 200 <= status < 300:
                extracted = extract_html(raw, final)
                if extracted["quality"] != "usable":
                    retained = source.get("versions", {}).get(source.get("active_version"), {})
                    rendered = str(retained.get("representation", "")).startswith("firecrawl-") or retained.get("representation") in {"jina-dom", "sf-rendered"}
                    if rendered and extracted["quality"] in {"empty_or_thin", "placeholder"}:
                        source["availability"] = "unknown"
                        check.update(outcome="direct-shell-for-rendered-capture", quotes_supported=None,
                                     observed_evidence=extracted["evidence"], observed_quality=extracted["quality"])
                    else:
                        source["availability"] = "nonpublic" if extracted["quality"] in ("login", "challenge") else "failed"
                        check["outcome"] = "unusable-" + extracted["quality"]
                else:
                    source["availability"] = "unknown" if redirected else "live"
                    source_claims = [c for c in state["claims"].values() if any(e["source_id"] == sid for e in c["evidence"])]
                    check["quotes_supported"] = all(normalize_quote(e["quote"]) in normalize_quote(extracted["evidence"].get(e["locator"], "")) for c in source_claims for e in c["evidence"] if e["source_id"] == sid)
                    check["automatic_quotes_supported"] = check["quotes_supported"]
                    old = source["versions"].get(source.get("active_version"), {})
                    markdown_representation = old.get("representation") == "firecrawl-markdown"
                    if markdown_representation:
                        # Provider Markdown/metadata are a different contract.
                        # Do not call markup differences a temporal change or
                        # pretend direct HTML automatically verified those quotes.
                        check["quotes_supported"] = None
                        check["quote_comparison_scope"] = "No automatic Markdown/provider-metadata quote verification; explicit human representation comparison required"
                    differences = [key for key in ("body", "metadata", "schema") if normalize_quote(extracted["evidence"].get(key, "")) != normalize_quote(old.get("evidence", {}).get(key, ""))]
                    comparable_body = old.get("representation") in {"sf-original", "direct-html"}
                    changed_locators = [key for key in differences if key != "body" or comparable_body]
                    if markdown_representation:
                        changed_locators = []
                    check["observed_representation"] = "direct-html"
                    check["retained_representation"] = old.get("representation")
                    check["different_locators"] = differences
                    check["body_comparison"] = "comparable" if comparable_body else "different-representation"
                    check["comparison_inconclusive"] = "body" in differences and not comparable_body
                    if markdown_representation:
                        check["comparison_inconclusive"] = True
                    if check["comparison_inconclusive"]:
                        # This comparison is not proof of a temporal content change.
                        # Preserve old review events, but require an actual human
                        # comparison before publication; quotations alone miss new
                        # exceptions or contradictory statements around the quote.
                        check["observed_evidence"] = extracted["evidence"]
                        source.setdefault("warnings", []).append("Direct HTML differs from the retained representation; compare the observed evidence for new exceptions/conflicts before release")
                    observed_http_robots = response.get("headers", {}).get("x-robots-tag")
                    if observed_http_robots is not None and observed_http_robots != source.get("http_robots"):
                        source["http_robots"] = observed_http_robots
                        source["x_robots_tag"] = observed_http_robots
                        check["observed_http_robots"] = observed_http_robots
                        source["inclusion_decision"] = None
                    changed = bool(changed_locators)
                    check["changed_locators"] = changed_locators
                    check["current_body_changed"] = ("body" in changed_locators) if comparable_body else None
                    quote_failure = not markdown_representation and not check["quotes_supported"]
                    check["requires_source_refresh"] = redirected or changed or quote_failure
                    check["supersedes_captured_failure"] = check["quotes_supported"] is True and not check["requires_source_refresh"]
                    if redirected:
                        check["outcome"] = "redirect-needs-identity-review"
                        source.setdefault("warnings", []).append("Citation redirected; collect and confirm the destination as a separate source before replacing the captured citation")
                    elif check["requires_source_refresh"]:
                        check["outcome"] = "checked-live-needs-evidence-review"
                    elif check["comparison_inconclusive"]:
                        check["outcome"] = "checked-live-comparison-required"
                    else:
                        check["outcome"] = "checked-live"
                    if not redirected:
                        source.pop("redirect_review_required", None)
                    if changed or quote_failure:
                        _invalidate_reviews(state, source_claims)
                        source.setdefault("warnings", []).append("Current evidence/robots metadata differs from retained evidence; collect/review the current representation before release")
            else:
                source["availability"] = "nonpublic" if status in (401, 403) else "failed"
                check["outcome"] = "known-unavailable"
            source.setdefault("availability_history", []).append(check)
            source["availability_check"] = check
            source["availability_acceptance"] = None
            observations.append({"source_id": sid, **check})
        except (ValueError, OSError) as error:
            from .politeness import AvailabilityPolicyError
            # A transport problem is not proof the service ceased or target is 404.
            source["availability_acceptance"] = None
            if source.get("availability") not in ("failed", "nonpublic"):
                source["availability"] = "unknown"
            check = {"url": source["citation_url"], "version_id": source["active_version"],
                     "at": at, "outcome": "check-failed", "reason": str(error) if isinstance(error, AvailabilityPolicyError) else type(error).__name__, "status": None}
            source["availability_check"] = check
            source.setdefault("availability_history", []).append(check)
            observations.append({"source_id": sid, **check})
    state["release_approvals"] = {}
    pacing = fetch.receipt() if hasattr(fetch, "receipt") else {"mode": "injected-test-transport; production pacing not evaluated"}
    return {"observations": observations, "scope": "enumerated-citations-only", "paid_provider_calls": 0, "pacing": pacing}
