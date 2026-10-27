"""Safe, specific source-check explanations; never echo transport secrets."""
import socket
import ssl
from urllib.error import URLError


def _unknown_action(source, config):
    if (source.get("availability") in {"failed", "nonpublic"}
            or isinstance(source.get("captured_status"), int) and source["captured_status"] >= 400):
        return "A known failed/nonpublic source remains blocked. Retain verified recovery or review replacement evidence; owner acceptance cannot waive it."
    action = "Resolve the check problem, then make a separately authorized check; no automatic retry or provider switch."
    if config.get("availability_mode") == "offline":
        action += " Alternatively, obtain the designated owner's actual acceptance of unknown current availability for only this included URL and capture version/date. That is not factual confirmation or publication approval."
    return action


def _network_kind(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, socket.gaierror):
            return "dns", "The hostname could not be resolved."
        if isinstance(error, (TimeoutError, socket.timeout)):
            return "timeout", "The request timed out before verification completed."
        if isinstance(error, ssl.SSLError):
            return "tls", "The secure connection could not be verified."
        if isinstance(error, PermissionError):
            return "permission", "The execution environment denied network access."
        if isinstance(error, ConnectionError):
            return "connection", "The connection failed before verification completed."
        reason = getattr(error, "reason", None) if isinstance(error, URLError) else None
        error = reason if isinstance(reason, BaseException) else error.__cause__
    return "unclassified", "The request could not be completed; the exact transport cause was not established."


def explain_failure(error, source, config):
    from .politeness import AvailabilityPolicyError, RobotsDisallowedError
    kind, summary = _network_kind(error)
    if isinstance(error, RobotsDisallowedError):
        return {"code": "robots-disallowed", "summary": "Robots rules disallow this URL; no page request was sent.",
                "next_step": "Do not bypass robots or switch providers. Keep this access restriction unresolved; use permitted supported evidence or obtain an authorized policy resolution."}
    if isinstance(error, AvailabilityPolicyError):
        message = str(error)
        if "pac" in message.lower() or "wait" in message.lower():
            return {"code": "pacing-deferred", "summary": message,
                    "next_step": "Resume at an allowed time within the agreed scope; do not shorten the delay or use a custom fetch loop."}
        summary = message + (" " + summary if kind != "unclassified" else "")
        kind = "robots-" + kind
    elif isinstance(error, ValueError):
        kind, summary = "response-rejected", "The response failed a safety or response-format check; it was not accepted as evidence."
    return {"code": kind, "summary": summary, "next_step": _unknown_action(source, config)}


def explain_check(check, source, config):
    outcome = check["outcome"]
    if outcome == "checked-live":
        summary, action = "The included source and checked passages are available.", "Continue readiness for the approved bundle; excluded facts need no reply."
    elif outcome == "known-unavailable" or outcome.startswith("unusable-"):
        detail = f"HTTP {check.get('status')}" if outcome == "known-unavailable" else outcome.removeprefix("unusable-").replace("_", " ")
        summary = f"The source is unavailable or unusable ({detail})."
        action = "This included source blocks readiness. Retain verified recovery or review replacement evidence; do not request an owner waiver of a known failure."
    elif outcome == "direct-shell-for-rendered-capture":
        summary = "Direct HTML returned a thin shell, so it could not verify the retained rendered page."
        action = "Use separately authorized rendering within the agreed budget, or the permitted unknown-availability process for this included source. Do not call the page confirmed live."
    elif outcome == "redirect-needs-identity-review":
        summary, action = "The citation redirected to another URL.", "Retain and confirm the destination, then review affected evidence. Unknown-availability acceptance cannot waive a known redirect."
    elif outcome == "checked-live-comparison-required":
        summary, action = "The page responded, but its representation needs comparison with the saved evidence.", "Compare the retained and observed evidence and record the actual human comparison. Do not repeat unrelated content approval."
    else:
        summary, action = "The observed evidence changed or a checked supporting quote is missing.", "Retain current evidence and review affected wording before a new candidate. Do not waive a known content change."
    return {"code": outcome, "summary": summary, "next_step": action}
