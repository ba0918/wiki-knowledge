# AGENTS.md

Shared project instructions for Claude Code, Codex CLI, and other agents.
`CLAUDE.md` is a thin wrapper that imports this file with `@AGENTS.md`.

## Wiki Knowledge Base

This project is the LLM Wiki Knowledge Base.

`wiki_root: .wiki`

## Scope

An experiment in LLM-driven knowledge-base construction. Implements
Karpathy's LLM Wiki concept as agent skills, packaged to drop into an
existing project.

## Conventions

- Wiki articles live flat under `.wiki/concepts/` as `{slug}.md`.
- Source documents live under `.wiki/raw/`, immutable.
- Cross-references use `[[wikilink]]` notation.
- Frontmatter follows `.wiki/schema/page-template.json`.
- Categories are managed in `.wiki/schema/categories.json`.
- **Schema regime**: v0 (`page-template.json`) is the schema-of-record.
  v1 (`page-template-v1.json` + `lib/` migrations) is a standby asset
  with an adoption trigger — decision doc:
  `.agents/artifacts/plans/20260707194819_schema-regime-decision.md`.

## Articles

- [[llm-wiki-knowledge-base]] — LLM Wiki Knowledge Base (concepts)
- [[wiki-knowledge-architecture]] — Wiki knowledge construction architecture (concepts)
- [[llm-wiki-use-cases]] — LLM Wiki use cases (concepts)
- [[llm-wiki-tooling]] — LLM Wiki tooling (tools)
- [[querylog]] — QueryLog metadata log substrate (concepts)
- [[trust-score]] — Trust Score for article confidence (concepts)
- [[gap-detection]] — Gap Detection and ingest suggestions (concepts)
- [[graphify-knowledge-graph-concepts]] — graphify knowledge graph patterns and applicability (concepts)
- [[wikilink-github-interop]] — GitHub × wikilink interoperability (concepts)
- [[wikilink-reader-comparison]] — wikilink reader implementations (tools)
- [[wikilink-conversion-strategies]] — wikilink ↔ standard Markdown link conversion (practices)
- [[wikilink-link-parser-spec]] — `lint-wiki.py` wikilink parser spec (references)
- [[inquiry-event-point-missing]] — Investigation playbook: unattributed event points (practices)
- [[inquiry-subscription-mismatch]] — Investigation playbook: subscription mismatch (practices)

## Feature reference

Detailed reference lives under `docs/reference/`. Each doc is short and
task-oriented — read the one that matches what you're doing.

| Area | Doc |
|---|---|
| QueryLog append + stats | [`docs/reference/querylog.md`](docs/reference/querylog.md) |
| Query retrieval pre-pass | [`docs/reference/query-retrieval.md`](docs/reference/query-retrieval.md) |
| Trust Score | [`docs/reference/trust-score.md`](docs/reference/trust-score.md) |
| Gap Detection | [`docs/reference/gap-detection.md`](docs/reference/gap-detection.md) |
| Wiki Lint (10 checks) | [`docs/reference/lint.md`](docs/reference/lint.md) |
| Repo Ingest | [`docs/reference/repo-ingest.md`](docs/reference/repo-ingest.md) |
| Discover (code → domain articles) | [`docs/reference/discover.md`](docs/reference/discover.md) |
| Ingest security scan | [`docs/reference/security-scan.md`](docs/reference/security-scan.md) |
| Tool Query (SQL / HTTP) | [`docs/reference/tool-query.md`](docs/reference/tool-query.md) |
| Browser Extract (containment) | [`docs/reference/browser-extract.md`](docs/reference/browser-extract.md) |
| Graph layer | [`docs/reference/graph-layer.md`](docs/reference/graph-layer.md) |
| Operation log (`log.md`) | [`docs/reference/operation-log.md`](docs/reference/operation-log.md) |

## Research Gaps

_Record uninvestigated topics here._
