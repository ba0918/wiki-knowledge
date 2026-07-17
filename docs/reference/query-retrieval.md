# Query Retrieval

`wiki-query` uses a retrieval pre-pass before answer synthesis.

- Command:
  ```bash
  python3 skills/wiki/scripts/query_retrieve.py \
    --wiki-root .wiki \
    --keywords <kw>...
  ```
- Consumes `outputs/graph.json` and Trust Score. Expands one hop in both
  directions (outbound + backlink). Seed contribution is distributed by
  degree normalization to prevent hub domination.
- Returns a trust-annotated candidate list.
- `outputs/graph.json` is required. If missing, exits with code 2 and
  prints how to run `graph_gen.py`.
- Output formats: `--format table` (default) / `json`.
- Rule of thumb: when citing an article with trust < 0.30, the
  `wiki-query` skill must annotate it as "(low trust)".
