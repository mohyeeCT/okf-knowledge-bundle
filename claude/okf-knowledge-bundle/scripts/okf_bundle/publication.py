"""Read-only publication verification through an explicitly supplied HTTP adapter.

This module never opens a network connection itself. The caller must authorize
the exact base URL and supply the guarded production adapter or a local harness.
"""
from __future__ import annotations

import io
import re
import tarfile
from urllib.parse import urljoin, urlsplit

from .common import digest
from .validation import safe_relative_path


def verify_publication(manifest, base_url, fetch, removed_paths=None, replacement_redirects=None):
    if not isinstance(base_url, str) or not base_url.endswith("/"):
        raise ValueError("An explicit authorized base URL ending in / is required")
    parsed_base = urlsplit(base_url)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.hostname or parsed_base.username or parsed_base.password:
        raise ValueError("Expected HTTP(S) base URL without credentials")
    if parsed_base.query or parsed_base.fragment:
        raise ValueError("Base URL cannot include a query or fragment")
    payload = manifest.get("payload", {})
    if not payload or any(not safe_relative_path(path) for path in payload):
        raise ValueError("Invalid expected payload manifest")
    issues, observations = [], []

    def issue(rule, path, message):
        issues.append({"rule": "publication." + rule, "entity": path, "message": message})

    def request(url, headers):
        response = fetch(url, headers=headers)
        if not isinstance(response, dict) or not isinstance(response.get("body"), bytes):
            raise ValueError("HTTP adapter must return {status, headers, body: bytes, url?}")
        return {**response, "headers": {str(k).lower(): str(v) for k, v in response.get("headers", {}).items()}}

    def check_archive(raw, path):
        expected = {name: value for name, value in payload.items() if name.endswith(".md")}
        observed = {}
        expanded = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r|gz") as archive:
                for member in archive:
                    if not member.isfile() or not safe_relative_path(member.name) or member.name in observed:
                        raise ValueError("Unsafe or duplicate archive member")
                    if member.name not in expected or member.size > 20 * 1024 * 1024:
                        raise ValueError("Unexpected or oversized archive member")
                    expanded += member.size
                    if expanded > 512 * 1024 * 1024:
                        raise ValueError("Archive exceeds expanded verification bound")
                    content = archive.extractfile(member).read(member.size + 1)
                    if len(content) != member.size:
                        raise ValueError("Archive member length mismatch")
                    observed[member.name] = digest(content)
            if observed != expected:
                raise ValueError("Archive Markdown contents do not equal the release manifest")
        except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
            issue("archive", path, str(exc))

    for path, expected_hash in sorted(payload.items()):
        url = urljoin(base_url, path)
        try:
            response = request(url, {"Accept-Encoding": "identity"})
        except Exception as exc:
            issue("fetch", path, f"Adapter failed: {type(exc).__name__}: {exc}")
            continue
        status = response.get("status")
        headers, body = response["headers"], response["body"]
        observations.append({"path": path, "url": url, "observed_url": response.get("url"), "status": status,
                             "sha256": digest(body), "content_type": headers.get("content-type"),
                             "content_encoding": headers.get("content-encoding")})
        if status != 200:
            issue("status", path, f"Expected HTTP 200, received {status}")
        if response.get("url") and response["url"] != url:
            issue("redirect", path, "Unexpected final URL; exact static paths must be served")
        if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            issue("encoding", path, "Unexpected content encoding despite identity-transfer request")
        mime = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if path.endswith(".md") and mime not in {"text/markdown", "text/plain"}:
            issue("content_type", path, "Expected Markdown/plain text, not an app shell or login response")
        if body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            issue("app_shell", path, "HTML application shell returned for a bundle file")
        if digest(body) != expected_hash:
            issue("hash", path, "Served entity bytes differ from the approved release")
        if path.endswith(".md"):
            declared_charset = re.search(r"(?:^|;)\s*charset\s*=\s*['\"]?([^;'\"\s]+)", headers.get("content-type", ""), re.I)
            if declared_charset and declared_charset.group(1).lower() not in {"utf-8", "utf8"}:
                issue("charset", path, "Serving charset conflicts with the required UTF-8 Markdown")
            try:
                body.decode("utf-8", errors="strict")
            except UnicodeError:
                issue("charset", path, "Markdown is not valid UTF-8")
            if path == "index.md" and manifest.get("deployment", {}).get("indexing") == "indexable":
                robots = headers.get("x-robots-tag", "").lower()
                if "noindex" in robots or "none" in robots or "nosnippet" in robots or re.search(r"\bmax-snippet\s*:\s*0\b", robots):
                    issue("indexing", path, "Entry response conflicts with indexable/snippet-eligible policy")
        if path == "okf.tar.gz":
            if mime not in {"application/gzip", "application/x-gzip"}:
                issue("content_type", path, "Expected gzip archive media type")
            check_archive(body, path)
            try:
                normal = request(url, {})
                if normal.get("status") != 200 or digest(normal["body"]) != expected_hash:
                    issue("normal_download", path, "Normal-client downloaded entity differs from approved archive")
                if normal["headers"].get("content-encoding", "identity").lower() not in {"", "identity"}:
                    issue("encoding", path, "Already-gzipped archive is incorrectly labelled with transport gzip")
                check_archive(normal["body"], path)
            except Exception as exc:
                issue("normal_download", path, f"Normal download failed: {type(exc).__name__}: {exc}")
    retired = sorted(set(manifest.get("changes", {}).get("removed", [])) | set(removed_paths or []))
    replacements = replacement_redirects or {}
    for path in retired:
        if not safe_relative_path(path) or path in payload:
            issue("retired_path", path, "Invalid retired-path instruction")
            continue
        url = urljoin(base_url, path)
        try:
            response = request(url, {"Accept-Encoding": "identity"})
            status = response.get("status")
            if status in {404, 410}:
                continue
            approved = replacements.get(path)
            location = response["headers"].get("location")
            if approved and status in {301, 302, 303, 307, 308} and location and urljoin(url, location) == approved:
                continue
            issue("retired_path", path, "Retired file remains served or lacks an approved replacement redirect")
        except Exception as exc:
            issue("retired_path", path, f"Could not verify retired URL: {type(exc).__name__}: {exc}")
    return {"ready": not issues, "issues": issues, "observations": observations, "retired_paths_checked": retired,
            "scope": "exact_manifest_files_and_enumerated_retired_paths",
            "limitations": ["Robots.txt/AI-bot access policies require separately scoped consumer checks.",
                            "No general site-wide absence-of-private-files claim is made.",
                            "HTTP retrieval does not prove indexing, citation, factual correctness or SEO benefit."]}
