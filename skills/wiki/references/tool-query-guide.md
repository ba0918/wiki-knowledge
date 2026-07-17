# Tool Query Guide — Dry-run Approval and Selection Recipes

Detailed reference for the `wiki-tool-query` skill. The operational
flow's main text is `skills/wiki-tool-query/SKILL.md`. The execution
contract's source of truth is `{wiki_root}/tools/catalog.json`. The
schema-of-record document is `tool-catalog-schema.json` (kept in this
repository's own wiki under `schema/`); enforcement itself is built
into `tool_query_run.py`'s validator, so a wiki without the schema
file still validates identically via `catalog-validate`.

## Funnel presentation format

Present approval requests so the data-narrowing process reads at a
glance. **How many rows dropped at each step** is the central input
for the approval decision:

```
Target: ev-2026 registrants who have NOT received a refund (compensation targets)
inclusion:
  - registrations.event = 'ev-2026'
exclusion:
  - user_id present in refunds
Funnel:
  ev-2026 registrants: 412
  → without refunds: 397 (-15)
Expected rows: 380–410
Delivery: outputs/deliveries
tool: events-db / plan_id: 20260716... / sql_digest: ab12...
```

Attach the SQL body in `<details>` (the body is executed from the
bundle's `query.sql` — that's the only executable artifact. For
display cross-check, `sql_display_digest` — a conservative
normalization digest of trim + newline unification — is available).

### Writing COUNT SQL

- One step = one file. Add WHERE conditions from the main SQL one at a
  time.
- Each COUNT must return "one row, one column, non-negative integer".
  Anything else rejects as `count_result_invalid`.
- The label is ≤ 64 chars, no control characters, no duplicates. It
  does NOT become the bundle filename (bundle uses `counts/{nn}.sql`).

### Expected-rows range (`expected_rows`)

- Runtime constraint. If the measured row count is outside the range,
  the result is **not published** — rejected as `rows_out_of_range`.
- Center the range on the last funnel COUNT, widening for the data
  drift possible between prepare and execute. Setting `min = max`
  requires an exact match.

## Reason codes on failure (cheatsheet)

| reason | Meaning | Next step |
|---|---|---|
| `not_approved` | Executed while draft (unapproved) | Ask for approval |
| `already_consumed` | Replay (double execute) | New prepare → re-approve |
| `ttl_expired` | Plan expired (**counted from prepare, 24h**; NOT approval time) | New prepare → re-approve |
| `sql_digest_mismatch` / `count_sql_digest_mismatch` | Bundle SQL was modified | Discard the bundle, prepare again |
| `proposal_digest_mismatch` | Proposal was rewritten after approval | Same |
| `catalog_digest_mismatch` | Catalog changed after prepare | New prepare (re-approve under the new contract) |
| `rows_out_of_range` | Measured rows outside the expected range | Investigate; adjust range or conditions and prepare again |
| `row_limit_exceeded` (and similar) | Catalog `limits` exceeded | Narrow conditions or propose a catalog change via PR |
| `delivery_conflict` | Delivery destination already has a run with the same id | Cannot re-execute (approval consumed). New prepare |
| `audit_write_failed` | Audit could not be written (fail closed) | Check disk / permissions. If it happened before DB access, approval is unconsumed |

## Connectors (Phase A2)

The catalog's `type` picks the connector: `sqlite` / `postgres` /
`mysql` / `http`. The approval flow (prepare → approve → execute),
audit, delivery, and single-use are **shared across all types**. The
per-type differences are confined to connection and enforcement.

### postgres / mysql (SQL remote DBs)

- Connection is assembled from catalog fields (`host` / `port` /
  `dbname` / `user` required). **User-input DSN strings are not
  accepted** (reduces injection surface).
- `credential_ref` is **required**. Only the password is resolved from
  it (the user is declared as a catalog field).
- SQL is passed the same way as sqlite: `--sql-file` / `--count-sql`.
  The **static SQL gate** (sqlglot) runs before connection and:
  (1) accepts single SELECT / WITH only, (2) matches against the
  relation allowlist, (3) rejects unknown functions.
- `allowed_tables` can be **fully qualified** (`schema.table` /
  `db.table`) or **unqualified**:
  - postgres: unqualified names are statically expanded against
    `connection.default_schema` (default `public`). Unquoted
    identifiers are lowercased for the match (`Users` == `users`).
    Quoted `"Users"` is treated as distinct.
  - mysql: `connection.dbname` is the default database used for
    expansion. Table name matching is **case-sensitive** (avoiding
    MySQL's config-dependent case handling — case-sensitive by
    default).
  - JOINs, subqueries, CTEs, and view underlying relations are all
    matched against the allowlist. CTE names and derived aliases are
    NOT relations (only underlying real tables are checked).
- **Only functions sqlglot recognizes as built-in are allowed.**
  Unknown functions (`pg_read_file` / `LOAD_FILE` / user-defined
  functions / LATERAL table functions) are rejected fail-closed
  (`sql_gate_function_not_allowed`). `count` / `sum` / `upper` /
  `coalesce` / window functions pass.

#### Read-only role setup (primary defense — always configure)

pg / mysql have no equivalent of sqlite's authorizer (the engine's own
judgment). **A DB-side read-only role is the primary defense** —
static SQL + session read-only are supplemental. Always prepare a
dedicated role:

```sql
-- PostgreSQL: SELECT-only role
CREATE ROLE wiki_readonly LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE analytics TO wiki_readonly;
GRANT USAGE ON SCHEMA public TO wiki_readonly;
GRANT SELECT ON public.users, public.registrations TO wiki_readonly;
-- Do NOT grant INSERT / UPDATE / DELETE / TRUNCATE / CREATE
```

```sql
-- MySQL: SELECT-only user
CREATE USER 'wiki_readonly'@'%' IDENTIFIED BY '...';
GRANT SELECT ON billing.users TO 'wiki_readonly'@'%';
GRANT SELECT ON billing.invoices TO 'wiki_readonly'@'%';
-- Do NOT grant CREATE TEMPORARY TABLES (closes the temp-table hole below)
```

The `doctor` subcommand mechanically verifies this role's read-only
property via introspection (below).

#### Read-only session execution-order contract

- **postgres**: set `Connection.read_only = True` before any
  transaction starts, then open a named cursor (server-side) inside
  the explicit transaction. Pass `statement_timeout` and
  `search_path=<default_schema>` as connection options so the static
  SQL gate and runtime resolution of unqualified relations agree.
- **mysql**: while autocommit is active (before a transaction),
  issue `SET SESSION TRANSACTION READ ONLY` + `max_execution_time`,
  then `START TRANSACTION` and execute via `SSCursor` (unbuffered).
- Large results are cut off at the row cap by the server-side cursor
  (never fully buffered client-side).

#### Assurance envelope limits (pg / mysql)

- **Envelope shift**: sqlite's authorizer is "engine self-judgment";
  pg / mysql's guarantee is the **combination** of DB-side read-only
  role (primary) + static SQL gate + session read-only. Without a
  SELECT-only role, defense shrinks to static + session.
- **MySQL temp-table hole**: MySQL read-only transactions **permit
  DML on temporary tables.** This is not closable at the session
  layer — close it on the role side by NOT granting
  `CREATE TEMPORARY TABLES` (the role setup above handles this).
- **MariaDB is out of scope**: MySQLConnector targets **MySQL**.
  MariaDB may work, but it is not verified.

### TLS (pg / mysql)

- Default is safe: postgres uses `sslmode=verify-full`; mysql uses
  CA + hostname verification.
- CA path: `connection.tls_ca_file` (wiki_root-relative or absolute;
  full-segment symlink rejection; falls back to system CA when
  omitted).
- Relaxation requires explicit opt-in via
  `connection.allow_insecure_tls: true` and is accepted **only when
  the host is localhost / 127.0.0.1 / ::1** (cannot co-exist with
  `tls_ca_file`).
- `doctor` verifies TLS negotiation. If relaxation is declared, it
  reports SKIP (warning).

### http (Redash / Kibana(ES) / internal APIs)

Pass a **request-spec file** (JSON) via `--request-file` /
`--count-request` in place of SQL. `--sql-file` / `--count-sql` are
SQL-only — passing an SQL flag to an http tool errors with guidance:

```json
{
  "method": "POST",
  "path": "/api/queries/42/results",
  "body": { "max_age": 0 },
  "records_path": "query_result.data.rows",
  "columns": ["user_id", "email"]
}
```

- **The request spec is validated against a JSON Schema**
  (`{wiki_root}/schema/tool-request-spec-schema.json`, unknown keys
  rejected). Row retrieval: `records_path` + `columns`. Funnel COUNT:
  `count_path` (single non-negative integer). The two are mutually
  exclusive.
- `records_path` / `count_path` are dot-paths (`a.b.c`, no array
  indices, no wildcards). Each record is either an object (projected
  by `columns`) or an array (projected by position). Values are
  normalized to None/int/float/str; booleans coerce to int; nested
  objects are rejected as type violations.
- **Catalog (`type: http`)**: `base_url` (origin only; https
  required; http restricted to localhost with `allow_insecure`
  opt-in) / `allowed_endpoints` (method + path_prefix allowlist) /
  `auth_header_name` + `auth_header_template` (`{credential}`
  substituted with the secret) / `limits.max_response_bytes`.
- **URLs are canonicalized before allowlist match**: encoded
  separators (`%2f` / `%5c` / `%2e%2e` / NUL / control) are NOT
  decoded — they reject. Double or malformed encoding fails closed.
  `.` / `..` are resolved. `//`, backslash, absolute URLs, userinfo,
  and fragments reject. Matching is exact origin + **segment-boundary
  path prefix** (`/api/query` matches `/api/query/42` but NOT
  `/api/query-delete`) + method match.
- **Redirects reject** (prevents allowlist bypass). CLI displays a
  neutral `request_digest` (bundle-internal field stays
  `sql_digest` for Phase A compatibility).

#### Representative responses

- **Redash**: `POST /api/queries/{id}/results` →
  `records_path: "query_result.data.rows"`, each row is an object
  `{"user_id": .., "email": ..}`.
- **Kibana (Elasticsearch) search**:
  `records_path: "hits.hits"`, `columns: ["_id", "_score"]`.
  Specifying `_source` (an object) as a column rejects as a type
  violation (nested projection is out of scope).
- **Elasticsearch count**: `count_path: "count"` (from the `_count`
  endpoint response).

#### Memory model and `max_response_bytes` (assurance envelope)

- Fixed `Accept-Encoding: identity` — compressed transfer is not
  used (wire bytes == actual size, so `max_response_bytes`'s
  streaming cutoff is real. If the server replies with an encoding,
  reject via `Content-Encoding`).
- Reading chunks and checking `max_response_bytes` — cut off before
  the whole payload is held.
- **After JSON parse, both the document AND normalized rows sit in
  memory at the same time.** This is by design — keep
  `max_response_bytes` small enough for the memory budget (default
  recommendation: **8 MiB**). If large results are needed, "narrow
  the query" is the correct fix (this tool is for summarization /
  funnels, not bulk transfer).
- **Out of scope**: async job / polling (Redash query-execution
  jobs — one-shot JSON API only). Streaming JSON parsers. Static
  inspection of the response DSL (ES / Redash query bodies).

## `doctor` subcommand — pre-flight for connections and remote enforcement

Once remote connections are involved, you want to confirm "does the
read-only role actually connect" before running anything. `doctor`
diagnoses connection, read-only, and delivery without touching real
data (does not even run COUNT):

```bash
python3 skills/wiki/scripts/tool_query_run.py doctor \
  --wiki-root .wiki [--tool <id>] [--probe-write <tool-id>]
```

- Fixed columns: `tool / check / status(OK|NG|SKIP) / reason_code /
  hint` (`--format table|json`).
- **Read-only is decomposed into independent checks** (a single
  connection cannot distinguish session from role):
  - `session_readonly` — introspect read-only status inside the same
    kind of transaction as the real query.
  - `role_grants` — pg: verify that every allowlist relation has
    table-level INSERT/UPDATE/DELETE/TRUNCATE and column-level
    INSERT/UPDATE all `false`. mysql: parse `SHOW GRANTS` and
    require nothing but SELECT. **If role grants (MySQL 8 roles) or
    unparseable grant rows are involved, mark
    `role_grants_incomplete` as SKIP** (do NOT fail-open) — real
    permissions require `SHOW GRANTS ... USING` verification.
  - `role_write_denial` — NOT mechanically verified on normal runs
    (**SKIP** default).
  - `role_uninspected_privileges` — CREATE / TEMPORARY / EXECUTE
    etc. are out of mechanical verification (**SKIP** explicit).
- Others: `credential_resolves` / `tls` / `connectivity`; for http,
  `http_allowlist` (dry-run, no real send); `delivery_writable`
  (temp probe → immediate delete); `audit` (whether the doctor
  event can be written — a write failure counts as `audit_write_failed`
  NG, not silently swallowed by exit 0).
- **Exit codes**: 0 = no NG (SKIP does not fail) / 1 = any NG /
  2 = usage / 130 = interrupted. The summary always includes the
  SKIP count; if a required check is SKIP, it explicitly reports the
  unverified count. JSON emits `required_skips` with the check
  names.
- **`--probe-write <tool-id>` (double opt-in)**: for tools that
  declare `connection.canary_relation`, attempt an INSERT on the
  canary and confirm it is rejected. If canary is not declared,
  the probe rejects. Unknown tool / non-postgres-mysql /
  `--tool` mismatch is a usage error (exit 2). Only a **permission
  denied (NOT_AUTHORIZED)** rejection counts as OK — connection
  drop, timeout, missing relation, and other failures classify as
  `probe_inconclusive` NG (do NOT conflate any failure with
  write-denial success). Because this connector uses read-only
  sessions, the probe confirms rejection at the **overlay** of
  session read-only + role (does not separate role-only write
  rejection from the session — the primary information source for
  the role side is `role_grants`). MySQL canary relations require
  a transactional engine (InnoDB); non-transactional engines may
  leave probe writes even after rollback.
- **TLS check**: connection success indicates
  "verify-full / CA+hostname-verified TLS negotiation succeeded".
  When relaxation is declared, mark SKIP. When the connection
  itself fails, also SKIP — do NOT mark OK just from configuration.

## Writing Selection Recipes

Recipes are the explanation layer for "what to fetch and how to
decide". Store as a regular article under `{wiki_root}/concepts/`.

- **category**: `practices`. **tags**: `selection-recipe` + domain
  tags.
- Template: `skills/wiki/assets/selection-recipe-template.md`.
- **How to populate `source_refs` (required — page-template.json has
  `minItems: 1`)**: the Recipe's source is "the initial request +
  the judgment notes at first execution." Save the request summary
  and decision trail immutably at `{wiki_root}/raw/articles/{slug}.md`
  and reference that path (same as text ingest with wiki-ingest). If
  an existing raw source contains the rationale, reference that.
  Empty arrays fail lint (missing frontmatter / format violation).
- Must include:
  - Target definition (one line, business language) plus the
    judgment for mapping it into SQL conditions (why this condition
    represents it).
  - **Exclusion conditions and their rationale** (Why not — "why NOT
    include X" is the Recipe's most important information).
  - Funnel composition (what order of adding conditions the approver
    can most easily cross-check).
  - `tool_id`, primary tables, `key_columns`.
  - Execution log (date / plan_id / row count / elapsed time
    from request receipt to hand-off / observations).
- Must NOT include: connection info, limits (catalog copies go stale
  — reference by `tool_id` instead).

### Promotion criteria (when to make a Recipe)

1. **Always create on the second request of the same kind** (an
   in-session note is fine for the first).
2. Even on the first, create it if the exclusion condition required
   business knowledge (e.g. "exclude test accounts with
   `email LIKE '%@example.com'`") — that judgment is exactly the
   knowledge worth externalizing.
3. Append to the execution-log section every run. When the judgment
   changes, update the body (git carries the history).

## Registration wizard (`register` mode)

Interactive catalog-entry authoring: the user answers short questions
(AskUserQuestion — one topic per question, concrete options with a
recommended default first), the LLM drafts the JSON, the scripts gate
it. Never ask the user to write JSON by hand, and never accept secret
values through the chat.

Question sequence:

1. **Identity** — `tool_id` (kebab-case) and a one-line description
   of what the source holds (this line seeds the future Selection
   Recipe).
2. **Connector type** — sqlite / postgres / mysql / http. Routing
   hints: a browser-only admin UI with no API belongs to
   `wiki-browser-extract`, not here; a tool with a JSON API behind it
   (Redash / Kibana / internal API) is `http`.
3. **Connection** (per type):
   - `sqlite`: `connection.path` — DB file path relative to
     `{wiki_root}`.
   - `postgres` / `mysql`: `host` / `port` (default 5432 / 3306) /
     `dbname` / `user`. **Gate**: `user` must be the read-only role —
     if it does not exist yet, pause the wizard and walk through the
     role-setup procedure in the connector section above first.
     postgres adds `default_schema` (default `public`); optionally a
     `canary_relation` to enable `doctor --probe-write`. Set
     `credential_ref` (same name as `tool_id` is fine).
   - `http`: `base_url` (https origin), `allowed_endpoints`
     (method + path_prefix — register only the endpoints this use
     case needs), `auth_header_name` + `auth_header_template`, and
     `credential_ref`.
4. **Table allowlist** (SQL types only) — which tables does this use
   case actually read? Keep it minimal; suggest qualified names for
   pg/mysql. Endpoints already play this role for `http`.
5. **Limits** — offer the defaults (`max_rows` 10000,
   `max_result_bytes` 10 MiB, `max_cell_bytes` 64 KiB, `timeout_sec`
   60; `http` adds `max_response_bytes`, recommended 8 MiB) and ask
   only whether to tighten them.
6. **Delivery** — `delivery.allowed_dirs` (default
   `outputs/deliveries`). Create the directory if it does not exist
   yet — step 9's `delivery_writable` check fails on a missing dir.
7. **Draft + validate** — write the entry into
   `{wiki_root}/tools/catalog.json`, show the diff, run
   `catalog-validate`.
8. **Credentials** (remote types) — the user edits
   `{wiki_root}/.local/credentials.json` themself (git-ignored, mode
   ≤ 0600, key = `credential_ref`). Secrets never appear in the
   conversation, argv, or any file the LLM writes.
9. **Pre-flight** — run `doctor` for EVERY connector type (sqlite
   included: it checks connectivity, delivery, and audit; the
   credential check reports SKIP, which is normal). Add
   `--probe-write` when a canary relation was declared. Show the
   table to the user.
10. **Land it** — the catalog change goes through normal PR review
    before the first extraction. After the first case closes, propose
    a Selection Recipe article.

## Sample catalog setup

`sample-events-db` in `.wiki/tools/catalog.json` is **for shape only** —
it does not run as-is (the DB file and delivery destination do not
exist). To try it:

```bash
# 1. Create the DB fixture (match catalog's connection.path)
python3 - <<'EOF'
import sqlite3, pathlib
pathlib.Path(".wiki/data").mkdir(exist_ok=True)
conn = sqlite3.connect(".wiki/data/sample-events.sqlite3")
conn.executescript("""
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, email TEXT);
CREATE TABLE IF NOT EXISTS registrations (user_id INTEGER, event TEXT);
CREATE TABLE IF NOT EXISTS refunds (user_id INTEGER, amount INTEGER);
""")
conn.commit(); conn.close()
EOF

# 2. Create the delivery destination (match catalog's delivery.allowed_dirs)
mkdir -p .wiki/outputs/deliveries

# 3. Validate
python3 skills/wiki/scripts/tool_query_run.py catalog-validate --wiki-root .wiki
```

For a real-data tool, declare the path to an existing DB (or a
`base_dir`), keep `allowed_tables` minimal, and put it through PR
review.

## Modifying the catalog

The catalog is a git-managed execution contract. Any change (adding
tables, relaxing limits, adding delivery destinations):

1. Edit `.wiki/tools/catalog.json`.
2. Validate:
   `python3 skills/wiki/scripts/tool_query_run.py catalog-validate --wiki-root .wiki`.
3. **Go through normal PR / commit review** — a design principle is
   that editing a wiki article cannot change the safety perimeter.

After a catalog change, existing approved plans reject with
`catalog_digest_mismatch` (intended behavior).
