"""Check the exact outgoing review, without changing evidence or approvals."""
import re
import html
from pathlib import Path

from .common import digest


def check_summary(path, expected_hash=None):
    raw = Path(path).read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("Review summary exceeds the text size bound")
    text = raw.decode("utf-8-sig")
    if re.search(r"</?[A-Za-z][^>]*>|<!--|```|~~~", text):
        raise ValueError("Use ordinary prose and inline Markdown links for the review summary, without HTML or code blocks")
    # Conservative whitespace count: formatting-only tokens are not words.
    # Inline link labels count normally; destinations/titles can overcount.
    # Do not strip hidden markup in a way that could undercount visible prose.
    # Count every numeric token, including list markers: a sentence-ending
    # number or ZIP code must not be mistaken for formatting.
    tokens = re.findall(r"\S+", html.unescape(text))
    words = [token for token in tokens if not re.fullmatch(r"[#>*_~`-]+", token)]
    sha = digest(raw)
    unchanged = expected_hash is None or expected_hash == sha
    valid = bool(words) and len(words) <= 150 and unchanged
    return {"valid": valid, "word_count": len(words), "limit": 150,
            "sha256": sha, "unchanged": unchanged, "text": text,
            "count_method": "conservative whitespace tokens; numbers, list markers and link labels included; standalone Markdown punctuation excluded",
            "instruction": "Send text verbatim as the entire review response. Recheck after any edit or added introduction/closing. This is not content approval."}
