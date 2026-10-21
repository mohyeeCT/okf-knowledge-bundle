# Private unresolved-exclusion continuity

Use actual client context, prior run exports and the connected shared-memory source documents. Verify exact domain/entity identity. Saved history is evidence of a prior hold, never new authorization. All routes use the same continuity step after init and before drafting; no live website fetch is required for it.

The assistant creates this private JSON; do not ask analysts to edit it. Example uses a fictional business and a previously reported free-versus-paid conflict:

```json
{
  "domains": ["example.com"],
  "checked_references": ["Actual prior review or shared-memory source reference"],
  "history_status": "checked",
  "exclusions": [{
    "key": "consultation-terms",
    "reason": "Prior captures conflict on whether this consultation is free or paid; no clarification recorded.",
    "source_reference": "Actual prior conflict reference",
    "selectors": [["consultation", "free"], ["consultation", "fee"], ["consultation", "paid"]]
  }]
}
```

Domains must equal the sorted exact approved hosts in the run config. Each selector is an AND of terms; alternative selectors are OR. Matching normalizes case, Unicode and punctuation and uses word boundaries. It checks topic title/description plus each statement, independently of claim IDs. Include relevant wording variants and source languages; avoid broad terms that unnecessarily exclude ordinary service descriptions. These literal selectors cannot recognize every paraphrase. The assistant must separately check semantic equivalence and preserve the reason even if no current selected topic matches.

```text
python scripts/okf.py continuity-import --run RUN_ROOT --file PRIVATE_CONTEXT.json
python scripts/okf.py continuity-export --run RUN_ROOT --out NEW_PRIVATE_CONTEXT.json
```

New CLI runs require a recorded check before preview or readiness. If there is no accessible prior history, use `history_status: unavailable`, explain the actual access limit in checked_references, and import any holds known from current user context. An accessible search with no relevant results can record `checked` with an empty exclusion list. Never manufacture a successful search. Do not turn a normal first job into a mandatory client questionnaire.

Imports merge rather than replace; empty lists do not delete holds, and changed definitions require a new distinct key. Wrong-domain files and injected approval fields are rejected atomically. Excluded topics stay private in the same run. Preview, approval and candidate readiness enforce holds against selected content only. CLI initialization always enables the continuity check. Existing saved runs and direct library consumers without that configuration retain their earlier behavior until context is explicitly imported; installing the update does not rewrite state.

Resolve only after actual clarification specifically covering the conflict:

```text
python scripts/okf.py continuity-resolve --run RUN_ROOT --key EXCLUSION_KEY --reason ACTUAL_CLARIFICATION --by human:ACTUAL_ROLE --at ACTUAL_RECEIVED_TIME --channel ACTUAL_CHANNEL --reference ACTUAL_MESSAGE
```

The event must follow this run's imported hold and retain the real human reference; a copied page or old content approval is insufficient. Resolution does not approve content or publication and does not make a private correction public evidence. Normal source and wording checks still apply. Export only unresolved holds; retain resolution history in private state. Keep the export outside developer/public packages. Preserve concise unresolved milestones in an authorized connected shared memory so a fresh ephemeral session can retrieve them; a local file path alone does not provide cross-assistant access.
