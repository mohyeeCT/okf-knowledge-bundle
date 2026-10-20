"""Read-only OKF v0.2 structural and stricter website-profile checks.

A clean report is not a factual or semantic certification. Core conformance
deliberately tolerates unknown types/fields and broken relationship links.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import html
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import string
import unicodedata
from urllib.parse import unquote, urlsplit, parse_qsl

import yaml

SPEC_COMMIT = "ad30107c31c06aec8a7d5636e0d1058118604e6f"
SPEC_BLOB = "c06e3eede0c910d0ecf12524c34204156f8795ac"
SPEC_URL = f"https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/{SPEC_COMMIT}/SPEC.md"
PUBLIC_FIELDS = {"type", "title", "description", "resource", "tags", "status", "generated", "sources", "verified", "stale_after"}
SOURCE_FIELDS = {"id", "resource", "title"}
EVENT_FIELDS = {"by", "at"}
RESERVED = {"index.md", "log.md"}
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
SECRET_QUERY = re.compile(r"(?:token|secret|password|passwd|session|authorization|api[_-]?key|signature|credential)", re.I)
HTTP_URL = re.compile(r"https?://[^\s<>\]\[\"'`]+", re.I)
FOOTNOTE = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")
FOOTNOTE_DEF = re.compile(r"^\[\^([A-Za-z0-9_-]+)\]:", re.M)
REFERENCE_DEF = re.compile(r"\[(?!\^)(?:\\.|[^\[\]\\]){1,999}\]:")
MARKDOWN_ESCAPE = re.compile(r"\\([" + re.escape(string.punctuation) + r"])")
ANGLE_EMAIL = re.compile(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*")
GFM_EMAIL = re.compile(r"(?<![\w.+-])(?:(?P<scheme>mailto|xmpp):)?(?P<local>[\w.+-]+)@(?P<domain>[\w.-]+)", re.I)
GFM_WWW = re.compile(r"(?<![a-zA-Z0-9])www\.[^\s<]+", re.I)


class FrontmatterLoader(yaml.SafeLoader):
    """Keep ISO timestamps as written; reject ambiguous duplicate mapping keys."""


FrontmatterLoader.yaml_implicit_resolvers = {
    key: [(tag, regex) for tag, regex in values if tag != "tag:yaml.org,2002:timestamp"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool)):
            raise ValueError("Unsupported mapping key")
        if key in result:
            raise ValueError(f"Duplicate mapping key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


FrontmatterLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_instant(value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Expected ISO datetime with explicit offset")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("Datetime is missing UTC offset")
    return instant.astimezone(timezone.utc)


def iso_instant(value):
    return parse_instant(value).isoformat().replace("+00:00", "Z")


def parse_document(text):
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return None, text
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("Unclosed YAML frontmatter") from exc
    raw = "\n".join(lines[1:end])
    if len(raw) > 1_000_000:
        raise ValueError("Frontmatter exceeds parser bound")
    # Aliases are not needed by this producer. A bounded parse prevents cycles
    # from reaching nested profile checks without rejecting ordinary YAML.
    events = list(yaml.parse(raw))
    if len(events) > 100_000 or sum(isinstance(e, yaml.AliasEvent) for e in events) > 100:
        raise ValueError("Frontmatter exceeds alias/node parser bound")
    data = yaml.load(raw, Loader=FrontmatterLoader)
    if not isinstance(data, dict):
        raise ValueError("Frontmatter must be a mapping")
    return data, "\n".join(lines[end + 1:]).strip()


def public_url_issue(value, allowed_domains=None):
    """Syntax/scope only: this never claims DNS or live availability checks."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        return "URL must be a nonempty whitespace-free string"
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        parsed.port
    except (ValueError, UnicodeError):
        return "Invalid URL"
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return "Only public HTTP(S) URLs without embedded credentials are allowed"
    if "\\" in value or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
        return "Local/private URL is not public evidence"
    try:
        if not ipaddress.ip_address(host).is_global:
            return "Nonpublic IP address"
    except ValueError:
        if "." not in host:
            return "Unqualified hostname"
    if any(SECRET_QUERY.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        return "Potential credential/session query parameter"
    if allowed_domains is not None:
        normalized = set()
        for domain in allowed_domains:
            candidate = urlsplit(domain).hostname if "://" in domain else domain
            normalized.add(str(candidate).encode("idna").decode("ascii").lower().rstrip("."))
        if host.rstrip(".") not in normalized:
            return "URL host is outside the approved domain set"
    return None


def sensitive_text_issue(text):
    if re.search(r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|file://|/(?:Users|home)/[^\s/]+/)", text):
        return "Local filesystem path in public text"
    if re.search(r"\b(?:sk|ghp|gho|github_pat)-?[A-Za-z0-9_]{20,}\b", text):
        return "Potential credential in public text"
    if re.search(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['\"]?[^\s'\"]{6,}", text):
        return "Potential secret assignment in public text"
    return None


def safe_relative_path(value):
    if (not isinstance(value, str) or not value or any(ord(c) < 32 for c in value)
            or any(c in value for c in '\\:*?"<>|#%') or value.startswith("/")):
        return False
    # Check literal components before Path collapses repeated slashes or dots.
    parts = value.split("/")
    return all(part not in {"", ".", ".."} and part.rstrip(". ") == part
               and part.split(".")[0].casefold() not in WINDOWS_RESERVED for part in parts)


def _escaped(text, offset):
    count = 0
    while offset > 0 and text[offset - 1] == "\\":
        count += 1
        offset -= 1
    return count % 2 == 1


def _link_whitespace(text, offset):
    start = offset
    while offset < len(text) and text[offset].isspace():
        offset += 1
    if text[start:offset].count("\n") > 1:
        raise ValueError("Blank lines inside a Markdown destination are unsupported")
    return offset


def _read_destination(text, offset, inline):
    """Read a bounded Markdown destination, never silently accept a partial parse."""
    offset = _link_whitespace(text, offset)
    start = offset
    if offset < len(text) and text[offset] == "<":
        offset += 1
        start = offset
        while offset < len(text) and (text[offset] != ">" or _escaped(text, offset)):
            if offset - start > 16_384 or text[offset] in "\r\n<":
                raise ValueError("Unsupported angle-bracket Markdown destination")
            offset += 1
        if offset == len(text):
            raise ValueError("Unclosed angle-bracket Markdown destination")
        destination = text[start:offset]
        offset += 1
    else:
        depth = 0
        while offset < len(text):
            char = text[offset]
            if offset - start > 16_384 or depth > 16:
                raise ValueError("Markdown destination exceeds parser bounds")
            if char == "\\" and offset + 1 < len(text) and text[offset + 1] in string.punctuation:
                offset += 2
                continue
            if char.isspace():
                break
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif char in "<>":
                raise ValueError("Unsupported delimiter inside Markdown destination")
            offset += 1
        if depth:
            raise ValueError("Unbalanced parentheses in Markdown destination")
        destination = text[start:offset]
    if not inline:
        if not destination or (offset < len(text) and not text[offset].isspace()):
            raise ValueError("Unsupported or incomplete reference destination")
        return destination, offset
    before_space = offset
    offset = _link_whitespace(text, offset)
    if offset > before_space and offset < len(text) and text[offset] in "\"'(":
        closing = ")" if text[offset] == "(" else text[offset]
        offset += 1
        title_start = offset
        while offset < len(text) and (text[offset] != closing or _escaped(text, offset)):
            if offset - title_start > 16_384 or "\n\n" in text[max(title_start, offset - 1):offset + 1]:
                raise ValueError("Unsupported Markdown link title")
            offset += 1
        if offset == len(text):
            raise ValueError("Unclosed Markdown link title")
        offset = _link_whitespace(text, offset + 1)
    if offset == len(text) or text[offset] != ")":
        raise ValueError("Unsupported or unclosed inline Markdown link")
    return destination, offset + 1


def _trim_gfm_url(value):
    value = value.rstrip("?!.,:*_~")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1].rstrip("?!.,:*_~")
    # GFM excludes an apparent entity reference at the end of an autolink.
    return re.sub(r"&[a-zA-Z0-9]+;$", "", value)


def _gfm_destinations(body):
    """Recognize implicit GFM links, not arbitrary domain-like plain text.

    GFM section 6.9 defines www boundaries, domain components and email endings:
    https://github.github.com/gfm/#autolinks-extension-
    The profile also checks the entity/escape-decoded view conservatively. It is
    a destination guard, not a claim of complete renderer/parser equivalence.
    """
    masked = list(body)
    # Never hide labels/reference uses based on regex guesses about Markdown
    # context: code spans and blank lines can make those guesses false. Only
    # whole HTTP(S) tokens suppress nested email/www interpretations; their
    # actual destinations are independently checked by the profile.
    for start, end in [match.span() for match in HTTP_URL.finditer(body)]:
        masked[start:end] = " " * (end - start)
    text = html.unescape(MARKDOWN_ESCAPE.sub(r"\1", "".join(masked)))
    destinations = []
    # Decoding an entity/escape may create a URL spelling that a renderer never
    # recognizes as a link. Check that destination too, but never use the new
    # spelling to hide an email/www candidate in the original text.
    for match in HTTP_URL.finditer(text):
        destinations.append(_trim_gfm_url(match[0]))
    www_spans = []
    for match in GFM_WWW.finditer(text):
        candidate = _trim_gfm_url(match[0])
        domain_match = re.match(r"[\w.-]+", candidate)
        domain = domain_match[0].rstrip(".") if domain_match else ""
        parts = domain.split(".")
        if len(parts) < 2 or not all(parts) or any("_" in part for part in parts[-2:]):
            continue
        destinations.append("http://" + candidate)
        www_spans.append(match.span())
    consumed_email_end = 0
    for match in GFM_EMAIL.finditer(text):
        if match.start() < consumed_email_end:
            continue
        if any(start <= match.start() < end for start, end in www_spans):
            continue
        # Do not backtrack to a valid prefix of a domain ending in '-' or '_'.
        domain = match["domain"].rstrip(".")
        parts = domain.split(".")
        if len(parts) < 2 or not all(parts) or domain.endswith(("-", "_")):
            continue
        scheme = (match["scheme"] or "mailto").lower()
        destination = scheme + ":" + match["local"] + "@" + domain
        consumed_email_end = match.end()
        if scheme == "xmpp":
            resource = re.match(r"/[a-zA-Z0-9@.]+", text[match.end():])
            if resource:
                destination += resource[0]
                consumed_email_end += len(resource[0])
        destinations.append(destination)
    return destinations


def _markdown_destinations(body):
    """Conservative profile scanner; this is not a renderer or a core OKF rule.

    Scan reference definitions even if unused (including in block quotes/lists).
    Code examples are checked conservatively too, so rendering-context ambiguity
    cannot hide a destination. Ordinary footnote prose is not a link definition.
    """
    destinations, errors = [], []
    for match in re.finditer(r"\]\(", body):
        if _escaped(body, match.start()):
            continue
        try:
            destination, end = _read_destination(body, match.end(), inline=True)
            destinations.append(destination)
        except ValueError as exc:
            errors.append(str(exc))
    for match in REFERENCE_DEF.finditer(body):
        if _escaped(body, match.start()):
            continue
        try:
            destination, end = _read_destination(body, match.end(), inline=False)
            destinations.append(destination)
        except ValueError as exc:
            errors.append(str(exc))
    # URI/email autolinks can become active without a Markdown label. HTML is
    # separately rejected; inspect entity/escape-obfuscated potential URIs too.
    for match in re.finditer(r"<([^<>]*)>", body):
        if not _escaped(body, match.start()) and re.search(r"[:@/\\&%]", match[1]):
            address = html.unescape(MARKDOWN_ESCAPE.sub(r"\1", match[1]))
            destinations.append("mailto:" + address if ANGLE_EMAIL.fullmatch(address) else match[1])
    destinations.extend(_gfm_destinations(body))
    return list(dict.fromkeys(destinations)), errors


def _destination_issue(raw, file, root, domains):
    href = html.unescape(MARKDOWN_ESCAPE.sub(r"\1", raw))
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in href):
        return "syntax", "Whitespace/control characters in a Markdown destination"
    if any(c in href for c in '<>"') or "\\" in href:
        return "syntax", "Unsafe delimiter in a Markdown destination"
    if re.search(r"%(?![0-9A-Fa-f]{2})", href):
        return "syntax", "Malformed percent encoding in a Markdown destination"
    # Check encoded controls in every kind of destination, including fragments
    # and relative queries. Ordinary escaped spaces/parentheses stay supported
    # in public HTTP(S) paths; encoded relative filenames are rejected below.
    decoded = href
    for _ in range(3):
        decoded = unquote(decoded)
        if "\\" in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded):
            return "syntax", "Encoded control/backslash in a Markdown destination"
    try:
        parsed = urlsplit(href)
    except ValueError:
        return "syntax", "Invalid Markdown destination"
    if parsed.scheme:
        reason = public_url_issue(href, domains)
        if reason:
            return "external", reason
        if "%" in parsed.netloc:
            return "syntax", "Encoded URL authority is unsupported"
        return None
    if href.startswith("/") or parsed.netloc:
        return "absolute", "Use relative bundle links or explicit public HTTP(S) URLs"
    if "%" in parsed.path:
        return "syntax", "Percent-encoded relative paths are unsupported; use literal bundle filenames"
    if any(SECRET_QUERY.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        return "external", "Potential credential/session query parameter"
    if any(part not in {".", ".."} and not safe_relative_path(part) for part in parsed.path.split("/") if parsed.path):
        return "syntax", "Unsafe relative Markdown destination"
    try:
        target = (file.parent / parsed.path).resolve()
        target.relative_to(root.resolve())
    except (ValueError, OSError):
        return "escape", "Link leaves the bundle or cannot be resolved safely"
    if parsed.path and (not target.is_file() or target.is_symlink()):
        return "missing", f"Missing linked file: {href}"
    return None


def _event_issues(value, field):
    issues = []
    if not isinstance(value, dict) or set(value) - EVENT_FIELDS:
        return [f"{field} must contain only by/at"]
    if not isinstance(value.get("by"), str) or not value["by"].strip():
        issues.append(f"{field}.by is required")
    elif "@" in value["by"] or ":\\" in value["by"]:
        issues.append(f"{field}.by must be an opaque public actor")
    try:
        parse_instant(value.get("at"))
    except (ValueError, TypeError):
        issues.append(f"{field}.at needs an offset-bearing ISO datetime")
    return issues


def _profile_frontmatter(meta, body, relative, issue, domains):
    extra = set(meta) - PUBLIC_FIELDS
    if extra:
        issue("profile.frontmatter.allowlist", relative, f"Unapproved fields: {sorted(map(str, extra))}")
    for field in ("title", "description"):
        if not isinstance(meta.get(field), str) or not meta[field].strip():
            issue("profile.frontmatter.required", relative, f"Missing readable {field}")
    if meta.get("status") not in ("stable", "deprecated"):
        issue("profile.lifecycle", relative, "Only stable or deprecated records may be released")
    if "resource" in meta:
        reason = public_url_issue(meta["resource"], domains)
        if reason:
            issue("profile.resource", relative, reason)
    if not isinstance(meta.get("tags", []), list) or any(not isinstance(t, str) for t in meta.get("tags", [])):
        issue("profile.tags", relative, "tags must be a list of strings")
    for message in _event_issues(meta.get("generated"), "generated"):
        issue("profile.generated", relative, message)
    verified = meta.get("verified", [])
    if isinstance(verified, dict):
        verified = [verified]  # Required OKF consumer normalization.
    if not isinstance(verified, list):
        issue("profile.verified", relative, "verified must be events")
        verified = []
    for event in verified:
        for message in _event_issues(event, "verified"):
            issue("profile.verified", relative, message)
    if meta.get("status") == "stable":
        if verified and not all(isinstance(e, dict) and str(e.get("by", "")).startswith("human:") for e in verified):
            issue("profile.verified", relative, "This profile emits only earned human verification events")
        try:
            parse_instant(meta.get("stale_after"))
        except (ValueError, TypeError):
            issue("profile.freshness", relative, "Missing valid stale_after")
    sources = meta.get("sources", [])
    if not isinstance(sources, list):
        issue("profile.sources", relative, "sources must be a list of mappings")
        sources = []
    ids = []
    for source in sources:
        if not isinstance(source, dict) or set(source) - SOURCE_FIELDS:
            issue("profile.sources.allowlist", relative, "Source contains unsupported nested keys")
            continue
        sid = source.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
            issue("profile.sources.id", relative, "Stable source footnote ID required")
        else:
            ids.append(sid)
        reason = public_url_issue(source.get("resource"), domains)
        if reason:
            issue("profile.sources.resource", relative, reason)
        if "title" in source and not isinstance(source["title"], str):
            issue("profile.sources.title", relative, "Source title must be text")
    if len(ids) != len(set(ids)):
        issue("profile.sources.id", relative, "Duplicate source ID")
    definitions = FOOTNOTE_DEF.findall(body)
    references = FOOTNOTE.findall(FOOTNOTE_DEF.sub("", body))
    if len(definitions) != len(set(definitions)) or set(definitions) != set(ids) or set(references) != set(ids):
        issue("profile.attribution", relative, "Footnote references, definitions and source IDs must match")
    if meta.get("status") == "stable" and not sources:
        issue("profile.sources", relative, "Stable concepts require public supporting sources")
    if not body.strip():
        issue("profile.body", relative, "Empty concept body")


def validate_bundle(path, profile=True, allowed_domains=None):
    root = Path(path)
    issues = []

    def issue(rule, entity, message, severity="error"):
        issues.append({"rule": rule, "entity": entity, "message": message, "severity": severity})

    if not root.is_dir() or root.is_symlink():
        issue("core.OKF.directory", ".", "Expected an ordinary bundle directory")
        return {"core": {"conformant": False}, "profile": {"ready": False}, "issues": issues, "semantic_review": "not_checked", "spec_commit": SPEC_COMMIT}
    paths = sorted(root.rglob("*"))
    seen = {}
    documents = {}
    for file in paths:
        relative = file.relative_to(root).as_posix()
        folded = unicodedata.normalize("NFC", relative).casefold()
        if file.is_symlink():
            issue("profile.path.symlink", relative, "Symbolic links are not publishable")
            continue
        if not safe_relative_path(relative):
            issue("profile.path.unsafe", relative, "Unsafe or reserved path")
        if folded in seen and seen[folded] != relative:
            issue("profile.path.collision", relative, "Case/Unicode path collision")
        seen[folded] = relative
        if file.is_dir():
            continue
        if file.suffix != ".md":
            if profile and relative != "okf.tar.gz":
                issue("profile.payload.allowlist", relative, "Only Markdown and the public tarball are allowed")
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="strict")
            meta, body = parse_document(text)
        except (ValueError, yaml.YAMLError, UnicodeError) as exc:
            issue("core.OKF.frontmatter", relative, str(exc))
            continue
        documents[relative] = (meta, body)
        if file.name == "index.md":
            if meta is not None and (relative != "index.md" or set(meta) != {"okf_version"} or not isinstance(meta.get("okf_version"), str)):
                issue("core.OKF.index", relative, "Only root index may declare okf_version frontmatter")
            if profile and relative == "index.md" and (meta or {}).get("okf_version") != "0.2":
                issue("profile.version", relative, "Publishing profile must declare OKF 0.2")
        elif file.name == "log.md":
            headings = re.findall(r"^##\s+(.+)$", body, re.M)
            if meta is not None:
                issue("core.OKF.log", relative, "Reserved logs do not carry frontmatter")
            if headings != sorted(headings, reverse=True):
                issue("core.OKF.log", relative, "Log date groups must be newest first")
            for heading in headings:
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", heading):
                        raise ValueError()
                    date.fromisoformat(heading)
                except ValueError:
                    issue("core.OKF.log", relative, "Log date headings must be real YYYY-MM-DD dates")
            if profile and (meta is not None or not headings):
                issue("profile.log", relative, "Profile log requires date-grouped entries without YAML")
        else:
            if meta is None or not isinstance(meta.get("type"), str) or not meta["type"].strip():
                issue("core.OKF.type", relative, "Concept needs YAML with a nonempty string type")
            if profile and meta is not None:
                _profile_frontmatter(meta, body, relative, issue, allowed_domains)
        if profile:
            reason = sensitive_text_issue(text)
            if reason:
                issue("profile.sensitive", relative, reason)
            if re.search(r"<!--|<!doctype\b|<\s*/?\s*[a-z][a-z0-9-]*(?:\s[^<>]*)?\s*/?>", text, re.I):
                issue("profile.html", relative, "Raw/active HTML is not allowed in public Markdown")
            for raw_url in HTTP_URL.findall(text):
                raw_url = html.unescape(MARKDOWN_ESCAPE.sub(r"\1", raw_url))
                reason = public_url_issue(raw_url.rstrip(".,;:!?)"), allowed_domains)
                if reason:
                    issue("profile.url", relative, reason)
            destinations, errors = _markdown_destinations(body)
            for message in errors:
                issue("profile.link.syntax", relative, message)
            for destination in destinations:
                problem = _destination_issue(destination, file, root, allowed_domains)
                if problem:
                    rule, message = problem
                    issue("profile.link." + rule, relative, message)
    if profile:
        for required in ("index.md", "log.md"):
            if required not in documents:
                issue("profile.required", required, "Publishing profile requires this file")
        if not any(PurePosixPath(p).name not in RESERVED for p in documents):
            issue("profile.empty", ".", "No publishable concepts")
        for directory in {str(PurePosixPath(p).parent) for p in documents if PurePosixPath(p).name not in RESERVED}:
            parent = PurePosixPath(directory)
            while str(parent) != ".":
                if (parent / "index.md").as_posix() not in documents:
                    issue("profile.index", str(parent), "Missing section/locale index")
                parent = parent.parent
    core_ok = not any(i["rule"].startswith("core.OKF.") and i["severity"] == "error" for i in issues)
    profile_ok = core_ok and not any(i["severity"] == "error" for i in issues)
    return {"core": {"conformant": core_ok}, "profile": {"enabled": profile, "ready": profile_ok if profile else None},
            "issues": issues, "semantic_review": "not_checked", "spec_commit": SPEC_COMMIT,
            "spec_url": SPEC_URL, "documents": len(documents)}
