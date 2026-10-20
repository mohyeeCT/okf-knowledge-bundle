"""Immutable snapshots committed by an atomic pointer under an exclusive lock."""
import copy
import json
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from .common import atomic_json, canonical_json, digest, stable_id, utc_now
from . import __version__

SCHEMA_VERSION = 1
MAX_DELTA_CHAIN = 500
INLINE_DELTA_BYTES = 64 * 1024


def _process_alive(pid):
    if os.name == "nt":
        # os.kill(pid, 0) can terminate a Windows process; query it read-only.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            raise RuntimeError("Cannot query writer process ownership")
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise RuntimeError("Cannot query writer process state")
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def new_state(config):
    from .managed import check_config
    check_config(config)
    config = copy.deepcopy(config)
    policy = config.setdefault("confirmation_policy", "critical-only")
    if policy not in {"critical-only", "all-risk"}:
        raise ValueError("Unknown business confirmation policy")
    defaults = {"locale": "en", "business_profiles": [], "model": "record-actual-host-model", "effort": "medium", "public_base_path": "/okf/", "indexing": "indexable", "availability_mode": "offline", "collection_authorized": False}
    for key, value in defaults.items():
        config.setdefault(key, value)
    if not config.get("domains"):
        raise ValueError("An approved exact domain list is required")
    now = utc_now()
    return {"schema_version": SCHEMA_VERSION, "tool_version": __version__, "run_id": stable_id("run", [config, now, os.urandom(16).hex()]), "created_at": now, "config": config, "config_hash": digest(config), "inventory": [], "sources": {}, "claims": {}, "concepts": {}, "review_history": [], "candidates": {}, "release_approvals": {}, "draft_failures": []}


class StateStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.private = self.root / "private-state"
        self.private.mkdir(parents=True, exist_ok=True)
        self.lock = self.private / "writer.lock"
        self._head = None
        self._delta_depth = 0

    @contextmanager
    def transaction(self):
        try:
            fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise RuntimeError("Run has a writer lock; prove stale ownership before recovery") from error
        token = os.urandom(16).hex()
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "at": utc_now(), "token": token}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        self._token = token
        try:
            yield self
        finally:
            if self.lock.exists():
                record = json.loads(self.lock.read_text(encoding="utf-8"))
                if record.get("token") == token:
                    self.lock.unlink()
            self._token = None

    @staticmethod
    def _check_hash(key):
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid state snapshot identity")

    def _read_record(self, record_hash):
        self._check_hash(record_hash)
        record = json.loads((self.private / "records" / record_hash[:2] / (record_hash + ".json")).read_text(encoding="utf-8"))
        if digest(record) != record_hash:
            raise ValueError("State record integrity mismatch")
        return record

    def _write_record(self, record, key=None):
        key = key or digest(record)
        target = self.private / "records" / key[:2] / (key + ".json")
        if not target.exists():
            atomic_json(target, record)
        return key

    def _delta_record(self, record):
        data = canonical_json(record).encode("utf-8")
        key = digest(data)
        # Tiny result records fit in the already-hashed immutable delta. Larger
        # source histories stay separate, with references reused by full saves.
        return {"hash": key, "record": record} if len(data) <= INLINE_DELTA_BYTES else self._write_record(record, key)

    def _read_delta_record(self, value):
        if isinstance(value, str):
            return self._read_record(value)  # Original v2 delta compatibility.
        if not isinstance(value, dict) or set(value) != {"hash", "record"} or digest(value["record"]) != value["hash"]:
            raise ValueError("State inline record integrity mismatch")
        return value["record"]

    def _load_full(self, snapshot, key):
        if snapshot.get("state_hash") != key:
            raise ValueError("State storage integrity/version mismatch")
        state = snapshot["state"]
        for field in ("sources", "claims", "concepts"):
            saved = state[field]
            records = saved.get("$inline", {})
            if records.keys() & saved["$records"].keys():
                raise ValueError("Duplicate inline/referenced state record")
            records.update({rid: self._read_record(record_hash) for rid, record_hash in saved["$records"].items()})
            state[field] = records
        if digest(state) != key or state.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("State integrity/schema mismatch; do not apply old approvals")
        if digest(state["config"]) != state["config_hash"]:
            raise ValueError("Configuration fingerprint mismatch")
        return state

    def _index(self, state):
        self._indexed_state = id(state)
        self._indexed_shape = tuple(len(state[field]) for field in ("sources", "claims", "concepts", "inventory"))
        self._config_hash = digest(state["config"])
        self._inventory_index, self._claim_index, self._alias_index = {}, {}, {}
        for index, row in enumerate(state["inventory"]):
            self._inventory_index.setdefault(row.get("source_id"), []).append(index)
        for cid, claim in state["claims"].items():
            for sid in {e["source_id"] for e in claim.get("evidence", [])}:
                self._claim_index.setdefault(sid, set()).add(cid)
        for sid, source in state["sources"].items():
            if source.get("alias_of"):
                self._alias_index.setdefault(source["alias_of"], set()).add(sid)
        self._warnings_count = len(state.get("warnings", []))

    def _apply_delta(self, state, delta):
        for field in ("sources", "claims"):
            for rid, record_hash in delta[field].items():
                if rid not in state[field]:
                    raise ValueError("Delta references an unknown record")
                state[field][rid] = self._read_delta_record(record_hash)
        for index, row in delta["inventory"]:
            if not isinstance(index, int) or not 0 <= index < len(state["inventory"]) or row.get("source_id") != state["inventory"][index].get("source_id"):
                raise ValueError("Invalid delta inventory patch")
            state["inventory"][index] = row
        state["release_approvals"] = delta["release_approvals"]
        history = state.setdefault("collection_history", [])
        index = delta["collection_index"]
        if not isinstance(index, int) or index < 0 or index > len(history) or index < len(history) - 1:
            raise ValueError("Invalid collection history delta")
        if index == len(history):
            history.append(delta["collection"])
        else:
            history[index] = delta["collection"]
        state["collection"] = history[index]
        warnings = state.setdefault("warnings", [])
        if len(warnings) != delta["warnings_start"]:
            raise ValueError("Invalid warnings delta")
        warnings.extend(delta["warnings_append"])

    def load(self):
        pointer = json.loads((self.private / "current.json").read_text(encoding="utf-8"))
        key = pointer["snapshot"]
        head, seen, deltas = key, set(), []
        while True:
            self._check_hash(key)
            if key in seen:
                raise ValueError("Cyclic state delta chain")
            seen.add(key)
            snapshot = json.loads((self.private / "snapshots" / (key + ".json")).read_text(encoding="utf-8"))
            if snapshot.get("storage_version") == 1:
                state = self._load_full(snapshot, key)
                break
            if snapshot.get("storage_version") != 2 or snapshot.get("kind") != "collection-delta" or digest(snapshot) != key:
                raise ValueError("State delta integrity/version mismatch")
            deltas.append(snapshot)
            if len(deltas) > MAX_DELTA_CHAIN:
                raise ValueError("State delta chain exceeds its bounded length")
            key = snapshot["parent"]
        for delta in reversed(deltas):
            self._apply_delta(state, delta)
        self._head, self._delta_depth = head, len(deltas)
        self._index(state)
        return state

    def _assert_writer(self):
        if not self.lock.exists():
            raise RuntimeError("State writes require a transaction")
        lock = json.loads(self.lock.read_text(encoding="utf-8"))
        if lock.get("pid") != os.getpid() or lock.get("host") != socket.gethostname() or lock.get("token") != getattr(self, "_token", None):
            raise RuntimeError("Writer lock belongs to another process")

    def save(self, state):
        """Full compact snapshot at command/batch boundaries; v1-compatible storage."""
        self._assert_writer()
        current = self.private / "current.json"
        if current.exists():
            previous = json.loads(current.read_text(encoding="utf-8"))["snapshot"]
            if not self._head or previous != self._head:
                raise RuntimeError("Stale or unloaded state; reload the current snapshot under the writer lock")
        state["config_hash"] = digest(state["config"])
        key = digest(state)
        snapshot = self.private / "snapshots" / (key + ".json")
        if not snapshot.exists():
            saved = copy.deepcopy(state)
            for field in ("sources", "claims", "concepts"):
                references, inline = {}, {}
                for rid, record in state[field].items():
                    encoded = canonical_json(record).encode("utf-8") if field == "sources" else None
                    if encoded is not None and len(encoded) <= INLINE_DELTA_BYTES:
                        inline[rid] = record
                    else:
                        references[rid] = self._write_record(record, digest(encoded) if encoded is not None else None)
                saved[field] = {"$records": references, "$inline": inline}
            atomic_json(snapshot, {"storage_version": 1, "state_hash": key, "state": saved})
        atomic_json(self.private / "current.json", {"snapshot": key, "schema_version": SCHEMA_VERSION})
        self._head, self._delta_depth = key, 0
        self._index(state)
        return key

    def checkpoint(self, state, source_id):
        """Commit only one collection result and its dependent invalidations.

        The source set, input rows, config, claim set and concepts must stay fixed
        during collect. Use save() for arbitrary edits or new/removed records.
        No full-state hash/deepcopy/traversal occurs on this path except bounded
        compaction at MAX_DELTA_CHAIN. Records and parent-linked deltas each carry
        their own content hash; the atomic pointer commits the complete update.
        """
        self._assert_writer()
        if not self._head or getattr(self, "_indexed_state", None) != id(state):
            raise ValueError("Incremental collection requires this state to be loaded or fully saved first")
        shape = tuple(len(state[field]) for field in ("sources", "claims", "concepts", "inventory"))
        if shape != self._indexed_shape or digest(state["config"]) != self._config_hash:
            raise ValueError("Collection state shape/config changed; make a full snapshot first")
        pointer = json.loads((self.private / "current.json").read_text(encoding="utf-8"))
        if pointer.get("snapshot") != self._head:
            raise RuntimeError("State pointer changed outside this writer")
        if self._delta_depth >= MAX_DELTA_CHAIN:
            return self.save(state)
        affected, queue = set(), [source_id]
        while queue:
            sid = queue.pop()
            if sid not in affected:
                affected.add(sid)
                queue.extend(self._alias_index.get(sid, ()))
        if not affected <= state["sources"].keys():
            raise ValueError("Collection delta references an unknown source")
        claim_ids = set().union(*(self._claim_index.get(sid, set()) for sid in affected))
        inventory = sorted({index for sid in affected for index in self._inventory_index.get(sid, [])})
        warnings = state.get("warnings", [])
        history = state.get("collection_history", [])
        if len(warnings) < self._warnings_count or not history or state.get("collection") != history[-1]:
            raise ValueError("Invalid collection metadata; make a full snapshot first")
        delta = {"storage_version": 2, "kind": "collection-delta", "parent": self._head,
            "sources": {sid: self._delta_record(state["sources"][sid]) for sid in sorted(affected)},
            "claims": {cid: self._delta_record(state["claims"][cid]) for cid in sorted(claim_ids)},
            "inventory": [[index, state["inventory"][index]] for index in inventory],
            "release_approvals": state.get("release_approvals", {}),
            "collection_index": len(history) - 1, "collection": state["collection"],
            "warnings_start": self._warnings_count, "warnings_append": warnings[self._warnings_count:]}
        key = digest(delta)
        atomic_json(self.private / "snapshots" / (key + ".json"), delta)
        atomic_json(self.private / "current.json", {"snapshot": key, "schema_version": SCHEMA_VERSION})
        self._head, self._delta_depth = key, self._delta_depth + 1
        self._warnings_count = len(warnings)
        return key

    def recover_lock(self, reason):
        if not reason or not reason.strip():
            raise ValueError("Recovery needs an audit reason")
        lock = json.loads(self.lock.read_text(encoding="utf-8"))
        if lock.get("host") != socket.gethostname():
            raise RuntimeError("Cannot prove a different host's writer is inactive")
        if _process_alive(int(lock["pid"])):
            raise RuntimeError("Writer is still alive; recovery refused")
        self.load()  # A coherent committed snapshot must remain readable.
        audit = self.private / "lock-recovery" / (digest(lock) + ".json")
        atomic_json(audit, {"old_lock": lock, "reason": reason, "at": utc_now()})
        if json.loads(self.lock.read_text(encoding="utf-8")) != lock:
            raise RuntimeError("Lock changed during recovery")
        self.lock.unlink()
