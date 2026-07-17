# Browser Extract (containment for browser-driven tools)

Separate line from Tool Query. SQL systems earn assurance through static
inspection + DB role — mechanical guarantees. Browser flows earn
assurance through containment + provenance with honestly scoped
guarantees. The two are not merged; they share only the catalog schema
convention and the audit JSONL format.

Skill entry point: `skills/wiki-browser-extract/SKILL.md` (thin router).
Approval is a human TTY operation. The LLM does not substitute.

## Approval model: seal-at-prepare

Not the SQL approve-then-execute model.

1. **prepare** — runs the flow, extracts artifacts, enforces the
   verification contract, seals the artifacts + manifest (SHA-256
   finalized), and records the seal hash in a `prepared` audit event.
2. **approve** (human, TTY) — re-derives the hash from the sealed
   artifact and manifest and fail-closed matches it against the audit
   anchor. Any mismatch = reject.
3. **execute** — delivery release only, from the sealed artifact. No
   browser rerun. Single-use.

Approval gates delivery only. The confidentiality boundary retreats to
"this machine" (honest scoping).

## Sources of truth

- Catalog: `.wiki/tools/browser-catalog.json`
  (schema: `.wiki/schema/browser-extract-catalog-schema.json`).
- Fixed flow code: `.wiki/tools/flows/{tool_id}.py` (PR review required).
  Flows never touch raw Playwright — they use the capability API only.

The LLM's degree of freedom is parameter injection only, validated by a
params meta-schema
(`.wiki/schema/browser-extract-params-schema.json`). Each parameter
requires `enum` / `pattern` / `maxLength`.

## CLI

```bash
python3 skills/wiki/scripts/browser_extract_run.py \
  {catalog-validate|prepare|approve|execute|doctor|login} \
  --wiki-root .wiki ...
```

Exit codes: 0 / 1 / 2 / 130 (same convention as Tool Query).

## Flow code assurance: three layers (honest scoping)

Structural containment of a malicious flow is not claimed. Assurance
rests on:

1. Catalog SHA-256 pin — mismatch = `flow_pin_mismatch`, execution
   rejected. Git is not consulted at run time.
2. Load-time AST static gate — rejects imports outside the allowlist,
   `exec` / `eval`, dunder access, and anything but a single `run`
   function.
3. PR review.

## Containment model

- Context-scoped request interception — allowlist is method +
  path-prefix + resource-type granularity.
- WebSocket denial.
- Service worker block.
- Redirect hop revalidation.
- `data:` / `blob:` denial.
- WebRTC disabled.
- Ephemeral user-data-dir, fresh context per run.
- URL canonicalization is shared with the HTTP connector.

## Verification vocabulary v1 (closed set)

Correctness: `filter_readback`, `row_count_range`, `selector_exists`.
Independent anchors: `export_metadata_match`, `ui_total_vs_file_rows`,
`tenant_id_match`, `primary_key_unique`. Completeness: `artifact_hash`.
Identity: `screen_fingerprint`.

Unknown vocabulary and insufficient evidence are fail-closed.

**Tier B1 requires at least one independent anchor** — enforced by
`catalog-validate`.

Tiers: B1 (export available) / B2 (DOM extraction, no completeness
guarantee) / B3 (OCR, out of v1 scope). The schema carries a per-tier
assurance matrix.

## Session state (credential-grade containment)

- Path: `.wiki/.local/browser-sessions/{tool_id}.json` (git-ignored).
- `0600` atomic write.
- `O_NOFOLLOW`.
- Full-segment symlink rejection.
- TTL.
- Bound to (tool, origin, account).

## Auth profiles

- `none`.
- `form` — declarative form config in `catalog.auth.login`; password
  via `credential_ref`; username is a non-secret catalog field.
- `form+totp` — TOTP secret via `totp_credential_ref`. TOTP is RFC 6238
  (source of truth: `lib/service/browser_login.py`).
- `human-assisted` — the `login` subcommand runs headed to capture the
  session. No extraction and no delivery path.

`prepare` resolves the session store first. If empty and the profile is
form-based, headless auto-login captures and writes `0600`.

## Approve: TTY required

`approve` requires `sys.stdin.isatty()`. Piped auto-approval exits 2.

The material presented to the human is the re-derivation of hashes from
the sealed artifact and manifest, filtered through the `prepared` audit
anchor comparison. Only after `yes` input is the approval committed.
The LLM does not substitute here.

## Doctor

Browser-independent checks: catalog resolve, flow pin, AST,
`params_schema`.

Under `BROWSER_EXTRACT_SMOKE`, real Chromium is used to judge
`login_reachability` / `selector_exists`. Data non-contact is not
claimed (honest scoping).

## Shared core

The approval infrastructure — single-use, TTL, and the
prepare-publish / approve-CAS / consume-CAS ordering with lock
discipline — is shared with Tool Query via `ApprovalService` in
`lib/service/tool_approval.py`.

Audit is generalized via registry injection in `lib/service/tool_audit.py`
and written to `outputs/browser-audit.jsonl`.

## Design docs, registration walkthrough, tier decision, reason-hint table, known limits

`skills/wiki/references/browser-extract-guide.md`.

## Dependencies

`playwright` — declared in `requirements-browser.txt` (opt-in), lower
bound 1.48 for `route_web_socket`. The main `requirements.txt` is not
polluted.

Browser binaries: `python -m playwright install chromium`.

Real E2E tests are opt-in via `BROWSER_EXTRACT_SMOKE` — skipped when
unset. Browser-independent decision logic (AST gate, allowlist match,
URL canonicalization, session containment, janitor, verification-
contract enforcement, seal-at-prepare audit-anchor match) runs on
every test invocation.

## Smoke / E2E execution

```bash
uv pip install -r requirements-browser.txt
python -m playwright install chromium
cd skills/wiki/scripts
BROWSER_EXTRACT_SMOKE=1 python -m pytest \
  lib/service/test_browser_flow_runner.py \
  test_browser_extract_e2e.py
```

Fixture: `lib/service/browser_fixture_server.py` (stdlib `http.server`
based). Covers login form, TOTP (stdlib `hmac`), tables with UI totals,
CSV export, and mutation routes for the false-success corpus.

Bootstrap notes: `browser-extract-guide.md` §17.
