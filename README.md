# Wiki Knowledge Base

A Claude Code plugin that compiles source documents into an interlinked
Markdown wiki and keeps it maintained.

Based on
[Karpathy's LLM Wiki concept](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f),
packaged as Claude skills so you can drop it into an existing project.

You don't just accumulate knowledge — you use it. `/wiki-query` answers
questions using the wiki as its source; `/wiki-tool-query` uses
Selection Recipe articles stored in the wiki to run approved
extractions against registered external data sources.

> Documentation is in English. Some in-repo wiki articles under
> `.wiki/concepts/` are in Japanese — they are dogfood content used to
> demonstrate the tool on a Japanese-language project, not part of the
> tool's documentation.

## Install

Installed as a Claude Code plugin (not an MCP server):

```
# Register the marketplace (this GitHub repo) inside Claude Code
/plugin marketplace add ba0918/wiki-knowledge

# Install the plugin (plugin-name@marketplace-name)
/plugin install wiki@wiki-knowledge
```

To register from a local clone: `/plugin marketplace add /path/to/wiki-knowledge`.

## Skills

| Skill | Role |
|---|---|
| `/wiki-init` | Bootstrap the wiki structure in a project |
| `/wiki-ingest` | Import sources (files, URLs, git repos) into `raw/` |
| `/wiki-compile` | Generate articles from `raw/` into `concepts/`. Includes `discover` mode for extracting domain knowledge from source code |
| `/wiki-query` | Answer questions using the wiki; optionally promote high-quality answers to articles |
| `/wiki-lint` | Ten quality checks + Trust Score + Gap Detection |
| `/wiki-cycle` | Orchestrator: runs ingest → compile → lint end to end |
| `/wiki-tool-query` | Approved ad-hoc data extraction against registered data sources (advanced) |

## Core workflow: grow the wiki

### 1. Initialize

```
/wiki-init
```

Creates the `.wiki/` directory and sets `wiki_root` in `AGENTS.md`.

### 2. Ingest sources

```
/wiki-ingest path/to/article.md
```

Sources pass security checks (sensitive data scan, prompt injection
detection) before being stored immutably under `raw/`.

Git repositories are supported and can be batched:

```
/wiki-ingest https://github.com/owner/repo https://gitlab.example.com/team/api
```

The clone cache lives at `{wiki_root}/.cache/repos/` and can be safely
`rm -rf`d (repos under `ghq` management are left alone).

### 3. Compile

```
/wiki-compile
```

Automatically finds unprocessed sources and generates articles. Runs
`[[wikilink]]` insertion and Backlink Audit.

To extract domain knowledge (architecture, DB schema, business rules)
from repo-ingested source code, use `discover`:

```
/wiki-compile discover
```

### 4. Lint

```
/wiki-lint
```

Runs the ten automated checks (dead link, orphan, missing source,
format violations, and more), plus **Trust Score** for per-article
confidence and **Gap Detection** for topics that were asked about but
have no article.

### 5. Do it all at once

```
/wiki-cycle
```

Runs ingest → compile → graph generation → lint in one shot.

### Ask the wiki

```
/wiki-query Why are ingest and compile kept separate?
```

A retrieval pre-pass (one-hop expansion over the link graph, annotated
with Trust Score) narrows candidate articles, then the wiki is used as
source material to synthesize a cited answer. Query metadata is
appended to QueryLog, which feeds Gap Detection. High-quality answers
can be promoted into `concepts/`.

## Advanced: constrained data extraction (`/wiki-tool-query`)

Handles ad-hoc extraction requests ("pull the users eligible for the
event compensation") by combining wiki knowledge with access to
external data sources.

The LLM plans the extraction; a human approves; the script executes.
Free planning + constrained execution.

Target: any data source the LLM can access. Not just databases — APIs,
admin tools, analytics tools like Kibana or Redash. Anything that can
return rows can be registered in the catalog. Currently the plugin
ships four connectors: **sqlite / postgres / mysql / HTTP API**
(Phase A2). Approval flow, audit, and delivery are connector-agnostic;
new sources are added by writing an adapter to the Connector protocol.
Browser-driven admin tools (no API) don't fit the "query → rows" model,
so they're carved out as a separate design — see `/wiki-browser-extract`
if you need it, though it lives outside the main plugin scope.

### Setup

- Register connection targets in `{wiki_root}/tools/catalog.json`
  (git-managed — the catalog is the source of truth for execution
  contracts: connection targets, table allowlists, row caps).
- Credentials go in `{wiki_root}/.local/credentials.json` (git-ignored,
  sqlite doesn't need any).

### Three-stage flow

```
prepare (dry-run) → approve (human runs it) → execute
```

1. **prepare** — the LLM consults the Selection Recipe article, drafts
   SQL, and presents a selection funnel (row counts as each condition
   is added) and an expected row-count range.
2. **approve** — a human reviews and runs the approve command. The LLM
   does not substitute here.
3. **execute** — the approved SQL is verified by digest against the
   proposal, then executed; results (CSV + verification manifest:
   counts, duplicates, NULLs) are handed off to the delivery
   destination.

For HTTP tools (no SQL), the SQL is replaced by a request-spec JSON
declaring method / path / records_path.

### Safety highlights

- **Per-connector read-only defense**: sqlite gets triple defense
  (read-only URI + `PRAGMA query_only` + authorizer). Postgres and
  MySQL use a DB-side read-only role (primary defense) + sqlglot
  static SQL check (single SELECT / relation allowlist / rejected
  function calls) + session read-only. HTTP uses an endpoint allowlist
  + URL canonicalization + redirect denial + response-size cap.
- **Single-use approval**: one approval, one execution. Changing SQL,
  the request spec, or the delivery destination after approval fails
  the digest check.
- **No result retention**: results are handed to the delivery
  destination and dropped. The audit log stores metadata only.
  Credentials that leak into a driver exception are sanitized — they
  never appear in stdout/stderr/audit.
- **Pre-flight `doctor`**: after registering a remote DB or API, run
  `doctor` to diagnose connectivity, read-only role, and delivery
  without touching real data (uses schema introspection to verify the
  read-only role is truly SELECT-only).

After a case closes, capture the decisions and exclusion rules as a
**Selection Recipe** article in the wiki. Next time the same shape of
request comes in, the LLM can read the Recipe and reproduce the same
quality of extraction. Manual work turns into shared knowledge — the
reason this skill lives inside the wiki, not next to it.

## Wiki directory layout

```
.wiki/
├── raw/                       # Immutable source documents
│   ├── articles/              # Web articles, blogs, papers
│   └── files/                 # Local files, repo inventories
├── concepts/                  # LLM-generated articles with cross-references
├── tools/
│   └── catalog.json           # tool-query connection catalog (source of truth)
├── outputs/
│   ├── queries/               # Query answers
│   ├── reports/               # Lint / Trust Score / Gap Detection reports
│   ├── graph.json             # Derived link graph (consumed by lint and query)
│   ├── querylog.jsonl         # Query metadata log (git-ignored)
│   ├── toolquery-plans/       # tool-query proposal bundles (git-ignored)
│   └── toolquery-audit.jsonl  # tool-query audit log (git-ignored)
├── schema/                    # page-template / categories / querylog / tool-catalog / tool-request-spec
├── .cache/                    # repo-ingest clones and manifests (git-ignored)
├── .local/                    # Credentials (git-ignored)
├── index.md                   # Full page catalog
└── log.md                     # Append-only operation log
```

## Design principles

- **Ingest / Compile separation.** `raw/` is immutable, so bulk
  ingest can be followed by a single compile pass.
- **Backlink Audit is mandatory.** Compile scans existing articles to
  add bidirectional links. Skip this and the wiki degrades into a blog.
- **Query → Wiki promotion.** Promoting good answers into the wiki
  compounds the knowledge base.
- **Derived layer.** Link graph, Trust Score, and Gap Detection are
  treated as re-derivable — not persisted in frontmatter.
- **Scripts are the source of truth.** JSONL append, schema
  validation, security scan, SQL execution enforcement — all in
  Python, so the LLM doesn't hand-assemble structured data.
- **Safety perimeter lives in git-managed files.** The tool-query
  execution contract is in `catalog.json`, so editing a wiki article
  cannot move the perimeter.
- **Explicit invocation.** `/wiki-*` slash commands only. Not
  description-word triggers.

## Roadmap

| Phase | Content | Status |
|-------|---------|--------|
| 0-1 | MVP + four-phase pipeline + skill registration | Done |
| 2 | QueryLog + Gap Detection + auto-ingest suggestion | Done |
| 3 | Trust Score + lint hardening | Done |
| repo-ingest / discover | Git repo ingest + domain knowledge extraction | Done |
| tool-query Phase A | Approved ad-hoc aggregation (sqlite) | Done |
| tool-query Phase A2 | Multi-connector (postgres / mysql / HTTP) + static SQL gate + doctor | Done |
| browser-extract | Containment-based UI scraping (separate design) | Done |
| tool-query Phase B | Audit reconcile, Slack delivery, Correction Mining | Planned |
| 4-5 | Multi-Resolution / Portal Adapter | On hold |

## License

MIT
