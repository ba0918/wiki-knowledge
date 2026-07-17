# Discover (domain knowledge from source code)

A `wiki-compile` mode that reads source code from a repo-ingested
repository and produces articles under `concepts/` directly.

## Prerequisites

The repository must be ingested first — a manifest must exist at
`.wiki/.cache/manifests/{slug}.json`.

## Source classification

```bash
python3 skills/wiki/scripts/source_scan.py --wiki-root .wiki --slug {slug}
```

Classifies files into six categories: `schema` / `routes` / `rules` /
`state` / `tests` / `entry`.

## Generated articles

Discover chooses among these article slugs based on what the repo
contains:

- `{slug}-architecture`
- `{slug}-db-schema`
- `{slug}-api-routes`
- `{slug}-business-rules`
- `{slug}-state-machines`
- `{slug}-glossary`

## Identifying discover articles

- `tags` contains `discover`.
- `source_refs` contains `raw/files/{slug}/repo-inventory.md`.

## Reading and prompts

- Reading guide: `skills/wiki/references/discover-guide.md`.
- Prompts: the "Discover" section of
  `skills/wiki/references/prompts.md`.

## Pipeline placement

```
wiki-ingest → wiki-compile discover → wiki-compile → graph_gen → wiki-lint
```

`discover` is optional. `wiki-cycle` runs the whole chain.
