# Synthetic semantic evaluation

The original synthetic corpus in `tests/fixtures/semantic_cases.json` contains 24 fictional evidence cases, 39 expected material facts and three planted material conflicts. Brands, domains, credential bodies, rules and prices are test data. It includes no client extracts and requires no external access. HTML is evidence data, never instructions or executed content.

The standard-library harness is `tests/evaluate_semantics.py`; run its `--help` for current commands. Give a candidate model only the permitted evidence and task instructions, keeping goldens separate. Evaluate whether output preserves qualifiers, attribution, entity distinctions, options, jurisdiction, language and planted conflicts. Check unsupported additions and omissions as well as quote occurrence. A matching quote alone does not prove the public statement follows from it.

The corpus is a development aid, not independent human adjudication or model accuracy certification. A balanced-model comparison and separate native ChatGPT workspace-agent pilot have not been completed by this public distribution. Such evaluations require their own bounded, authorized scope and honest reporting of corrections and limits. Ordinary analyst jobs do not rerun the semantic evaluation suite.
