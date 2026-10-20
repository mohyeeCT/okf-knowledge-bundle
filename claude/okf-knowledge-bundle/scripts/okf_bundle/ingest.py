"""Bounded offline input adapters with audited URL identity and coverage."""
from __future__ import annotations

import csv
import base64
import hashlib
import io
import json
import re
import stat
import unicodedata
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

from lxml import etree

from .common import normalize_url, public_url, stable_id, utc_now
from .extraction import EXTRACTION_CONTRACT, extract_html


DEFAULT_LIMITS = {"zip_compressed_bytes": 250 * 1024**2, "zip_expanded_bytes": 2 * 1024**3,
                  "html_bytes": 20 * 1024**2, "zip_entries": 50000, "zip_ratio": 1000,
                  "csv_bytes": 25 * 1024**2, "csv_rows": 50000, "xml_bytes": 20 * 1024**2,
                  "sitemap_bytes": 100 * 1024**2, "sitemap_documents": 1000,
                  "sitemap_depth": 10, "urls": 50000}
URL_NAMES = {"url", "urls", "address", "page", "page url", "page_url", "website url", "top pages"}


def limits(config):
    return {**DEFAULT_LIMITS, **config.get("limits", {})}


def empty_result():
    return {"inventory": [], "sources": {}, "warnings": []}


def new_source(url, config, identity_basis="supplied URL", identity_confirmed=True, fallback=None):
    normalized = normalize_url(url) if url else None
    sid = stable_id("src", normalized or fallback)
    allowed = bool(normalized and public_url(normalized, config.get("domains", [])))
    return {"id": sid, "source_key": sid, "url_policy_version": 1, "key": normalized or fallback, "requested_url": url,
            "normalized_url": normalized, "citation_url": normalized if identity_confirmed and allowed else None,
            "confirmed_identity_url": normalized if identity_confirmed and allowed else None,
            "identity_basis": identity_basis, "identity_confirmed": identity_confirmed,
            "fragment": urlsplit(normalized).fragment if normalized else None,
            "scope": "included" if allowed and identity_confirmed else "pending" if not normalized or not identity_confirmed else "excluded",
            "scope_reason": "Within approved scope" if allowed else "Identity unknown" if not normalized else "Unsafe or out-of-scope URL",
            "classification": {"purpose": "Webpage", "signals": [], "entities": []},
            "availability": "unknown", "availability_acceptance": None, "captured_status": None,
            "provider_status": None, "canonical": None, "og_url": None, "robots": None,
            "active_version": None, "versions": {}, "warnings": ["URL fragment preserved; review whether it is a hash-routed view or an ordinary anchor"] if normalized and urlsplit(normalized).fragment else []}


def representation_settings(settings):
    """Settings selecting content; operational reject-only ceilings are provenance."""
    return {key: value for key, value in settings.items() if key not in ("token_budget", "response_bytes")}


def add_version(source, raw, extracted, representation, config, settings=None, capture_at=None):
    raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
    content_hash = hashlib.sha256(raw_bytes).hexdigest()
    settings = {"extractor": EXTRACTION_CONTRACT, **(settings or {})}
    locale = str(config.get("locale", "en")).lower()
    identity_settings = representation_settings(settings)
    version_id = stable_id("ver", {"hash": content_hash, "representation": representation, "settings": identity_settings, "locale": locale})
    # Reuse old state identities without rewriting historical claim citations.
    existing = sorted(source["versions"], key=lambda key: key != source.get("active_version"))
    for key in existing:
        previous = source["versions"][key]
        if (previous["content_hash"] == content_hash and previous["representation"] == representation
                and previous.get("locale") == locale and representation_settings(previous.get("settings", {})) == identity_settings):
            version_id = key
            break
    if version_id not in source["versions"]:
        source["versions"][version_id] = {"id": version_id, "representation": representation,
            "settings": dict(settings), "identity_settings": identity_settings, "locale": locale, "observed_language": extracted.get("language") or None, "content_hash": content_hash,
            "raw_content": raw_bytes.decode("utf-8", errors="replace"),
            "text": extracted["text"], "evidence": extracted["evidence"], "headings": extracted["headings"],
            "links": extracted["links"], "imported_at": utc_now(), "capture_at": capture_at,
            "warnings": list(extracted.get("warnings", []))}
        try:
            raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            source["versions"][version_id]["raw_bytes_base64"] = base64.b64encode(raw_bytes).decode("ascii")
    version = source["versions"][version_id]
    history = version.setdefault("settings_history", [dict(version["settings"])])
    if settings not in history:
        history.append(dict(settings))
    source["active_version"] = version_id
    for field in ("canonical", "og_url", "robots", "classification"):
        if field in extracted:
            source[field] = extracted[field]
    source["warnings"] = list(dict.fromkeys(source["warnings"] + extracted.get("warnings", [])))
    return version_id


def _csv_rows(path, config, url_column=None):
    bound = limits(config)
    path = Path(path)
    if path.stat().st_size > bound["csv_bytes"]:
        raise ValueError("CSV byte limit exceeded; split the input")
    text = path.read_text(encoding="utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return []
    first = rows[0]
    headerless = len(first) == 1 and first[0].strip().lower().startswith(("http://", "https://"))
    if headerless:
        index, header, data, offset = 0, ["URL"], rows, 1
    else:
        header = [h.strip() for h in first]
        candidates = [i for i, h in enumerate(header) if h.casefold() in URL_NAMES]
        if url_column is not None:
            candidates = [i for i, h in enumerate(header) if h.casefold() == url_column.casefold()]
        if len(candidates) != 1:
            raise ValueError("Choose one unambiguous URL column; available columns: " + ", ".join(header))
        index, data, offset = candidates[0], rows[1:], 2
    if len(data) > bound["csv_rows"]:
        raise ValueError("CSV row limit exceeded; split the input")
    return [{"row": row_no, "url": row[index].strip() if len(row) > index else "",
             "metadata": {header[i] if i < len(header) else f"extra_{i}": value for i, value in enumerate(row)}}
            for row_no, row in enumerate(data, offset)]


def _add_candidate(result, url, input_ref, config, metadata=None):
    item = {"input_ref": input_ref, "url": url, "source_id": None, "state": "pending", "reason": "Awaiting collection"}
    result["inventory"].append(item)
    try:
        source = new_source(url, config)
    except (ValueError, TypeError):
        item.update(state="excluded", reason="Invalid HTTP(S) URL")
        return None
    if not url:
        item.update(state="excluded", reason="Empty URL")
        return None
    sid = source["id"]
    item["source_id"] = sid
    if sid in result["sources"]:
        item.update(state="duplicate", reason="Exact normalized URL already inventoried; input row retained")
        existing = result["sources"][sid]
        existing.setdefault("input_metadata", []).append({"input_ref": input_ref, "values": metadata or {}, "origin": "supplied; not a live observation"})
        return existing
    if source["scope"] == "excluded":
        item.update(state="excluded", reason=source["scope_reason"])
    source["input_metadata"] = [{"input_ref": input_ref, "values": metadata or {}, "origin": "supplied; not a live observation"}]
    result["sources"][sid] = source
    return source


def discover_csv(path, config, url_column=None):
    result = empty_result()
    for row in _csv_rows(path, config, url_column):
        _add_candidate(result, row["url"], {"file": Path(path).name, "row": row["row"]}, config, row["metadata"])
    return result


def _macos_metadata(name):
    path = PurePosixPath(name)
    return path.name == ".DS_Store" or path.name.startswith("._") or (path.parts and path.parts[0] == "__MACOSX")


def _checked_zip(path, config):
    bound = limits(config)
    if Path(path).stat().st_size > bound["zip_compressed_bytes"]:
        raise ValueError("Compressed ZIP byte limit exceeded")
    archive = zipfile.ZipFile(path)
    try:
        entries = archive.infolist()
        if len(entries) > bound["zip_entries"] or sum(e.file_size for e in entries) > bound["zip_expanded_bytes"]:
            raise ValueError("ZIP entry/expanded byte limit exceeded")
        names = set()
        for e in entries:
            name = e.filename
            p = PurePosixPath(name)
            if "\\" in name or p.is_absolute() or any(s in ("..", ".") or ":" in s or s.endswith((" ", ".")) for s in name.rstrip("/").split("/")) or re.match(r"^[a-zA-Z]:", name) or "\x00" in name:
                raise ValueError(f"Unsafe archive path: {name!r}")
            key = unicodedata.normalize("NFC", name).casefold().rstrip("/")
            if key in names:
                raise ValueError(f"Duplicate/colliding archive destination: {name!r}")
            names.add(key)
            if stat.S_ISLNK(e.external_attr >> 16) or e.flag_bits & 1:
                raise ValueError(f"Symlink or encrypted ZIP entry rejected: {name!r}")
            if e.file_size > bound["html_bytes"] or e.file_size > max(1, e.compress_size) * bound["zip_ratio"]:
                raise ValueError(f"Entry size or archive expansion ratio limit exceeded: {name!r}")
            if not e.is_dir() and not _macos_metadata(name) and p.suffix.lower() not in (".html", ".htm"):
                raise ValueError(f"Unsupported ZIP entry; only HTML documents are accepted: {name!r}")
        return archive
    except Exception:
        archive.close()
        raise


def _filename_candidate(name):
    stem = PurePosixPath(name).stem
    match = re.match(r"^(?:original|rendered)_(https?)_([^_]+)(.*)$", stem, re.I)
    if not match:
        return None
    # This is a candidate only: '_' is ambiguous between path delimiters and literal text.
    tail = unquote(match.group(3)).replace("_", "/")
    return f"{match.group(1).lower()}://{match.group(2)}{tail or '/'}"


def _bind_companion_metadata(source, matches, csv_path):
    source["input_metadata"] = [{"input_ref": {"file": Path(csv_path).name, "row": row["row"]},
        "values": row["metadata"], "origin": "Companion CSV assertion; never establishes captured-file identity"} for row in matches]
    if not source["identity_confirmed"] or len(matches) != 1:
        source["csv_metadata_review_required"] = True
        source.update(scope="pending", scope_reason="Companion metadata join is missing or ambiguous; review required")
        return
    row = matches[0]
    values = {key.casefold(): str(value).strip() for key, value in row["metadata"].items()}
    provenance = {"file": Path(csv_path).name, "row": row["row"], "address": row["url"],
        "binding": "exact unique normalized requested-URL match", "file_identity_basis": source["identity_basis"],
        "temporal_alignment": "unknown", "establishes_identity": False}
    source["csv_metadata_provenance"] = provenance
    source["csv_metadata_review_required"] = False
    source["csv_indexability"] = values.get("indexability")
    source["csv_indexability_status"] = values.get("indexability status")
    if source["csv_indexability"] and source["csv_indexability"].casefold().replace("-", "").replace(" ", "") != "indexable":
        source.update(scope="pending", scope_reason="Companion CSV indexability requires version-bound inclusion review")
    status = values.get("status code", values.get("status", ""))
    if status.isdigit() and 100 <= int(status) <= 599:
        source["captured_status"] = int(status)
        source["captured_status_origin"] = "Companion CSV exact unique URL match; file identity established separately; capture time/alignment unknown"
        if int(status) in (401, 403):
            source.update(availability="nonpublic", scope="excluded", scope_reason="Captured authorization failure; public source evidence required")
        elif int(status) >= 400:
            source.update(availability="failed", scope="pending", scope_reason="Captured unavailable source; a new availability observation is required")
    elif status:
        source["csv_metadata_review_required"] = True
        source.update(scope="pending", scope_reason="Companion CSV HTTP status is invalid; metadata review required")


def ingest_sf(zip_path, config, csv_path=None):
    result = empty_result()
    rows = _csv_rows(csv_path, config) if csv_path else []
    confirmed = config.get("sf_mapping", {})  # explicit {archive filename: confirmed URL}
    csv_by_url = {}
    for row in rows:
        try:
            csv_by_url.setdefault(normalize_url(row["url"]), []).append(row)
        except ValueError:
            result["warnings"].append(f"Companion CSV row {row['row']} has an invalid URL")
    with _checked_zip(zip_path, config) as archive:
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            if _macos_metadata(entry.filename):
                result["inventory"].append({"input_ref": {"archive": Path(zip_path).name, "file": entry.filename},
                    "url": None, "source_id": None, "state": "excluded", "reason": "Bounded macOS metadata skipped; not an HTML capture"})
                continue
            raw = archive.read(entry)
            input_ref = {"archive": Path(zip_path).name, "file": entry.filename}
            item = {"input_ref": input_ref, "url": None, "source_id": None, "state": "pending", "reason": ""}
            result["inventory"].append(item)
            try:
                candidate = confirmed.get(entry.filename) or _filename_candidate(entry.filename)
                extracted = extract_html(raw, candidate)
                canonical = extracted["canonical"]
                identity_confirmed = False
                basis = "unknown"
                if entry.filename in confirmed:
                    basis, identity_confirmed = "analyst-confirmed file mapping", True
                elif candidate and canonical:
                    # A literal underscore or encoded separator in the declared path makes this export spelling ambiguous.
                    cpath = urlsplit(canonical).path
                    try:
                        agrees = normalize_url(candidate) == normalize_url(canonical)
                    except ValueError:
                        agrees = False
                    if "_" not in cpath and not re.search(r"%2f|%5c", canonical, re.I) and agrees:
                        basis, identity_confirmed = "filename mapping agrees with self-canonical", True
                    else:
                        basis = "filename/metadata disagreement; confirmation required"
                elif candidate or canonical or extracted["og_url"]:
                    basis = "filename-only or metadata-only candidate; confirmation required"
                candidate = candidate or canonical or extracted["og_url"]
                source = new_source(candidate, config, basis, identity_confirmed, fallback={"archive_file": entry.filename, "hash": hashlib.sha256(raw).hexdigest()})
                if not identity_confirmed:
                    # Ambiguous flattened spellings must never overwrite another
                    # capture merely because their candidate URLs happen to agree.
                    source["id"] = stable_id("src", {"candidate": source["normalized_url"], "file": entry.filename})
                    source["source_key"] = source["id"]
                sid = source["id"]
                if sid in result["sources"]:
                    previous = result["sources"][sid]
                    previous.setdefault("input_refs", []).append(input_ref)
                    source = previous
                else:
                    source["input_refs"] = [input_ref]
                    result["sources"][sid] = source
                representation = "sf-rendered" if PurePosixPath(entry.filename).name.lower().startswith("rendered_") else "sf-original"
                previous_hashes = {v["content_hash"] for v in source["versions"].values() if v["representation"] == representation}
                if previous_hashes and hashlib.sha256(raw).hexdigest() not in previous_hashes:
                    source.update(identity_confirmed=False, citation_url=None, confirmed_identity_url=None, scope="pending", scope_reason="Multiple files map to one URL/representation; confirm the captured identity and version")
                    source["warnings"].append("Ambiguous many-to-one archive mapping requires analyst confirmation")
                before = len(source["versions"])
                add_version(source, raw, extracted, representation, config, {"extractor": EXTRACTION_CONTRACT})
                source["capture_date_basis"] = "unknown; archive timestamp is not crawl time"
                matches = csv_by_url.get(source["normalized_url"], [])
                if csv_path:
                    _bind_companion_metadata(source, matches, csv_path)
                else:
                    source.setdefault("input_metadata", [])
                if extracted["quality"] in ("login", "challenge"):
                    source.update(scope="excluded", scope_reason=extracted["quality"] + " content is not public evidence", availability="nonpublic")
                elif extracted["quality"] != "usable" or any("requires" in w for w in extracted["warnings"]):
                    source.update(scope="pending", scope_reason="Extraction/relevance/indexing review required")
                item.update(url=candidate, source_id=sid, state="included" if source["scope"] == "included" else "excluded" if source["scope"] == "excluded" else "pending", reason=source["scope_reason"])
                if len(source["versions"]) == before:
                    item.update(state="duplicate", reason="Identical representation retained once; input entry retained")
            except (ValueError, etree.Error, UnicodeError) as exc:
                item.update(state="failed", reason=str(exc))
    # Reconcile companion rows too; a URL match is metadata, never proof that a
    # flattened archive filename captured that URL. Unjoined rows remain visible.
    confirmed_urls = {s["normalized_url"]: s["id"] for s in result["sources"].values() if s["identity_confirmed"]}
    for row in rows:
        normalized = None
        try:
            normalized = normalize_url(row["url"])
        except ValueError:
            pass
        sid = confirmed_urls.get(normalized)
        result["inventory"].append({"input_ref": {"file": Path(csv_path).name, "row": row["row"], "kind": "companion-metadata"},
            "url": row["url"], "source_id": sid,
            "state": "duplicate" if sid else "pending" if normalized and public_url(normalized, config.get("domains", [])) else "excluded",
            "reason": "Companion metadata retained; exact unique URL binding is separate from captured-file identity" if sid else "Companion row has no confirmed capture join"})
    return result


def bounded_gzip(data, maximum):
    if data[:2] != b"\x1f\x8b":
        if len(data) > maximum:
            raise ValueError("Expanded document byte limit exceeded")
        return data
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    result = decoder.decompress(data, maximum + 1)
    if len(result) > maximum or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
        raise ValueError("Gzip is oversized, incomplete or contains extra members")
    return result


def discover_sitemap(location, config, fetch=None, *, state=None, clock=None, sleep=None):
    result = empty_result()
    bound = limits(config)
    pacing = None
    pacing_state = state if state is not None else {"config": config}

    def native_fetch(url):
        nonlocal pacing
        if not config.get("collection_authorized", False):
            raise ValueError("Remote sitemap collection is not authorized")
        if pacing is None:
            from .network import direct_fetch
            from .politeness import DirectFetcher
            pacing = DirectFetcher(pacing_state, direct_fetch, clock=clock, sleep=sleep)
        from .network import SITEMAP_REQUEST_HEADERS
        return pacing(url, max_bytes=bound["xml_bytes"], headers=SITEMAP_REQUEST_HEADERS, decode_content_encoding=False)
    remote = str(location).lower().startswith(("http://", "https://"))
    if remote and fetch is None:
        if not config.get("collection_authorized", False):
            raise ValueError("Remote sitemap collection is not authorized")
        fetch = native_fetch
    queue, seen, total, documents = [(str(location), 0, None)], set(), 0, 0
    url_entries = 0
    while queue:
        target, depth, parent = queue.pop(0)
        is_url = target.lower().startswith(("http://", "https://"))
        try:
            key = normalize_url(target) if is_url else str(Path(target).resolve())
        except (ValueError, OSError) as exc:
            result["inventory"].append({"input_ref": {"sitemap": target, "parent": parent}, "url": target,
                "source_id": None, "state": "failed", "reason": str(exc)})
            result["warnings"].append("Sitemap discovery partial: an invalid document location was quarantined")
            continue
        if key in seen:
            result["warnings"].append(f"Duplicate/cyclic sitemap skipped: {key}")
            continue
        seen.add(key)
        if depth > bound["sitemap_depth"] or documents >= bound["sitemap_documents"] or url_entries >= bound["urls"]:
            result["warnings"].append("Sitemap discovery partial: configured depth/document/URL limit reached")
            break
        try:
            documents += 1
            if is_url:
                if not public_url(target, config.get("domains", [])):
                    raise ValueError("Sitemap destination is unsafe or outside approved domains")
                if fetch is None:
                    if not config.get("collection_authorized", False):
                        raise ValueError("Child sitemap network collection is not authorized")
                    fetch = native_fetch
                response = fetch(target)
                if isinstance(response, bytes):
                    data = response
                else:
                    if response.get("status", 200) >= 400:
                        raise ValueError("Child sitemap returned a failing HTTP status")
                    returned = response.get("url", target)
                    if not public_url(returned, config.get("domains", [])):
                        raise ValueError("Sitemap redirect is unsafe or outside approved domains")
                    data = response["body"]
            else:
                if Path(target).stat().st_size > bound["xml_bytes"]:
                    raise ValueError("XML file byte limit exceeded")
                data = Path(target).read_bytes()
            data = bounded_gzip(data, bound["xml_bytes"])
            total += len(data)
            if total > bound["sitemap_bytes"]:
                raise ValueError("Aggregate expanded sitemap byte limit exceeded")
            if re.search(br"<!\s*(DOCTYPE|ENTITY)", data, re.I):
                raise ValueError("XML DTD/entity declarations are not supported")
            root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False))
            kind = etree.QName(root).localname
            if kind not in ("urlset", "sitemapindex"):
                raise ValueError("Expected sitemap urlset or sitemapindex")
            expected_child = "sitemap" if kind == "sitemapindex" else "url"
            for entry_index, child in enumerate(root, start=1):
                if not isinstance(child.tag, str) or etree.QName(child).localname != expected_child:
                    continue
                if kind == "urlset":
                    if url_entries >= bound["urls"]:
                        result["warnings"].append("Sitemap discovery partial: URL entry limit reached")
                        break
                    url_entries += 1
                locs = [n for n in child if isinstance(n.tag, str) and etree.QName(n).localname == "loc" and etree.QName(n).namespace == etree.QName(root).namespace]
                if not locs or not (locs[0].text or "").strip():
                    result["inventory"].append({"input_ref": {"sitemap": target, "entry": entry_index}, "url": None, "source_id": None, "state": "excluded", "reason": "Missing sitemap loc"})
                    continue
                value = locs[0].text.strip()
                if kind == "sitemapindex":
                    try:
                        if not value.lower().startswith(("http://", "https://")):
                            # Remote XML cannot choose local files, even on Windows.
                            if is_url:
                                value = urljoin(target, value)
                                if not value.lower().startswith(("http://", "https://")):
                                    raise ValueError("Remote sitemap children must use HTTP(S)")
                            else:
                                base = Path(target).resolve().parent
                                child_path = (base / value).resolve()
                                if not child_path.is_relative_to(base):
                                    raise ValueError("Local sitemap index escapes its input directory")
                                value = str(child_path)
                    except (ValueError, OSError) as exc:
                        result["inventory"].append({"input_ref": {"sitemap": value, "parent": target}, "url": value,
                            "source_id": None, "state": "failed", "reason": str(exc)})
                        result["warnings"].append("Sitemap discovery partial: an invalid child location was quarantined")
                        continue
                    queue.append((value, depth + 1, target))
                else:
                    lastmod = next((n.text for n in child if isinstance(n.tag, str) and etree.QName(n).localname == "lastmod"), None)
                    _add_candidate(result, value, {"sitemap": target, "parent": parent, "entry": entry_index}, config, {"sitemap_lastmod_assertion": lastmod})
                    if url_entries >= bound["urls"]:
                        result["warnings"].append("Sitemap discovery partial: URL limit reached")
                        break
        except (ValueError, OSError, etree.Error, zlib.error) as exc:
            result["inventory"].append({"input_ref": {"sitemap": target, "parent": parent}, "url": target, "source_id": None, "state": "failed", "reason": str(exc)})
            result["warnings"].append("Sitemap discovery partial: a document could not be processed")
            if total > bound["sitemap_bytes"]:
                break
    result["sitemap_summary"] = {"documents": documents, "expanded_bytes": total, "partial": any("partial" in w for w in result["warnings"])}
    if pacing is not None:
        result["sitemap_summary"]["pacing"] = pacing.receipt()
        pacing_state.setdefault("sitemap_collection_history", []).append(result["sitemap_summary"])
    return result
