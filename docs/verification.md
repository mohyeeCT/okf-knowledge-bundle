# Public distribution verification

Processing release: **0.2.17**. Public packaging revision: **1**, prepared 21 October 2026.

All 400 synthetic regressions passed, including all three route-to-handoff fixtures and 22 new review-delivery/continuity tests. Processing scripts, requirements and tests are identical to the tested team implementation. Both platform folders and ZIPs contain matching current files. The public packages exclude client records, historical notes, internal team guides and private Git history. No client output is published.

See the [manifest](package-manifest-0.2.17-public.1.json) and [verification receipt](public-distribution-verification.json). Run `python tools/verify_distribution.py` for archive/folder parity, safe paths and local links. Current limitations and test scope are in [VERIFICATION.md](../openai/okf-knowledge-bundle/VERIFICATION.md). Installed copies and native host behavior are verified separately. Literal exclusion matching supplements semantic review; it cannot recognize every paraphrase. Host message delivery is checked from actual output, not inferred from a validated draft.

No separate native ChatGPT workspace-agent pilot has been completed. Each analyst account needs execution/tool setup and a bounded supervised job. Content approval, owner release approval and publication remain distinct.
