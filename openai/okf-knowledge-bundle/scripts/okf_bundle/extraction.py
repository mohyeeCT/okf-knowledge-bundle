"""Offline HTML evidence extraction. No scripts, external resources or URLs run."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

EXTRACTION_CONTRACT = "html-v2"
from .common import normalize_url


def _space(value):
    return " ".join(str(value or "").split())


def _schema_nodes(value):
    if isinstance(value, dict):
        if "@type" in value:
            yield value
        for child in value.values():
            yield from _schema_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _schema_nodes(child)


def _types(node):
    value = node.get("@type", [])
    return [value] if isinstance(value, str) else [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def classify(url, title, headings, schema):
    """Conservative page-purpose rules; nested entities never vote on page type."""
    path = urlsplit(url or "").path.lower()
    segments = [part for part in path.split("/") if part]
    first = segments[0] if segments else ""
    purpose, signals = "Webpage", []
    rules = {
        "About Page": {"about", "about-us", "company", "our-story"},
        "Contact Page": {"contact", "contact-us", "get-in-touch"},
        "FAQ": {"faq", "faqs", "questions"},
        "Location": {"location", "locations", "branches"},
        "Service": {"service", "services", "training-services", "solutions"},
        "Product Category": {"category", "categories", "collection", "collections", "catalog", "c"},
        "Product": {"product", "products", "p", "item"},
        "Course": {"course", "courses"},
        "Job Posting": {"career", "careers", "job", "jobs"},
        "Archive": {"tag", "tags", "author", "news"},
    }
    if url and path in ("", "/", "/index.html"):
        purpose, signals = "Homepage", ["Confirmed root URL; nested schema is separate entity evidence"]
    else:
        for label, choices in rules.items():
            if first in choices:
                purpose, signals = label, [f"Exact URL segment: {first}"]
                break
        if purpose == "Archive" and first == "news" and len(segments) > 1 and segments[1] != "page":
            purpose = "Article"
        if purpose == "Webpage":
            label_text = " ".join([title, *[h["text"] for h in headings[:2]]]).lower()
            if any(x in path for x in ("policy", "terms-of-service", "notice", "disclaimer", "accessibility")):
                purpose, signals = "Policy", ["Policy-like URL; analyst review required"]
            elif any(x in label_text for x in ("training", "certification", "consulting")):
                purpose, signals = "Service", ["Offering wording in title/primary headings; provisional"]
    entities = []
    non_entities = {"WebPage", "WebSite", "BreadcrumbList", "ListItem", "Question", "Answer", "SearchAction", "ReadAction", "EntryPoint", "PropertyValueSpecification"}
    for node in schema:
        types = _types(node)
        if node.get("name") and any(t not in non_entities for t in types):
            # Preserve subtype labels; no unsupported hierarchy inference.
            entities.append({"name": _space(node["name"]), "types": types, "origin": "schema"})
    return {"purpose": purpose, "signals": signals or ["Insufficient deterministic page-purpose signals"], "entities": entities}


def extract_html(raw, url=None):
    warnings = []
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raw = raw.decode("utf-8", errors="replace")
            warnings.append("Invalid UTF-8 replaced; encoding review required")
    parser = html.HTMLParser(no_network=True, recover=True, huge_tree=False)
    try:
        tree = html.fromstring(raw, parser=parser)
    except (etree.ParserError, ValueError) as exc:
        raise ValueError("HTML cannot be parsed") from exc
    title = _space(tree.xpath("string(//title)"))
    def meta(name):
        values = tree.xpath("//meta[@name=$name or @property=$name]/@content", name=name)
        return _space(values[0]) if values else None
    canonical_values = tree.xpath("//link[contains(concat(' ', normalize-space(@rel), ' '), ' canonical ')]/@href")
    canonical = urljoin(url, canonical_values[0]) if canonical_values and url else canonical_values[0] if canonical_values else None
    og_url = meta("og:url")
    robots = meta("robots")
    language = tree.get("lang") or tree.xpath("string(//html/@lang)") or None
    schema = []
    for node in tree.xpath("//script[contains(@type,'ld+json')]"):
        try:
            schema.extend(_schema_nodes(json.loads(node.text or "")))
        except (ValueError, RecursionError):
            warnings.append("Invalid or excessively nested JSON-LD ignored")
    heads = [{"level": int(n.tag[1]), "text": _space(n.text_content())} for n in tree.xpath("//h1|//h2|//h3|//h4|//h5|//h6") if _space(n.text_content())]
    links = []
    for node in tree.xpath("//a[@href]"):
        href = node.get("href", "")
        resolved = urljoin(url or "", href)
        if urlsplit(resolved).scheme in ("http", "https"):
            links.append({"url": resolved, "text": _space(node.text_content())})
    iframe_count = len(tree.xpath("//iframe|//video"))
    password_form = bool(tree.xpath("//input[translate(@type,'PASSWORD','password')='password']"))
    body_nodes = tree.xpath("//body")
    body = body_nodes[0] if body_nodes else tree
    for node in list(body.xpath(".//script|.//style|.//noscript|.//template|.//svg|.//iframe|.//canvas|.//nav|.//*[@role='navigation']")):
        if node.getparent() is not None:
            node.drop_tree()
    for node in list(body.xpath(".//header|.//footer|.//*[@role='banner']|.//*[@role='contentinfo']")):
        if not any(p.tag in {"main", "section", "article"} for p in node.iterancestors()) and node.getparent() is not None:
            node.drop_tree()
    # Render block boundaries and table cells, avoiding concatenated table values.
    blocks = {"p", "div", "section", "article", "main", "li", "tr", "ul", "ol", "dl", "dt", "dd", "blockquote", "header", "footer", "pre"}
    def render(node):
        if not isinstance(node.tag, str):
            return ""
        prefix = "#" * int(node.tag[1]) + " " if re.fullmatch("h[1-6]", node.tag) else "* " if node.tag == "li" else ""
        text = prefix + (node.text or "")
        for child in node:
            text += render(child) + (child.tail or "")
        if node.tag in {"td", "th"}:
            text += " | "
        elif node.tag in blocks or re.fullmatch("h[1-6]", node.tag) or node.tag == "br":
            text += "\n"
        return text
    text = "\n".join(_space(line) for line in render(body).splitlines() if _space(line))
    selected_schema = [n for n in schema if not set(_types(n)) <= {"WebSite", "SearchAction", "EntryPoint", "PropertyValueSpecification", "ReadAction", "BreadcrumbList", "ListItem"}]
    metadata = {"title": title, "description": meta("description"), "canonical": canonical, "og_url": og_url, "robots": robots, "language": language}
    classification = classify(url, title, heads, schema)
    lower = text.lower()
    quality = "usable"
    if password_form and any(x in lower for x in ("log in", "login", "password")):
        quality = "login"
    elif any(x in (title + " " + text[:500]).lower() for x in ("just a moment", "verify you are human", "checking your browser", "access denied")):
        quality = "challenge"
    elif lower.strip().strip("# ") in ("loading", "loading...", "please wait"):
        quality = "placeholder"
    elif len(text.strip()) < 40:
        quality = "empty_or_thin"
    if iframe_count:
        warnings.append(f"{iframe_count} embedded media item(s); no transcript inferred")
    if url and re.search(r"/(courses|quizzes|lessons)(/|$)", urlsplit(url).path, re.I):
        warnings.append("LMS/course content requires relevance review; do not assume this is a current offering")
    if robots and "noindex" in robots.lower():
        warnings.append("Noindex source requires an explicit inclusion decision")
    if canonical and url:
        try:
            if normalize_url(canonical) != normalize_url(url):
                warnings.append("Canonicalized-away source requires an explicit inclusion decision")
        except ValueError:
            warnings.append("Invalid canonical declaration requires review")
    if classification["purpose"] == "Archive" or (url and re.search(r"/(account|cart|checkout|confirmation|page)(/|$)", urlsplit(url).path, re.I)):
        warnings.append("Utility/archive page requires bundle-scope review")
    return {"text": text, "evidence": {"body": text, "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True), "schema": json.dumps(selected_schema, ensure_ascii=False, sort_keys=True)}, "headings": heads, "links": links, "canonical": canonical, "og_url": og_url, "robots": robots, "classification": classification, "quality": quality, "warnings": warnings, "language": language}
