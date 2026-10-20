import copy
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from okf_bundle.ingest import discover_csv
from okf_bundle.common import utc_now
from okf_bundle.network import UPSTREAM_LIMITATION, _PinnedHTTPS, collect, direct_fetch, jina_request, pending_attempts, public_addresses, reconcile_attempt


CONFIG = {"domains": ["example.com"], "locale": "en", "collection_authorized": True, "retry_base_delay": 0}


def success(url="https://example.com/a/", text="Our training includes classroom instruction for adult learners and a separate examination."):
    return {"provider_status": 200, "data": {"url": url, "content": '<html><head><title>Training</title><link rel="canonical" href="' + url + '"></head><body><h1>Training</h1><p>' + text + '</p></body></html>', "usage": {"tokens": 80}}}


class FakeResponse:
    def __init__(self, status=200, body=b"<urlset/>", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    def getheader(self, key):
        return self.headers.get(key.lower())

    def getheaders(self):
        return list(self.headers.items())

    def read(self, size):
        return self.body[:size]


class NetworkTests(unittest.TestCase):
    def state(self, count=1, config=None):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "urls.csv"
            path.write_text("URL\n" + "\n".join("https://example.com/" + ("a" if i == 0 else str(i)) + "/" for i in range(count)), encoding="utf-8")
            cfg = {**CONFIG, **(config or {})}
            state = {**discover_csv(path, cfg), "config": cfg, "claims": {}, "release_approvals": {}}
        return state

    def test_success_keeps_status_date_cache_and_representation_distinct(self):
        state = self.state()
        checkpoints = []
        collect(state, lambda url, settings: success(url), checkpoint=lambda s: checkpoints.append(copy.deepcopy(s)))
        source = next(iter(state["sources"].values()))
        version = source["versions"][source["active_version"]]
        self.assertEqual(200, source["provider_status"])
        self.assertIsNone(source["captured_status"])
        self.assertEqual("unknown", source["availability"])
        self.assertIsNone(version["capture_at"])
        self.assertIsNone(version["cache_status"])
        self.assertIsNone(version["cache_age"])
        self.assertEqual("jina-dom", version["representation"])
        self.assertTrue(version["settings"]["no_cache"])
        self.assertIn("retrieved_at", version)
        self.assertNotIn("final_url", source)
        self.assertEqual(source["requested_url"], source["returned_url"])
        self.assertIn(UPSTREAM_LIMITATION, state["warnings"])
        self.assertEqual(3, len(checkpoints))
        reserved = checkpoints[0]
        self.assertEqual("pending", next(iter(reserved["sources"].values()))["collection_attempts"][-1]["outcome"])
        self.assertEqual(100000, reserved["collection"]["reserved_unknown_tokens"])
        self.assertEqual(0, state["collection"]["reserved_unknown_tokens"])

    def test_retry_429_timeout_then_success_bounded_and_resume(self):
        state = self.state()
        responses = [{"provider_status": 429, "headers": {"retry-after": "0"}}, TimeoutError(), success()]
        calls = []
        def transport(url, settings):
            calls.append(url)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        collect(state, transport)
        self.assertEqual(3, len(calls))
        self.assertEqual(1, state["collection"]["succeeded"])
        collect(state, lambda *_: self.fail("Unchanged capture should be reused"))
        self.assertEqual(1, state["collection"]["reused"])
        fresh = self.state()
        calls.clear()
        collect(fresh, lambda *args: calls.append(1) or {"provider_status": 503})
        self.assertEqual(3, len(calls))
        self.assertEqual("failed", fresh["inventory"][0]["state"])

    def test_quality_failures_never_become_successful_evidence(self):
        cases = {
            "empty": {"provider_status": 200, "data": {"content": ""}},
            "placeholder": {"provider_status": 200, "data": {"content": "<html><body>Loading...</body></html>"}},
            "login": {"provider_status": 200, "data": {"content": '<html><body><h1>Log in</h1><p>Enter your password to access the current training material.</p><input type="password"></body></html>'}},
            "challenge": {"provider_status": 200, "data": {"content": "<html><title>Just a moment</title><body>Verify you are human before continuing to the site.</body></html>"}},
            "truncated": {**success(), "truncated": True},
            "wrong_format": {"provider_status": 200, "data": {"content": "# Markdown returned despite an explicit request for HTML."}},
            "invalid": ["wrong", "shape"],
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                state = self.state()
                collect(state, lambda *_: response)
                source = next(iter(state["sources"].values()))
                self.assertIsNone(source["active_version"])
                self.assertEqual(0, state["collection"]["succeeded"])
                self.assertEqual(1, state["collection"]["attempts"])
                if name in ("login", "challenge"):
                    self.assertEqual("nonpublic", source["availability"])

    def test_destination_scope_alias_and_literal_private_checks(self):
        for returned in ("https://127.0.0.1/", "https://outside.example/a/", "https://example.com/b/", "https://user:secret@example.com/a/"):
            state = self.state()
            collect(state, lambda *_: success(returned))
            self.assertIsNone(next(iter(state["sources"].values()))["active_version"])
        state = self.state(config={"domains": ["127.0.0.1"]})
        collect(state, lambda *_: self.fail("Unsafe submitted URL must not be sent"))
        self.assertEqual(0, state["collection"]["attempts"])

    def test_batch_ceiling_resume_and_total_authorized_budgets(self):
        state = self.state(3)
        collect(state, lambda url, settings: success(url), max_pages=1)
        self.assertEqual(2, state["collection"]["pending"])
        collect(state, lambda url, settings: success(url), max_pages=1)
        self.assertEqual(1, state["collection"]["pending"])
        capped = self.state(3, {"collection_budget": {"max_pages": 1, "max_attempts": 1}})
        collect(capped, lambda url, settings: success(url))
        collect(capped, lambda *_: self.fail("Resume must not reset the authorized budget"))
        self.assertEqual(0, capped["collection"]["attempts"])
        tokens = self.state(2, {"collection_budget": {"max_tokens": 100}})
        observed = []
        def unmetered(url, settings):
            observed.append(settings["token_budget"])
            response = success(url)
            response["data"].pop("usage")
            return response
        collect(tokens, unmetered)
        self.assertEqual([100], observed)
        self.assertEqual(0, tokens["collection"]["provider_tokens"])
        self.assertEqual(100, tokens["collection"]["reserved_unknown_tokens"])
        collect(tokens, lambda *_: self.fail("Unknown usage reservation must persist"))

    def test_failed_and_changed_refresh_invalidate_dependents_without_erasing_history(self):
        state = self.state()
        collect(state, lambda url, settings: success(url))
        source = next(iter(state["sources"].values()))
        original = source["active_version"]
        claim = {"evidence": [{"source_id": source["id"], "version_id": original}], "evidence_review": "approved", "publication": "approved", "business_confirmation": "confirmed"}
        state["claims"]["clm"] = claim
        state["release_approvals"] = {"release": "approved"}
        source["availability_acceptance"] = {"owner": "approved"}
        collect(state, lambda *_: {**success(), "truncated": True}, refresh=True)
        self.assertEqual(original, source["active_version"])
        self.assertEqual("pending", claim["publication"])
        self.assertEqual("required", claim["business_confirmation"])
        self.assertIsNone(source["availability_acceptance"])
        self.assertEqual({}, state["release_approvals"])
        collect(state, lambda url, settings: success(url, "Changed training duration is two hours for adult participants in the classroom."))
        self.assertNotEqual(original, source["active_version"])
        self.assertIn(original, source["versions"])
        self.assertEqual("pending", claim["evidence_review"])

    def test_request_token_cap_does_not_change_representation_reuse(self):
        state = self.state(config={"collection_budget": {"max_tokens": 1000}})
        collect(state, lambda url, settings: success(url))
        source = next(iter(state["sources"].values()))
        self.assertEqual(1000, source["versions"][source["active_version"]]["settings"]["token_budget"])
        collect(state, lambda *_: self.fail("Unchanged successful capture must be reused"))
        self.assertEqual(1, state["collection"]["reused"])
        self.assertEqual("included", state["inventory"][0]["state"])
        state["config"]["collection_settings"] = {"readiness": "html"}
        collect(state, lambda *_: self.fail("Changed representation needs explicit refresh"))
        self.assertEqual(0, state["collection"]["reused"])
        self.assertEqual("pending", state["inventory"][0]["state"])

    def test_operational_caps_keep_version_identity_and_exact_settings_provenance(self):
        state = self.state(config={"collection_settings": {"token_budget": 1000, "response_bytes": 20000}})
        collect(state, lambda url, settings: success(url))
        source = next(iter(state["sources"].values()))
        original = source["active_version"]
        state["claims"]["claim"] = {"evidence": [{"source_id": source["id"]}], "evidence_review": "accepted", "publication": "approved", "business_confirmation": "confirmed"}
        state["release_approvals"] = {"release": "approved"}
        source["availability_acceptance"] = {"owner": "approved"}
        state["config"]["collection_settings"] = {"token_budget": 2000, "response_bytes": 30000}
        collect(state, lambda *_: self.fail("Byte/token caps alone do not alter the representation"))
        self.assertEqual(1, state["collection"]["reused"])
        collect(state, lambda url, settings: success(url), refresh=True)
        self.assertEqual(original, source["active_version"])
        self.assertEqual(1, len(source["versions"]))
        self.assertEqual([1000, 2000], [item["token_budget"] for item in source["versions"][original]["settings_history"]])
        self.assertEqual([20000, 30000], [item["settings"]["response_bytes"] for item in source["collection_attempts"]])
        self.assertEqual("approved", state["claims"]["claim"]["publication"])
        self.assertTrue(state["release_approvals"])
        self.assertIsNotNone(source["availability_acceptance"])

    def test_old_extractor_requires_explicit_refresh_and_invalidates_reviews(self):
        for old_contract in (None, "html-v1"):
            with self.subTest(old_contract=old_contract):
                state = self.state()
                with patch("okf_bundle.network.EXTRACTION_CONTRACT", old_contract):
                    collect(state, lambda url, settings: success(url))
                source = next(iter(state["sources"].values()))
                old = source["active_version"]
                if old_contract is None:
                    source["versions"][old]["settings"].pop("extractor", None)
                state["claims"]["claim"] = {"evidence": [{"source_id": source["id"], "version_id": old}],
                    "evidence_review": "accepted", "publication": "approved", "business_confirmation": "confirmed"}
                state["release_approvals"] = {"release": "approved"}
                collect(state, lambda *_: self.fail("Old extractor requires explicit refresh"))
                self.assertEqual(0, state["collection"]["reused"])
                self.assertEqual("pending", state["inventory"][0]["state"])
                self.assertEqual(old, source["active_version"])
                collect(state, lambda url, settings: success(url), refresh=True)
                self.assertNotEqual(old, source["active_version"])
                self.assertEqual(2, len(source["versions"]))
                self.assertEqual(1, len({v["content_hash"] for v in source["versions"].values()}))
                self.assertEqual("pending", state["claims"]["claim"]["publication"])
                self.assertEqual({}, state["release_approvals"])

    def test_provider_access_failure_stops_batch_without_clearing_source_reviews(self):
        for status in (401, 402, 403):
            with self.subTest(status=status):
                state = self.state(2)
                collect(state, lambda url, settings: success(url))
                source = next(iter(state["sources"].values()))
                source["availability_acceptance"] = {"owner": "approved"}
                state["claims"]["claim"] = {"evidence": [{"source_id": source["id"]}], "evidence_review": "accepted", "publication": "approved", "business_confirmation": "confirmed"}
                state["release_approvals"] = {"release": "approved"}
                before = copy.deepcopy(state)
                calls = []
                collect(state, lambda *_: calls.append(status) or {"provider_status": status}, refresh=True)
                self.assertEqual([status], calls)
                self.assertEqual(before["claims"], state["claims"])
                self.assertEqual(before["release_approvals"], state["release_approvals"])
                self.assertEqual(before["inventory"], state["inventory"])
                self.assertEqual(before["sources"][source["id"]]["availability_acceptance"], source["availability_acceptance"])
                self.assertEqual("complete", source["collection_state"])
                self.assertEqual("provider_error", source["collection_attempts"][-1]["outcome"])
                self.assertEqual(1, state["collection"]["attempts"])
                self.assertEqual(100000, state["collection"]["reserved_unknown_tokens"])
                self.assertIn("provider/account/access", state["collection"]["stopped_reason"])

    def reviewed(self, count=1):
        state = self.state(count)
        collect(state, lambda url, settings: success(url))
        source = next(iter(state["sources"].values()))
        state["claims"]["claim"] = {"evidence": [{"source_id": source["id"], "version_id": source["active_version"]}],
            "evidence_review": "accepted", "publication": "approved", "business_confirmation": "confirmed"}
        state["release_approvals"] = {"release": "approved"}
        source["availability_acceptance"] = {"owner": "approved"}
        return state, source

    def test_provider_failures_preserve_reviews_and_transient_identical_success(self):
        for failure in (TimeoutError(), OSError(), {"provider_status": 429}, {"provider_status": 503}, {"provider_status": 422}, {"provider_status": 404}):
            with self.subTest(failure=str(failure)):
                state, source = self.reviewed()
                before = copy.deepcopy(state)
                def transport(*_):
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                collect(state, transport, refresh=True)
                self.assertEqual(before["claims"], state["claims"])
                self.assertEqual(before["release_approvals"], state["release_approvals"])
                self.assertEqual(before["sources"][source["id"]]["availability_acceptance"], source["availability_acceptance"])
                self.assertEqual(before["sources"][source["id"]]["versions"], source["versions"])
                self.assertEqual("provider_error", source["collection_attempts"][-1]["outcome"])
                self.assertLessEqual(state["collection"]["attempts"], 3)
        state, source = self.reviewed()
        before = copy.deepcopy(state)
        responses = [{"provider_status": 429}, TimeoutError(), success()]
        def transient(*_):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        collect(state, transient, refresh=True)
        self.assertEqual(3, state["collection"]["attempts"])
        self.assertEqual(200000, state["collection"]["reserved_unknown_tokens"])
        self.assertEqual(80, state["collection"]["provider_tokens"])
        self.assertEqual(before["claims"], state["claims"])
        self.assertEqual(before["release_approvals"], state["release_approvals"])
        self.assertEqual(before["sources"][source["id"]]["availability_acceptance"], source["availability_acceptance"])

    def test_actual_adapter_error_status_survives_unreadable_body(self):
        for status in (401, 402, 403):
            for body_failure in ("timeout", "oversize"):
                with self.subTest(status=status, body_failure=body_failure):
                    state, source = self.reviewed(2)
                    state["config"]["collection_settings"] = {"response_bytes": 1000}
                    before = copy.deepcopy(state)
                    response = FakeResponse(status=status)
                    response.read = MagicMock(side_effect=TimeoutError()) if body_failure == "timeout" else MagicMock(return_value=b"x" * 1001)
                    connection = MagicMock()
                    connection.getresponse.return_value = response
                    with patch.dict(os.environ, {"JINA_API_KEY": "OFFLINE_TEST_ONLY"}), patch("okf_bundle.network.http.client.HTTPSConnection", return_value=connection) as ctor:
                        collect(state, refresh=True)
                    self.assertEqual(1, ctor.call_count)
                    response.read.assert_not_called()
                    self.assertEqual(status, source["provider_status"])
                    self.assertEqual(status, source["collection_attempts"][-1]["provider_status"])
                    self.assertEqual("not-read-provider-error", source["collection_attempts"][-1]["body_status"])
                    self.assertEqual(before["claims"], state["claims"])
                    self.assertEqual(before["release_approvals"], state["release_approvals"])
                    self.assertEqual(before["inventory"], state["inventory"])
                    self.assertEqual(before["sources"][source["id"]]["availability_acceptance"], source["availability_acceptance"])

    def test_capture_failures_still_invalidate_reviewed_evidence(self):
        for response in ({**success(), "truncated": True}, {**success(), "data": {**success()["data"], "warning": "Target URL returned error 404: Not Found"}},
                         success("https://outside.example/a/"), {"provider_status": 200, "data": {"content": "<html><body>Loading...</body></html>"}}):
            with self.subTest(response=response):
                state, source = self.reviewed()
                collect(state, lambda *_: response, refresh=True)
                self.assertEqual("pending", state["claims"]["claim"]["publication"])
                self.assertEqual({}, state["release_approvals"])
                self.assertIsNone(source["availability_acceptance"])
                self.assertEqual("failed", source["collection_attempts"][-1]["outcome"])

    def test_pending_history_reconciliation_is_bound_audited_and_terminal_close(self):
        state, source = self.reviewed()
        state["config"]["publication_owner"] = "human:owner"
        old = source["active_version"]
        pending = {"requested_at": utc_now(), "outcome": "pending", "usage_tokens": None,
            "reserved_tokens": 1000, "settings": {"token_budget": 1000}, "version_id_at_request": old}
        source["collection_attempts"].insert(0, pending)  # Legacy pending followed by success.
        source["collection_state"] = "complete"
        descriptor = pending_attempts(source)[0]
        before = copy.deepcopy(pending)
        collect(state, lambda *_: self.fail("Legacy pending must block even when a later capture succeeded"), refresh=True)
        self.assertEqual(1, state["collection"]["blocked_pending"])
        def reconcile(**overrides):
            values = {"action": "close-without-retry", "reason": "Operator elected to retain previous reviewed capture", "by": "human:owner", "at": utc_now(),
                "channel": "local-review", "reference": "fixture-human-decision", "expected_attempt_hash": descriptor["attempt_hash"], "expected_version": old}
            values.update(overrides)
            return reconcile_attempt(state, source["id"], 0, **values)
        for overrides in ({"expected_attempt_hash": "stale"}, {"expected_version": "old-version"}, {"by": "human:other"}, {"action": "approve"}, {"reason": ""}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                reconcile(**overrides)
        for index in (-1, 99, True, 1):
            with self.subTest(index=index), self.assertRaises(ValueError):
                reconcile_attempt(state, source["id"], index, "allow-retry", "Explicit fixture decision", "human:owner", utc_now(),
                    "local-review", "fixture", expected_attempt_hash=descriptor["attempt_hash"], expected_version=old)
        state["config"]["collection_authorized"] = False
        with self.assertRaises(ValueError):
            reconcile()
        state["config"]["collection_authorized"] = True
        self.assertEqual(before, pending)
        event = reconcile()
        self.assertEqual(event, reconcile())
        self.assertEqual(1, len(source["attempt_reconciliation_history"]))
        self.assertEqual([], pending_attempts(source))
        self.assertEqual(before, {key: value for key, value in pending.items() if key != "reconciliation"})
        with self.assertRaises(ValueError):
            reconcile(action="allow-retry")
        source.pop("collection_retry_policy")  # Legacy reimport must not bypass terminal history.
        collect(state, lambda *_: self.fail("Close without retry is terminal for this source"), refresh=True)
        self.assertEqual(1, state["collection"]["closed_without_retry"])
        pending["settings"]["token_budget"] = 2000
        self.assertEqual(1, len(pending_attempts(source)))  # Changed request fingerprint invalidates resolution.

    def test_reconciliation_fractional_time_accepts_current_and_rejects_future(self):
        now = datetime(2026, 10, 15, 12, 0, 1, 900000, tzinfo=timezone.utc)
        for at, accepted in (("2026-10-15T12:00:01.500000+00:00", True),
                             ("2026-10-15T12:00:02.000000+00:00", False)):
            with self.subTest(at=at):
                state = self.state(config={"publication_owner": "human:owner"})
                source = next(iter(state["sources"].values()))
                source["collection_attempts"] = [{"requested_at": "2026-10-15T12:00:00Z", "outcome": "pending",
                    "usage_tokens": None, "reserved_tokens": 100}]
                descriptor = pending_attempts(source)[0]
                before = copy.deepcopy(state)
                with patch("okf_bundle.review.datetime") as review_clock, patch("okf_bundle.network.utc_now", return_value="2026-10-15T12:00:01Z"):
                    review_clock.now.return_value = now
                    def reconcile():
                        return reconcile_attempt(state, source["id"], 0, "allow-retry", "Explicit fixture decision", "human:owner", at,
                            "local-review", "fixture", expected_attempt_hash=descriptor["attempt_hash"], expected_version=None)
                    if accepted:
                        self.assertEqual(at, reconcile()["at"])
                        self.assertEqual([], pending_attempts(source))
                    else:
                        with self.assertRaisesRegex(ValueError, "future"):
                            reconcile()
                        self.assertEqual(before, state)

    def test_reader_warning_via_real_adapter_is_retained_and_never_silent(self):
        for warning, status in (("Target URL returned error 404: Not Found", 404),
                                ("Target URL returned error 503: Service Unavailable", 503),
                                ("Target URL returned error 403: Forbidden", 403),
                                ("This is a cached snapshot of the original page, consider retry with caching opt-out.", None),
                                ("Unknown future provider warning", None)):
            with self.subTest(warning=warning):
                state = self.state()
                data = {**success()["data"], "warning": warning}
                connection = MagicMock()
                connection.getresponse.return_value = FakeResponse(body=json.dumps({"data": data}).encode())
                with patch.dict(os.environ, {"JINA_API_KEY": "OFFLINE_TEST_ONLY"}), patch("okf_bundle.network.http.client.HTTPSConnection", return_value=connection):
                    collect(state)
                source = next(iter(state["sources"].values()))
                self.assertEqual(0, state["collection"]["succeeded"])
                self.assertEqual(200, source["provider_status"])
                self.assertEqual(warning, source["collection_attempts"][-1]["reader_warning"])
                self.assertEqual(status, source["captured_status"])
                self.assertNotEqual("included", source["scope"])
                if status:
                    self.assertIsNone(source["active_version"])
                    self.assertIn(source["availability"], ("failed", "nonpublic"))
                else:
                    version = source["versions"][source["active_version"]]
                    self.assertEqual(warning, version["reader_warning"])
                    self.assertIn("retrieved_at", version)
                    self.assertEqual("unknown", source["availability"])

    def test_missing_key_and_invalid_local_config_do_not_mutate_or_spend(self):
        state = self.state(config={"collection_budget": {"max_tokens": 1000}})
        before = copy.deepcopy(state)
        with patch.dict(os.environ, {"JINA_API_KEY": ""}), patch("okf_bundle.network.http.client.HTTPSConnection") as connection:
            with self.assertRaisesRegex(ValueError, "Configure JINA_API_KEY"):
                collect(state)
            connection.assert_not_called()
        self.assertEqual(before, state)
        cases = [{"collection_budget": {"max_tokens": "1000"}},
                 {"collection_settings": {"token_budget": float("nan")}},
                 {"collection_settings": {"response_bytes": -1}},
                 {"collection_max_attempts_per_page": "three"}, {"retry_base_delay": float("inf")}]
        for config in cases:
            with self.subTest(config=config):
                current = self.state(config=config)
                # NaN does not equal itself, so compare the unchanged serialized shape.
                before_json = json.dumps(current, sort_keys=True)
                with self.assertRaises(ValueError):
                    collect(current, lambda *_: self.fail("Invalid local config must fail before transport"))
                self.assertEqual(before_json, json.dumps(current, sort_keys=True))

    def test_nonfinite_retry_after_cannot_poison_persisted_json(self):
        state = self.state()
        responses = [{"provider_status": 429, "headers": {"retry-after": "inf"}}, success()]
        collect(state, lambda *_: responses.pop(0))
        self.assertEqual(1, state["collection"]["succeeded"])
        json.dumps(state, allow_nan=False)

    def test_explicit_target_failure_is_not_waived_to_unknown_or_live(self):
        state = self.state()
        collect(state, lambda url, settings: {**success(url), "target_status": 404})
        source = next(iter(state["sources"].values()))
        self.assertEqual("failed", source["availability"])
        self.assertEqual(404, source["captured_status"])
        self.assertIsNone(source["active_version"])
        collect(state, lambda url, settings: success(url))
        self.assertEqual("failed", source["availability"])
        self.assertIsNotNone(source["active_version"])

    def test_long_retry_after_is_deferred_and_cooldown_honored(self):
        state = self.state()
        with patch("okf_bundle.network.time.sleep") as sleeper:
            collect(state, lambda *_: {"provider_status": 429, "headers": {"retry-after": "120"}})
            sleeper.assert_not_called()
        self.assertEqual(1, state["collection"]["attempts"])
        collect(state, lambda *_: self.fail("Cooldown must survive a new call"))
        self.assertIn("cooldown", state["collection"]["stopped_reason"])

    def test_authorization_settings_and_content_bounds(self):
        with self.assertRaises(ValueError):
            collect(self.state(config={"collection_authorized": False}), lambda *_: self.fail("Not authorized"))
        with self.assertRaises(ValueError):
            collect(self.state(config={"collection_settings": {"max_tokens": 100}}), lambda *_: self.fail("No truncation"))
        state = self.state(config={"limits": {"html_bytes": 30}})
        collect(state, lambda *_: success())
        self.assertIsNone(next(iter(state["sources"].values()))["active_version"])

    def test_dns_mixed_private_answers_rejected_and_numeric_connection_pinned(self):
        def resolve(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with self.assertRaises(ValueError):
            public_addresses("example.com", 443, resolve)
        with patch("okf_bundle.network.socket.create_connection") as connect, patch("okf_bundle.network.ssl.create_default_context") as context:
            connection = _PinnedHTTPS("example.com", 443, "8.8.8.8", 10)
            connection.connect()
            connect.assert_called_once_with(("8.8.8.8", 443), 10)
            context.return_value.wrap_socket.assert_called_once_with(connect.return_value, server_hostname="example.com")

    def test_direct_redirect_scope_dns_and_size_checked_without_network(self):
        first, second = MagicMock(), MagicMock()
        first.getresponse.return_value = FakeResponse(302, headers={"location": "https://example.com/next.xml"})
        second.getresponse.return_value = FakeResponse(body=b"<urlset/>")
        with patch("okf_bundle.network.public_addresses", return_value=["8.8.8.8"]) as resolve, patch("okf_bundle.network._PinnedHTTPS", side_effect=[first, second]):
            result = direct_fetch("https://example.com/start.xml", CONFIG)
            self.assertEqual("https://example.com/next.xml", result["url"])
            self.assertEqual(2, resolve.call_count)
        first.getresponse.return_value = FakeResponse(302, headers={"location": "http://127.0.0.1/private"})
        with patch("okf_bundle.network.public_addresses", return_value=["8.8.8.8"]), patch("okf_bundle.network._PinnedHTTPS", return_value=first), self.assertRaises(ValueError):
            direct_fetch("https://example.com/", CONFIG)
        second.getresponse.return_value = FakeResponse(body=b"x" * 20)
        with patch("okf_bundle.network.public_addresses", return_value=["8.8.8.8"]), patch("okf_bundle.network._PinnedHTTPS", return_value=second), self.assertRaises(ValueError):
            direct_fetch("https://example.com/", CONFIG, max_bytes=10)

    def test_official_api_shape_no_cache_no_truncation_and_no_redirect_forwarding(self):
        connection = MagicMock()
        connection.getresponse.return_value = FakeResponse(body=json.dumps({"data": success()["data"]}).encode())
        settings = {"engine": "auto", "locale": "en", "timeout": 60, "readiness": "network-idle", "no_cache": True, "token_budget": 100, "response_bytes": 10000}
        with patch.dict("os.environ", {"JINA_API_KEY": "TEST_ONLY_KEY"}), patch("okf_bundle.network.http.client.HTTPSConnection", return_value=connection) as ctor:
            result = jina_request("https://example.com/a/", settings)
            self.assertEqual("r.jina.ai", ctor.call_args.args[0])
            headers = connection.request.call_args.kwargs["headers"]
            self.assertEqual("true", headers["X-No-Cache"])
            self.assertEqual("100", headers["X-Token-Budget"])
            self.assertNotIn("X-Max-Tokens", headers)
            self.assertEqual("POST", connection.request.call_args.args[0])
            self.assertEqual("/", connection.request.call_args.args[1])
            self.assertEqual(200, result["provider_status"])
            connection.getresponse.return_value = FakeResponse(status=302, headers={"location": "https://untrusted.example/"})
            self.assertEqual(302, jina_request("https://example.com/a/", settings)["provider_status"])

    def test_publication_options_preserve_raw_gzip_and_observe_unfollowed_redirect(self):
        compressed = gzip.compress(b"public content")
        connection = MagicMock()
        connection.getresponse.return_value = FakeResponse(body=compressed, headers={"content-encoding": "gzip"})
        with patch("okf_bundle.network.public_addresses", return_value=["8.8.8.8"]) as resolve, patch("okf_bundle.network._PinnedHTTPS", return_value=connection):
            raw = direct_fetch("https://example.com/okf/a", CONFIG, headers={"Accept-Encoding": "identity"}, decode_content_encoding=False)
            self.assertEqual(compressed, raw["body"])
            decoded = direct_fetch("https://example.com/okf/a", CONFIG, headers={})
            self.assertEqual(b"public content", decoded["body"])
            connection.getresponse.return_value = FakeResponse(302, headers={"location": "http://127.0.0.1/private"})
            before = resolve.call_count
            redirect = direct_fetch("https://example.com/retired", CONFIG, follow_redirects=False)
            self.assertEqual(302, redirect["status"])
            self.assertEqual(before + 1, resolve.call_count)
            connection.getresponse.return_value = FakeResponse(body=b"not gzip", headers={"content-encoding": "gzip"})
            with self.assertRaisesRegex(ValueError, "incorrectly labels"):
                direct_fetch("https://example.com/a", CONFIG)


if __name__ == "__main__":
    unittest.main()
