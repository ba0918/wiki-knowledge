# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows semantic versioning at the plugin level.

## [Unreleased]

### Changed
- Documentation: primary language switched to English (README, AGENTS.md,
  SKILL.md files, `skills/wiki/references/*.md`).
- `AGENTS.md` slimmed to a thin index. Feature reference material moved to
  `docs/reference/*.md`.
- Repo name corrected in internal string references: `wiki-knowladge` →
  `wiki-knowledge`. The GitHub-side rename happens separately; old URLs will
  keep working through GitHub's redirect.

### Added
- GitHub Actions CI (`.github/workflows/ci.yml`): runs the pytest suite on
  Python 3.11 and 3.12, plus a wiki lint job.
- `CHANGELOG.md` (this file).

## [0.6.0] — Browser Extract

Sealed `wiki-browser-extract` skill for containment-based UI scraping —
separate from `wiki-tool-query` because the assurance model differs (SQL
systems get structural read-only guarantees; browser flows get honest
containment and provenance).

### Added
- `wiki-browser-extract` skill and `browser_extract_run.py` CLI with
  `catalog-validate` / `prepare` / `approve` / `execute` / `doctor` / `login`
  subcommands.
- Seal-at-prepare approval model: prepare runs the flow, extracts artifacts,
  enforces the verification contract, seals results with SHA-256, and records
  the seal hash in a `prepared` audit event. Approve re-derives from the
  sealed artifact and manifest and fail-closed matches against the audit
  anchor; execute is delivery-only.
- Catalog schema (`browser-catalog.json`) with per-tool flow SHA-256 pinning,
  auth profile (`none` / `form` / `form+totp` / `human-assisted`), and tier
  (B1 / B2 / B3) with a closed verification vocabulary.
- Flow AST static gate: rejects imports outside allowlist, `exec`/`eval`,
  dunder access, and anything beyond a single `run` function.
- Playwright containment: context-scoped request interception (method +
  path-prefix + resource-type allowlist), WebSocket denial, service worker
  block, redirect-hop revalidation, `data:` / `blob:` denial, WebRTC off,
  ephemeral user-data-dir, fresh context per run.
- Session state store parity with credentials: `0600` atomic write,
  `O_NOFOLLOW`, symlink-segment rejection, TTL, tool·origin·account binding.
- Form-based auto-login with optional TOTP (RFC 6238) and human-assisted
  `login` subcommand for headed capture.
- Fixture web server for smoke/E2E (`browser_fixture_server.py`) covering
  login, TOTP, tables with UI totals, CSV export, and mutation routes for
  false-success corpus.

## [0.5.x] — Tool Query Phase A / A2

`wiki-tool-query` for constrained ad-hoc data extraction with approval and
audit. Multi-connector: sqlite (Phase A) → postgres / mysql / HTTP JSON API
(Phase A2).

### Added
- `wiki-tool-query` skill and `tool_query_run.py` CLI (`catalog-validate` /
  `prepare` / `approve` / `execute` / `doctor`).
- Three-stage flow: prepare (funnel COUNT + immutable proposal bundle) →
  human approve (TTY) → execute (verification matrix → run → CSV +
  manifest → atomic publish).
- Catalog as the source of truth (`tools/catalog.json` + schema), with
  connection targets, relation allowlists, output caps, and delivery
  destinations declared per tool.
- SQLite read-only triple defense: read-only URI + `PRAGMA query_only` +
  authorizer callback + `setlimit`.
- Postgres / MySQL enforcement: DB-side read-only role (primary defense) +
  sqlglot static SQL gate (single SELECT / relation allowlist / rejected
  function calls) + session read-only.
- HTTP connector: request-spec JSON + endpoint allowlist (method +
  path-prefix on segment boundaries) + URL canonicalization + redirect
  denial + `Accept-Encoding: identity` + `max_response_bytes` chunked cutoff.
- `doctor` subcommand: pre-flight diagnostics without touching real data
  (no COUNT, no probe rows). Checks connectivity, session read-only, role
  grants, TLS, and delivery.
- Credential handling: `.wiki/.local/credentials.json` referenced by
  `credential_ref`; full-segment symlink rejection + `O_NOFOLLOW`
  same-fd verification; secrets never emitted to stdout/stderr/audit.
- Audit log (`toolquery-audit.jsonl`, append-only, metadata only).

## [0.4.x] — Repo Ingest / Discover

### Added
- `wiki-ingest` accepts git URLs and local repo paths; auto-clones via
  `ghq get --shallow` (fallback: `git clone --depth 1`) into a cache.
- Repo manifest and machine-generated `repo-inventory.md`; raw frontmatter
  extended with `source_revision` (commit hash) and `source_path`.
- `wiki-compile discover` mode: LLM reads repo source code (classified into
  schema / routes / rules / state / tests / entry) and produces domain
  articles (`{slug}-architecture`, `{slug}-db-schema`, `{slug}-api-routes`,
  etc.).
- Positive-match protocol allowlist (rejects `ext://` / `file://`),
  `GIT_ALLOW_PROTOCOL` restriction, userinfo stripping, two base-path
  containment.

## [0.3.x] — Trust Score v2 + Query Retrieval

### Added
- Trust Score v2: absolute-scale saturation curves per factor
  (`n/(n+1)`, `c/(c+2)`, `b/(b+2)`) instead of min-max normalization, so
  the 0.30 threshold has meaning for a single article.
- Freshness half-life of 365 days (exponential decay), aligned with
  snapshot policy (`source_revision` pinning).
- `query_retrieve.py` retrieval pre-pass: consumes `graph.json`
  (outbound + backlink one-hop expansion, seed contribution normalized by
  degree) and Trust Score to return trust-annotated candidates.

## [0.2.x] — QueryLog + Gap Detection

### Added
- QueryLog append-only JSONL (`querylog.jsonl`) with schema validation
  and `flock`-guarded append.
- Gap Detection over QueryLog `gap_topics` with coverage matching.
- Trust Score v1.

## [0.1.x] — MVP

### Added
- Four-phase pipeline: Ingest → Compile → Query → Lint.
- Wiki structure: `raw/` (immutable sources), `concepts/` (LLM-generated
  articles with `[[wikilink]]` cross-references), `outputs/`, `schema/`.
- `wiki-init`, `wiki-ingest`, `wiki-compile`, `wiki-query`, `wiki-lint`,
  `wiki-cycle` skills.
- Wiki lint checks: dead link, orphan, missing source, missing
  frontmatter, coverage gap, link quality, article quality, format
  violations, wikilink rendering, index sync.
- Derived graph layer (`outputs/graph.json`) as the single source for
  dead-link / orphan detection.
- Codex CLI plugin manifest alongside the Claude Code manifest.
