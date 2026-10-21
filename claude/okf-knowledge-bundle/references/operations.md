# Assistant operations

These commands are for the host assistant/developer. Analysts upload ordinary files and review a short summary with an exact-wording preview; spreadsheets are optional; they do not edit the configuration or invoke a terminal. Use Python 3.12+ and `requirements.txt`. Dependencies are local processing libraries, not services. Installing a portable skill into Claude/Codex is separate from this source build.

Run `python scripts/okf.py --help` from the skill folder. Replace `python` with the verified runtime's absolute path when necessary. Save assistant-created configuration and model drafts under the run's private workspace. Paths below are placeholders, not literal client identities or approvals.

## Configuration and initialization

Create a JSON configuration reflecting actual analyst scope and authorization:

```json
{
  "site_name": "Client name",
  "domains": ["example.com"],
  "locale": "en",
  "business_profiles": ["b2b", "services"],
  "model": "actual-host-model",
  "effort": "medium",
  "publication_owner": "human:client-owner-role",
  "public_base_path": "/okf/",
  "indexing": "noindex-crawlable",
  "availability_mode": "offline",
  "confirmation_policy": "critical-only",
  "collection_authorized": false,
  "managed_budget": {"max_requests": 500, "max_credits": 1000, "per_page_credit_reservation": 2, "max_response_bytes": 16777216}
}
```

Exact approved hosts are required; apex and www are separate. Domain aliases are not inferred from canonical declarations. The configuration is private; never include credentials. Before enabling `collection_authorized`, record the real target/scope/budget authorization. `max_pages` in the configuration is a run-wide authorized ceiling; the collect command is additionally bounded per batch. Unknown provider usage consumes a conservative reservation, not a measured token total. An account-level budget remains necessary because providers may meter requests differently.

Approved client reviewers can be listed in `business_reviewers`; absent that list, only `publication_owner` may confirm business applicability. Agency evidence review cannot impersonate that decision. For a volatile claim, configure `claim_maintenance[CLAIM_ID]` with `owner`, `method`, `reason`, `revision` and positive `interval_days` before review. The exact claim revision must match. Fixed-price claims also require currency and offer-context qualifiers. The earliest applicable review/maintenance/expiry deadline controls freshness.

```text
python scripts/okf.py init --run RUN_ROOT --config PRIVATE_CONFIG.json
python scripts/okf.py import-sf --run RUN_ROOT --zip EXPORT.zip
python scripts/okf.py import-sf --run RUN_ROOT --zip EXPORT.zip --csv NORMAL_INTERNAL_HTML.csv
python scripts/okf.py discover-csv --run RUN_ROOT --csv URLS.csv
python scripts/okf.py discover-sitemap --run RUN_ROOT --sitemap SITEMAP.xml.gz
python scripts/okf.py discover-sitemap --run RUN_ROOT --sitemap https://example.com/sitemap.xml
python scripts/okf.py status --run RUN_ROOT
```

CSV recognizes ordinary URL/Address/URLs and Search Console Top pages headers, including BOM/delimiter variants. Multiple plausible recognized columns still require explicit selection; no content-based column guessing occurs. Remote sitemap discovery also needs authorized direct network access. Uploaded sitemap references are bounded and may not escape the supplied local directory. For ambiguous CSV columns, inspect the reported candidates and supply the analyst's selection through `--column`; do not rename the file's columns. Companion SF CSV addresses alone do not prove file mapping; unverified joins remain pending.

## Describing source observations

- An uploaded SF HTML capture supports "the uploaded export shows." Use a saved capture's actual observation date where known; its import date is not proof of capture freshness.
- A sitemap or CSV is URL discovery, not page-content verification. After an authorized content fetch and examination, describe that specific page as "checked on [actual date]."
- A citation availability check establishes its reported availability only. It does not make old quoted content current.
- For mixed or resumed runs, attribute each finding to its actual uploaded, historical or newly fetched evidence. A missing statement in a limited capture is not proof that it was removed from the website.

Keep these distinctions in the short chat review without adding another analyst question or exceeding 150 words. They guide assistant wording; the word-count checker does not prove temporal accuracy.

## Current managed collection

Read [managed-collection.md](managed-collection.md) before online collection. The current workflow uses guarded public direct HTML and the existing account-connected Firecrawl MCP when rendering is needed. Analysts never supply API keys or environment files. The host assistant reserves MCP requests, calls the discovered tool, saves the exact complete result, and imports its receipt; local scripts do not automatically invoke host connectors. No separate model API is used. Review/availability/publication gates below still apply.

```text
python scripts/okf.py collect-direct --run RUN_ROOT --max-pages 25
python scripts/okf.py mcp-prepare --run RUN_ROOT --ids SOURCE_ID --settings PRIVATE_SETTINGS.json --reference ACTUAL_AUTHORIZATION_REFERENCE
python scripts/okf.py mcp-import --run RUN_ROOT --receipt PRIVATE_RECEIPT.json
```

The historical `collect` command and following Reader-specific behavior are retained for old Jina runs and offline regressions, not new managed client collection. The new MCP workflow has no automatic retries. Its unknown credits remain reserved; it does not refund legacy Jina reservations. A failed fetch is not proof a service has ceased. Settings/locale changes do not silently reuse incompatible captures. Upstream provider redirect/DNS hops are unobservable. Direct fetches pin public DNS and check each redirect. Explicit source changes or target failure still invalidate affected reviews.

Before each request, the attempt and conservative token reservation are committed durably. An interrupted request retains unknown outcome/usage; do not refund or retry it automatically. Each response is a separate result commit. Any unreconciled pending attempt blocks release, including old runs with a later successful attempt. Provider account/access denials (401/402/403) stop the batch after one request and preserve prior source reviews; transient provider failures retain the bounded retry policy above. The adapter retains status/headers before processing a response and deliberately does not read any non-2xx error body; an error-body timeout or byte cap cannot erase the known status. Missing error-body usage remains conservatively reserved. Actual target failures or changed evidence invalidate affected reviews. Missing credentials and invalid local settings fail before reserving budget. Operational token/byte caps remain recorded exactly but do not change an otherwise unchanged evidence version or its reuse identity. Reader target-error warnings fail closed; other warnings quarantine evidence for explicit review. Live provider fields still require pilot verification.

After proving a crashed writer is inactive and recovering its lock, call `status` to inspect `unresolved_collection_attempts`. The descriptors contain the source ID, zero-based attempt index, exact attempt hash and current version. An actual designated collection reviewer (configuration `collection_reviewers`, defaulting to the publication owner) records either a new budgeted retry authorization or closure without another request:

```text
python scripts/okf.py status --run RUN_ROOT
python scripts/okf.py collection-reconcile --run RUN_ROOT --id SOURCE_ID --attempt-index ATTEMPT_INDEX --attempt-hash EXACT_ATTEMPT_HASH --expected-version CURRENT_VERSION_OR_none --action allow-retry --reason ACTUAL_RECONCILIATION_REASON --by human:actual-authorized-role --at ACTUAL_RECEIVED_ISO_TIME --channel operator-message --reference ACTUAL_MESSAGE_REFERENCE
```

`--expected-version none` means no active capture. Use `--action close-without-retry` when no further submission should occur in this run. Reconciliation binds the exact attempt and current source version; stale hashes/versions are refused. It never creates a successful capture, changes the unknown request's outcome, refunds its reservation or expands the agreed budget. A permitted new attempt is budgeted normally and a further interruption is quarantined again. `status` also lists `awaiting_retry_results`; release stays blocked after `allow-retry` until a new request following that decision has a committed result. A provider-error result preserves prior evidence per the failure policy, while all ordinary availability/release checks still apply. Closure keeps collection disabled for that source in this run; a previously retained capture still needs all normal evidence/availability/release checks. Do not invent an operator decision or mutate snapshots to bypass quarantine.

## Compact review (default)

Prepare a preview of the exact selected, supported concepts. Give a separate short issues summary in chat and link the clean proposed-content page; critical unresolved facts must be resolved or omitted first. Keep prior known conflicts excluded until actually reconciled, even when a smaller new capture lacks the competing page. Record a single explicit content approval for that preview through the batch helper. It does not confirm business conflicts or authorize publication.

```text
python scripts/okf.py review-preview --run RUN_ROOT --concept-ids SELECTED_ID ... --out PRIVATE_PREVIEW.json
python scripts/okf.py review-batch --run RUN_ROOT --preview PRIVATE_PREVIEW.json --preview-hash EXACT_PREVIEW_HASH --by human:actual-analyst --at ACTUAL_RECEIVED_ISO_TIME --channel chat --reference ACTUAL_APPROVAL_MESSAGE
```

The command returns `content_preview` (PRIVATE_PREVIEW.content.html) and `audit_details` (PRIVATE_PREVIEW.audit.md), plus the unchanged private JSON contract/hash. Send the content page to the analyst: titles, descriptions, exact statements and per-statement source links. It contains no evidence quotations, IDs or review-state machinery; secondary topic metadata is collapsed. Keep the evidence/audit file available separately, not another required task. Do not hand-write another renderer or put audit details back into a dashboard. The short chat summary holds the critical questions/exclusions; a single approval of the clean preview is still sufficient. Keep meaningful factual qualifications in the public wording itself. Never ask analysts to edit JSON. Batch acceptance is atomic, refuses pending critical facts and binds the current source/revision set. New runs use `critical-only`; `all-risk` remains an explicit optional stricter profile and the default for legacy configurations with no policy. Do not change a saved run policy merely to bypass its earlier decisions.

`review-batch` also binds the exact selected topic/statement revisions to that actual content-batch decision. Native readiness, candidate and packaging use this selection in the existing run. Excluded records remain private, unchanged and unresolved; they do not require a scoped run, owner partial-release acceptance or another content approval. The manifest lists withheld topic IDs. Its `partial` flag describes withheld inventory; it is not a new approval or a requirement to pass `--partial` for a fully ready approved selection. Selected failures still block the whole selection.

For a pre-0.2.13 batch, bind its existing exact approved preview after upgrading, without recording another human decision:

```text
python scripts/okf.py bundle-select --run RUN_ROOT --preview ORIGINAL_APPROVED_PREVIEW.json --preview-hash ORIGINAL_APPROVED_HASH
```

This refuses unapproved, edited, changed-revision or superseded selections and missing original review records. Initial owner setup or renewed availability alone does not require another content approval. Normal freshness and source gates still apply. Runs without an explicit selection retain legacy whole-run readiness; installing does not rewrite their state or old candidates. A newly bound selection changes the candidate fingerprint, so generate a new candidate and obtain its actual release approval later.

Generate the unpublished candidate folder after content approval when other readiness requirements are satisfied. It already contains `okf/`, `release-manifest.json` and `DEPLOYMENT.md`. Do not demand deployment or client release approval simply to show that candidate; obtain owner approval before production packaging/publication. If actual review or availability is still pending, a user-requested demonstration can show draft output without inventing approvals.

### Validate the final outgoing review

Write the complete final assistant review response to a private UTF-8 Markdown file, including any introduction, headings, questions, link labels, approval request and unpublished notice. Run:

```text
python scripts/okf.py review-summary --file PRIVATE_FINAL_REVIEW.md
python scripts/okf.py review-summary --file PRIVATE_FINAL_REVIEW.md --expected-hash PREVIOUS_SHA256
```

The first command returns `valid`, the conservative word count, SHA256 and exact `text`. Empty or over-150 summaries fail (CLI exit 2). Send only the returned text when valid; no extra preface, closing or second summary. Re-run after any edit. The optional hash refuses a changed file even if the new count still fits. This helper does not alter the run or approve anything. It counts whitespace tokens conservatively, excluding standalone Markdown punctuation while including every numeric token, ordered-list markers and link labels; link destinations/titles may overcount. Use ordinary prose and inline Markdown links, without HTML or code blocks. Proposed content, private evidence details and developer instructions are separate files. Only the issues summary is subject to the 150-word limit. A helper cannot intercept a host's final chat stream: the assistant must send the checked text verbatim.

## Drafts and reviews

```text
python scripts/okf.py packets --run RUN_ROOT --max-chars 24000
python scripts/okf.py draft-import --run RUN_ROOT --draft ASSISTANT_DRAFT.json
python scripts/okf.py review-export --run RUN_ROOT
python scripts/okf.py review-import --run RUN_ROOT --file RETURNED_REVIEW.xlsx --by human:actual-reviewer --at ACTUAL_RECEIVED_ISO_TIME --channel returned-workbook --reference ACTUAL_MESSAGE_REFERENCE
python scripts/okf.py review-import --run RUN_ROOT --file RETURNED_CLIENT.xlsx --client --by human:actual-client-role --at ACTUAL_RECEIVED_ISO_TIME --channel returned-workbook --reference ACTUAL_MESSAGE_REFERENCE
```

See [draft-contract.md](draft-contract.md) and [review-and-release.md](review-and-release.md). Chat decisions can be recorded with `review --entity claim|concept --id ID --revision REV --dimension DIMENSION --decision DECISION` and the same four review provenance options. For concepts, use dimension `decision`. All decisions must use an actual received time no later than the current time; future-dated entries are rejected before mutation. Never generate human decisions from model agreement. Corrected public text is a new draft, not an editable approval field.

Ambiguous source identity may be confirmed with `source-review --id SOURCE_ID --url CONFIRMED_CAPTURED_URL --scope included|excluded|pending --reason REASON` plus reviewer provenance. New identity confirmation resets affected reviews so attribution is checked against the confirmed identity. An already confirmed capture cannot be relabelled with a different citation destination. Retain the replacement page as a source and redraft against its actual evidence before substituting a canonical/final URL.

`alias-approve --source-id OLD_ID --target-id TARGET_ID --reason REASON` plus reviewer provenance explicitly groups confirmed equal-evidence captures under one source key. Both capture IDs, original keys, representations, citations and review history remain available. This conservative v1 operation requires identical active body evidence and locale, and refuses existing alias chains/cycles. Canonical equality, trailing-slash resemblance or www/apex spelling alone cannot trigger it. Divergent views remain distinct. Alias grouping does not silently replace a citation or skip unique source facts.

## Candidate, approval and handoff

Before the first candidate, obtain the actual final publication approver once if not already supplied. Record that designation in the current run:

```text
python scripts/okf.py owner-set --run RUN_ROOT --owner human:ACTUAL_OWNER_ROLE --by human:ACTUAL_DESIGNATING_REVIEWER --at ACTUAL_RECEIVED_ISO_TIME --channel chat --reference ACTUAL_DESIGNATION_MESSAGE
```

The assistant maps the supplied name/client role to an opaque role ID; analysts do not prepare commands. Initial `owner-set` updates only the owner configuration/fingerprint and adds a separate provenance record. Sources, claims, concepts, existing content reviews and run identity are preserved. The same owner is idempotent. It refuses replacement of another owner and refuses initial setup in a malformed ownerless run with existing candidates/release approvals; those exceptional changes still need a separate configuration migration. It never creates client confirmation, availability acceptance or release approval. An unrecorded preview from before configuration setup becomes stale and must be regenerated; already-recorded exact-content approvals remain valid without another approval request. Do not clone a normal run merely to set its initial owner.

Availability checks use the same HTML Accept/User-Agent settings as direct collection through the guarded transport. The sitemap/XML request defaults remain separate. No custom fetch adapter, automatic retry, provider switch or failure waiver is needed. A genuine denied/failed target remains blocked.

```text
python scripts/okf.py availability-check --run RUN_ROOT --ids SOURCE_ID OTHER_SOURCE_ID --authorized
python scripts/okf.py availability-accept --run RUN_ROOT --ids SOURCE_ID OTHER_SOURCE_ID --reason ACTUAL_LIMITATION_ACCEPTANCE --by human:actual-owner --at ACTUAL_RECEIVED_ISO_TIME --channel client-message --reference ACTUAL_MESSAGE_REFERENCE
python scripts/okf.py readiness --run RUN_ROOT
python scripts/okf.py candidate --run RUN_ROOT --release-id release-001
python scripts/okf.py release-approve --run RUN_ROOT --candidate CANDIDATE_DIRECTORY --manifest-hash REVIEWED_MANIFEST_HASH --by human:actual-owner --at ACTUAL_RECEIVED_ISO_TIME --channel client-message --reference ACTUAL_MESSAGE_REFERENCE
python scripts/okf.py package --run RUN_ROOT --candidate CANDIDATE_DIRECTORY --out CLIENT_DEVELOPER_HANDOFF.zip
```

The optional availability check requires separate authorization for that exact enumerated public URL set; `--authorized` is the caller's assertion, not permission by itself. It uses guarded direct HTTP, not paid Jina. It records current target status separately from old capture status, checks usable content and supporting passages, and requires current source collection/review if the body changed. An observed recovery can supersede an old captured failure; an owner waiver cannot. The alternative availability acceptance command covers unknown current availability only, at the exact cited URL/source version, and refuses known failed/nonpublic sources. `readiness` provides detailed blockers. A frozen candidate precedes owner approval. No overwrite is permitted; a new candidate uses a new release ID. The developer ZIP contains public `okf/` plus private instructions/manifest, never analyst state. Developers publish only `okf/`.

The producer profile checks destinations in inline links/images, reference definitions and autolinks, including GFM implicit `www.` links and email/protocol forms. The latter pass through the same destination policy; unsupported mailto/xmpp schemes are rejected rather than silently becoming public links. Unsupported or malformed destination syntax is blocked. Encoded relative filenames are conservatively rejected, and link-like syntax inside code examples is checked too; use ordinary readable prose and reviewed safe links. Core OKF conformance remains a separate, more permissive report. Generated source markers appear in their own paragraph so they cannot become part of a reviewed reference-definition URL.

For legacy whole-run readiness without an approved bundle selection, a partial candidate needs `--partial` plus an explicit configuration `partial_release_acceptance` with owner, received-at `at`, reason and exact eligible concept IDs. Alternative indexing and custom type/freshness/confirmation policies require recorded owner decisions before review. Except for actual initial owner setup through `owner-set`, configuration changes must go through a fresh run/review migration rather than manually editing a snapshot; preserve the original run and approvals.

## Unpublished demonstration handoffs

Only when requested, select exact concept IDs from the existing run and create a separate hypothetical handoff:

```text
python scripts/okf.py demonstration --run RUN_ROOT --concept-ids SELECTED_ID OTHER_SELECTED_ID --out NEW_DEMO_DIRECTORY
```

The destination must be new and outside private state. This is a read-only state operation; it creates `Developer-Handoff-DEMO/` and `Developer-Handoff-DEMO.zip` at the destination, never a production candidate or approval. The assistant selects the agreed concepts; analysts do not prepare IDs or commands. No new collection occurs. Explicitly rejected concepts, withheld/rejected/disputed/open-conflict claims and excluded, unidentified, failed, pending or stale-version sources are refused before delivery. Unknown current availability stays unknown. Do not remove a blocked claim silently; explain the scope and retain it in private questions.

The helper preserves selected public wording, descriptions and qualifiers, marks every concept `draft`, omits human `verified` events and freshness dates, and labels all assumptions. Core/domain/link/privacy checks must pass with only the expected draft lifecycle findings. Markdown-only tarball and developer ZIP parity are checked. `MANIFEST.json` is a private demonstration inventory, not a candidate manifest; the normal approval/package commands cannot accept it. The developer receives only the handoff ZIP, not analyst evidence. Future production uses the ordinary review/readiness/candidate/approval/package workflow.

The drafting guidance applies before importing either demonstration or production content: descriptions must bind each condition to its own option, and component/total durations must be compared across retained context. The exporter does not infer or rewrite business facts. Use “selected draft wording” for hypothetical logs; actual review language is reserved for genuinely reviewed production content.

Production and demonstration developer instructions cover both restoring a prior release and undoing a first publication. Rollback must preserve unrelated files that existed at the destination before the release.

## Diff, historical copy and live checks

```text
python scripts/okf.py diff --old OLD_MANIFEST.json --new NEW_MANIFEST.json
python scripts/okf.py candidate --run RUN_ROOT --release-id release-002 --previous-candidate OLD_CANDIDATE_DIRECTORY
python scripts/okf.py validate --bundle CANDIDATE_DIRECTORY/okf --run RUN_ROOT
python scripts/okf.py validate --bundle EXTRACTED_PUBLIC_FOLDER --domains example.com www.example.com
python scripts/okf.py historical-rebuild --candidate OLD_CANDIDATE --out NEW_HISTORICAL_DIRECTORY
python scripts/okf.py verify-publication --manifest RELEASE_MANIFEST.json --base-url https://example.com/okf/ --authorized
```

Standalone validation reports whether approved domains were supplied. Without `--run` or `--domains`, it can report core conformance and structural findings, but publication-profile readiness is false and the command exits nonzero. A successful scoped check does not verify the candidate manifest or replace human/release approval. Candidate and package stages continue to enforce exact byte and domain-policy bindings.

Live checks require separate authorization for that exact destination. `--authorized` records the caller's assertion; it is not authorization by itself. The verifier checks expected payload paths, tarball parity, entity hashes/content types, app-shell fallback, transport-gzip errors and enumerated retired paths. Robots/AI-consumer policy checks are a separately scoped manual part of the developer procedure. Reading files does not prove SEO/AI impact.

## State and recovery

`private-state/current.json` selects an immutable full snapshot or parent-linked collection delta. Small records are inlined; larger records are content-addressed. Collection commits changed sources, dependent claims, inventory rows and counters before every request and after every result, with full snapshots at batch/chain-cap boundaries. The pointer changes only after every referenced write completes. Hash checks detect corruption. Writes require an exclusive run lock; stale readers cannot overwrite a newer pointer. Workbooks and release files are not the state authority. Previous commits/releases are retained. A crashed writer may leave unreachable records; it cannot commit half an approval.

`recover-lock --run RUN_ROOT --reason REASON` checks that the same host's process is actually inactive and that a coherent snapshot survives. It refuses a live writer or unverifiable remote host. Windows process checks are read-only (not `os.kill(pid, 0)`). Do not delete locks manually or run Claude and Codex as simultaneous writers.

No automated scheduling, deployment, model execution or live consumer testing is included. Run the test suite through `python -B scripts/run_tests.py -v`; this runner sets the package import path. Bare unittest discovery additionally requires `PYTHONPATH=scripts`. Use a writable workspace TEMP/TMP when sandbox defaults are unsuitable. One fixture binds loopback HTTP only and may need sandbox permission for local sockets.

### Final producer-profile limits

Implicit www/email candidates are scanned conservatively in labels, reference uses and code; regex guesses never exempt a label. Literal HTTP(S) URLs are checked separately and can contain email-like paths. Entity/escape-created URL spellings do not hide implicit candidates. This can refuse an inactive email-like link label, filename or code example. Use descriptive labels and the ordinary HTTP(S) contact page. Bare emails and mailto/xmpp remain outside this HTTP(S)-only public profile, even for the client's domain; broader contact-email support is a deferred usability feature, not a pilot gate. Do not silently remove or alter a reviewed fact to pass validation; have the analyst approve the supported public wording/URL.

Malformed successful JSON/page envelope shape is a provider protocol failure and preserves prior reviewed evidence. A valid string containing empty/unusable/login/truncated/changed target content still follows the target/evidence failure rules. An explicit target-error warning is not neutralized by a missing HTML field. Before recording release approval, current candidate state, policy and eligibility are checked, as they are during packaging. Validation domains are exact public hostnames; wildcard domains are rejected.

## Generated handoff instructions

Candidate generation automatically writes a private, hash-bound DEPLOYMENT.md with a release hold and actual freshness limits. Reuse it instead of drafting a weaker README. Any additional notice must preserve the checked-live cutoff (configured hours, default 24), separate claim-review deadline, exact-manifest release approval and separate public/staging deployment authorization. After renewed source checks, build a new candidate and obtain approval of its new hash; do not overwrite the previous candidate or repeat still-valid content reviews. A requested review-only ZIP is transport, not a successful production package command.

### Availability pacing (0.2.10)

Use the native `availability-check --authorized` after authorization for the enumerated citations. Its guarded transport checks robots once per origin per batch, applies the stricter configured `direct_interval_seconds` (default 0.5), matching robots Crawl-delay (including fractions) and supported Request-rate, and paces metadata-to-page and redirect requests. Requests to different origins have separate clocks; saved last-request times also pace a later invocation in the same run. Do not use a custom fast GET loop. No special adapter or manual sleep is needed.

The output's `pacing` receipt records actual request start times, waits and effective intervals; robots status, body, hash and applicable timing remain in private state. A 404/410 robots response permits checks with configured pacing. Disallowed, denied, unavailable or HTML/challenge policy holds page checks without automatic metadata retries or paid fallback. A wait over 60 seconds is held for a later invocation, never capped into an early request. A failed new check replaces the prior live observation and clears unknown-availability acceptance; it does not erase unchanged content reviews. Old candidates remain immutable. A fake injected test transport is explicitly labeled as not evaluating production pacing. This was the scope of 0.2.10; 0.2.11 extends the same native guard to sitemap discovery and direct collection as described below. Managed provider dispatch remains separate.

### Shared direct pacing (0.2.11)

Use native `discover-sitemap`, `collect-direct` and authorized `availability-check` in the same run. They share saved `direct_request_times` and `direct_robots_checks` per origin, including clocks retained between commands, canaries and batches. Existing 0.2.10 availability timestamps/policies are read as a compatibility floor; installing does not rewrite historical evidence. Discovery returns `sitemap_summary.pacing` and retains `sitemap_collection_history`; collection returns `pacing` and retains `direct_collection_history`. Receipts and policy bodies are private and never belong in `okf/`.

The native guard retrieves robots once per origin per command and applies the stricter configured interval, matching Crawl-delay and supported Request-rate before sitemap documents, HTML and every redirect hop. It also waits between robots and the first document/page. Different origins have separate clocks. XML requests retain XML Accept, identity encoding and the stable collector user agent; HTML requests retain HTML negotiation. Robots failures/disallow and required waits over 60 seconds hold the affected acquisition without automatic retries or provider bypass; collection reports pending sources rather than advertising rendering fallback. Genuine target failures still need source review. Native direct requests always honor robots; a saved `respect_robots: false` is not a production bypass.

Uploaded SF ZIPs, CSV inventory and local-only XML discovery perform no new requests. An authorized remote child of an uploaded index uses the same guard. A deliberately injected fixture transport is outside production timing verification. Managed Firecrawl dispatch is unchanged and retains its explicit scope/budget/receipt rules. Do not replace native commands with a fast custom loop to recover a held result. No API keys or environment files are needed for connected Firecrawl.

### Numeric content in the review count (0.2.12)

Every numeric token counts, including sentence-ending ZIP codes, quantities, years and amounts. Ordered-list markers such as `1.` and `2)` count too: this conservative choice avoids guessing whether a numeric token is formatting or content. Standalone Markdown punctuation such as a heading marker is still excluded. The exact final response, hash checks, empty/over-limit refusal and 150-token maximum remain. Do not remove meaningful numbers to pass the limit; shorten ordinary prose and recheck the complete final file. Counting changed; review text and approval bindings are not rewritten automatically.
