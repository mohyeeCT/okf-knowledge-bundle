"""Initial owner setup from an actual designation; no content or release approval."""
import re
from .common import digest
from .review import reviewer_event


def set_publication_owner(state, owner, by, at, channel, reference):
    if not re.fullmatch(r"human:[a-zA-Z0-9][a-zA-Z0-9_.-]{1,80}", str(owner)):
        raise ValueError("Use an opaque human publication-owner role ID")
    for value in (channel, reference):
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Owner setup needs a single-line channel and actual designation reference")
    config = state["config"]
    before_hash = digest(config)
    if state.get("config_hash") != before_hash:
        raise ValueError("Configuration fingerprint mismatch")
    provenance = reviewer_event(by, at, channel, reference, before_hash)
    current = config.get("publication_owner")
    if current not in (None, "", owner):
        raise ValueError("Owner already designated; replacement needs a separate configuration migration")
    result = {"publication_owner": owner, "owner_setup_recorded": False,
              "content_reviews_preserved": True, "release_approval_recorded": False}
    if current == owner:
        return result
    if state.get("candidates") or state.get("release_approvals"):
        raise ValueError("Initial owner setup cannot alter a run with existing candidates or release approvals")
    history = state.get("owner_designations", [])
    if not isinstance(history, list):
        raise ValueError("Invalid owner-designation history")
    configured = {**config, "publication_owner": owner}
    after_hash = digest(configured)
    event = {"type": "publication-owner-designation", "owner": owner, **provenance,
             "config_hash_before": before_hash, "config_hash_after": after_hash}
    state.update(config=configured, config_hash=after_hash, owner_designations=[*history, event])
    return {**result, "owner_setup_recorded": True, "config_hash": after_hash}
