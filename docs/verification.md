# Public distribution verification

Processing release: **0.2.13**. Public packaging revision: **1**, prepared 20 October 2026.

This repository contains current skill resources, processing code, original synthetic tests and install packages. Historical release/design notes, historical benchmark results, private Git history, client captures, site bundles, approvals, account settings and chat exports are excluded.

Processing scripts, requirements and synthetic tests are byte-identical to the tested 0.2.13 implementation. Current user documentation has been prepared for this standalone repository. Full package hashes differ from other distributions because obsolete/private-context documentation is not included here. Both platform folders and install ZIPs contain the same current files.

See the [package manifest](package-manifest-0.2.13-public.1.json) and [verification receipt](public-distribution-verification.json). Run `python tools/verify_distribution.py` to check platform/archive parity, safe archive paths and local Markdown links without site/provider calls. Maintainers can run `python -B scripts/run_tests.py -v` from either skill folder using Python 3.12+, pinned requirements, writable temporary storage and localhost fixture permissions. Do not run software regressions on every analyst job.

Package and software validation do not certify factual truth, all-site coverage, current provider access, host compatibility, search rankings or AI citations. A separate native ChatGPT workspace-agent pilot has not been completed. Each analyst account still needs execution/tool setup and a bounded supervised job. Actual content review, owner release approval and publication remain separate actions.
