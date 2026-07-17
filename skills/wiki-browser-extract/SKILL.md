---
name: wiki-browser-extract
description: >
  Extract data from catalog-registered browser-driven tools (B1: TSV/CSV
  export, etc.) with containment + provenance. This is Tool Query's
  parallel line for browser flows. Also the entry point for registering
  a new tool (walkthrough). Trigger phrases: "extract from the browser",
  "grab the table off the screen", "browser extract", "log in and
  export", "register a browser tool". Approval model is
  seal-at-prepare — prepare (extract + seal) → human approve (TTY) →
  execute (delivery release only).
---

# Wiki Browser Extract

"Contained extraction" against catalog-registered browser-driven
tools. A fixed flow drives an authenticated browser through a
capability API, undeclared traffic is contained by interception, and a
verification contract (closed vocabulary) detects false-success cases
(data that looks right but isn't).

This is Tool Query's **parallel line** — SQL systems earn mechanical
guarantees via static inspection + DB role; browser systems earn
honestly-scoped assurance via containment + provenance. The two share
only the catalog schema convention and the audit JSONL format. The
approval model is seal-at-prepare (NOT the SQL
approve-then-execute).

The source of truth for design rulings, the registration walkthrough,
tier decisions, the reason-hint table, known limits, and the bootstrap
procedure is
[browser-extract-guide.md](../wiki/references/browser-extract-guide.md).

**Resolving `wiki_root`**: read the `wiki_root:` field from `AGENTS.md`.
If missing, point the user at `wiki-init`.

**Prerequisites**: the target tool must be registered in
`{wiki_root}/tools/browser-catalog.json` (git-managed, schema:
`{wiki_root}/schema/browser-extract-catalog-schema.json`) and a fixed
flow (`{wiki_root}/tools/flows/{tool_id}.py`, SHA-256 pinned in
`flow.sha256`) must exist. The catalog + flow are the execution
contract's source of truth; Selection Recipe articles are the
explanation layer — editing an article does not move the safety
perimeter (connection target, allowlist, limits, verification
contract).

## What seal-at-prepare means (what approval gates)

- **Prepare completes the extraction inside an authenticated session
  and seals it.** By the time approval is asked for, the real data is
  already on this machine.
- **Human approval only gates delivery** (release off the machine).
  The confidentiality boundary retreats to "this machine."
- Approve re-derives the hash from the sealed artifact + manifest and
  fail-closed matches it against the `prepared` audit anchor
  (mismatch = reject). The preview stored in the spool is not
  trusted.
- **Read-only is NOT mechanically enforced** (honest scoping).
  Assurance rests on "don't act outside the declared flow" + audit
  provenance.

## Process

**Actor rules**: `login` (human-assisted) and `approve` are for the
human only. Everything else (`catalog-validate` / `doctor` / `prepare`
/ `execute`) is fine for the LLM to run.

### 0. Registration (first time — walkthrough)

If the user asks for a tool that isn't in the catalog, DO NOT reach for
an ad-hoc script or manual extraction. Route to this registration
walkthrough (cite guide §14-15 in your response to the user).
Registration is not something the LLM finishes alone — it requires a
walkthrough with the human AND an **independent reviewer** (an actor
distinct from the flow author — a different session's LLM or a human;
this is separate from the PR review that lands the catalog / flow):

1. **HTTP reduction gate (highest priority)**: check whether the
   export operation's underlying request can be reproduced by the
   HTTP connector. If it can, do NOT build a browser tool.
2. **Tier decision**: a TSV/CSV export button → B1 candidate (at
   least one independent anchor required). DOM extraction only → B2.
3. **Prerequisite check**: ask the user to prepare a dedicated,
   minimum-privilege account (no write permissions) — this is a
   precondition for B1/B2 registration.
4. Build the verification contract → independent reviewer (must reject
   every false-success case in the counterexample fixture) → doctor →
   full flow smoke.

Any change to the catalog or a flow (new or edit) goes through PR
review.

### 1. Catalog validation

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py catalog-validate \
  --wiki-root {wiki_root}
```

### 2. `doctor` (pre-flight)

Diagnose catalog integrity / flow pin / AST gate / `params_schema`
without extracting anything or producing artifacts. With
`BROWSER_EXTRACT_SMOKE` set, real Chromium probes too (login →
navigate → selector-exists). Not required before every prepare —
recommended after registration, before running a tool that has been
idle for a while, or when UI changes are suspected. Doctor does NOT
claim data non-contact (it has a login side effect — guide §16):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py doctor \
  --wiki-root {wiki_root} --tool <tool_id> --format table
```

### 3. `login` (human-assisted profile only — human runs it)

The `form` and `form+totp` profiles auto-login inside prepare, so
`login` is unnecessary. Only `human-assisted` profile requires it —
the human uses a headed browser to log in and capture the session
state:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py login \
  --wiki-root {wiki_root} --tool <tool_id>
```

- `login` has **no extraction path and no delivery path** (session
  capture + tool/origin/account binding only).
- Immediately after capture, validity is checked and the binding
  metadata + TTL are shown to the human (guide §10).

### 4. Prepare (extract + seal)

Run the flow to extract, enforce the verification contract, and seal
the artifacts + manifest into a bundle
(`outputs/browser-plans/{plan_id}/`). **Extraction completes before
approval**:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py prepare \
  --wiki-root {wiki_root} --tool <tool_id> \
  --param <key>=<value> --param <key>=<value> \
  --deliver-to <dir> --format json
```

- `params` values are validated against the catalog's `params_schema`
  (bounded by `enum` / `pattern` / `maxLength`).
- `--deliver-to` accepts only directories declared in the catalog's
  `delivery_allowed_dirs` (undeclared paths reject with
  `delivery_not_allowed`). If the user's requested destination is
  undeclared, a catalog change (PR) is required.
- If any verification-contract clause fails, prepare rejects (does
  not seal false-success data).

### 5. Approval request (summary-first)

Present the prepare output (plan_id / row_count / artifact_digest /
expires_at) and a manifest preview to the user. Options are **exactly
three** — no auto-approve default:

1. **Approve** → guide the user to run the approve command themself
   (below).
2. **Modify conditions** → fix `params` and rerun prepare (new
   plan_id; the old bundle expires with its TTL).
3. **Cancel**.

### 6. Approve (the human runs it — the LLM does not substitute)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py approve \
  --wiki-root {wiki_root} --plan-id <plan_id> --approved-by <name>
```

- **Never run approve as the LLM.** Ask the user to run it themself
  (e.g. `! <command>`). Even if the user explicitly asks you to "do
  it for me," refuse — a request is not a substitute for consent.
- If the user asks something in the middle of the wait (a
  substitution request, a follow-up question, a modification), keep
  the three-option list (approve / modify + re-prepare / cancel) at
  the end of your reply.
- `--approved-by` is the approver's name (recorded in the bundle and
  audit). The user fills this in themself.
- Approve re-derives the hash from the sealed artifact + manifest,
  matches it against the `prepared` audit anchor, and only then
  presents the approval material on a TTY: identity, an explicit
  statement that read-only is not enforced, an explicit statement
  that approval only controls distribution (extraction is already
  done), the hash, a preview, row count + anchor match, seal time +
  TTL remaining.
- Approval is single-use (`consumed` = approval consumed). TTL is 24h
  from prepare.

### 7. Execute and completion report

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py execute \
  --wiki-root {wiki_root} --plan-id <plan_id> --format json
```

- The LLM runs execute **after** the user reports back that approval
  is done.
- Execute is **delivery release from the sealed artifact only** (no
  browser rerun). It re-matches the seal hash before delivering.
- Completion report: row count / CSV + manifest location
  (`<dir>/<run_id>/`) / run_id / plan_id.

### 8. Record and Recipe promotion

After a case closes, propose creating or updating a Selection Recipe
article (`category: practices`, `selection-recipe` tag) — capture
decisions, exclusion rules, and any changes to the verification
contract.

## Assurance envelope (answer honestly when asked — no overstating)

**Guaranteed**: byte-identical distribution (what the human sees is
what gets released), false-success detection via the verification
contract (filter not applied, wrong tenant, dropped pagination,
partial fetch, duplicates), containment (blocked and audited
undeclared origin / method / path), rejection of post-seal
modification (`seal_mismatch`).

**Not guaranteed (honest scoping, guide §16)**:

- **Mechanical enforcement of read-only** (assurance is "don't act
  outside the declared flow" + provenance; a dedicated
  minimum-privilege account is a prerequisite).
- Structural containment of a malicious flow (in-process Python;
  hash pin + AST gate + PR review are for accident prevention and
  review support).
- Write-protection of the audit JSONL itself (raises the bar so the
  attacker must also tamper with audit history — but does not
  prevent it).
- After prepare and before approval, data is already on this
  machine (confidentiality boundary retreats to the machine).

## Dependencies and tests

- Playwright is declared opt-in in `requirements-browser.txt` (lower
  bound 1.48 = `route_web_socket`; the main `requirements.txt` is
  not polluted). Install:
  `uv pip install -r requirements-browser.txt` +
  `python -m playwright install chromium` (guide §17).
- Smoke / E2E requiring real Chromium are gated by
  `BROWSER_EXTRACT_SMOKE` (skipped when unset). Browser-independent
  decision logic (AST gate, allowlist match, URL canonicalization,
  session containment, janitor, verification-contract enforcement,
  seal-at-prepare audit-anchor match) runs on every test invocation.
