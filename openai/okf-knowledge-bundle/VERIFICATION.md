# Verification — processing release 0.2.13

This public distribution includes the current processing scripts and original synthetic regression fixtures. It excludes historical release/design notes, benchmark result files, private client artifacts and private Git history.

The processing code, requirements and synthetic tests are byte-identical to the tested 0.2.13 implementation. Current public-package verification is recorded in the repository's docs/public-distribution-verification.json; it checks both platform folders, archive parity and local links.

Run `python -B scripts/run_tests.py -v` from this skill folder with Python 3.12+, pinned requirements, writable temporary storage and localhost fixture permissions. These tests exercise local mechanics and mocked providers. They do not establish factual correctness, all-site coverage, account/provider access or SEO outcomes. A separate native ChatGPT workspace-agent pilot has not been completed. Account setup, actual client review, release approval and publication remain distinct.
