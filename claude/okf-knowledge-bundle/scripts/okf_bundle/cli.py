"""Assistant-operated command interface. Analysts use chat and ordinary files."""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from .common import atomic_json, digest, parse_time, public_url, utc_now, normalize_url
from .state import StateStore, new_state
from .review import import_drafts, apply_review, accept_availability, reviewer_event, approve_alias
from .workbook import export_review, import_review
from .workflow import merge_import, packets, reports


def _wall_now():
    """Do not round a just-received fractional human approval into the future."""
    return datetime.now(timezone.utc).isoformat()


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def parser():
    root = argparse.ArgumentParser(description="Reviewed OKF website bundles — no automatic model execution or deployment")
    sub = root.add_subparsers(dest="command", required=True)
    def command(name, run=True):
        item = sub.add_parser(name)
        if run:
            item.add_argument("--run", required=True)
        return item
    p = command("init"); p.add_argument("--config", required=True)
    p = command("import-sf"); p.add_argument("--zip", required=True); p.add_argument("--csv")
    p = command("discover-csv"); p.add_argument("--csv", required=True); p.add_argument("--column")
    p = command("discover-sitemap"); p.add_argument("--sitemap", required=True)
    p = command("collect"); p.add_argument("--refresh", action="store_true"); p.add_argument("--max-pages", type=int, default=500); p.add_argument("--max-attempts", type=int, default=1500)
    p = command("collect-direct"); p.add_argument("--max-pages", type=int, default=25); p.add_argument("--refresh", action="store_true")
    p = command("mcp-prepare"); p.add_argument("--ids", nargs="+", required=True); p.add_argument("--settings", required=True); p.add_argument("--reference", required=True)
    p = command("mcp-import"); p.add_argument("--receipt", required=True)
    p = command("collection-reconcile"); p.add_argument("--id", required=True); p.add_argument("--attempt-index", type=int, required=True); p.add_argument("--attempt-hash", required=True); p.add_argument("--expected-version", required=True); p.add_argument("--action", choices=["allow-retry", "close-without-retry"], required=True); p.add_argument("--reason", required=True); _review_args(p)
    p = command("packets"); p.add_argument("--max-chars", type=int, default=24000)
    p = command("draft-import"); p.add_argument("--draft", required=True)
    command("review-export")
    p = command("review-preview"); p.add_argument("--concept-ids", nargs="+", required=True); p.add_argument("--out", required=True)
    p = command("review-summary", False); p.add_argument("--file", required=True); p.add_argument("--expected-hash")
    p = command("review-batch"); p.add_argument("--preview", required=True); p.add_argument("--preview-hash", required=True); _review_args(p)
    p = command("bundle-select"); p.add_argument("--preview", required=True); p.add_argument("--preview-hash", required=True)
    p = command("review-import"); p.add_argument("--file", required=True); p.add_argument("--client", action="store_true"); _review_args(p)
    p = command("review"); p.add_argument("--entity", choices=["claim", "concept"], required=True); p.add_argument("--id", required=True); p.add_argument("--revision", required=True); p.add_argument("--dimension", required=True); p.add_argument("--decision", required=True); p.add_argument("--reason"); _review_args(p)
    p = command("source-review"); p.add_argument("--id", required=True); p.add_argument("--url", required=True); p.add_argument("--scope", choices=["included", "excluded", "pending"], required=True); p.add_argument("--reason", required=True); p.add_argument("--accept-comparison", action="store_true"); _review_args(p)
    p = command("availability-accept"); p.add_argument("--ids", nargs="+", required=True); p.add_argument("--reason", required=True); _review_args(p)
    p = command("availability-check"); p.add_argument("--ids", nargs="+", required=True); p.add_argument("--authorized", action="store_true")
    p = command("owner-set"); p.add_argument("--owner", required=True); _review_args(p)
    p = command("alias-approve"); p.add_argument("--source-id", required=True); p.add_argument("--target-id", required=True); p.add_argument("--reason", required=True); _review_args(p)
    command("readiness")
    command("status")
    p = command("candidate"); p.add_argument("--release-id", required=True); p.add_argument("--as-of", default=None); p.add_argument("--previous-manifest"); p.add_argument("--previous-candidate"); p.add_argument("--partial", action="store_true")
    p = command("release-approve"); p.add_argument("--candidate", required=True); p.add_argument("--manifest-hash", required=True); _review_args(p)
    p = command("package"); p.add_argument("--candidate", required=True); p.add_argument("--out", required=True)
    p = command("demonstration"); p.add_argument("--concept-ids", nargs="+", required=True); p.add_argument("--out", required=True)
    p = command("historical-rebuild", False); p.add_argument("--candidate", required=True); p.add_argument("--out", required=True)
    p = command("validate", False); p.add_argument("--bundle", required=True)
    policy = p.add_mutually_exclusive_group()
    policy.add_argument("--run", dest="policy_run", help="Use this run's approved domain policy")
    policy.add_argument("--domains", nargs="+", help="Approved exact public domains for this validation")
    p = command("diff", False); p.add_argument("--old", required=True); p.add_argument("--new", required=True)
    p = command("verify-publication", False); p.add_argument("--manifest", required=True); p.add_argument("--base-url", required=True); p.add_argument("--authorized", action="store_true")
    p = command("recover-lock"); p.add_argument("--reason", required=True)
    return root


def _review_args(item):
    item.add_argument("--by", required=True, help="Actual human reviewer role ID, never an invented approval")
    item.add_argument("--at", required=True, help="Actual received-at offset-bearing timestamp")
    item.add_argument("--channel", required=True)
    item.add_argument("--reference", required=True)


def _validation_domains(args):
    from .validation import public_url_issue
    domains = StateStore(args.policy_run).load()["config"].get("domains") if args.policy_run else args.domains
    if domains is None:
        return None
    if not isinstance(domains, list) or not domains:
        raise ValueError("Validation needs a nonempty approved domain list")
    normalized = []
    for domain in domains:
        if not isinstance(domain, str) or not domain.strip():
            raise ValueError("Approved domains must be nonempty strings")
        url = normalize_url(domain if "://" in domain else "https://" + domain)
        parsed = urlsplit(url)
        if not parsed.hostname or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in parsed.hostname.split(".")):
            raise ValueError("Approved domains must be exact hostnames; wildcards are unsupported")
        if parsed.path != "/" or parsed.query or parsed.fragment or parsed.port is not None or public_url_issue(url):
            raise ValueError("Approved domains must name exact public hosts without paths, queries or ports")
        normalized.append(parsed.hostname)
    return sorted(set(normalized))


def execute(args):
    from . import export, validation
    cmd = args.command
    if cmd == "review-summary":
        from .summary import check_summary
        return check_summary(args.file, args.expected_hash)
    if cmd == "validate":
        domains = _validation_domains(args)
        report = validation.validate_bundle(args.bundle, allowed_domains=domains)
        report["domain_scope"] = {"checked": domains is not None, "source": "run" if args.policy_run else "arguments" if domains is not None else "not_supplied", "domains": domains or []}
        if domains is None:
            report["profile"]["ready"] = False
            report["issues"].append({"rule": "profile.domain_scope.unchecked", "entity": ".", "message": "Approved domains were not supplied; use --run or --domains before treating this as publication-ready", "severity": "error"})
        return report
    if cmd == "diff":
        return export.manifest_diff(_json(args.old), _json(args.new))
    if cmd == "historical-rebuild":
        return export.rebuild_historical(args.candidate, args.out)
    if cmd == "verify-publication":
        if not args.authorized:
            raise ValueError("Read-only live verification requires the exact target's authorization")
        from .network import direct_fetch
        from .publication import verify_publication
        manifest = _json(args.manifest)
        config = {"domains": manifest["deployment"]["domains"], "collection_authorized": True}
        def fetch(url, headers):
            return direct_fetch(url, config, max_bytes=250 * 1024 * 1024, headers=headers,
                                decode_content_encoding=not bool(headers), follow_redirects=False)
        return verify_publication(manifest, args.base_url, fetch)
    store = StateStore(args.run)
    if cmd == "review-preview":
        from .batch_review import review_preview
        from .preview_output import write_preview
        preview = review_preview(store.load(), args.concept_ids)
        return write_preview(preview, args.out, store.private)
    if cmd == "demonstration":
        from .demonstration import build_demonstration
        destination = Path(args.out).resolve()
        if destination == store.root or store.root.is_relative_to(destination) or destination.is_relative_to(store.private):
            raise ValueError("Demonstration destination must not contain the run or be inside private state")
        return build_demonstration(store.load(), destination, args.concept_ids)
    if cmd == "recover-lock":
        store.recover_lock(args.reason)
        return {"recovered": True}
    if cmd in ("status", "readiness", "packets", "review-export"):
        state = store.load()
        out = store.root / "outputs"
        if cmd == "status":
            from .network import awaiting_retry_results, pending_attempts
            report = reports(state, out)
            report["unresolved_collection_attempts"] = [descriptor for source in state["sources"].values() for descriptor in pending_attempts(source)]
            report["awaiting_retry_results"] = [descriptor for source in state["sources"].values() for descriptor in awaiting_retry_results(source)]
            return report
        if cmd == "readiness":
            report = export.eligibility(state, _wall_now())
            atomic_json(out / "validation.json", report)
            (out / "validation.md").write_text("# Publication readiness\n\n" + json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return report
        if cmd == "packets":
            return packets(state, store.private / "model-packets", args.max_chars)
        return export_review(state, out / "review.xlsx", out / "client-confirmation.xlsx")
    with store.transaction():
        if cmd == "init":
            if (store.private / "current.json").exists():
                raise ValueError("Run already exists; no reset or previous release overwrite")
            state = new_state(_json(args.config))
            result = {"run_id": state["run_id"], "run_root": str(store.root)}
        else:
            state = store.load()
            if cmd in ("import-sf", "discover-csv", "discover-sitemap"):
                from .ingest import ingest_sf, discover_csv, discover_sitemap
                if cmd == "import-sf":
                    imported = ingest_sf(args.zip, state["config"], args.csv)
                elif cmd == "discover-csv":
                    imported = discover_csv(args.csv, state["config"], args.column)
                else:
                    imported = discover_sitemap(args.sitemap, state["config"], state=state)
                result = merge_import(state, imported)
                if cmd == "discover-sitemap":
                    result["sitemap_summary"] = imported["sitemap_summary"]
            elif cmd == "collect":
                from .network import collect
                result = collect(state, refresh=args.refresh, max_pages=args.max_pages, max_attempts=args.max_attempts, checkpoint=store.save, incremental_checkpoint=store.checkpoint)
                result = {"sources": len(state["sources"]), "collection": state["collection"]}
            elif cmd == "collect-direct":
                from .managed import collect_direct
                result = collect_direct(state, max_pages=args.max_pages, refresh=args.refresh, checkpoint=store.save)
            elif cmd == "mcp-prepare":
                from .managed import prepare_mcp
                result = prepare_mcp(state, args.ids, _json(args.settings), args.reference)
                atomic_json(store.root / "private-state" / "mcp-requests" / (result["id"] + ".json"), result)
            elif cmd == "mcp-import":
                from .managed import import_mcp
                result = import_mcp(state, _json(args.receipt), Path(args.receipt).resolve().parent)
            elif cmd == "collection-reconcile":
                from .network import reconcile_attempt
                result = reconcile_attempt(state, args.id, args.attempt_index, args.action, args.reason,
                                           args.by, args.at, args.channel, args.reference,
                                           expected_attempt_hash=args.attempt_hash,
                                           expected_version=None if args.expected_version == "none" else args.expected_version)
            elif cmd == "draft-import":
                result = import_drafts(state, _json(args.draft))
            elif cmd == "review-batch":
                from .batch_review import approve_preview
                result = approve_preview(state, _json(args.preview), args.preview_hash,
                                         args.by, args.at, args.channel, args.reference)
            elif cmd == "bundle-select":
                from .batch_review import bind_approved_bundle
                result = bind_approved_bundle(state, _json(args.preview), args.preview_hash)
            elif cmd == "review-import":
                result = import_review(state, args.file, args.by, args.at, args.channel, args.reference, args.client)
            elif cmd == "review":
                apply_review(state, args.entity, args.id, args.revision, {args.dimension: args.decision}, args.by, args.at, args.channel, args.reference, reason=args.reason)
                result = {"review_recorded": True}
            elif cmd == "source-review":
                source = state["sources"][args.id]
                if not public_url(args.url, state["config"]["domains"]):
                    raise ValueError("Identity confirmation needs an approved public URL")
                confirmed_url = normalize_url(args.url)
                old_url = source.get("confirmed_identity_url") or source.get("citation_url")
                if source.get("identity_confirmed") and old_url and normalize_url(old_url) != confirmed_url:
                    raise ValueError("An already confirmed captured citation cannot be relabelled; retain the replacement URL capture and redraft its evidence")
                identity_changed = not source.get("identity_confirmed") or not old_url or normalize_url(old_url) != confirmed_url
                event = reviewer_event(args.by, args.at, args.channel, args.reference, source.get("active_version") or "identity")
                if args.accept_comparison:
                    check = source.get("availability_check", {})
                    from .availability import markdown_comparison_required
                    comparable = check.get("quotes_supported") is True or markdown_comparison_required(source,check)
                    if (not check.get("comparison_inconclusive") or check.get("requires_source_refresh")
                            or check.get("redirected") or not comparable
                            or check.get("version_id") != source.get("active_version")
                            or not check.get("response_hash") or parse_time(args.at) < parse_time(check["at"])):
                        raise ValueError("No current eligible raw/rendered evidence comparison to accept")
                    if not args.reason.strip() or any(ord(char) < 32 for char in args.reason):
                        raise ValueError("Comparison acceptance needs the human's single-line reason")
                    source["availability_comparison_review"] = {**event, "decision": "accepted", "reason": args.reason,
                        "version_id": source["active_version"], "response_hash": check["response_hash"]}
                source.update(citation_url=confirmed_url, confirmed_identity_url=confirmed_url, identity_confirmed=True, identity_basis="analyst-confirmed", scope=args.scope, scope_reason=args.reason)
                source["inclusion_decision"] = {**event, "version_id": source.get("active_version"), "decision": args.scope, "reason": args.reason}
                if identity_changed:
                    source["availability_acceptance"] = None
                    affected = set()
                    for cid, claim in state["claims"].items():
                        if any(e["source_id"] == args.id for e in claim["evidence"]):
                            affected.add(cid)
                            claim.setdefault("review_history", []).extend(claim.get("reviews", []))
                            claim["reviews"] = []
                            claim.update(evidence_review="pending", publication="pending")
                            if claim["business_confirmation"] != "not_required":
                                claim["business_confirmation"] = "required"
                    for concept in state["concepts"].values():
                        if affected.intersection(concept["claim_ids"]):
                            concept["review"] = {"decision": "pending"}
                source.setdefault("review_history", []).append({**event, "reason": args.reason, "confirmed_url": args.url})
                for row in state["inventory"]:
                    if row.get("source_id") == args.id and row.get("state") != "duplicate":
                        row.update(state=args.scope, reason=args.reason)
                state["release_approvals"] = {}
                result = {"source_review_recorded": True, "source_id": args.id}
            elif cmd == "owner-set":
                from .owner_setup import set_publication_owner
                result = set_publication_owner(state, args.owner, args.by, args.at, args.channel, args.reference)
            elif cmd == "availability-accept":
                accept_availability(state, args.ids, args.by, args.at, args.reason, args.channel, args.reference)
                result = {"availability_mode": "offline-with-availability-acceptance", "sources": args.ids}
            elif cmd == "availability-check":
                if not args.authorized:
                    raise ValueError("Cited-URL availability checks require separate target/scope authorization")
                from .availability import check_availability
                result = check_availability(state, args.ids)
            elif cmd == "alias-approve":
                approve_alias(state, args.source_id, args.target_id, args.by, args.at, args.channel, args.reference, args.reason)
                result = {"alias_source_id": args.source_id, "target_source_id": args.target_id, "history_preserved": True}
            elif cmd == "candidate":
                from .common import safe_slug
                if args.release_id != safe_slug(args.release_id):
                    raise ValueError("Use a safe lowercase release ID")
                candidate = store.root / "outputs" / "candidates" / args.release_id
                manifest = export.build_candidate(state, candidate, args.release_id, args.as_of or utc_now(), previous_manifest=_json(args.previous_manifest) if args.previous_manifest else None, allow_partial=args.partial, previous_candidate=args.previous_candidate)
                key = export.manifest_hash(manifest)
                state["candidates"][key] = {"path": str(candidate.resolve()), "manifest_hash": key}
                result = {"candidate": str(candidate), "manifest_hash": key, "status": "awaiting-release-owner-approval"}
            elif cmd == "release-approve":
                manifest = export.load_manifest(args.candidate)
                key = export.manifest_hash(manifest)
                if key != args.manifest_hash or key not in state["candidates"]:
                    raise ValueError("Unknown or changed candidate manifest")
                if args.by != state["config"].get("publication_owner"):
                    raise ValueError("Release approval requires the designated client owner or recorded delegate")
                reviewer_event(args.by, args.at, args.channel, args.reference, key)
                if parse_time(args.at) < parse_time(manifest["release_as_of"]):
                    raise ValueError("Release approval cannot predate the frozen candidate")
                # The payload must still match its manifest before the decision is saved.
                export.verify_candidate(args.candidate)
                export.current_candidate_eligibility(state, manifest, _wall_now())
                state["release_approvals"][key] = {"owner": args.by, "at": args.at, "channel": args.channel, "reference": args.reference, "manifest_hash": key, "config_hash": state["config_hash"], "deployment_fingerprint": export.deployment_fingerprint(state)}
                result = {"approved_manifest_hash": key}
            elif cmd == "package":
                result = export.package_release(state, args.candidate, args.out, _wall_now())
            else:
                raise ValueError("Unsupported command")
        store.save(state)
        reports(state, store.root / "outputs")
        return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        result = execute(parser().parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        failed = isinstance(result, dict) and (result.get("accepted") is False or result.get("ready") is False or result.get("valid") is False or result.get("profile", {}).get("ready") is False or result.get("core", {}).get("conformant") is False)
        return 2 if failed else 0
    except (ValueError, RuntimeError, KeyError, OSError) as error:
        report = {"error": str(error), "status": "stopped-without-release"}
        if getattr(error, "report", None):
            report["details"] = error.report
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
