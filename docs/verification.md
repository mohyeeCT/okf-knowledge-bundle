# Public distribution verification

Processing release: **0.2.18**. Public packaging revision: **1**, prepared 27 October 2026.

All 414 synthetic regressions passed, including all three route-to-handoff fixtures and 14 new approved-source/diagnostic tests. Scripts, requirements and tests are identical to the tested team implementation. Both platform folders and ZIPs contain matching current files. Public packages exclude client records, historical notes, internal guides and private Git history.

See the [manifest](package-manifest-0.2.18-public.1.json) and [verification receipt](public-distribution-verification.json). Run `python tools/verify_distribution.py` for folder/archive parity, safe paths and local links. Test scope and limitations are in [VERIFICATION.md](../openai/okf-knowledge-bundle/VERIFICATION.md). Installed copies and native host behavior are verified separately. Content approval, owner release approval and publication remain distinct. No client bundle is published.

No separate native ChatGPT workspace-agent pilot has been completed. Each analyst account needs execution/tool setup and a bounded supervised job.
