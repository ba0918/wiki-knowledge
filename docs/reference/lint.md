# Wiki Lint

Automated quality and consistency checks across ten dimensions.

## Graph dependency

`dead_link` and `orphan` are detected via the graph layer, so
**`graph_gen.py` must run before `lint-wiki.py`**.

- `--use-graph` is ON by default. When `.wiki/outputs/graph.json` is
  missing, the lint exits with code 2 and prints instructions to run
  `graph_gen.py`.
- `--auto-graph` opts into a rescue path: the lint spawns `graph_gen`
  as a subprocess when the graph is missing.
- `--no-graph` is the legacy path: recomputes dead_link / orphan directly
  from the inventory.

## Running

```bash
python3 skills/wiki/scripts/graph_gen.py --wiki-root .wiki && \
python3 skills/wiki/scripts/lint-wiki.py --wiki-root .wiki
```

Output formats: `--format table` (default) / `json` / `report` (Markdown).

Report path: `.wiki/outputs/reports/{YYYYMMDD}-lint.md`.

## Checks

| Check | What it catches |
|---|---|
| Dead link | `[[slug]]` targets that do not exist |
| Orphan | Articles with no inbound links |
| Missing source | `source_refs` pointing to missing files |
| Missing frontmatter | Required fields absent |
| Coverage gap | Topics referenced ≥ 2× with no article |
| Link quality | One-directional links; mismatch between `related` and body wikilinks |
| Article quality | Articles < 50 words; speculation blocks > 30% |
| Format violations | Slug naming, `page-template.json` compliance, category/type/date/tags validation, unadopted-v1 detection (`schema_version_unadopted`) |
| Wikilink rendering | `[[slug]]` missing the GitHub Web UI companion `([↗](slug.md))` — fix with `python3 skills/wiki/scripts/wikilink_render.py --write .wiki/concepts/`; compile integrates this automatically |
| Index sync | Divergence between `.wiki/index.md` and `concepts/` (unlisted articles / phantom entries) |
