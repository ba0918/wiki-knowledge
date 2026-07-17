---
name: wiki-tool-query
description: >
  Constrained, audited ad-hoc extraction against catalog-registered data
  sources (sqlite / postgres / mysql / HTTP API), gated by a dry-run
  approval flow. Trigger phrases: "extract the compensation targets",
  "pull the target list from the DB", "ad-hoc aggregation", "tool query".
  The LLM consults a Selection Recipe article (practices) to build the
  SQL or request spec; prepare → human approve → execute delivers the
  result CSV.
---

# Wiki Tool Query

"Free planning + constrained execution" against catalog-registered data
sources. The LLM may draft the SQL, but a human approves a dry-run plan
(target definition, selection funnel, expected row-count range) before
execution.

**Supported connectors** (chosen by the catalog's `type`; approval flow,
audit, delivery, and single-use are the same for every type):

- `sqlite` — local DB file (read-only URI + PRAGMA + authorizer)
- `postgres` / `mysql` — remote DB (read-only role + static SQL check
  + session read-only)
- `http` — generic JSON API (Redash / Kibana(ES) / internal APIs — a
  request spec takes the place of SQL)

Details on per-connector writing conventions, read-only role setup,
TLS, request spec, and assurance envelope live in
[tool-query-guide.md](../wiki/references/tool-query-guide.md).
Pre-flight diagnostics use `doctor` (below).

**Resolving `wiki_root`**: read the `wiki_root:` field from `AGENTS.md`.
If missing, point the user at `wiki-init`.

**Never touch the data source directly** — no ad-hoc `sqlite3` /
driver connections, not even read-only schema peeks. Every access
(including COUNTs and schema discovery) goes through
`tool_query_run.py`, which enforces and audits it. Schema knowledge
comes from the catalog, the Recipe, or the requester.

**Prerequisite**: the target tool must be registered in
`{wiki_root}/tools/catalog.json` (git-managed). The catalog is the
execution contract's source of truth; wiki articles (Selection Recipes)
are the explanation layer — editing an article does not change the
connection target, allowlist, or limits (safety perimeter).

**If the tool is NOT in the catalog**, do not improvise a connection —
route to registration: draft a catalog entry with the user (connector
sections + "Sample catalog setup" in
[tool-query-guide.md](../wiki/references/tool-query-guide.md)), have
them place credentials in `{wiki_root}/.local/credentials.json` for
remote connectors, then run `catalog-validate` and `doctor`. The
catalog change lands through normal PR review before any extraction.

## Process

### 1. Consult a Recipe and build SQL

1. Find the Selection Recipe article that matches the request (
   `category: practices`, `selection-recipe` tag) via
   `{wiki_root}/index.md`, and read it. If none exists (first-time
   case), question the requester to nail down the target definition
   and exclusion rules.
2. Using the Recipe + user's request, write the main SQL and the
   **selection funnel COUNT SQL** (a row-count estimate for each
   condition added in turn).
3. Save the SQL to a file (a temp path under `{wiki_root}/.cache/` is
   fine — the bundle copies the bytes, so edits to your file after
   prepare are irrelevant).
4. Writing details:
   [tool-query-guide.md](../wiki/references/tool-query-guide.md).

### 2. Prepare (dry-run)

For SQL tools (sqlite / postgres / mysql):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/tool_query_run.py prepare \
  --wiki-root {wiki_root} --tool <tool_id> \
  --sql-file <main.sql> \
  --count-sql "<step-label>=<count1.sql>" --count-sql "<next step>=<count2.sql>" \
  --key-columns <col>... --expected-rows <min>:<max> --deliver-to <dir> --format json
```

For HTTP tools, pass a **request-spec file** (JSON) instead of SQL
(`--sql-file` is not accepted):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/tool_query_run.py prepare \
  --wiki-root {wiki_root} --tool <http_tool_id> \
  --request-file <main.request.json> \
  --count-request "<step-label>=<count.request.json>" \
  --key-columns <col>... --expected-rows <min>:<max> --deliver-to <dir> --format json
```

- `<step-label>` must not contain `=` — the option splits on the
  first `=`. Use plain-language labels ("all users", "active",
  "minus test accounts"), never SQL fragments like `status=active`.
- `--expected-rows`: derive the band from the Recipe's recorded row
  counts or the funnel's final COUNT estimate. It is the
  drift-detection line checked again at execute time.
- **COUNTs run before approval**: prepare's funnel COUNT queries go
  through the same enforcement as real execution (connector defense /
  allowed_tables or endpoint allowlist / timeout) and are audited. Say
  this to the user.
- The generated immutable proposal bundle
  (`outputs/toolquery-plans/{plan_id}/`) becomes the only executable
  artifact from that point on.
- Request-spec conventions (`records_path` / `count_path` / URL
  canonicalization / memory model) live in the guide.

### 3. Approval request (summary-first)

Present to the user in this order. Full SQL goes in a `<details>` fold:

```
Target: <one line>
inclusion / exclusion: <bullets>
Funnel: <label>: <count> → <label>: <count> → …
Expected rows: <min>–<max>
Delivery: <dir>
tool: <tool_id> / plan_id: <plan_id> / sql_digest: <digest>
```

Options are **exactly three** — no auto-approve default:

1. **Execute** → guide the user to run the approve command themself
   (below).
2. **Modify conditions** → fix the SQL and rerun prepare (produces a
   new plan_id). Report the old plan honestly: an unapproved plan
   stays draft and cannot run; **an approved plan within its TTL
   remains executable** — if the intent is to discard it, tell the
   user the enforcement is "don't run it."
3. **Cancel**.

### 4. Approve (the human runs it — the LLM does not substitute)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/tool_query_run.py approve \
  --wiki-root {wiki_root} --plan <plan_id> --approved-by <name>
```

- **Never run the approve command as the LLM.** Ask the user to run it
  themselves (e.g. via `! <command>`).
- Approve re-shows the summary (plan_id / tool / sql_digest / expected
  rows / delivery / expires_at / funnel) and requires `yes` on a
  confirmation prompt (TTY required, stderr). No hand-editing JSON.
- The approval expires 24 hours after prepare (`expires_at`).

### 5. Execute and completion report

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/tool_query_run.py execute \
  --wiki-root {wiki_root} --plan <plan_id> --format json
```

Completion template (fill from the execute JSON output):

```
✅ Execution complete
- Rows: <row_count> (within expected range <min>–<max>)
- manifest: duplicate keys <duplicate_key_count> / NULL <key facts> / csv_sha256: <digest>
- Sanitized cells: <sanitized_cell_count>
- delivery: <dir>/<run_id>/ (result.csv + manifest.json)
- published_at: <published_at> / plan_id: <plan_id>
```

If the execute JSON contains `warnings` (published audit event or
receipt logging failures), add a line to the template. Publish itself
succeeded — the audit gap is a Phase B reconcile target. Do NOT
overstate as "audited."

**Failure template — which to use**: on failure, first check
`{wiki_root}/outputs/toolquery-plans/{plan_id}/state.json` for
`status`:

- Still `status: approved` → the approval is unconsumed (verification
  matrix rejected, etc.). Fix the cause and **re-execute the same
  plan** within TTL.
- `status: consumed` → the approval is spent. Report as:

```
❌ Execution failed: <reason>
This plan's approval has been consumed (consumed = the approval was
spent — not that execution succeeded). Re-running requires a new
prepare → approval. Same conditions — re-prepare?
```

### 6. Record and Recipe promotion

- After a case closes, propose creating or updating a Recipe article
  (capture the decisions, exclusion rules, and any funnel changes).
  Template:
  `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/selection-recipe-template.md`.
  Promotion criteria:
  [tool-query-guide.md](../wiki/references/tool-query-guide.md).
- Record the request arrival time in the Recipe's execution-log
  section — this is the start of the elapsed-time measurement.
  The end is the audit log's `published` event. Time spent waiting on
  approval is deducted using the `approved` event.

## `doctor` (pre-flight for remote connections and enforcement)

After registering a remote DB or API, diagnose connectivity, read-only
role, and delivery:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/tool_query_run.py doctor \
  --wiki-root {wiki_root} [--tool <id>] [--probe-write <tool-id>] --format table
```

- Fixed columns: `tool / check / status(OK|NG|SKIP) / reason_code / hint`.
  Exit codes: 0 = required OK / 1 = NG / 2 = usage / 130 = interrupted.
- Read-only is decomposed into independent checks (`session_readonly`
  / `role_grants`). `role_write_denial` and permissions outside
  mechanical verification (CREATE / TEMPORARY / EXECUTE) are marked
  **SKIP** explicitly — do not silently pass over them.
- Doctor never touches result data (no COUNT). Audit is a
  plan-independent `doctor` event.
- `--probe-write` (double opt-in) verifies that INSERT to a canary
  relation is rejected. Guide has the details.

## Assurance envelope (answer honestly when asked — no overstating)

**Guaranteed**: detection and rejection of *accidental* changes to
SQL / request spec / delivery destination after approval; mix-ups;
staleness (execution after catalog change); replay. Read-only
violations, out-of-allowlist access, and limit overruns are rejected.

**Per-connector envelope**:

- **sqlite**: read-only triple defense via authorizer (the execution
  engine itself).
- **postgres / mysql**: guarantee is the **combination** of "DB-side
  read-only role (primary defense) + static SQL check + session
  read-only." Defense shrinks if the role is not SELECT-only — the
  guide's role-setup procedure is a prerequisite. MySQL read-only
  transactions have a hole allowing temp-table DML; close it on the
  role side by not granting `CREATE TEMPORARY TABLES`. **MariaDB is
  out of scope.**
- **http**: guarded by endpoint allowlist + method restriction +
  response-size cap. Query bodies (ES / Redash DSL) are NOT statically
  inspected. Async job / polling patterns are not supported (one-shot
  JSON API only).

**Not guaranteed (PoC limits)**:

- Detecting proposal / approval file tampering by a malicious process
  running as the same OS user (no privilege separation — no
  cryptographic authenticity proof). The authenticity of human
  approval is an **operational property** upheld by this skill's flow
  + the PR review of the git-managed catalog — not something the
  script proves.
- DB snapshot binding: the DB may change between prepare's COUNT and
  execute. The manifest's `data_as_of` compared against the expected
  row-count range is the drift-detection line.
- Credentials never appear in prompts, argv, stdout/stderr, audit, or
  errors, but file reads by another process running as the same OS
  user are not prevented.
- Delivery no-clobber assumes every writer goes through this script.

## Constraints (script-enforced)

- SQL tools: SELECT / WITH only (no multi-statements, no comment
  starts, no unknown function calls). Per-connector read-only defense
  + relation allowlist (`allowed_tables` in the catalog; pg/mysql get
  the sqlglot static gate).
- http: endpoint allowlist (method + segment-boundary path prefix) +
  URL canonicalization + redirect denial + response-size cap
  (`max_response_bytes`).
- Output caps: catalog `limits` (`max_rows` / `max_result_bytes` /
  `max_cell_bytes` / `timeout_sec`).
- Result data is handed to the delivery destination and not retained.
  The audit log (`outputs/toolquery-audit.jsonl`) is metadata only,
  no values.
- CSV is sanitized against formula injection (OWASP compliant).
