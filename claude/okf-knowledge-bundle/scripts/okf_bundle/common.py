"""Shared deterministic identity and file operations. No network access."""
import hashlib
import ipaddress
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

URL_POLICY_VERSION = 1
UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    data = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def stable_id(prefix, value):
    return prefix + "-" + digest(value)[:16]


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value):
    date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Timestamp needs an explicit UTC offset")
    return date.astimezone(timezone.utc)


def _percent(value):
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("Invalid percent escape")
    def replace(match):
        char = chr(int(match.group(0)[1:], 16))
        return char if char in UNRESERVED else match.group(0).upper()
    return re.sub(r"%[0-9a-fA-F]{2}", replace, value)


def normalize_url(url):
    if not isinstance(url, str) or not url or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        raise ValueError("URL is empty or contains whitespace/control characters")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("Expected an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Credentials in URLs are forbidden")
    host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parts.port
    scheme = parts.scheme.lower()
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host += ":" + str(port)
    return urlunsplit((scheme, host, _percent(parts.path or "/"), _percent(parts.query), _percent(parts.fragment)))


def public_url(url, domains):
    try:
        normalized = normalize_url(url)
        host = urlsplit(normalized).hostname
        if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal", ".localhost")):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        allowed = {str(d).rstrip(".").encode("idna").decode("ascii").lower() for d in domains}
        return bool(allowed) and host in allowed
    except (ValueError, UnicodeError):
        return False


def safe_slug(value):
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:70]
    if not slug or slug.upper() in {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}:
        raise ValueError("Empty or reserved filename key")
    return slug


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
