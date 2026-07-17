# Architecture

## Design principles

The LLM Wiki Knowledge Base has three layers.

### Three-layer structure

| Layer | Location | Responsibility | Owner |
|---|---|---|---|
| Source | `{wiki_root}/raw/` | Immutable source documents | Human (curation) |
| Knowledge | `{wiki_root}/concepts/` | Cross-referenced wiki articles | LLM (compile / promote) |
| Output | `{wiki_root}/outputs/` | Query answers, lint reports, derived graph | LLM / scripts |

**Principle**: the Source layer is immutable. The LLM only modifies the
Knowledge and Output layers.

### Derivation inside the Knowledge layer (concepts → inventory → graph)

The Knowledge layer flows in one direction: **source of truth
(concepts) → derived index (inventory) → derived graph (graph)**.

```
concepts/*.md   (source of truth — edited by humans / the LLM)
     │
     ▼ parse (lib/inventory.py)
ArticleInventory  (derived index — in-memory only, not persisted)
     │
     ▼ graph_gen.py
outputs/graph.json  (derived graph — nodes / edges / metadata.dangling_links)
     │
     ├─▼ lint-wiki.py --use-graph (ON by default)
     │  Lint Findings  (dead_link / orphan detected via the graph layer)
     │
     └─▼ query_retrieve.py (query retrieval pre-pass)
        Candidate list  (seed keyword match → one-hop expansion in both directions → trust annotation)
```

The graph layer serves two purposes: **(1) a single place to compute
`dead_link` / `orphan`** (lint reads `metadata.dangling_links` and
`edges` from `outputs/graph.json` instead of walking the inventory
again — this removes the double implementation of the detection logic),
and **(2) an expansion substrate for query retrieval** (`query_retrieve.py`
walks `edges` for outbound **and backlink** expansion; backlinks are
invisible from article bodies — only the graph provides them).

### Four-phase pipeline (with the derivation step)

```
Ingest → Compile → graph_gen → Lint → (back to Ingest)
                       ▲
                       └ derivation step; runs after compile and before lint
```

`graph_gen` is a derivation step — not an independent phase. It sits
between compile and lint. `wiki cycle` is the orchestrator that
explicitly calls `compile → graph_gen → lint`.

### Four phases

```
Ingest → Compile → Query → Lint → (back to Ingest)
```

| Phase | Input | Output | Trigger |
|---|---|---|---|
| Ingest | File / URL | Staged under `raw/` | User adds a source |
| Compile | Sources under `raw/` | Articles under `concepts/` | After ingest, or on demand |
| graph_gen | `concepts/*.md` | `outputs/graph.json` | After compile, before lint (derivation step) |
| Query | User question | Answer (optionally promoted to an article) | User asks |
| Lint | Whole wiki + `outputs/graph.json` | Report + fix suggestions | Periodic, or on demand |

### Path resolution

Every skill picks `wiki_root` up from `AGENTS.md` (or `CLAUDE.md`).

```yaml
---
wiki_root: .wiki
---
```

All wiki-internal paths are relative to `{wiki_root}`.

## Schema regime (v0 = schema-of-record / v1 = standby)

Decision (2026-07-07): v0 is formally declared schema-of-record. v1
stays on standby until "the first feature that writes state to
concepts/ that cannot be re-derived from raw/" lands. Five-repo real
adoption may proceed on v0.

- **v0 (`{wiki_root}/schema/page-template.json`) is the
  schema-of-record.** Every article, every consumer script, the
  compile procedure, and the wiki-init templates all comply with v0.
- **v1 (`page-template-v1.json` + `lib/`'s Article aggregate + the
  full `migrations/` set) is a standby asset with an adoption trigger.**
  Do NOT delete it. But do NOT use it for new articles.
- **Adoption trigger (invariant)**: the first feature that writes
  "state to concepts/ that cannot be re-derived from raw/" — think
  `wiki review resolve`, claim arbitration, source-less promote —
  must ship in the same cycle as the v1 migration (a `migrate.py` CLI
  + full-article promotion + consumer-side v1 support).
- **Rationale**: `concepts/` is a re-derivable output of `raw/`
  (matching the three-layer principle above). What is truly
  irreversible is `raw/` — and raw frontmatter revision pinning is
  already implemented by repo-ingest. The thing v1 maximalism
  protects (irrecoverable article state) does not exist until a
  feature arrives that writes it.
- **Watchdog**: the lint `schema_version_unadopted` check
  (`format_violations`, error) catches v1 articles that slip in.

### `lib/` status classification

| Class | Modules | State |
|---|---|---|
| Current (shared service layer) | `path_validator.py` / `clock.py` / `file_lock.py` / `lib/domain/repo.py` / `repo_clone.py` | Consumed by `repo_ingest`. Future deterministic scripts use these. |
| Standby (activated on trigger) | `lib/domain/types.py` Article aggregate / `schema.py` v1 load-dump / `wiki_repo.py` / all of `migrations/` | Full test suite, on standby. Re-verify and adopt when the trigger fires. |

## Retrofit migration: adding the graph layer to an existing wiki

If a wiki was created before the graph layer arrived (no
`{wiki_root}/.gitignore`, no `outputs/graph.json`), run this once. New
wikis created via `wiki init` do not need this.

### Procedure

1. **Place `.gitignore`**
   - Copy
     `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/wiki-gitignore-template`
     to `{wiki_root}/.gitignore`.
   - If one already exists, append only the missing lines (merge).
     At minimum, these three lines must be present:
     ```
     outputs/querylog.jsonl
     outputs/inventory.json
     outputs/graph.json
     ```
2. **Generate the graph for the first time**
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/graph_gen.py --wiki-root {wiki_root}
   ```
   Placing `.gitignore` first prevents accidental commits.
3. **From here on**
   - `wiki cycle` runs `compile → graph_gen → lint` automatically.
   - Standalone lint reads `outputs/graph.json`
     (`lint-wiki.py --use-graph` — ON by default).
   - To regenerate the graph, run `lint-wiki.py --auto-graph` or
     `graph_gen.py` directly.

### Watch out

- If you skip step 1 and go straight to step 2, `outputs/graph.json`
  may end up in the git index. Always place `.gitignore` first.
- If derived files under `outputs/` are already committed, remove
  them with
  `git rm --cached outputs/graph.json outputs/inventory.json outputs/querylog.jsonl`.

## Backlink Audit

Required step in `compile` / `promote`. After adding a new article,
walk the existing articles and establish bidirectional links.

Why required: one-directional links degrade the wiki into a blog.
Bidirectional links let you reach related information from anywhere.

### Procedure

1. Extract title / tags / keywords from the new article.
2. `grep`-walk every article under `{wiki_root}/concepts/`.
3. Add `[[new-slug]]` to strongly-related existing articles.
4. Add the same to the existing article's `related` frontmatter.

## Wikilink rendering (GitHub-compatible companion)

GitHub Flavored Markdown does not parse `[[slug]]`, so `.wiki/concepts/*.md`
loses its links when viewed in the GitHub Web UI or PR review. This
project uses the **companion form**
([[wikilink-conversion-strategies]] strategy 3) and rewrites `[[slug]]`
to `[[slug]] ([↗](slug.md))` automatically.

- Converter: `skills/wiki/scripts/wikilink_render.py` (pure
  `render_wikilinks(text)` + a thin CLI).
- Runs at: final step of `wiki compile` (automatic). Manual invocation:
  `python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/wikilink_render.py --write .wiki/concepts/`.
- Idempotent: lines already in companion form are skipped. Running
  multiple times is safe.
- Verification: the lint `wikilink_rendering` check (warning) catches
  bare `[[wikilink]]`s.
- Code fences and inline code are excluded using the same regex as
  `lib/inventory.py`. Tilde fences (`~~~`) are a known limitation.
