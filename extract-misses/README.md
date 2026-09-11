# Extract misses

One JSON file per Gmail message the dashboard marked as extracted wrong.
Repeat clicks on the same email **overwrite** that file and bump `report_count`.

Cloud Run cannot keep these files. Production stores them in Postgres. To work
on them locally:

```bash
python -m app.run pull-misses
```

That writes `extract-misses/{gmail-id}.json` plus `_index.json`.

Start with `_index.json` (`problem.tags`, `user_note`, `summary`). Then open the
matching `{id}.json` and compare:

- `extracted.candidates` — what the parser claimed
- `stored_jobs` — what actually became Job rows (scrape status + description)
- `email.body_text` / `email.html` — the source
- `gap.expected_posting_count` — title-shaped lines/anchors vs claimed count

Fix `extract_jobs.py`, `llm_extract.py`, `pipeline._should_store_jobs`, or scrape,
then add a regression test from this email. Do not guess from a screenshot.
