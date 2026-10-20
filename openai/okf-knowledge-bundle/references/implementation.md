# Implementation — 0.2.13

The core format is [Google's OKF v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/ad30107c31c06aec8a7d5636e0d1058118604e6f/SPEC.md), pinned to specification commit `ad30107c31c06aec8a7d5636e0d1058118604e6f`. Core conformance and this tool's stricter publishing profile are distinct. Structural validation does not certify factual truth or discoverability outcomes.

| Component | Responsibility |
|---|---|
| common/state | Stable identities and hashes, immutable content-addressed records, atomic pointers and writer locks |
| ingest/extraction | Bounded ZIP/CSV/XML discovery, source identity, body/table/schema evidence and classification hints |
| network/politeness | Guarded public DNS/redirect checks, robots-aware spacing, bounded direct transport and legacy adapter compatibility |
| managed | Host-connected MCP plans, conservative request/credit reservations, exact saved-result imports and representations |
| review/workbook/workflow | Evidence packets, revision-bound drafts and actual human decision provenance |
| batch_review | One exact-content approval and automatic approved bundle selection in the original run |
| owner_setup | Initial publication-owner designation preserving existing content reviews |
| export/validation | Eligibility/freshness, reviewed output, immutable candidates, hash-bound approval and reproducible archives |
| publication | Separately authorized checks of deployed bytes and retired paths |

State stays outside the installed skill. Review-batch binds selected revisions; readiness covers that selection while retaining omitted unresolved records privately. Selected failures remain blocking. Native sitemap, direct collection and availability requests share per-origin pacing. Unknown provider outcomes remain reserved until explicitly reconciled; local reservation is not a provider account-wide billing cap.

The current host model interprets bounded evidence. Local helpers do deterministic processing. No separate model API is required for the interactive workflow. The managed online route uses compatible account-connected Firecrawl where rendering is needed; the legacy Jina adapter remains only for compatibility with existing runs/tests. Source-capture status, direct current availability, business confirmation and publication approval are separate observations/decisions.

Use [operations](operations.md), [managed collection](managed-collection.md), [draft contract](draft-contract.md) and [review/release](review-and-release.md) for the current contracts. The public repository's installation guide describes execution requirements and platform limits. No dependency runtime, credential, client capture or approval record is bundled.
