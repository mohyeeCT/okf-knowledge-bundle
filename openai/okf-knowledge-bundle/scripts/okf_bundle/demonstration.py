"""Explicitly hypothetical handoffs, separate from release candidates and approvals."""
import io
import re
from pathlib import Path
import shutil
import tarfile
import tempfile
import zipfile

import yaml
from . import __version__
from .common import canonical_json, digest, utc_now
from .export import (_markdown_label, _normal_text, _supported_extraction, _tar_bytes,
                     concept_path, first_release_rollback)
from .validation import SPEC_COMMIT, public_url_issue, validate_bundle
from .network import awaiting_retry_results, pending_attempts

BANNER = "UNPUBLISHED DEMONSTRATION - approvals are assumed for illustration only. Do not deploy."


def _selected(state, ids):
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Select a nonempty unique concept ID list")
    concepts = []
    for cid in ids:
        concept = state["concepts"].get(cid)
        if not concept or concept.get("review", {}).get("decision") == "rejected" or concept.get("status") == "deprecated":
            raise ValueError("Unknown, excluded or deprecated demonstration concept")
        if not concept.get("claim_ids"):
            raise ValueError("Demonstration concepts need source-supported claims")
        for claim_id in concept["claim_ids"]:
            claim = state["claims"].get(claim_id)
            if (not claim or claim.get("publication") == "withheld" or claim.get("evidence_review") == "rejected"
                    or claim.get("business_confirmation") == "disputed" or claim.get("conflict") == "open"):
                raise ValueError("Excluded or unresolved claim cannot enter a demonstration")
            if not claim.get("evidence"):
                raise ValueError("Demonstration claims need retained evidence")
            for evidence in claim["evidence"]:
                source = state["sources"].get(evidence.get("source_id"), {})
                version = source.get("versions", {}).get(evidence.get("version_id"), {})
                status = source.get("captured_status")
                if (source.get("scope") != "included" or not source.get("identity_confirmed")
                        or source.get("active_version") != evidence.get("version_id")
                        or not version or not _supported_extraction(version)
                        or source.get("collection_state") == "pending" or pending_attempts(source) or awaiting_retry_results(source)
                        or source.get("availability") in ("failed", "nonpublic")
                        or isinstance(status, int) and status >= 400):
                    raise ValueError("Demonstration needs an included, identified, usable active source")
                url = source.get("citation_url")
                if public_url_issue(url, state["config"].get("domains")):
                    raise ValueError("Demonstration citation is outside the domain policy")
                identity = source.get("confirmed_identity_url") or source.get("normalized_url")
                if url != identity:
                    raise ValueError("Demonstration citation differs from captured identity")
                retained = version.get("evidence", {})
                locator = evidence.get("locator")
                quote = evidence.get("quote")
                if (locator not in ("body", "metadata", "schema") or locator not in retained
                        or not isinstance(quote, str) or not _normal_text(quote)
                        or _normal_text(quote) not in _normal_text(retained[locator])):
                    raise ValueError("Demonstration quotation does not match retained evidence")
                language = version.get("observed_language")
                if language and language.split("-")[0] != concept.get("locale", "").split("-")[0]:
                    raise ValueError("Demonstration cannot translate source language")
        concepts.append(concept)
    return concepts


def _document(state, concept, generated_at):
    claims = [state["claims"][cid] for cid in concept["claim_ids"]]
    ids = sorted({e["source_id"] for claim in claims for e in claim["evidence"]})
    refs = {sid: "source-" + digest(sid)[:16] for sid in ids}
    meta = {key: concept[key] for key in ("type", "title", "description")}
    meta.update(status="draft", tags=concept.get("tags", []),
                generated={"by": "okf-knowledge-bundle-demonstration/" + __version__, "at": generated_at},
                sources=[{"id": refs[sid], "resource": state["sources"][sid]["citation_url"]} for sid in ids])
    if concept.get("resource"):
        meta["resource"] = concept["resource"]
    lines = ["# " + _markdown_label(concept["title"]), "", "> " + BANNER, ""]
    for claim in claims:
        refs_used = sorted({e["source_id"] for e in claim["evidence"]})
        lines += [claim["public_statement"], "", "".join("[^" + refs[sid] + "]" for sid in refs_used), ""]
    lines += ["[^" + refs[sid] + "]: " + state["sources"][sid]["citation_url"] for sid in ids]
    return ("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=1000)
            + "---\n" + "\n".join(lines) + "\n").encode("utf-8")


def _write_handoff(state, root, concepts):
    generated_at = utc_now()
    paths = {c["id"]: concept_path(c, state["config"]) for c in concepts}
    payload = {paths[c["id"]]: _document(state, c, generated_at) for c in concepts}
    sections = sorted({str(Path(p).parent).replace("\\", "/") for p in paths.values()})
    locales = sorted({s.split("/")[0] for s in sections})
    payload["index.md"] = ("---\nokf_version: '0.2'\n---\n# Knowledge bundle demonstration\n\n" + BANNER
                           + "\n\nSelected concepts only; whole-website coverage and current source availability are not established.\n\n"
                           + "\n".join("- [" + locale + "](" + locale + "/index.md)" for locale in locales)
                           + "\n\n[Markdown download](okf.tar.gz)\n").encode("utf-8")
    for locale in locales:
        links = ["- [" + s.split("/")[1] + "](" + s.split("/")[1] + "/index.md)" for s in sections if s.startswith(locale + "/")]
        payload[locale + "/index.md"] = ("# Demonstration language scope\n\n" + BANNER + "\n\n" + "\n".join(links) + "\n").encode("utf-8")
    for section in sections:
        links = ["- [" + _markdown_label(c["title"]) + "](" + Path(paths[c["id"]]).name + ")"
                 for c in concepts if str(Path(paths[c["id"]]).parent).replace("\\", "/") == section]
        payload[section + "/index.md"] = ("# Demonstration concepts\n\n" + BANNER + "\n\n" + "\n".join(links) + "\n").encode("utf-8")
    payload["log.md"] = ("# Bundle change log\n\n## " + generated_at[:10] + "\n\n"
                         "Created an unpublished demonstration using the selected draft wording. "
                         "Approvals are hypothetical. No human verification or new source check was recorded.\n").encode("utf-8")
    payload["okf.tar.gz"] = _tar_bytes(payload)
    for name, content in payload.items():
        target = root / "okf" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    report = validate_bundle(root / "okf", allowed_domains=state["config"].get("domains"))
    if (not report["core"]["conformant"] or len(report["issues"]) != len(concepts)
            or any(i["rule"] != "profile.lifecycle" for i in report["issues"])):
        raise ValueError("Demonstration failed scoped format/link/privacy validation", report)
    docs = {
        "START-HERE.md": "# UNPUBLISHED DEMONSTRATION\n\n" + BANNER + "\n\nOnly okf/ is a proposed public payload. "
        "The files beside it are private. See DEPLOYMENT.md and ASSUMPTIONS.md. "
        "Concept files are Markdown records, not HTML copies of the website. Source markers are citation footnotes. "
        "generated.by names the demonstration software, not a client or human verifier. "
        "Core format and archive integrity pass; production readiness remains false because all concepts are draft.\n",
        "ASSUMPTIONS.md": "# Hypothetical approvals only\n\nAnalyst wording/evidence review, client business confirmation, "
        "source availability/selected-scope acceptance and release-owner approval are assumed solely to illustrate the handoff. "
        "This command records none of them and does not create a release candidate. "
        "Existing decisions are not changed; known unresolved/excluded claims are refused. "
        "Unknown current availability remains unknown. No verified events or freshness dates are invented. "
        "A real release must be rebuilt through the normal readiness/candidate/approval/package workflow.\n",
        "DEPLOYMENT.md": "# Developer instructions - demonstration only\n\n" + BANNER
        + "\n\nFor a separately approved production release, mount only okf/ at the approved base path `"
        + state["config"].get("public_base_path", "/okf/") + "`; avoid duplicate okf/ nesting. "
        "Keep START-HERE.md, DEPLOYMENT.md, ASSUMPTIONS.md, MANIFEST.json, state, workbooks and provider results private. "
        "Closed CMSs require a developer-verified static route/storage/proxy plan. "
        "Serve Markdown as text/markdown; charset=utf-8 and okf.tar.gz as application/gzip. "
        "The archive is already gzipped; do not add Content-Encoding: gzip to the unchanged archive or double-gzip it. "
        "Preserve exact paths, bytes, relative links and source citations; no app-shell or login rewrites.\n\n"
        "Apply only the real release's approved indexing/consumer-access plan; preserve crawler controls. "
        "Website entry links, HTML landing pages, sitemap, llms.txt or CORS changes need a reviewed implementation decision. "
        "After separately authorized publication verify the exact file set, statuses, content types, hashes, links and tarball. "
        "Replace complete releases, remove specifically retired paths and purge approved caches. "
        "Keep the complete previous release privately and restore it as a whole if checks fail. "
        + first_release_rollback() + "\n\nDo not remove demo banners or change draft to stable by hand. "
        "Successful hosting does not establish indexing, AI citations or SEO gains.\n"
    }
    for name, content in docs.items():
        (root / name).write_text(content, encoding="utf-8", newline="\n")
    manifest = {"manifest_kind": "UNPUBLISHED_DEMONSTRATION_NOT_A_RELEASE", "tool_version": __version__,
                "okf_version": "0.2", "spec_commit": SPEC_COMMIT, "generated_at": generated_at,
                "assumed_approvals": True, "actual_approvals_created": False, "production_ready": False,
                "publication_authorized": False, "selected_concepts": sorted(paths),
                "statements": len({cid for c in concepts for cid in c["claim_ids"]}),
                "payload": {p: digest(b) for p, b in sorted(payload.items())},
                "private_handoff_files": {p: digest((root / p).read_bytes()) for p in docs},
                "expected_draft_findings": len(concepts)}
    (root / "MANIFEST.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    with tarfile.open(fileobj=io.BytesIO(payload["okf.tar.gz"]), mode="r:gz") as tar:
        members = tar.getmembers()
        if ({m.name for m in members} != {p for p in payload if p.endswith(".md")}
                or len(members) != len(payload) - 1
                or any(not m.isfile() or tar.extractfile(m).read() != payload[m.name] for m in members)):
            raise ValueError("Demonstration archive parity failed")
    return manifest


def build_demonstration(state, destination, concept_ids):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Demonstration destination already exists; preserve prior outputs")
    if not state["config"].get("domains"):
        raise ValueError("Demonstration requires an explicit domain policy")
    if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*", state["config"].get("public_base_path", "/okf/")):
        raise ValueError("Unsafe demonstration public base path")
    before = digest(state)
    concepts = _selected(state, concept_ids)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="demo-staging-", dir=destination.parent))
    try:
        root = staging / "Developer-Handoff-DEMO"
        root.mkdir()
        manifest = _write_handoff(state, root, concepts)
        with zipfile.ZipFile(staging / "Developer-Handoff-DEMO.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    z.write(p, "Developer-Handoff-DEMO/" + p.relative_to(root).as_posix())
        with zipfile.ZipFile(staging / "Developer-Handoff-DEMO.zip") as z:
            expected = {"Developer-Handoff-DEMO/" + p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            if z.testzip() or len(z.namelist()) != len(expected) or set(z.namelist()) != set(expected) or any(z.read(p) != b for p, b in expected.items()):
                raise ValueError("Demonstration ZIP parity failed")
        if digest(state) != before:
            raise ValueError("Demonstration must not change run state")
        staging.rename(destination)
    finally:
        if staging.exists():
            if staging.is_symlink() or staging.resolve().parent != destination.parent or not staging.name.startswith("demo-staging-"):
                raise ValueError("Refusing demonstration cleanup outside its staging directory")
            shutil.rmtree(staging)
    return {"status": "unpublished-demonstration", "production_ready": False,
            "concepts": len(concepts), "statements": manifest["statements"],
            "handoff": str(destination / "Developer-Handoff-DEMO"),
            "zip": str(destination / "Developer-Handoff-DEMO.zip"),
            "manifest_hash": digest(manifest), "expected_draft_findings": len(concepts)}
