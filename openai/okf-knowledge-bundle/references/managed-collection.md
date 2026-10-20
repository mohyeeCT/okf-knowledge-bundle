# Account-connected collection

The analyst chooses SF ZIP, sitemap or URL CSV. Provider selection is separate. SF import stays offline. For online inputs use `collect-direct` where public HTML suffices; use an existing account-connected Firecrawl MCP for a necessary rendered fallback or an explicitly authorized comparison. Never ask analysts for API keys or `.env` files. A missing connection is a connection/setup issue, not a reason to request secrets in chat. Prefer a connection already working in the current host; Firecrawl supports account sign-in through its hosted MCP. The legacy `collect` command is the historical Jina adapter and is not the managed route.

## Direct collection

Remote sitemap discovery uses XML Accept headers and the same stable user agent as direct HTML collection. The built-in transport supplies these on the index, redirects and child sitemaps; no custom-header download or local-file substitution is needed. A failed target stays failed without automatic retry.

`python scripts/okf.py collect-direct --run RUN_ROOT --max-pages 25`

The helper needs `collection_authorized: true`, exact public domains, and local network permission. It reuses existing active SF/other captures unless `--refresh` is explicit. It checks robots, pins public DNS and verifies redirects. A blocked robots URL is held, not sent to another provider. Transport failure preserves prior evidence; an observed target failure or changed representation invalidates affected reviews. Thin/app-shell captures retain a reason for fallback. Login/challenge and known failed sources remain held for source review. Direct collection creates no provider requests or credits. Page ceilings are per invocation; the assistant must remain within the user's run-wide target scope.

An observed robots denial clears any prior shell-fallback eligibility and blocks managed dispatch even if an inclusion decision later changes the scope. A subsequent successful robots check is needed to clear that observation. Direct `X-Robots-Tag` is retained separately from HTML metadata; a noindex/none response needs explicit inclusion review. A changed HTTP robots restriction invalidates the previous inclusion decision and release approval even when the HTML bytes are unchanged.

## Host MCP sequence

1. Discover the actual connected tool and its complete schema. Verify account access with a read-only status call. Confirm target scope, request/credit limits, formats and any additional charges. Do not infer access from a configuration entry or logged-in browser. Do not add extraction/agent/search products to a scrape job. Token/byte estimates are advisory; rendered output size is not known exactly before collection.
2. Configure positive `managed_budget` values: `max_requests`, `max_credits`, `per_page_credit_reservation`, `max_response_bytes`. These are local dispatch/reservation controls, not a provider-enforced account cap or billing guarantee. Track actual job usage when reported; unknown usage keeps its reservation. Other account activity is not this run's cost.
3. Save settings and run:

   `python scripts/okf.py mcp-prepare --run RUN_ROOT --ids SOURCE_ID ... --settings SETTINGS.json --reference ACTUAL_AUTHORIZATION_REFERENCE`

   The command makes a guarded public robots GET per selected origin and refuses disallowed/unavailable policy before reserving anything. It needs the run's existing collection/network authorization, including for SF comparison sources. It then durably reserves each request before returning an MCP plan and exact arguments. Dispatch only after it succeeds. The plan is saved under `private-state/mcp-requests/`. Keep the state snapshot, attempt hashes and request hash. A second preparation refuses an unresolved request; no automatic retries occur. Reconciled/closed attempts cannot accept a late result, and close-without-retry prevents further dispatch.
4. The host assistant calls the account-connected MCP with the plan's exact arguments. The implemented Firecrawl batch contract supports a one-page canary and then a bounded explicit URL list. Default settings are Markdown, main-content filtering, `maxAge=0`, basic proxy, TLS verification and one concurrent page. The currently exposed batch tool supports the cache option even where a single-page wrapper does not. Verify capability on each platform. No cookies, API headers, scripts, webhooks or arbitrary browser actions are accepted by this plan.
5. Save the complete original tool JSON through the host's artifact facility. With Composio, request `sync_response_to_workbench=true` when retrieving large results, use the returned remote-file path, export that exact file and verify its SHA256 after download. This adds no scrape. Do not copy the inline preview, summarize into evidence, or retype HTML. If the host cannot persist the complete payload, stop/mark partial rather than claiming full coverage. The hash proves retained artifact integrity; it does not prove origin HTTP wire-byte identity.
6. Poll the existing job until complete; polling is not a new page submission. The importer refuses incomplete/missing-page batches or a remaining pagination cursor. Retrieve all pages through a supported scoped tool before import; never substitute a preview or infer missing documents. A provider failure preserves previous reviews and the pending reservation. Reconcile uncertainty explicitly through the existing attempt-bound operator workflow before any new submission; this version does not refund unknown reservations.
7. Create a receipt with exactly these fields and run `mcp-import`:

```json
{
  "request_id": "ID_FROM_PLAN",
  "request_hash": "HASH_FROM_PLAN",
  "artifact": "COMPLETE_SAVED_TOOL_RESULT.json",
  "sha256": "ACTUAL_ARTIFACT_SHA256",
  "completed_at": "ACTUAL_OFFSET_BEARING_TIME",
  "job_id": "OBSERVED_JOB_ID",
  "complete": true
}
```

   `python scripts/okf.py mcp-import --run RUN_ROOT --receipt RECEIPT.json`

   Relative artifact paths resolve against the receipt directory. The helper verifies the artifact hash, bounded response, exact reserved URL coverage, timestamps and unchanged unreconciled attempts before atomically importing. A completed artifact/job cannot satisfy another request in the same run. Job IDs and job-level created/completed timestamps are checked when present in the artifact; absent fields remain unverified, so retain the host submission/poll evidence separately. It reads known Composio batch envelopes; other host wrappers need a tested adapter. It records provider target-status assertions separately from direct origin observations. Reported job credits replace only that request's conservative credit reservation; missing usage stays reserved. `status` still exposes unresolved collection attempts. Import never fabricates evidence/business/publication approval.

Credit telemetry must be finite, nonnegative numeric data. NaN, infinity, booleans and malformed values are refused atomically with reservations retained. Invalid credit values already recorded by an earlier version block further managed dispatch until explicitly reconciled; they are never interpreted as zero or automatically refunded.

## Representation and freshness

Use complete Markdown as lean body evidence; the importer retains its exact text rather than flattening tables or removing qualifiers. Recognized page metadata fields are separately quotable as provider assertions; status, credits, proxy and scrape IDs remain private operational observations. The current `markdown-verbatim-v2` contract does not contain JSON-LD by inference. Older v1 captures remain readable but need controlled reimport/refresh and new review before release. Full HTML formats are extracted offline into body/metadata/schema evidence. Every representation has its own version and settings. When Markdown and HTML are requested together, Markdown is primary and HTML versions are retained for comparison. Source restrictions from all formats remain review signals; a body-only change to an inactive sidecar does not invalidate an unchanged primary. A claim referring to an inactive HTML sidecar cannot pass current-source readiness. For HTML/schema claims select HTML as primary by preparing an HTML-only collection; do not bypass the current-version review contract.

`maxAge=0` records a request for fresh provider retrieval. It does not establish the origin's historical capture time or direct availability. Store observed cache metadata when supplied; missing cache fields stay unknown. Current direct availability and publication-owner acceptance are separate existing decisions. Keep all ordinary freshness and risk reviews, including client confirmation of statistics, course availability and credentials.

Main-content filtering can omit sitewide facts; duplicated carousel blocks are flagged without rewriting raw evidence. Summaries, question-selected passages and silently truncated outputs cannot establish full-page coverage. Embedded video without a captured transcript supports only observed listing/embed facts. A rendering counter matching raw attributes is still a site assertion, not an independently verified business fact.

Markdown-only captures with an unreported canonical need a recorded human inclusion decision for unknown HTML indexing signals. A direct shell for a rendered capture leaves current availability unknown; it does not establish a failed/nonpublic page. Actual target failures and login/challenge responses stay blocking. A complete batch with an identifiable per-page destination mismatch retains that page for identity review and imports valid peers; incomplete/ambiguous coverage or malformed formats still refuse import. Keep the complete paid artifact and never re-request automatically.

Use a separate run for a provider comparison. A collection in an existing run deliberately changes its primary evidence and invalidates old-version reviews. The separate comparison run preserves the reviewed baseline and costs only the explicitly authorized comparison requests.

Provider credits, Jina billing units, response bytes and host chat context are separate measurements. Never convert between them by naming them all "tokens". Full HTML is normally kept outside model context; send the host model bounded extracted packets. A new provider or format is tested with a small explicit sample before a sitewide batch. No source collection result authorizes client publication or skill installation.

Primary references: [Firecrawl MCP](https://docs.firecrawl.dev/mcp-server), [Firecrawl scrape formats and status layers](https://docs.firecrawl.dev/features/scrape).
