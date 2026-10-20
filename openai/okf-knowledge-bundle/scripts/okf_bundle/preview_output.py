"""Readable content and separate private evidence, from one bound preview."""
import html
import json
from pathlib import Path
from urllib.parse import urlsplit

from .common import atomic_json, digest


def _link(url):
    text = html.escape(str(url or ""), quote=True)
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return text
    return f'<a href="{text}" rel="noopener noreferrer">{text}</a>'


def content_html(preview):
    """Show public wording and destinations; leave review machinery in the audit."""
    esc = lambda value: html.escape(str(value or ""), quote=True)
    sections = []
    for concept in preview["concepts"].values():
        parts = [f'<section><h2>{esc(concept["title"])}</h2>',
                 f'<p class="wording">{esc(concept["description"])}</p>']
        if concept.get("resource"):
            parts.append('<p>Website page: ' + _link(concept["resource"]) + '</p>')
        for cid in concept["claim_ids"]:
            claim = preview["claims"][cid]
            urls = dict.fromkeys(preview["sources"][e["source_id"]]["url"] for e in claim["evidence"])
            parts += [f'<div class="statement"><p class="wording">{esc(claim["public_statement"])}</p>',
                      '<p class="sources">Source: ' + ' · '.join(_link(url) for url in urls) + '</p></div>']
        details = [concept.get("type", ""), concept.get("locale", ""), *concept.get("tags", [])]
        parts.append('<details><summary>Topic details</summary><p>' + esc(' · '.join(details)) + '</p></details></section>')
        sections.append(''.join(parts))
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Proposed OKF content</title><style>'
            'body{font:17px/1.6 system-ui,sans-serif;color:#18232c;background:#fff;max-width:850px;margin:40px auto;padding:0 24px}'
            'h1{font-size:30px}h2{font-size:23px}section{border-top:1px solid #d9dfe3;margin-top:32px;padding-top:12px}'
            '.wording{white-space:pre-wrap}.sources,details{font-size:14px;color:#46545e}a{color:#155c92;overflow-wrap:anywhere}'
            '.statement{margin:24px 0}summary{cursor:pointer}</style><body>'
            '<h1>Proposed OKF content</h1><p><strong>Unpublished · Awaiting content approval</strong></p>'
            f'<p>{len(preview["concepts"])} topics · {len(preview["claims"])} statements. '
            'Review the wording and website links below. Critical questions and exclusions are in the separate short review. '
            'One content approval covers this preview; it does not authorize publication.</p>'
            + ''.join(sections) + '</body></html>')


def audit_markdown(preview):
    lines = ['# Private evidence details', '',
             'Supporting record only. Review proposed content in the separate content preview.', '',
             f'Preview hash: `{digest(preview)}`', f'Run: `{preview["run_id"]}`', '',
             'Excluded topic IDs: ' + ', '.join(preview["excluded_concept_ids"]), '']
    for cid, claim in preview["claims"].items():
        lines += [f'## {cid}', '', claim['public_statement'], '',
                  'Qualifiers: ' + json.dumps(claim.get('qualifiers', {}), ensure_ascii=False),
                  'Review hints: ' + ', '.join(claim.get('risk_categories', [])),
                  'Revision: ' + claim['revision'], '']
        for evidence in claim['evidence']:
            lines += [preview['sources'][evidence['source_id']]['url'],
                      f"{evidence['locator']} · {evidence['version_id']}",
                      '\n'.join('> ' + line for line in evidence['quote'].splitlines()), '']
    return '\n'.join(lines) + '\n'


def write_preview(preview, destination, private_root):
    destination = Path(destination).resolve()
    content = destination.with_suffix('.content.html')
    audit = destination.with_suffix('.audit.md')
    targets = [destination, content, audit]
    if len(set(targets)) != 3 or any(p.exists() or p.is_relative_to(private_root) for p in targets):
        raise ValueError('Use new preview files outside immutable private state')
    # Render completely before writing any output. The original JSON contract is unchanged.
    rendered_content, rendered_audit = content_html(preview), audit_markdown(preview)
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(destination, preview)
    content.write_text(rendered_content, encoding='utf-8', newline='\n')
    audit.write_text(rendered_audit, encoding='utf-8', newline='\n')
    return {'preview': str(destination), 'content_preview': str(content), 'audit_details': str(audit),
            'preview_hash': digest(preview), 'concepts': len(preview['concepts']),
            'statements': len(preview['claims']),
            'scope': 'Content review only; no client confirmation or release approval'}
