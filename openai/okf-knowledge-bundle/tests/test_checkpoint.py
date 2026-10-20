import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okf_bundle.common import atomic_json, digest, utc_now
from okf_bundle.ingest import discover_csv
from okf_bundle.network import collect, pending_attempts, reconcile_attempt
from okf_bundle.state import StateStore, new_state
from okf_bundle.workflow import merge_import


class Interrupted(RuntimeError):
    pass


def response(url, changed=False):
    text = "Revised public workshop for adults lasts two hours." if changed else "Public classroom training for adult employees includes a separate optional assessment."
    return {"provider_status": 200, "data": {"url": url, "content": '<html lang="en"><head><title>Training</title></head><body><h1>Workshop</h1><p>' + text + '</p></body></html>', "usage": {"tokens": 50}}}


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def initial(self, count=5):
        csv = self.root / "input.csv"
        csv.write_text("URL\n" + "".join(f"https://example.com/services/{i}/\n" for i in range(count)), encoding="utf-8")
        state = new_state({"domains": ["example.com"], "collection_authorized": True, "retry_base_delay": 0})
        merge_import(state, discover_csv(csv, state["config"]))
        store = StateStore(self.root / "run")
        with store.transaction():
            store.save(state)
        return store, state

    def partial(self, stop_after=2):
        store, state = self.initial()
        calls = 0
        def checkpoint(current, sid):
            nonlocal calls
            store.checkpoint(current, sid)
            if current["sources"][sid]["collection_attempts"][-1]["outcome"] == "pending":
                return
            calls += 1
            if calls == stop_after:
                raise Interrupted("Simulated process interruption after committed result")
        with self.assertRaises(Interrupted), store.transaction():
            collect(state, lambda url, _: response(url), checkpoint=store.save, incremental_checkpoint=checkpoint)
        return store, state

    def test_interrupted_delta_chain_reloads_and_resume_does_not_recollect(self):
        store, interrupted = self.partial()
        resumed_store = StateStore(self.root / "run")
        with resumed_store.transaction():
            resumed = resumed_store.load()
            self.assertEqual(interrupted, resumed)
            self.assertEqual(2, sum(bool(s["active_version"]) for s in resumed["sources"].values()))
            self.assertEqual(2, resumed["collection"]["succeeded"])
            calls = []
            collect(resumed, lambda url, _: calls.append(url) or response(url), checkpoint=resumed_store.save, incremental_checkpoint=resumed_store.checkpoint)
            self.assertEqual(3, len(calls))
        final = StateStore(self.root / "run").load()
        self.assertEqual(5, sum(bool(s["active_version"]) for s in final["sources"].values()))
        self.assertEqual(5, sum(len(s["collection_attempts"]) for s in final["sources"].values()))
        self.assertTrue(all(row["state"] == "included" for row in final["inventory"]))

    def test_failed_pointer_commit_leaves_previous_snapshot_coherent(self):
        store, state = self.initial(1)
        previous = copy.deepcopy(state)
        def fail_pointer(path, value):
            if Path(path).name == "current.json":
                raise OSError("Simulated interruption before pointer commit")
            return atomic_json(path, value)
        with self.assertRaises(OSError), store.transaction(), patch("okf_bundle.state.atomic_json", side_effect=fail_pointer):
            collect(state, lambda url, _: response(url), checkpoint=store.save, incremental_checkpoint=store.checkpoint)
        self.assertEqual(previous, StateStore(self.root / "run").load())

    def test_corrupt_delta_or_parent_rejected(self):
        store, _ = self.partial(1)
        pointer = json.loads((store.private / "current.json").read_text())
        path = store.private / "snapshots" / (pointer["snapshot"] + ".json")
        delta = json.loads(path.read_text())
        delta["parent"] = "0" * 64
        atomic_json(path, delta)
        with self.assertRaisesRegex(ValueError, "delta integrity"):
            StateStore(self.root / "run").load()

    def test_corrupt_changed_record_rejected(self):
        store, _ = self.partial(1)
        pointer = json.loads((store.private / "current.json").read_text())
        delta = json.loads((store.private / "snapshots" / (pointer["snapshot"] + ".json")).read_text())
        inline = next(iter(delta["sources"].values()))
        inline["record"]["availability"] = "live"
        # Even with a recalculated envelope hash, an inconsistent embedded
        # record hash must fail rather than silently reconstruct changed state.
        changed_key = digest(delta)
        atomic_json(store.private / "snapshots" / (changed_key + ".json"), delta)
        atomic_json(store.private / "current.json", {"snapshot": changed_key, "schema_version": 1})
        with self.assertRaisesRegex(ValueError, "record integrity"):
            StateStore(self.root / "run").load()

    def test_large_source_uses_verified_external_record_and_old_v1_loads(self):
        store, state = self.initial(1)
        def large_response(url, _):
            value = response(url)
            value["data"]["content"] = value["data"]["content"].replace("</p>", " Large evidence " * 6000 + "</p>")
            return value
        def checkpoint(current, sid):
            store.checkpoint(current, sid)
            if current["sources"][sid]["collection_attempts"][-1]["outcome"] != "pending":
                raise Interrupted()
        with self.assertRaises(Interrupted), store.transaction():
            collect(state, large_response, checkpoint=store.save, incremental_checkpoint=checkpoint)
        pointer = json.loads((store.private / "current.json").read_text())
        delta = json.loads((store.private / "snapshots" / (pointer["snapshot"] + ".json")).read_text())
        record_hash = next(iter(delta["sources"].values()))
        self.assertIsInstance(record_hash, str)
        self.assertEqual(state, StateStore(self.root / "run").load())
        with store.transaction():
            key = store.save(state)
        snapshot_path = store.private / "snapshots" / (key + ".json")
        snapshot = json.loads(snapshot_path.read_text())
        for field in ("sources", "claims", "concepts"):
            self.assertEqual({}, snapshot["state"][field].pop("$inline"))
        atomic_json(snapshot_path, snapshot)  # Old v1 shape used only $records.
        self.assertEqual(state, StateStore(self.root / "run").load())
        path = store.private / "records" / record_hash[:2] / (record_hash + ".json")
        record = json.loads(path.read_text())
        record["availability"] = "live"
        atomic_json(path, record)
        with self.assertRaisesRegex(ValueError, "record integrity"):
            StateStore(self.root / "run").load()

    def test_concurrent_writer_and_unowned_checkpoint_are_rejected(self):
        store, state = self.initial(1)
        other = StateStore(self.root / "run")
        with store.transaction():
            with self.assertRaises(RuntimeError), other.transaction():
                self.fail("Second writer acquired the lock")
            with self.assertRaisesRegex(RuntimeError, "another process"):
                other.checkpoint(state, next(iter(state["sources"])))

    def test_stale_full_save_cannot_overwrite_newer_committed_state(self):
        store, state = self.initial(1)
        stale = StateStore(self.root / "run")
        old_state = stale.load()
        with store.transaction():
            state["config"]["model"] = "reviewed-new-model"
            store.save(state)
        with stale.transaction(), self.assertRaisesRegex(RuntimeError, "Stale"):
            stale.save(old_state)
        self.assertEqual("reviewed-new-model", StateStore(self.root / "run").load()["config"]["model"])

    def test_delta_does_not_hash_or_deepcopy_the_whole_state(self):
        store, state = self.initial(1)
        original_digest = digest
        def checked_digest(value):
            if value is state:
                raise AssertionError("Delta path traversed whole state")
            return original_digest(value)
        def checkpoint(current, sid):
            with patch("okf_bundle.state.digest", side_effect=checked_digest), patch("okf_bundle.state.copy.deepcopy", side_effect=AssertionError("Delta copied whole state")):
                store.checkpoint(current, sid)
            raise Interrupted()
        with self.assertRaises(Interrupted), store.transaction():
            collect(state, lambda url, _: response(url), checkpoint=store.save, incremental_checkpoint=checkpoint)
        self.assertEqual(state, StateStore(self.root / "run").load())

    def test_chain_cap_compacts_and_v1_snapshots_remain_readable(self):
        store, state = self.initial()
        with store.transaction(), patch("okf_bundle.state.MAX_DELTA_CHAIN", 2):
            collect(state, lambda url, _: response(url), checkpoint=store.save, incremental_checkpoint=store.checkpoint)
        nodes = [json.loads(path.read_text()) for path in (store.private / "snapshots").glob("*.json")]
        self.assertGreaterEqual(sum(node["storage_version"] == 1 for node in nodes), 3)
        self.assertGreaterEqual(sum(node["storage_version"] == 2 for node in nodes), 3)
        self.assertEqual(state, StateStore(self.root / "run").load())

    def test_refresh_alias_and_claim_invalidations_commit_in_one_delta(self):
        store, state = self.initial(2)
        collect(state, lambda url, _: response(url))
        target_id, alias_id = list(state["sources"])
        target, alias = state["sources"][target_id], state["sources"][alias_id]
        alias.update(alias_of=target_id, source_key=target_id, original_source_key=alias_id,
                     availability_acceptance={"owner": "approved"}, alias_history=[])
        old_citation = alias["citation_url"]
        state["claims"]["claim-alias"] = {"id": "claim-alias", "evidence": [{"source_id": alias_id}],
            "evidence_review": "approved", "publication": "approved", "business_confirmation": "confirmed"}
        state["release_approvals"] = {"release": {"approved": True}}
        with store.transaction():
            store.save(state)
        def checkpoint(current, sid):
            store.checkpoint(current, sid)
            if current["sources"][sid]["collection_attempts"][-1]["outcome"] != "pending":
                raise Interrupted()
        with self.assertRaises(Interrupted), store.transaction():
            collect(state, lambda url, _: response(url, changed=True), refresh=True, max_pages=1,
                    checkpoint=store.save, incremental_checkpoint=checkpoint)
        loaded = StateStore(self.root / "run").load()
        self.assertEqual(state, loaded)
        changed_alias = loaded["sources"][alias_id]
        self.assertNotIn("alias_of", changed_alias)
        self.assertEqual(alias_id, changed_alias["source_key"])
        self.assertTrue(changed_alias["alias_review_required"])
        self.assertFalse(changed_alias["alias_history"][-1]["human_verified"])
        self.assertEqual(old_citation, changed_alias["citation_url"])
        self.assertIsNone(changed_alias["availability_acceptance"])
        self.assertEqual("pending", loaded["claims"]["claim-alias"]["publication"])
        self.assertEqual("required", loaded["claims"]["claim-alias"]["business_confirmation"])
        self.assertEqual({}, loaded["release_approvals"])
        self.assertEqual(2, len(loaded["sources"][target_id]["versions"]))

    def test_inflight_request_is_durably_reserved_before_transport_and_resume(self):
        store, state = self.initial(1)
        state["config"]["collection_budget"] = {"max_attempts": 1, "max_tokens": 1000}
        with store.transaction():
            store.save(state)
        calls = []
        def interrupted_transport(url, settings):
            loaded = StateStore(self.root / "run").load()
            self.assertEqual(1, loaded["collection"]["attempts"])
            self.assertEqual(1000, loaded["collection"]["reserved_unknown_tokens"])
            self.assertEqual("pending", next(iter(loaded["sources"].values()))["collection_attempts"][-1]["outcome"])
            calls.append(url)
            raise KeyboardInterrupt("Response lost after submission")
        with self.assertRaises(KeyboardInterrupt), store.transaction():
            collect(state, interrupted_transport, checkpoint=store.save, incremental_checkpoint=store.checkpoint)
        resumed_store = StateStore(self.root / "run")
        with resumed_store.transaction():
            resumed = resumed_store.load()
            collect(resumed, lambda *_: self.fail("Interrupted request has exhausted the authorized budget"),
                    checkpoint=resumed_store.save, incremental_checkpoint=resumed_store.checkpoint)
        loaded = StateStore(self.root / "run").load()
        self.assertEqual(1, len(calls))
        self.assertEqual(1, sum(item["attempts"] for item in loaded["collection_history"]))
        self.assertEqual(1000, sum(item["reserved_unknown_tokens"] for item in loaded["collection_history"]))
        self.assertEqual(0, sum(item["provider_tokens"] for item in loaded["collection_history"]))

    def test_failed_reservation_commit_prevents_transport(self):
        store, state = self.initial(1)
        before = copy.deepcopy(state)
        with self.assertRaises(OSError), store.transaction():
            collect(state, lambda *_: self.fail("Transport must follow durable reservation"),
                    incremental_checkpoint=lambda *_: (_ for _ in ()).throw(OSError("Reservation failed")))
        self.assertEqual(before, StateStore(self.root / "run").load())

    def test_pending_refresh_blocks_by_state_without_destroying_reviews_and_auth_restores(self):
        store, state = self.initial(1)
        collect(state, lambda url, _: response(url))
        source = next(iter(state["sources"].values()))
        sid = source["id"]
        source["availability_acceptance"] = {"owner": "approved"}
        state["claims"]["claim"] = {"id": "claim", "evidence": [{"source_id": sid}],
            "evidence_review": "accepted", "publication": "approved", "business_confirmation": "confirmed"}
        state["release_approvals"] = {"release": {"approved": True}}
        with store.transaction():
            store.save(state)
        before = copy.deepcopy(state)
        def provider_denial(url, settings):
            pending = StateStore(self.root / "run").load()
            self.assertEqual("pending", pending["sources"][sid]["collection_state"])
            self.assertEqual(before["claims"], pending["claims"])
            self.assertEqual(before["release_approvals"], pending["release_approvals"])
            self.assertEqual(before["sources"][sid]["availability_acceptance"], pending["sources"][sid]["availability_acceptance"])
            return {"provider_status": 402}
        with store.transaction():
            collect(state, provider_denial, refresh=True, checkpoint=store.save, incremental_checkpoint=store.checkpoint)
        loaded = StateStore(self.root / "run").load()
        self.assertEqual(state, loaded)
        self.assertEqual("complete", loaded["sources"][sid]["collection_state"])
        self.assertEqual(before["claims"], loaded["claims"])
        self.assertEqual(before["release_approvals"], loaded["release_approvals"])
        self.assertEqual(before["inventory"], loaded["inventory"])
        self.assertEqual("provider_error", loaded["sources"][sid]["collection_attempts"][-1]["outcome"])

    def test_spare_budget_reload_blocks_pending_allows_other_urls_then_audited_retry(self):
        store, state = self.initial(2)
        state["config"].update(publication_owner="human:owner", collection_budget={"max_attempts": 10, "max_tokens": 1000}, collection_settings={"token_budget": 100})
        with store.transaction():
            store.save(state)
        first_id, other_id = list(state["sources"])
        calls = []
        def interrupted(url, settings):
            calls.append(url)
            raise KeyboardInterrupt("Submitted request; no committed response")
        with self.assertRaises(KeyboardInterrupt), store.transaction():
            collect(state, interrupted, checkpoint=store.save, incremental_checkpoint=store.checkpoint)
        resumed_store = StateStore(self.root / "run")
        with resumed_store.transaction():
            resumed = resumed_store.load()
            resumed_calls = []
            collect(resumed, lambda url, _: resumed_calls.append(url) or response(url),
                    checkpoint=resumed_store.save, incremental_checkpoint=resumed_store.checkpoint)
            self.assertEqual([resumed["sources"][other_id]["normalized_url"]], resumed_calls)
            self.assertEqual(1, resumed["collection"]["blocked_pending"])
            self.assertEqual(1, len(pending_attempts(resumed["sources"][first_id])))
            self.assertIsNone(resumed["sources"][first_id]["active_version"])
        decision_store = StateStore(self.root / "run")
        with decision_store.transaction():
            current = decision_store.load()
            descriptor = pending_attempts(current["sources"][first_id])[0]
            accounting = copy.deepcopy(current["collection_history"])
            event = reconcile_attempt(current, first_id, descriptor["attempt_index"], "allow-retry",
                "Human approved a new bounded attempt; original unknown usage remains reserved", "human:owner", utc_now(),
                "local-review", "fixture-operator-decision", expected_attempt_hash=descriptor["attempt_hash"], expected_version=None)
            self.assertEqual(accounting, current["collection_history"])
            self.assertEqual(None, event["reconciled_at_version"])
            decision_store.save(current)
        final_store = StateStore(self.root / "run")
        with final_store.transaction():
            current = final_store.load()
            retried = []
            collect(current, lambda url, _: retried.append(url) or response(url),
                    checkpoint=final_store.save, incremental_checkpoint=final_store.checkpoint)
            self.assertEqual([current["sources"][first_id]["normalized_url"]], retried)
        final = StateStore(self.root / "run").load()
        source = final["sources"][first_id]
        self.assertIsNotNone(source["active_version"])
        self.assertEqual([], pending_attempts(source))
        self.assertEqual("pending", source["collection_attempts"][0]["outcome"])
        self.assertEqual(100, source["collection_attempts"][0]["reserved_tokens"])
        self.assertEqual(100, sum(history["reserved_unknown_tokens"] for history in final["collection_history"]))
        self.assertEqual(3, sum(history["attempts"] for history in final["collection_history"]))
        self.assertEqual(100, sum(history["provider_tokens"] for history in final["collection_history"]))
        self.assertEqual(1, len(source["attempt_reconciliation_history"]))
        self.assertEqual(1, len(calls))


if __name__ == "__main__":
    unittest.main()
