# Website OKF Knowledge Bundle 0.2.16

Prepare reviewed website knowledge as an unpublished `okf/` folder from Screaming Frog full-HTML ZIPs, XML sitemaps or URL CSVs. The assistant handles processing; analysts review exact wording and sources. Online routes prefer guarded direct HTML and use compatible account-connected Firecrawl rendering when needed.

Start with [SKILL.md](SKILL.md). Current references:

- [Operations and helper commands](references/operations.md)
- [Managed collection](references/managed-collection.md)
- [Draft contract](references/draft-contract.md)
- [Review and release](references/review-and-release.md)
- [Implementation](references/implementation.md)
- [Synthetic semantic evaluation](references/semantic-evaluation.md)
- [Verification](VERIFICATION.md)

Use Python 3.12+ with the pinned requirements. No analyst API keys or environment files are needed for connected Firecrawl. Connections and host execution capability must be verified in each account.

The complete analyst-facing issues summary is at most 150 words, with at most three grouped critical questions and one exact-wording approval. Excluded topics stay unresolved/private in the same run and do not block the approved bundle. Candidate generation is followed by exact-manifest owner approval and separately authorized publication. Developers publish only `okf/`, keeping state, reviews, manifests and instructions private. Read generated DEPLOYMENT.md for each release's holds and freshness limits.

The clean content preview shows exact wording and original source links, ordered by business overview, offerings, supporting topics and policies. The explicit business-overview tag keeps the overview ahead of contact details; the older overview tag on Organization topics is also recognized. Other organization topics join supporting information. Reviews distinguish uploaded/historical captures from content actually checked live; an import or availability-only check does not establish current wording. Detailed quotes, IDs and hashes stay in a separate private audit file. New runs use noindex-crawlable; generated instructions require an HTTP noindex header with crawler access allowed. Saved runs retain their recorded policy.

No ranking, indexing or AI-citation gains are promised. Maintainers can run `python -B scripts/run_tests.py -v` with the pinned requirements and localhost fixture permissions. Software tests do not require client/provider calls and are not part of ordinary analyst jobs.
