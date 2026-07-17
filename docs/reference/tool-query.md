# Tool Query (constrained ad-hoc extraction with audit)

Exploratory ad-hoc aggregation against catalog-registered data sources
(sqlite / postgres / mysql / HTTP API), gated by a dry-run approval flow.
Skill entry point: `wiki-tool-query`.

The approval flow, audit, delivery, and single-use semantics are shared
across all connectors. Per-type differences are confined to connection
and enforcement layers.

## The catalog is the source of truth

The execution contract lives in the git-managed catalog:

- `.wiki/tools/catalog.json`
- Schema: `.wiki/schema/tool-catalog-schema.json`

It declares connection target, relation allowlist, output limits, and
delivery destination. Changes go through PR review — the safety
perimeter cannot be moved by editing a Wiki article. Type-specific
fields are required conditionally (tagged config).

## Flow

1. **prepare** — funnel COUNT + immutable proposal bundle generated.
2. **approve** (human) — TTY confirmation prompt. The LLM does not
   substitute here.
3. **execute** — verification matrix → real run → CSV + manifest →
   atomic publish.

Approval is single-use (`consumed` = approval consumed). TTL is 24h.

## CLI

```bash
python3 skills/wiki/scripts/tool_query_run.py \
  {catalog-validate|prepare|approve|execute|doctor} \
  --wiki-root .wiki ...
```

Exit codes: 0 = success / 1 = rejected or failed / 2 = usage /
130 = SIGINT. Python 3.11+ required.

## Per-connector enforcement

- **sqlite**: read-only triple defense (read-only URI + `PRAGMA
  query_only` + authorizer callback) + `setlimit`.
- **postgres / mysql**: DB-side read-only role (primary defense —
  must be configured) + sqlglot static SQL check (single SELECT /
  relation allowlist / rejected function calls) + session read-only.
  - Postgres: `read_only=True` set before the transaction + named
    cursor.
  - MySQL: `SET READ ONLY` + `SSCursor`.
  - TLS verify is on by default.
  - The MySQL temp-table hole is closed on the role side. MariaDB is
    out of the assurance envelope.
- **http**: request-spec JSON (`--request-file` / `--count-request`)
  + endpoint allowlist (method + path-prefix on segment boundaries)
  + URL canonicalization (encoded-separator rejection, double-encoding
  fail-closed) + redirect denial + `Accept-Encoding: identity` +
  `max_response_bytes` chunk cutoff. One-shot JSON API only.
- Shared: output limits + full-segment symlink rejection for path
  containment + CSV neutralization (OWASP).

## Doctor (pre-flight)

```bash
python3 skills/wiki/scripts/tool_query_run.py doctor \
  --wiki-root .wiki [--tool <id>] [--probe-write <tool-id>]
```

Never touches real data (no COUNT). Diagnoses connectivity,
`session_readonly`, `role_grants`, TLS, and delivery.

Read-only is broken into independent checks; `role_write_denial` etc.
mark SKIP explicitly.

Exit codes: 0 = required OK / 1 = NG / 2 = usage. Audit event type is
plan-independent: `doctor`.

## Credentials

`.wiki/.local/credentials.json` (git-ignored, no group/other permission
bits — mode ≤ `0600`). SQLite may omit credentials; remote connectors
require them. Referenced by `credential_ref`.

Path reads apply full-segment symlink rejection with `O_NOFOLLOW`
same-fd verification. Secrets never appear on stdout/stderr, in audit,
or in errors — driver exceptions are sanitized to sqlstate/errno for
classification only.

## Audit and bundles

- Audit log: `.wiki/outputs/toolquery-audit.jsonl` — append-only,
  git-ignored, metadata only (no values).
- Bundle path: `.wiki/outputs/toolquery-plans/{plan_id}/` — git-ignored.

## Selection Recipe

The explanation layer lives as Wiki articles with `category: practices`
and tag `selection-recipe`. They capture "what to fetch and how to
decide."

- Template: `skills/wiki/assets/selection-recipe-template.md`.
- Guide: `skills/wiki/references/tool-query-guide.md`.

The interaction with `wiki-query` stops at "surface the Recipe article."
`wiki-query` never executes the extraction.

## Dependencies

- `psycopg[binary]` (postgres)
- `PyMySQL` (mysql)
- `sqlglot` (static SQL gate)

Declared in `requirements.txt` with `major.minor` upper bounds. Real DB
smoke tests are opt-in via `TOOL_QUERY_SMOKE_PG` /
`TOOL_QUERY_SMOKE_MYSQL` environment variables — skipped when unset.
