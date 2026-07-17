# QueryLog

Append-only JSONL log of query metadata, feeding Trust Score citation
frequency and Gap Detection input.

- Written when `wiki-query` runs. Path: `.wiki/outputs/querylog.jsonl`.
- Schema: `.wiki/schema/querylog-schema.json`.
- Append:
  ```bash
  python3 skills/wiki/scripts/querylog_append.py \
    --wiki-root .wiki \
    --question <q> \
    --consulted <paths>... \
    --answer-file <path>
  ```
  The script generates the id, extracts `sources_cited` from wikilinks in
  the answer, validates against the schema, and appends under `flock`.
  The LLM never hand-assembles the JSON.
- A test enforces that the schema's `required` field and the script's
  `REQUIRED_FIELDS` stay in sync.
- Stats: `python3 skills/wiki/scripts/querylog_stats.py --wiki-root .wiki`.
- The log is git-ignored by default (`.wiki/.gitignore`).
