# Frontmatter Schemas

Frontmatter definitions per file type.

## Wiki articles (`concepts/*.md`)

The formal JSON Schema lives at `{wiki_root}/schema/page-template.json`.

```yaml
---
title: Page title                # required
type: wiki                       # required, constant
source_refs:                     # required, relative to {wiki_root}
  - "raw/articles/xxx.md"
created: 2026-04-05              # required, YYYY-MM-DD
updated: 2026-04-05              # required, YYYY-MM-DD
category: concepts               # required, slug from categories.json
tags: [tag1, tag2]               # required
related:                         # optional, relative to {wiki_root}
  - "concepts/yyy.md"
---
```

## Raw sources (`raw/articles/*.md`)

```yaml
---
title: Document title            # required
source_url: https://example.com  # optional (for URL ingest)
scraped: 2026-04-05              # required, ingest date
tags: [tag1, tag2]               # required
---
```

## Raw sources — repo-derived (`raw/files/{repo-slug}/*.md`)

Files ingested through repo-ingest carry additional fields:

```yaml
---
title: "ripgrep GUIDE"
source_url: "https://github.com/BurntSushi/ripgrep/blob/master/GUIDE.md"  # remote URL (userinfo must be stripped)
source_revision: "48b0c795f4feb37343b2832d991c5c6a3900c08a"  # required, HEAD commit hash at ingest time
source_path: "GUIDE.md"          # required, in-repo relative path
scraped: 2026-07-03
tags: [ripgrep, docs]
---
```

**Note**: do NOT use the name `source_version` — it collides with the
source-agnostic pipeline's `Source.source_version: int` (a monotonic
counter).

**Mapping to pipeline v1 schema** (migration table — finalized):

| Raw field | Pipeline `Source` field |
|---|---|
| `source_url` | `permalink` |
| `source_revision` | `revision` (finalized — `Source.revision` in `lib/domain/types.py` / added to `page-template-v1.json`) |
| `source_path` | `extensions["repo"]["source_path"]` (reserved namespace) |

Note: the v1 schema itself is not adopted (v0 is the schema-of-record).
Adoption trigger: the first feature that writes state to `concepts/`
that cannot be re-derived. Run the v1 migration in the same cycle.

## Query output (`outputs/queries/*.md`)

```yaml
---
title: Question summary
type: query
question: Original question text
answered: 2026-04-05
sources_consulted:
  - "concepts/xxx.md"
promoted: false                  # true if copied to concepts/
---
```

## Lint report (`outputs/reports/*.md`)

```yaml
---
title: Lint Report YYYY-MM-DD
type: lint
date: 2026-04-05
summary:
  error: 0
  warning: 0
  info: 0
---
```

## QueryLog entries (`outputs/querylog.jsonl`)

Each line is one JSON object (JSONL).

```jsonl
{"id":"q_20260405T223000","timestamp":"2026-04-05T22:30:00+09:00","question":"How is Ingest different from Compile?","sources_consulted":["concepts/llm-wiki-knowledge-base.md"],"sources_cited":["concepts/llm-wiki-knowledge-base.md"],"gap_noted":false,"gap_topics":[],"promoted":false,"promoted_to":null}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | Yes | `q_{YYYYMMDDTHHMMSS}` (timestamp-based) |
| `timestamp` | string | Yes | ISO 8601 timestamp |
| `question` | string | Yes | User question verbatim |
| `sources_consulted` | string[] | Yes | Article paths actually read from the retrieval candidates |
| `sources_cited` | string[] | Yes | `[[wikilink]]`s extracted from the answer via regex |
| `gap_noted` | boolean | Yes | Did the answer flag "not in the wiki"? |
| `gap_topics` | string[] | Yes | Gap topic names (may be empty) |
| `promoted` | boolean | Yes | Was the answer promoted to `concepts/`? |
| `promoted_to` | string \| null | Yes | Path of the promoted article (null when `promoted=false`) |

**Note**: `question` stores the user's question verbatim. Because it
may contain sensitive information, this file is git-ignored by
default.
