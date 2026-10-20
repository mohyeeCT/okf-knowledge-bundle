"""Guarded HTTP adapters and bounded, resumable Jina collection.

No request occurs at import. Tests inject transports; production collection requires
an explicit authorization flag and a host-configured JINA_API_KEY.
"""
from __future__ import annotations

import email.utils
import http.client
import ipaddress
import json
import math
import os
import random
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from .common import digest, normalize_url, parse_time, public_url, stable_id, utc_now
from .extraction import EXTRACTION_CONTRACT, extract_html
from .ingest import add_version, bounded_gzip, limits, representation_settings
from .review import reviewer_event


UPSTREAM_LIMITATION = "Upstream Jina redirect/DNS hops are unobservable; this adapter validates submitted and observed destinations only."
REQUEST_USER_AGENT = "OKF-Knowledge-Bundle/0.2.1"
HTML_REQUEST_HEADERS = {"Accept": "text/html", "User-Agent": REQUEST_USER_AGENT}
SITEMAP_REQUEST_HEADERS = {"Accept": "application/xml,text/xml,application/gzip", "User-Agent": REQUEST_USER_AGENT}
DEFAULT_SETTINGS = {"engine": "auto", "format": "html", "readiness": "network-idle",
                    "locale": "en", "no_cache": True, "timeout": 60,
                    "token_budget": 100000}
TRANSIENT = {408, 429, 500, 502, 503, 504}
PROVIDER_ACCESS_FAILURES = {401, 402, 403}


def public_addresses(host, port, resolver=None):
    """Resolve once, reject mixed answers, return numeric addresses to pin to I/O."""
    resolver = resolver or socket.getaddrinfo
    addresses = []
    for answer in resolver(host, port, type=socket.SOCK_STREAM):
        address = answer[4][0]
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
        mapped = getattr(parsed, "ipv4_mapped", None)
        if not parsed.is_global or (mapped is not None and not mapped.is_global) or "%" in address:
            raise ValueError("DNS resolved to a non-public destination")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError("DNS supplied no public destination")
    return addresses


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        # Numeric IP avoids a second hostname lookup between validation and connect.
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _read_bounded(response, maximum):
    length = response.getheader("Content-Length")
    if length is not None and int(length) > maximum:
        raise ValueError("HTTP response exceeds the configured byte limit")
    data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("HTTP response exceeds the configured byte limit")
    return data


def direct_fetch(url, config, max_bytes=20 * 1024**2, headers=None, decode_content_encoding=True, follow_redirects=True, before_request=None):
    """Fetch sitemap bytes with pinned public DNS and checks on every redirect."""
    if not config.get("collection_authorized", False):
        raise ValueError("Direct network collection is not authorized")
    current = normalize_url(url)
    redirects = []
    for _ in range(6):
        if not public_url(current, config.get("domains", [])):
            raise ValueError("Direct destination is unsafe or outside approved domains")
        if before_request is not None:
            before_request(current)
        parts = urlsplit(current)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        address = public_addresses(parts.hostname, port)[0]
        cls = _PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP
        connection = cls(parts.hostname, port, address, min(60, int(config.get("http_timeout", 60))))
        try:
            request_headers = {**SITEMAP_REQUEST_HEADERS, "Accept-Encoding": "identity" if headers is None or not decode_content_encoding else "gzip"}
            if headers:
                if any(k.lower() not in ("accept", "accept-encoding", "user-agent") for k in headers):
                    raise ValueError("Direct fetch accepts only non-sensitive request headers")
                request_headers.update(headers)
            connection.request("GET", parts.path + ("?" + parts.query if parts.query else ""), headers=request_headers)
            response = connection.getresponse()
            response_headers = {k.lower(): v for k, v in response.getheaders()}
            if follow_redirects and response.status in (301, 302, 303, 307, 308):
                if not response_headers.get("location"):
                    raise ValueError("Redirect has no destination")
                target = normalize_url(urljoin(current, response_headers["location"]))
                if not public_url(target, config.get("domains", [])):
                    raise ValueError("Redirect is unsafe or outside approved domains")
                redirects.append({"from": current, "to": target, "status": response.status})
                current = target
                continue
            data = _read_bounded(response, max_bytes)
            if decode_content_encoding and response_headers.get("content-encoding", "").lower() == "gzip":
                if not data.startswith(b"\x1f\x8b"):
                    raise ValueError("HTTP response incorrectly labels gzip encoding")
                data = bounded_gzip(data, max_bytes)
            return {"body": data, "status": response.status, "url": current,
                    "headers": response_headers, "redirects": redirects}
        finally:
            connection.close()
    raise ValueError("Direct redirect limit exceeded")


def jina_request(url, settings):
    """Official Reader POST. No cookies, proxy URLs, or credential logging."""
    key = os.environ.get("JINA_API_KEY")
    if not key:
        raise ValueError("Configure JINA_API_KEY in the host environment before authorized collection")
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json", "Content-Type": "application/json",
               "X-Respond-With": "html", "X-Engine": settings["engine"], "X-Locale": settings["locale"],
               "X-Timeout": str(settings["timeout"]), "X-Respond-Timing": settings["readiness"],
               "X-Token-Budget": str(settings["token_budget"])}
    if settings["no_cache"]:
        headers["X-No-Cache"] = "true"
    else:
        headers["X-Cache-Tolerance"] = str(settings["cache_tolerance"])
    body = json.dumps({"url": url, "respondWith": "html"}).encode("utf-8")
    connection = http.client.HTTPSConnection("r.jina.ai", timeout=settings["timeout"], context=ssl.create_default_context())
    try:
        connection.request("POST", "/", body=body, headers=headers)
        response = connection.getresponse()
        # Do not follow provider redirects or forward Authorization elsewhere.
        status = response.status
        response_headers = {k.lower(): v for k, v in response.getheaders()}
        result = {"provider_status": status, "headers": response_headers, "data": {}, "response_hash": None}
        if not 200 <= status < 300:
            # An error body cannot turn this provider status into an origin
            # observation. Do not let a hanging/oversized body erase the status.
            return {**result, "body_status": "not-read-provider-error"}
        try:
            data = _read_bounded(response, settings["response_bytes"])
        except (TimeoutError, socket.timeout, OSError, http.client.HTTPException):
            return {**result, "provider_failure": "Reader response body transport failed", "retryable": True}
        except ValueError:
            return {**result, "provider_failure": "Reader response body exceeds configured bound", "retryable": False}
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {**result, "response_hash": digest(data), "provider_failure": "Reader returned an invalid JSON response", "retryable": False}
        if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), dict):
            return {**result, "response_hash": digest(data), "provider_failure": "Reader returned an invalid page envelope", "retryable": False}
        page = parsed["data"]
        return {"provider_status": status, "headers": response_headers,
                "data": page, "reader_warning": page.get("warning") if isinstance(page, dict) else None,
                "response_hash": digest(data)}
    finally:
        connection.close()


def _settings(config):
    configured = config.get("collection_settings", {})
    settings = {**DEFAULT_SETTINGS, "locale": config.get("locale", "en"), **configured}
    allowed = set(DEFAULT_SETTINGS) | {"cache_tolerance", "response_bytes"}
    if set(settings) - allowed:
        raise ValueError("Unsupported collection setting; silent truncation is forbidden")
    if settings["engine"] not in ("auto", "browser", "curl") or settings["format"] != "html":
        raise ValueError("Unsupported acquisition engine or format")
    if settings["readiness"] not in ("html", "visible-content", "mutation-idle", "resource-idle", "media-idle", "network-idle"):
        raise ValueError("Unsupported readiness setting")
    if type(settings["timeout"]) is not int or type(settings["token_budget"]) is not int or not 1 <= settings["timeout"] <= 60 or not 1 <= settings["token_budget"] <= 1000000:
        raise ValueError("Collection timeout/token limit is invalid")
    if type(settings["no_cache"]) is not bool:
        raise ValueError("Collection no_cache must be a boolean")
    if not settings["no_cache"] and "cache_tolerance" not in settings:
        raise ValueError("Cache-tolerant collection needs an explicit tolerance")
    response_bytes = settings.get("response_bytes", limits(config)["html_bytes"])
    if type(response_bytes) is not int or response_bytes < 1:
        raise ValueError("Collection response byte limit must be a positive integer")
    settings["response_bytes"] = min(response_bytes, limits(config)["html_bytes"])
    settings["extractor"] = EXTRACTION_CONTRACT
    return settings


def _retry_delay(attempt, headers, base):
    value = headers.get("retry-after")
    if value:
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                delay = (email.utils.parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                delay = 0
        return max(0, delay) if math.isfinite(delay) else 0
    return min(30, base * (2 ** (attempt - 1)) + random.uniform(0, base / 4))


def _representation_settings(settings):
    # The token ceiling rejects oversized responses; it does not select different
    # content. Keep the actual ceiling in provenance without forcing recollection.
    return representation_settings(settings)


def _reader_warning(response):
    data = response.get("data", {})
    raw = data.get("warning") if isinstance(data, dict) else None
    raw = raw or response.get("reader_warning")
    if raw is None or raw == "":
        return None
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)


def _page_shape_valid(response):
    """Distinguish protocol shape from an observed empty/unusable target page."""
    page = response.get("data")
    if not isinstance(page, dict):
        return False
    # A warning or explicit failing origin observation must still quarantine or
    # invalidate evidence even when the error page has no HTML representation.
    status = response.get("target_status")
    if _reader_warning(response) or (type(status) is int and not 200 <= status < 300):
        return True
    field = "html" if "html" in page else "content"
    return isinstance(page.get(field), str)


def _commit_collection(state, sid, checkpoint, incremental_checkpoint):
    if incremental_checkpoint:
        incremental_checkpoint(state, sid)
    elif checkpoint:
        checkpoint(state)


def _attempt_descriptor(source, index, record):
    material = {key: value for key, value in record.items() if key != "reconciliation"}
    return {"attempt_index": index,
            "attempt_id": record.get("attempt_id") or stable_id("attempt", [source["id"], index, material]),
            "attempt_hash": digest(material), "source_id": source["id"],
            "source_url": record.get("source_url", source.get("normalized_url")), "requested_at": record.get("requested_at"),
            "version_id_at_request": record.get("version_id_at_request"), "current_version_id": source.get("active_version")}


def _valid_reconciliation(source, index, record):
    event = record.get("reconciliation")
    if not isinstance(event, dict) or event.get("action") not in ("allow-retry", "close-without-retry"):
        return False
    descriptor = _attempt_descriptor(source, index, record)
    if descriptor["source_url"] != source.get("normalized_url"):
        return False
    if any(event.get(key) != descriptor[key] for key in ("attempt_index", "attempt_id", "attempt_hash", "source_id", "source_url")):
        return False
    designated = event.get("authorized_reviewers")
    if (event.get("id") != stable_id("attempt-review", {key: value for key, value in event.items() if key != "id"})
            or event.get("collection_authorized") is not True or not isinstance(designated, list) or event.get("by") not in designated):
        return False
    try:
        reviewer_event(event.get("by"), event.get("at"), event.get("channel"), event.get("reference"), descriptor["attempt_hash"])
    except (ValueError, TypeError):
        return False
    return bool(event.get("reason"))


def pending_attempts(source):
    """Unreconciled requests anywhere in history, including legacy pending rows."""
    return [_attempt_descriptor(source, index, record)
            for index, record in enumerate(source.get("collection_attempts", []))
            if record.get("outcome") == "pending" and not _valid_reconciliation(source, index, record)]


def awaiting_retry_results(source):
    """A retry decision is not a committed response and cannot lift release gates."""
    attempts = source.get("collection_attempts", [])
    awaiting = []
    for index, record in enumerate(attempts):
        if not _valid_reconciliation(source, index, record) or record["reconciliation"]["action"] != "allow-retry":
            continue
        event = record["reconciliation"]
        start = event.get("followup_after_attempt_count", index + 1)
        if type(start) is not int or not index < start <= len(attempts):
            awaiting.append(_attempt_descriptor(source, index, record))
            continue
        # Legacy events lack a sequence boundary. Request time prevents earlier
        # history from satisfying a later decision; floor to the stored request
        # timestamp precision so a legitimate same-second retry remains valid.
        boundary = parse_time(event["at"]).replace(microsecond=0)
        def committed(attempt):
            try:
                return (attempt.get("outcome") in {"success", "failed", "provider_error"}
                        and attempt.get("completed_at") is not None
                        and parse_time(attempt["requested_at"]) >= boundary)
            except (KeyError, TypeError, ValueError):
                return False
        if not any(committed(attempt) for attempt in attempts[start:]):
            awaiting.append(_attempt_descriptor(source, index, record))
    return awaiting


def _retry_closed(source):
    return source.get("collection_retry_policy") == "close-without-retry" or any(
        _valid_reconciliation(source, index, record) and record["reconciliation"]["action"] == "close-without-retry"
        for index, record in enumerate(source.get("collection_attempts", [])))


def reconcile_attempt(state, source_id, attempt_index, action, reason, by, at, channel, reference,
                      *, expected_attempt_hash, expected_version):
    """Audit an unknown request without refunding usage or fabricating a result.

    Caller saves under the writer lock. A repeated same decision is idempotent;
    stale request/version intent and changed decisions are rejected.
    """
    source = state.get("sources", {}).get(source_id)
    if not source or type(attempt_index) is not int or not 0 <= attempt_index < len(source.get("collection_attempts", [])):
        raise ValueError("Unknown source or invalid zero-based attempt index")
    if action not in ("allow-retry", "close-without-retry") or not isinstance(reason, str) or not reason.strip():
        raise ValueError("Reconciliation needs an explicit supported action and reason")
    config = state["config"]
    designated = config.get("collection_reviewers", [config.get("publication_owner")])
    if (not config.get("collection_authorized") or not isinstance(designated, list)
            or not designated or any(not isinstance(role, str) for role in designated) or by not in designated):
        raise ValueError("Reconciliation requires collection authorization and a designated human collection reviewer")
    record = source["collection_attempts"][attempt_index]
    if record.get("outcome") != "pending":
        raise ValueError("Only an unresolved request outcome may be reconciled")
    descriptor = _attempt_descriptor(source, attempt_index, record)
    if (descriptor["attempt_hash"] != expected_attempt_hash or source.get("active_version") != expected_version
            or descriptor["source_url"] != source.get("normalized_url")):
        raise ValueError("Stale attempt hash or source version; inspect the current request before deciding")
    if expected_version is not None and expected_version not in source.get("versions", {}):
        raise ValueError("Current source version is missing")
    event = reviewer_event(by, at, channel, reference, descriptor["attempt_hash"], "attempt_reconciliation", action)
    decision_time = parse_time(at)
    if record.get("requested_at") and decision_time < parse_time(record["requested_at"]):
        raise ValueError("Reconciliation time must follow the request")
    current_version = source.get("versions", {}).get(source.get("active_version"), {})
    if current_version.get("imported_at") and decision_time < parse_time(current_version["imported_at"]):
        raise ValueError("Reconciliation predates the current source version")
    if record.get("reconciliation"):
        if _valid_reconciliation(source, attempt_index, record) and record["reconciliation"]["action"] == action:
            return record["reconciliation"]
        raise ValueError("This attempt already has a different or stale reconciliation; history cannot be replaced")
    if _retry_closed(source) and action != "close-without-retry":
        raise ValueError("Collection was closed without retry for this source in this run")
    event.update(descriptor, action=action, reason=reason.strip(), reconciled_at_version=source.get("active_version"),
                 followup_after_attempt_count=len(source["collection_attempts"]),
                 authorized_reviewers=list(designated), collection_authorized=True,
                 accounting="Original outcome and unknown usage reservation retained; no refund or captured result inferred")
    event["id"] = stable_id("attempt-review", event)
    record["reconciliation"] = event
    source.setdefault("attempt_reconciliation_history", []).append(event)
    state.setdefault("review_history", []).append({"type": "attempt_reconciliation", **event})
    source["collection_retry_policy"] = action
    unresolved = pending_attempts(source)
    source["collection_state"] = "pending" if unresolved else "retry-authorized" if action == "allow-retry" else "closed-without-retry"
    for row in state.get("inventory", []):
        if row.get("source_id") == source_id and row.get("state") != "duplicate":
            row.update(state="included" if source.get("active_version") and not unresolved else "pending",
                       reason="Unknown request reconciled; previous evidence and original budget accounting retained")
    return event


def _collection_indexes(state):
    rows, claims, aliases = {}, {}, {}
    for row in state["inventory"]:
        rows.setdefault(row.get("source_id"), []).append(row)
    for claim in state.get("claims", {}).values():
        for sid in {e["source_id"] for e in claim.get("evidence", [])}:
            claims.setdefault(sid, []).append(claim)
    for sid, source in state["sources"].items():
        if source.get("alias_of"):
            aliases.setdefault(source["alias_of"], []).append(sid)
    return rows, claims, aliases


def _set_rows(state, sid, status, reason, row_index):
    for row in row_index.get(sid, ()):
        if row["state"] != "duplicate":
            row.update(state=status, reason=reason)


def _invalidate_source_reviews(state, sid, reason, claim_index, alias_index):
    affected, queue = set(), [sid]
    while queue:
        current = queue.pop()
        if current not in affected:
            affected.add(current)
            queue.extend(alias_index.get(current, ()))
    for current in affected:
        source = state["sources"][current]
        source["availability_acceptance"] = None
        if source.get("alias_of"):
            target = source.pop("alias_of")
            source["source_key"] = source.get("original_source_key", source["id"])
            source["alias_review_required"] = True
            source.setdefault("alias_history", []).append({"kind": "automatic-invalidation", "at": utc_now(),
                "trigger_source_id": sid, "previous_target_id": target, "reason": reason, "human_verified": False})
        for claim in claim_index.get(current, ()):
            claim.update(evidence_review="pending", publication="pending", refresh_reason=reason)
            if claim.get("business_confirmation") != "not_required":
                claim["business_confirmation"] = "required"
    state["release_approvals"] = {}


def _success_content(source, response, config, settings):
    data = response.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("Reader response data is not an object")
    warning = _reader_warning(response)
    target_status = response.get("target_status")
    if warning:
        # Official FormattedPageDto.warning / SnapshotFormatter source emits this
        # line for origin HTTP errors, including HTML responses. Other warnings
        # remain unclassified and require review, never an inferred origin status.
        matches = re.findall(r"(?m)^Target URL returned error ([1-5][0-9]{2})(?::[^\n]*)?$", warning)
        reported_errors = [int(value) for value in matches if not 200 <= int(value) < 400]
        if reported_errors:
            target_status = reported_errors[0]
            response["target_status"] = target_status
            response["target_status_origin"] = "Reader FormattedPageDto.warning target-error assertion"
        source.update(scope="pending", scope_reason="Reader warning requires evidence/availability review")
        source["reader_warning_review_required"] = True
        source["warnings"] = list(dict.fromkeys(source["warnings"] + ["Reader warning requires review: " + warning]))
    if isinstance(target_status, int) and not 200 <= target_status < 300:
        source.update(captured_status=target_status, availability="nonpublic" if target_status in (401, 403) else "failed",
                      captured_status_origin=response.get("target_status_origin", "Explicit Reader transport target assertion"))
        raise ValueError("Reader observed a failing target-site HTTP status")
    observed = {"returned_url": data.get("url"), "final_url": response.get("final_url")}
    for field, value in observed.items():
        if value:
            if not public_url(value, config.get("domains", [])):
                raise ValueError("Observed Reader destination is unsafe or outside approved domains")
            source[field] = value
            if normalize_url(value) != source["normalized_url"]:
                raise ValueError("Observed destination differs from requested identity; alias review required")
    content = data.get("html") or data.get("content") or ""
    if not isinstance(content, str) or len(content.encode("utf-8")) > settings["response_bytes"]:
        raise ValueError("Reader content is invalid or exceeds the configured byte limit")
    if response.get("truncated") or data.get("truncated") or data.get("isTruncated"):
        raise ValueError("Truncated Reader evidence rejected")
    if not content.strip():
        raise ValueError("Reader content is empty")
    if "<" not in content or ">" not in content:
        raise ValueError("Reader did not return the requested HTML representation")
    extracted = extract_html(content, source["normalized_url"])
    if extracted["quality"] != "usable":
        if extracted["quality"] in ("login", "challenge"):
            source["availability"] = "nonpublic"
        raise ValueError("Extraction quality: " + extracted["quality"])
    version_id = add_version(source, content, extracted, "jina-dom", config, settings)
    version = source["versions"][version_id]
    version.setdefault("retrieved_at", utc_now())
    version.setdefault("cache_status", response.get("cache_status"))
    version.setdefault("cache_age", response.get("cache_age"))
    version.setdefault("capture_date_basis", "unknown; retrieval time is not origin capture time")
    version.setdefault("collector", "jina-reader")
    version.setdefault("response_hash", response.get("response_hash"))
    version.setdefault("usage", data.get("usage") or response.get("usage"))
    if warning:
        version["reader_warning"] = warning
        version["warnings"] = list(dict.fromkeys(version["warnings"] + ["Reader warning requires review: " + warning]))
        raise ValueError("Reader warning requires human review; capture quarantined")
    source["reader_warning_review_required"] = False
    # Explicit target observations only; Reader HTTP 200 does not fill this field.
    source["captured_status"] = response.get("target_status")
    source["captured_status_origin"] = "Explicit Reader transport target observation" if source["captured_status"] is not None else "Unknown; Reader HTTP status is separate"
    if response.get("availability_checked") is True and isinstance(source["captured_status"], int):
        source["availability"] = "live" if 200 <= source["captured_status"] < 300 else "failed"
        source["availability_checked_at"] = response.get("availability_checked_at") or utc_now()
    else:
        if source["availability"] not in ("failed", "nonpublic"):
            source["availability"] = "unknown"
    if any("requires" in w for w in extracted["warnings"]):
        source.update(scope="pending", scope_reason="Extraction/relevance/indexing review required")
    return version_id


def collect(state, transport=None, refresh=False, max_pages=500, max_attempts=1500, checkpoint=None, incremental_checkpoint=None):
    """Collect an authorized batch; fixture transport has signature (url, settings).

    Commit each request reservation before transport, then its result. A crash
    leaves a conservative unknown-usage reservation, never an uncounted request.
    The final checkpoint(state) compacts the batch. Caller holds the lock.
    """
    config = state["config"]
    if not config.get("collection_authorized", False):
        raise ValueError("Page collection is not authorized")
    settings = _settings(config)
    if transport is None and not os.environ.get("JINA_API_KEY"):
        raise ValueError("Configure JINA_API_KEY in the host environment before authorized collection")
    row_index, claim_index, alias_index = _collection_indexes(state)
    transport = transport or jina_request
    budget = config.get("collection_budget", {})
    for name, value in budget.items():
        if name in ("max_pages", "max_attempts", "max_tokens") and (type(value) is not int or value < 1):
            raise ValueError("Collection budget ceilings must be positive integers")
    page_limit = min(max_pages, budget.get("max_pages", max_pages))
    attempt_limit = min(max_attempts, budget.get("max_attempts", max_attempts))
    if page_limit < 1 or attempt_limit < 1:
        raise ValueError("Collection ceilings must be positive")
    retry_base = config.get("retry_base_delay", 1.0)
    retry_count = config.get("collection_max_attempts_per_page", 3)
    if type(retry_count) is not int or retry_count < 1:
        raise ValueError("Per-page attempts must be a positive integer")
    if type(retry_base) not in (int, float) or not math.isfinite(retry_base) or retry_base < 0:
        raise ValueError("Retry delay must be a finite nonnegative number")
    retry_count = min(3, retry_count)
    summary = {"started_at": utc_now(), "pages": 0, "attempts": 0, "reused": 0, "succeeded": 0, "failed": 0,
               "provider_tokens": 0, "reserved_unknown_tokens": 0, "usage_unknown": 0, "settings": settings, "stopped_reason": None,
               "blocked_pending": 0, "closed_without_retry": 0}
    history = state.setdefault("collection_history", [])
    prior_pages = sum(h.get("pages", 0) for h in history)
    prior_attempts = sum(h.get("attempts", 0) for h in history)
    prior_tokens = sum(h.get("provider_tokens", 0) + h.get("reserved_unknown_tokens", 0) for h in history)
    if "max_pages" in budget:
        page_limit = min(page_limit, max(0, budget["max_pages"] - prior_pages))
    if "max_attempts" in budget:
        attempt_limit = min(attempt_limit, max(0, budget["max_attempts"] - prior_attempts))
    history.append(summary)
    state["collection"] = summary
    warnings = state.setdefault("warnings", [])
    if UPSTREAM_LIMITATION not in warnings:
        warnings.append(UPSTREAM_LIMITATION)
    token_limit = budget.get("max_tokens")
    for sid, source in state["sources"].items():
        if source["scope"] != "included" or not source["identity_confirmed"]:
            continue
        if pending_attempts(source):
            source["collection_state"] = "pending"
            _set_rows(state, sid, "pending", "Unknown request outcome needs explicit human reconciliation before another request", row_index)
            summary["blocked_pending"] += 1
            continue
        if _retry_closed(source):
            summary["closed_without_retry"] += 1
            continue
        if source.get("retry_not_before_epoch", 0) > time.time():
            summary["stopped_reason"] = "Provider cooldown has not expired; pending inventory retained"
            break
        active = source["versions"].get(source.get("active_version"))
        previous_failed = bool(source.get("collection_attempts") and source["collection_attempts"][-1]["outcome"] != "success")
        if active and not refresh and not previous_failed:
            current_extractor = active.get("settings", {}).get("extractor") == EXTRACTION_CONTRACT
            if current_extractor and (active["representation"].startswith("sf-") or _representation_settings(active.get("settings", {})) == _representation_settings(settings)):
                summary["reused"] += 1
                continue
            _set_rows(state, sid, "pending", "Acquisition settings changed; explicitly refresh before replacing evidence", row_index)
            continue
        if summary["pages"] >= page_limit or summary["attempts"] >= attempt_limit or (token_limit is not None and prior_tokens + summary["provider_tokens"] + summary["reserved_unknown_tokens"] >= token_limit):
            summary["stopped_reason"] = "Configured page/attempt/provider-token ceiling reached; pending inventory retained"
            break
        if not public_url(source["normalized_url"], config.get("domains", [])):
            _set_rows(state, sid, "excluded", "Submitted destination is unsafe or outside approved domains", row_index)
            source.update(scope="excluded", scope_reason="Unsafe destination")
            continue
        summary["pages"] += 1
        success = False
        for attempt in range(1, retry_count + 1):
            if summary["attempts"] >= attempt_limit:
                break
            # Reserve the maximum per-request token budget; do not knowingly exceed
            # an authorized token ceiling even when the previous usage was unknown.
            request_settings = dict(settings)
            if token_limit is not None:
                remaining = token_limit - prior_tokens - summary["provider_tokens"] - summary["reserved_unknown_tokens"]
                if remaining < 1:
                    break
                request_settings["token_budget"] = min(settings["token_budget"], remaining)
            summary["attempts"] += 1
            requested_at = utc_now()
            status, response, reason, retry, provider_failure = None, {}, "", False, True
            previous_version = source.get("active_version")
            previous_collection_state = source.get("collection_state")
            previous_rows = [(row, row["state"], row["reason"]) for row in row_index.get(sid, ())]
            reservation = request_settings["token_budget"]
            summary["usage_unknown"] += 1
            summary["reserved_unknown_tokens"] += reservation
            record = {"requested_at": requested_at, "completed_at": None, "provider_status": None,
                      "outcome": "pending", "reason": "Request reserved; response and usage are unknown until committed",
                      "settings": request_settings, "usage_tokens": None, "reserved_tokens": reservation,
                      "cache_status": None, "cache_age": None, "response_hash": None, "target_status": None, "reader_warning": None}
            record.update(source_url=source["normalized_url"], version_id_at_request=previous_version)
            record["attempt_id"] = stable_id("attempt", [sid, len(source.get("collection_attempts", [])), record])
            source.setdefault("collection_attempts", []).append(record)
            source["collection_state"] = "pending"
            _set_rows(state, sid, "pending", "Request reserved; awaiting a committed result", row_index)
            _commit_collection(state, sid, checkpoint, incremental_checkpoint)
            try:
                response = transport(source["normalized_url"], request_settings)
                if not isinstance(response, dict):
                    response = {}
                    raise ValueError("Reader response is not an object")
                status = response.get("provider_status")
                source["provider_status"] = status
                if response.get("provider_failure"):
                    reason = "Reader response transport/protocol failure"
                    retry = response.get("retryable") is True
                elif not isinstance(status, int) or not 200 <= status < 300:
                    retry = status in TRANSIENT
                    reason = "Reader HTTP " + str(status)
                elif not _page_shape_valid(response):
                    reason = "Reader response has an invalid page envelope"
                else:
                    provider_failure = False
                    _success_content(source, response, config, request_settings)
                    success = True
                    reason = "Captured HTML; current public availability remains separately evaluated"
            except (TimeoutError, socket.timeout):
                reason, retry = "Reader request timed out", True
            except (OSError, http.client.HTTPException):
                reason, retry = "Reader transport failed", True
            except (ValueError, TypeError, KeyError) as exc:
                # Exceptions may originate in provider-supplied text. Keep logs free
                # of raw payloads and environment credentials while preserving known outcomes.
                reason = str(exc) if type(exc) is ValueError and str(exc).startswith(("Reader", "Observed", "Extraction", "Truncated", "Configure")) else "Reader response validation failed"
            data = response.get("data", {}) if isinstance(response, dict) else {}
            usage = data.get("usage") if isinstance(data, dict) else None
            usage = usage or response.get("usage")
            tokens = usage.get("tokens") if isinstance(usage, dict) else None
            provider_access_failure = status in PROVIDER_ACCESS_FAILURES
            if type(tokens) is int and tokens >= 0:
                summary["provider_tokens"] += tokens
                summary["usage_unknown"] -= 1
                summary["reserved_unknown_tokens"] -= reservation
                record["reserved_tokens"] = 0
            record.update({"completed_at": utc_now(), "provider_status": status,
                      "outcome": "provider_error" if provider_failure else "success" if success else "failed", "reason": reason,
                      "settings": request_settings, "usage_tokens": tokens,
                      "cache_status": response.get("cache_status"), "cache_age": response.get("cache_age"),
                      "response_hash": response.get("response_hash"), "target_status": response.get("target_status"),
                      "target_status_origin": response.get("target_status_origin"), "reader_warning": _reader_warning(response),
                      "body_status": response.get("body_status")})
            if provider_failure:
                # A provider HTTP/transport/protocol failure is not an origin
                # observation. Preserve already reviewed source evidence.
                if previous_collection_state is None:
                    source.pop("collection_state", None)
                else:
                    source["collection_state"] = previous_collection_state
                for row, row_state, row_reason in previous_rows:
                    row.update(state=row_state, reason=row_reason)
                if not previous_version:
                    _set_rows(state, sid, "failed", reason, row_index)
                if provider_access_failure:
                    summary["stopped_reason"] = "Reader provider/account/access failure; resolve before another batch"
                    retry = False
            else:
                source["collection_state"] = "complete" if success else "failed"
                if not success or previous_version != source.get("active_version"):
                    _invalidate_source_reviews(state, sid, "Source changed or capture failed; recheck public evidence and availability", claim_index, alias_index)
                if not success and source["availability"] not in ("nonpublic", "failed"):
                    source["availability"] = "unknown"
                if not success and source["availability"] == "nonpublic":
                    source["scope"] = "excluded"
                    source["scope_reason"] = "Non-public/challenge content is not releasable evidence"
                _set_rows(state, sid, "included" if success and source["scope"] == "included" else "pending" if success else "failed", reason, row_index)
            done = success or not retry or attempt >= retry_count or summary["attempts"] >= attempt_limit
            if token_limit is not None and prior_tokens + summary["provider_tokens"] + summary["reserved_unknown_tokens"] >= token_limit:
                done = True
            delay = 0
            if not done:
                delay = _retry_delay(attempt, response.get("headers", {}), retry_base)
                if delay > 30:
                    source["retry_not_before_epoch"] = time.time() + delay
                    summary["stopped_reason"] = "Provider Retry-After exceeds immediate wait; resume after recorded cooldown"
                    done = True
            if done:
                summary["succeeded" if success else "failed"] += 1
            _commit_collection(state, sid, checkpoint, incremental_checkpoint)
            if done:
                break
            if delay:
                time.sleep(delay)
        if summary["stopped_reason"]:
            break
    summary["pending"] = sum(r["state"] in ("pending", "failed") for r in state["inventory"])
    summary["finished_at"] = utc_now()
    if checkpoint:
        checkpoint(state)
    return state
