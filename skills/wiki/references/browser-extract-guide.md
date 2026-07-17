# Browser Extract Guide — Design Rulings, Registration Walkthrough, Tier Decision

Design reference and rulings document for the wiki-browser-extract line
(browser-extract's parallel system). The execution contract's source of
truth is the git-managed **catalog** (the `type: browser` entries in
`{wiki_root}/tools/catalog.json`; schema:
`{wiki_root}/schema/browser-extract-catalog-schema.json`) plus **fixed
flow code** (`{wiki_root}/tools/flows/{tool_id}.py`, PR review
required). Wiki articles (Selection Recipes) are the explanation
layer — natural-language editing of an article cannot move this safety
perimeter.

This document fixes "the decided points" that implementation steps
(Step 2–7) reference. Do NOT leave Open Questions — entering
implementation with ambiguity invites false-success (data that looks
right but isn't).

---

## 0. Most important ruling — seal-at-prepare's approval integrity (audit-anchor match)

The last WARN that remained through the refine phase. Fix this first.

### Problem

In the seal-at-prepare model, prepare completes extraction inside an
authenticated session and seals artifacts + manifest into a
containment bundle (spool = `outputs/browser-plans/{plan_id}/`).
Approve is the human viewing a preview on a TTY. **The spool is an
agent-readable AND writable local file**, so a coordinated
post-prepare pre-approve rewrite of artifact / manifest / preview
inside the spool could forge "an already human-approved delivery."

### Ruling

The approve TTY view **does NOT trust the preview file saved by
prepare at all**. At display time:

1. **Re-compute the hash and preview (first N rows) from the sealed
   artifact itself**.
2. Fail-closed compare the re-derived hash against the **anchor
   outside the spool = the seal hash already recorded in the
   `prepared` audit event**.
   - Match scope: **both the artifact hash and the manifest hash**.
   - The `prepared` audit event records `artifact_digest` (SHA-256 of
     the result body) and `manifest_digest` (SHA-256 of `manifest.json`
     bytes).
3. **Mismatch is "reject," not "reflect in display"** (exit 1, with a
   dedicated reason `seal_mismatch` — analogous to `readback_mismatch`).
   - Manifest content (row count, anchor match results) records
     execution against the live page at prepare time and cannot be
     re-derived from the artifact. **This audit-anchor match is the
     only thing guaranteeing its integrity.** So manifest tampering
     also only passes as "reject." An implementation that reflects
     forged anchor results into the display "with a verified face"
     **fails**.
4. Only display on match, and record the pinned hash in the `approved`
   audit event as well.
5. Execute matches the approval record (`sealed_artifact_digest` /
   `sealed_manifest_digest` in `state.json`) **and** the `prepared`
   audit anchor **both** before releasing delivery.

This closes the "rewrite artifact + manifest consistently after prepare
so a human-approved delivery is established" path.

### Accepted residual (known limits)

The audit JSONL (`browser-audit.jsonl`) itself is also in an
agent-readable environment. If the attacker also rewrites the digest
in the `prepared` event, the match passes. This ruling **raises the
bar to "the attacker must also tamper with audit history"** and no
further. This is on par with the residual accepted by tool-query
family (audit log writability) — recorded in this guide's "Known
limits" section. OS-layer append-only / off-host aggregation are
future options.

### Honest scoping of the trust boundary (trade-off)

Unlike SQL systems (approve-then-execute), **seal-at-prepare's human
approval does not gate data access**. Prepare completes extraction
inside an authenticated session before approval, and the sealed
bundle becomes an agent-readable local file. Approve only guards
**delivery** (release off the machine); the confidentiality boundary
retreats to "this machine."

- Rejected alternative: **two-step approval** (approve → extract →
  re-approve) — doubles friction, and the first-step approval
  becomes ceremonial because it lacks real data.
- Rejected alternative: **approve-then-execute** (borrowing the SQL
  pattern) — approval material lacks real extracted data, and
  TOCTOU during the prepare→execute gap (up to 24h) reappears as
  data / permissions / UI / session drift.
- Adopted because: fixing "approval subject = distribution artifact
  identity" by hash guarantees "what the human saw is byte-identical
  to what leaves." B2's "human verification required" also becomes
  the approve confirmation point — pre-verification publish
  structurally cannot happen.

Cheap guards that reinforce the seal-at-prepare consequence:

- Make `prepared` a **first-class audit event with `row_count`**
  (extraction fact is recorded even without approval).
- Add a per-tool **unapproved-bundle count cap**
  (`catalog.limits.max_unapproved_bundles`) — prepare rejects when
  exceeded.
- Include unapproved bundles in **the TTL janitor's collection
  scope** (no lingering sensitive spool).

This ruling is also stated in the tier assurance matrix (§5) and the
approve TTY text (§10).

---

## 1. System-separation stance

Do **NOT** integrate as `type: browser` inside the existing tool-query.
The assurance level differs:

- SQL system = static gate + DB role → **mechanical guarantee**.
- Browser system = containment + provenance → **NOT enforced**;
  honest scoping.

Implement as a **parallel system that shares only the catalog schema
convention and the audit JSONL format**. Reuse shared service layers
(approval bundle, single-use consume, TTL, delivery, path
containment, credential resolution) as imports of the existing
modules — do NOT re-implement (see §11 for reuse boundaries).

---

## 2. Enforcement mechanism — why capability API + AST gate instead of a restricted DSL

### Assurance statement (honest scoping)

Fixed flow code is in-process Python. **Structural containment of
Python cannot be achieved**
(`().__class__.__bases__[0].__subclasses__()` etc. — language
mechanisms cannot be closed off). Therefore **do NOT claim a
structural boundary against malicious flows.** Assurance rests on
three layers of **accident prevention and review support**:

1. **Catalog SHA-256 pin** (mismatch = execution rejected).
   - The browser catalog entry declares the flow file's content hash
     in `flow.sha256`.
   - At load time, the runner reads the flow file and matches the
     hash. Mismatch rejects as `flow_pin_mismatch`. "Reject
     untracked code" is a rephrasing of this hash mismatch —
     **git is NOT queried at run time** (git-independent; the hash
     is the sole source of truth).
2. **Load-time AST static gate** (allowed grammar defined in §3).
   - Reject `import` / `from import` statements, `exec` / `eval`
     calls, and dunder attribute access (`__globals__` etc.).
3. **PR review** (any change to flows / verification contract must
   go through human review).

### Rejected alternatives

- **record/replay**: replay a recorded operation sequence. Brittle
  under dynamic DOM (loading, virtualized); cannot type parameter
  injection; cannot own a false-success detection vocabulary.
- **Restricted DSL** (a custom flow-description language): worst
  expressiveness vs implementation-cost trade-off. Every time real
  tools need control (pagination, conditional branching), the DSL
  itself bloats into a second programming language whose
  parser/evaluator becomes a new attack surface. Narrowing the
  allowed grammar with Python's AST gate reuses the existing type
  system and locator API, and is more reviewable.
- **In-flow assertion callback** (writing verification logic inside
  the flow): flow + verification written by the same LLM embeds the
  same misunderstanding in both (common-cause failure).
  Verification is separated as a closed vocabulary + independent
  reviewer.

### Conclusion

**Flow = written only against the capability API (typed operation
surface) / Verification = closed vocabulary.** Flows do not touch
raw Playwright. Additions to the capability set or the verification
vocabulary both go through the same PR discipline (on par with
adding a connector).

---

## 3. Capability API v1 (closed set) and AST allowed-node set

### Capability API v1 (provided by `browser_flow_runner.FlowContext`)

Flow is written as a single function `def run(ctx, params)`. `ctx` has
only these typed methods:

| capability | Meaning | Origin / param handling |
|---|---|---|
| `ctx.goto(route_id, **path_params)` | Navigate to a catalog-declared named route | Origin always from catalog. `path_params` canonicalized before embedding. Direct `page.goto(param)` is not allowed |
| `ctx.get_by_role(role, name=None, exact=False)` | Get a locator by role + accessible name | `name` uses value binding (no string interpolation) |
| `ctx.get_by_label(text)` / `ctx.get_by_text(text)` | Get a locator by label / text | Same |
| `ctx.fill(locator, value)` | Fill an input | `value` is a validated params-derived value |
| `ctx.click(locator, *, role, name)` | Click. `role + accessible name` are required kwargs | Selector-only click is not allowed (§7 destructive-op suppression) |
| `ctx.wait_stable(predicate)` | Wait until a stability predicate (§4) is satisfied | No raw `sleep` |
| `ctx.read_text(locator)` | Read the locator's text (for readback) | Extracted bytes are untrusted |
| `ctx.download(trigger_locator, *, role, name)` | Capture a click → download. Saved with a runner-generated random name | Server-supplied `filename` is not used (§retention) |
| `ctx.expect_row_count(locator)` | Count rows (for `row_count_range`) | — |

Capabilities are a closed set in v1. Additions go through PR. Flows
can write control flow (`if` / `for`); repetition (pagination, etc.)
is intended to shift to capability primitives
(`ctx.paginate(...)` planned). v1 permits simple `for` (§AST).

### AST allowed-node set (`browser_flow_runner` load-time gate)

Before loading a flow file, `ast.parse` it and inspect the nodes with
an **allowlist scheme**. Any node outside the list rejects the file
with `flow_ast_violation` (at load time, browser-independent).

**Allowed nodes** (this list is synced with `_ALLOWED_AST_NODES` and
mechanically verified in tests):

- Module structure: `Module`, a single `FunctionDef` (named `run`,
  args `ctx, params`).
- Statements: `Assign`, `AnnAssign`, `AugAssign`, `Expr`, `Return`,
  `Pass`, `If`, `For`, `While`, `Break`, `Continue`, `With` (for
  `ctx`'s context manager).
- Expressions: `Call`, `Attribute` (dunder names rejected), `Name`,
  `Constant`, `Compare`, `BoolOp`, `UnaryOp`, `BinOp`, `Subscript`,
  `Index`, `List`, `Tuple`, `Dict`, `Set`, `keyword`, `Starred`,
  `Slice`, comparison / operator nodes, `comprehension`.
- Argument nodes: `arguments`, `arg`, `Load`/`Store`/`Del` context.

**Explicitly rejected nodes** (paired with negative tests):

- `Import` / `ImportFrom` (blanket module-import ban — capabilities
  only through the argument `ctx`).
- Nested `FunctionDef` / `Lambda` / `AsyncFunctionDef` (flow is a
  single `run` only).
- `ClassDef`.
- `Global` / `Nonlocal`.
- `Attribute` where the name starts and ends with `__` (dunders —
  `__globals__` / `__class__` / `__subclasses__` etc.).
- `Call` targeting `exec` / `eval` / `compile` / `__import__` /
  `open` / `getattr` / `setattr` (name-based callee reject list).

The AST gate is NOT "complete prevention of malicious code" (honest
scoping). Dunder rejection and import rejection close **obvious
escape routes and accidents**, and support review.

---

## 4. Verification vocabulary v1 (closed set)

What can't be predetermined is the **combination**, not the
vocabulary. Verification outside the vocabulary is added by engine
PR (= same discipline as adding a connector). Unknown vocabulary
**fail-closed rejects** at catalog-validate.

### Correctness (false-success detection)

| check | Meaning | Role |
|---|---|---|
| `filter_readback` | Read back the UI's filter display (period, conditions) and match it against `params` | Detects filter-not-applied false success |
| `row_count_range` | Extracted row count within the expected range (`{min, max}`) | Detects partial fetch / dropped pagination |
| `selector_exists` | The declared locator actually exists (also used by doctor smoke) | Detects UI drift |

### Independent anchors (required for B1, §5)

Independent oracles that don't depend on the same DOM interpretation
as the selectors. At least one of:

| check | Meaning |
|---|---|
| `export_metadata_match` | Metadata inside the export file (generated period, filter) matches params |
| `ui_total_vs_file_rows` | Total-count displayed in UI matches the export file's row count |
| `tenant_id_match` | Tenant / account ID in extracted data matches the catalog-declared account |
| `primary_key_unique` | No duplicates on the primary key (detects partial joins, double fetch) |

### Completeness (tamper detection)

| check | Meaning |
|---|---|
| `artifact_hash` | Tamper detection between bundle and delivery. **No known baseline hash exists for dynamic data**, so this does NOT catch selector drift (completeness only) |

### Identity (screen sameness)

| check | Meaning |
|---|---|
| `screen_fingerprint` | Fingerprint based on Playwright's accessibility / DOM snapshot. Detects same-shape screens on a different tenant. **Bespoke pixel hashes are NOT adopted** (rendering variance produces false positives) |

### Stability vocabulary (stability predicates — anti-DOM-nondeterminism)

"Deterministic" describes the decision rule, not the DOM. To avoid
mistaking loading, stale DOM, or virtualized tables for normal
state, provide these as `ctx.wait_stable(...)` predicates. **Raw
`sleep` is banned**:

| predicate | Meaning |
|---|---|
| `navigation_settled` | Navigation complete (`networkidle`-equivalent + URL settled) |
| `loading_indicator_gone` | The declared loading-indicator locator disappears |
| `readback_stable` | The declared locator's text matches N times consecutively (value settles) |
| `row_count_settled` | Row count stays fixed within a window |

Locale / timezone / viewport are fixed at context creation (§7).

### Vocabulary responsibility summary

- `filter_readback` / `row_count_range` + independent anchors =
  **correctness** (false-success detection).
- `artifact_hash` = **completeness** (tamper detection — NOT
  correctness).
- `screen_fingerprint` = **identity** (different-screen detection).

---

## 5. Tier classification and assurance matrix

Do NOT use the single label "B1 = high assurance." Carry per-tier
assurance in a **machine-readable matrix** in the schema; the
manifest and audit output unguaranteed items too.

| tier | Definition | integrity | identity | filter correctness | completeness | human verification |
|---|---|---|---|---|---|---|
| **B1** | TSV/CSV export + at least one independent anchor | ○ (artifact_hash) | ○ (screen_fingerprint) | ○ (filter_readback) | ○ (row_count_range + anchor) | approve |
| **B2** | DOM extraction (no completeness guarantee) | △ | ○ | ○ | ✗ (silent-truncation detection only; NOT guaranteed; human verification required) | approve (required confirmation point) |
| **B3** | OCR | — | — | — | — | Out of v1 scope (tier definition only) |

- **B1 required condition**: the verification contract must include
  **at least one** independent anchor (`export_metadata_match` /
  `ui_total_vs_file_rows` / `tenant_id_match` /
  `primary_key_unique` etc.). Tools that cannot form an independent
  oracle cannot claim B1 (fall back to B2). This is mechanically
  enforced by catalog-validate (Step 4).
- **A dedicated minimum-privilege account is a precondition for
  B1/B2 registration** (not an Open Question) — register with an
  account that does NOT hold unnecessary write permissions.
- **Pass the HTTP reduction gate first** (§14).

---

## 6. Auth profile and session-state binding

| profile | Meaning | Secret |
|---|---|---|
| `none` | No authentication | — |
| `form` | Form login | `credentials.json` |
| `form+totp` | Form + TOTP | `credentials.json` (TOTP secret) |
| `human-assisted` | Human login → hand off session state | Captured by the `login` subcommand |

### Form / form+totp auto-login configuration (catalog `auth`)

For `form` / `form+totp`, prepare auto-form-logs-in when resolving
the session. Declare these under catalog `auth` (value-binding
selectors only — no string interpolation):

- `username`: login identifier (**non-secret** — catalog field).
- `credential_ref`: ref that resolves the password
  (`.local/credentials.json`).
- `totp_credential_ref`: ref that resolves the TOTP secret (base32,
  required for `form+totp`).
- `login`: `{route, username_label, password_label, submit_role,
  submit_name, success_url_contains}` (+ `totp_label` for
  `form+totp`). Completion is detected via post-login URL
  (`success_url_contains`) — no transition → timeout →
  `session_expired` (the wrong-creds detection line).

Session resolution order
(`browser_extract_run._resolve_session_state`): profile=none → no
session / form family → reuse valid bound session if present;
otherwise headless form login captures, saves at 0600, and reuses /
human-assisted → store required (else hint `login` subcommand).
TOTP is RFC 6238 (`browser_login.totp_code`, stdlib `hmac` is the
source of truth; fixture re-exports).

### Doctor's Chromium probe (under `BROWSER_EXTRACT_SMOKE`)

Doctor runs browser-independent checks (catalog resolve / flow pin /
AST / `params_schema`), and under smoke, additionally does OK/NG
judgment on real Chromium: `login_reachability` (reachability of the
login route) and `selector_exists` (existence of the login form's
username / password / submit). It does NOT extract or produce
artifacts. Data non-contact is NOT claimed (honest scoping, §16).

- Session state is at credential-grade containment (0600, TTL,
  re-auth policy).
- **Bound to (tool, origin, account)**: the state file carries
  binding metadata (`tool_id` / `origin` / `account`) and is matched
  against catalog declarations at run time. Bringing in generic
  browser profiles or sharing profiles across tools is **banned**
  (binding mismatch rejects as `session_binding_mismatch`).
- Writes are 0600 atomic (O_NOFOLLOW / umask). Do NOT let Playwright
  write its default (0644 plain-text JSON).

### Login-time allowlist surface (ruling)

Real-site logins that cross SSO / IdP / captcha / CDN break under a
method + path-granularity allowlist (IdP origins cannot be enumerated
in advance). Ruling: **auth profile may declaratively extend the
allowlist via the catalog** — carry `auth.login_origins` (additional
origin allowlist valid only during login) as a schema concept.
Separate from the extraction-phase allowlist (`origin_allowlist`);
`login_origins` applies only during the `login` subcommand and the
`form` login procedure. Not surfaced by v1's local fixture, but
carried in the schema early (Step 3).

---

## 7. Containment model (interception spec)

"Undeclared traffic blocking" does NOT stand up under a naive
`page.route()` application. Fix the following as implementation
spec:

- **First-line defense is the flow-code discipline**: navigation
  origin is always constructed from a catalog-declared named route,
  and parameters are supplied only as validated path / value.
  Direct `ctx.goto(param)` param→origin coupling is structurally
  forbidden by the capability API. **Interception is the second
  defense line.**
- **Interception is context-scoped**:
  `context.route('**/*', ...)` (page scope misses popups / new
  tabs). Every request is matched against the catalog allowlist;
  undeclared traffic aborts + is audited (`origin_blocked`).
- **Allowlist granularity**: narrow beyond origin down to
  **method + path prefix + resource type**. Canonicalize URLs before
  matching (reject userinfo; IDN / punycode normalize; strip
  trailing dot; make port explicit; reject encoded separators).
  Canonicalize borrows the http connector's
  `_canonicalize_segments` / origin normalization style.
- **Suppress destructive operations inside the same origin**:
  state-changing requests (POST etc.) allow only what the catalog
  explicitly declares (login / TOTP / export job creation). Click
  targets require role + accessible name as a compound condition
  (`ctx.click`'s required kwargs). Primary defense is stripping
  write permissions from the dedicated account.
- **Service workers off**: `service_workers='block'` at context
  creation.
- **WebSocket deny-by-default**: `route_web_socket` rejects all WS
  (do NOT add WS-required tools to v1's scope).
- **Redirects revalidate each hop**: redirect targets are matched
  against the origin allowlist; undeclared aborts. Blanket rejection
  is unrealistic — per-hop revalidation.
- **Reject navigation to `data:` / `blob:`** (no network request —
  interception does not fire).
- **Disable WebRTC via launch args**:
  `--webrtc-ip-handling-policy=disable_non_proxied_udp` etc. The
  residual is a known limit (§Known limits, on par with DNS
  rebinding).
- **Launch profile isolation**: headless + per-run ephemeral
  user-data-dir + no remote debugging port + fresh context per run.
  The only exception is the `login` subcommand (headed but
  structurally cannot extract or deliver).
- **Fixed context**: locale / timezone / viewport fixed at context
  creation (suppresses DOM non-determinism).

---

## 8. Parameter-injection safety rules

"JSON Schema validation + escaping" alone does NOT prevent selector
injection (adversarially induced false success):

- **String interpolation into selectors is banned**: flows use
  parameters only as locator value bindings
  (`ctx.get_by_role(name=...)` / `.filter(has_text=...)`
  equivalents). `f-string` embedding into XPath / CSS strings
  rejects mechanically at registration review (a review-checklist
  item + AST gate assistance).
- **`params_schema` requires strict per-value constraints**: each
  parameter must have one of `enum` / `pattern` / `maxLength`
  (arbitrary strings not permitted by default). Enforced by the
  meta-schema (`browser-extract-params-schema.json`). Do NOT expose
  types for selectors / JS / arbitrary URLs / arbitrary paths.
- **URL embedding**: origin from the catalog; parameters as path
  segments / query values, canonicalized (reuses http connector's
  encoded-separator rejection and double-encoding fail-closed).

---

## 9. Seal-at-prepare state machine

### Transition table

```
prepared(sealed) → approved → delivering → delivered
       │                          │
       │                          └──(unknown failure, hash matches)──> delivering (resume)
       │                          └──(unknown failure, hash mismatch)─> failed (re-prepare required)
       ├──(TTL exceeded)─────────────────────────────────────────────> expired (janitor collects)
       └──(TTL exceeded while unapproved)────────────────────────────> expired (janitor collects)
```

| status | Meaning | Next transition |
|---|---|---|
| `prepared` | Extraction complete, sealed (has `sealed_artifact_digest` / `sealed_manifest_digest`) | Approve → `approved` |
| `approved` | Human-approved (pin hash recorded) | Execute → `delivering` |
| `delivering` | Delivery in progress (persisted via CAS) | Success → `delivered`; unknown failure → conditional resume or `failed` |
| `delivered` | Delivery complete (terminal) | — |
| `failed` | Unrecoverable (terminal, re-prepare required) | — |
| `expired` | TTL exceeded (terminal, janitor collects) | — |

- Transitions **persist via CAS** (following the existing
  `state.json` durable-write style).
- **Delivery-mid unknown failures do NOT auto-retry**. Only when
  the sealed artifact's hash matches can `delivering` resume (else
  `failed` → re-prepare).
- `prepared` is a **first-class audit event carrying `row_count`**
  (the extraction fact is recorded even without approval).

### Approval is single-use, TTL 24h

Approve is single-use (`consumed` = approval consumed), TTL 24h.
Reuses tool-query's `consume_transition` / `is_expired` /
`compute_expires_at` (§11).

---

## 10. Approve TTY prompt and reason codes

### Approval material (TTY display, LLM substitution banned)

Approve, after passing §0's re-derivation + audit-anchor match,
presents to the human:

- Which identity / live session was used to acquire the data.
- **Read-only is not enforced** — stated explicitly.
- **Approval only controls distribution; extraction is already
  complete** — stated explicitly.
- The sealed artifact's hash (the re-derived value) and preview
  (first N rows + column → source mapping).
- Row count + independent-anchor match results.
- **Seal time (`extracted_at` in manifest), elapsed time, TTL
  remaining**.
- On mid-run failure, next step is re-prepare.

### Preview rendering rules (treat as untrusted bytes)

Extracted data is untrusted bytes. Do NOT let the approval prompt
be spoofed:

- Non-printing / ESC characters shown escaped (terminal-escape
  injection defense).
- Width-aware clipping considering East Asian Width.
- Explicit truncation markers for rows / columns.
- Default first-N rows (`PREVIEW_ROWS = 10`).

### Browser reason codes + hint table (what / why / next)

Each reason carries "what happened / why / next step." **Location
information policy**: step index, capability name, and check id are
git-managed flow / catalog identifiers and do NOT break sanitize
invariants — attachable. Runtime values (URL, selector value, DOM)
are **not attachable**.

| reason code | what | why | next |
|---|---|---|---|
| `selector_not_found` | Expected locator not found | UI change or flow error | Run doctor → flow-fix PR |
| `ui_drift` | Screen structure drifted from doctor baseline | UI change | Run doctor → flow-fix PR |
| `session_expired` | Session state expired | TTL exceeded or server-side expiry | Re-auth (`login` or `form`) |
| `session_binding_mismatch` | Session doesn't match tool / origin / account | Wrong profile brought in | Recapture with correct session |
| `origin_blocked` | Request to undeclared origin / method / path | Flow error or attack | Flow-fix PR / allowlist-revision PR |
| `readback_mismatch` | `filter_readback` disagrees with params | Filter not applied | Flow-fix PR / re-check params |
| `seal_mismatch` | Re-derived hash disagrees with audit anchor | Bundle tampered post-prepare | Re-prepare (do NOT approve) |
| `flow_timeout` | Hard wall-clock timeout exceeded | Delay or infinite wait | Flow-fix PR / timeout revision |
| `bundle_cap_exceeded` | Unapproved-bundle count over cap | Approval backlog | Approve or expire outstanding bundles |
| `flow_pin_mismatch` | Flow SHA-256 disagrees with catalog declaration | Untracked code | Catalog-update PR / restore the flow |
| `flow_ast_violation` | AST gate violation (import / exec / dunder etc.) | Forbidden syntax | Flow-fix PR |
| `internal_error` | Unclassifiable catch-all | Unexpected exception | Check logs, file an issue |

Route each reason's hint from this table into the CLI output
(Step 6).

### Login completion detection and validation

- Completion signals: post-login URL detection or selector
  detection, plus **TTY-Enter-wait fallback**, plus timeout.
- Immediately after capture, validate state validity with a
  doctor-equivalent login-reachability check (do NOT capture
  unauthenticated and let it surface later as `session_expired`).
- On success, display the binding metadata (tool / origin /
  account) and TTL to the human.

---

## 11. Service-layer reuse boundary (symbol granularity)

Module-wide "reuse vs re-implement" cannot draw the boundary. Ruling:

### Import as-is

- `tool_delivery` (CSV neutralization / staging-publish).
- `tool_paths` (symlink-rejecting path containment).
- `tool_catalog.load_credential` (wiki_root + ref only, no SQL
  coupling — the catalog parser body is NOT reused).
- `lib/domain/tool_query` pure predicates (`consume_transition` /
  `approve_transition` / `is_expired` / `compute_expires_at` /
  `sha256_hex` / `parse_plan_id` / `build_plan_id`).

### Extract and share first (Step 2)

The single-use / TTL enforcement body (fail-closed CAS sequence
under the plan lock: read state → evaluate matrix →
`execute_attempted` audit → consume → durable state write) is
currently inside `tool_query_runner.execute`. Extract it as a
connector-independent approval-lifecycle service
(`tool_approval.py`) and use it from both SQL and browser sides.
To avoid double-implementation and divergence in security core,
extract — do NOT re-implement.

- **Parameterize the state machine**: `tool_approval` takes the
  status set and transition table as arguments (SQL default =
  `draft/approved/consumed` unchanged). Add a transition-table-driven
  general transition function to the domain; keep the existing
  `consume_transition` / `approve_transition` as specializations.
- **State-record codec / per-status invariant validation are also
  adapter injection surfaces**. Existing `state_from_json_dict`
  hard-codes draft/approved/consumed × field invariants; the
  browser's state schema (seal hash, delivery-resume metadata)
  needs a different codec. Do NOT bake the SQL PlanState shape into
  the stub adapter's contract test.
- Between the shared core and browser adapter, use a **versioned
  interface** (bundle-schema version + state-transition semantics
  fixed). Place a cross-SQL/browser contract test.
- Browser transitions
  (`prepared→approved→delivering→delivered/failed/expired`,
  including hash-matched delivery resume) live only in the browser
  transition table.

### Generalize the audit (Step 2)

The shared `tool_audit.py` `ALLOWED_REASONS` / `AUDIT_EVENTS` are
closed sets that cannot pass browser reasons. To avoid touching the
existing SQL trust boundary (including the enum-sync test),
**separate audit into `browser-audit.jsonl`** and generalize
`AuditLog` so the allowed-enum registry and output path are
injectable. **Injection surfaces (explicit)**:

1. **events**: inject as `(name, plan_dependent)` pairs (the
   existing `PLAN_INDEPENDENT_EVENTS` plan_id required/forbidden
   judgment is also an injection surface — browser's `login` is a
   plan-independent event).
2. **subcommands**.
3. **reasons** (allowed-enum registry).
4. **digest field spec** (`sql_digest` → browser's
   `artifact_digest` / `manifest_digest` / flow ref).
5. **output path**.

SQL-side defaults unchanged; enum-sync tests preserved. The
"metadata only, no values" invariant is identical.

### New for browser only

Catalog parse (for the browser schema), flow execution, verification-
contract engine.

### Plan-namespace type guard

Bundle location is separated under a browser-only root
(`outputs/browser-plans/`). At approve / execute time, verify that
the target plan's tool type matches the launching CLI (blocks a mix-up
where a SQL CLI consumes a browser plan).

---

## 12. Intermediate artifact retention policy and the janitor

Screenshots, traces, and temporary downloads carry sensitive material
even beyond CSV:

- Enclose under the bundle (spool) — the contract includes TTL + a
  delete-on-execute-complete rule.
- `storage_state` is persisted by the runner via 0600 atomic write
  (O_NOFOLLOW / umask).
- **Traces disable network body capture** on recording (Authorization
  / Set-Cookie / token would remain in `trace.zip`). **HAR is off by
  default.** Screenshots during auth operations are suppressed.
- Artifacts and traces have a byte cap (`max_artifact_bytes` in
  catalog limits).
- **Delete-on-normal-exit alone cannot collect** (SIGKILL, restart,
  disk full) — the CLI carries a **janitor path** that collects
  expired / incomplete bundles at startup. Delete failures are
  audited and retried next time.

### Download safety discipline

- Server-supplied `filename` is NOT used as the save name — the
  runner generates a random name + atomic rename (after size / hash
  finalization).
- Redirect origin revalidation per hop is the interception layer's
  responsibility. Byte cap / time cap overrun aborts.
- Partial files are treated as failure and deleted. Delivery before
  verification completion is structurally impossible (a
  seal-at-prepare consequence).

---

## 13. Exception handling

| Anomaly | Handling |
|---|---|
| 2FA expired | Fail-closed with `session_expired`; hint re-auth (`login` / `form`) |
| Permission change (post-prepare) | Seal-at-prepare: no browser rerun at execute time — delivery only. Completed under prepare-time permissions |
| Partial fetch | B1: `row_count_range` + independent anchor detect and fail-closed. B2: silent-truncation mark or reject |
| Delivery-mid unknown failure | Resume `delivering` only when the seal hash matches; else `failed` → re-prepare |

### Exception sanitization (all exceptions at the runner boundary)

Playwright TimeoutError etc. embed URLs (with tokens in query),
selectors, call logs, and DOM fragments. Non-Playwright exceptions
inside the flow code or capability API (parameter-value-carrying
`ValueError` etc.) can also leak through tracebacks. **Catch every
exception crossing the runner boundary**, map it to the closed
browser reason enum, and **NEVER** pass raw exception text to audit
/ stdout / CLI output (same discipline as the http connector using
`from None` to strip credential-carrying exceptions).

### Browser lifecycle contract

Manage browser / context / page with a context manager and reliably
close in `finally`. On per-flow hard wall-clock timeout and on
SIGINT, force-kill the browser process before exit (maintains the
130 contract). No zombie Chromium and no lingering user-data-dir
locks.

---

## 14. HTTP reduction gate (mandatory before registration)

Always pass this before creating a browser tool:

- Capture the export request from the network log during flow
  execution; try to replay it via `tool_connector_http`.
- If reproducible, **register as an HTTP connector and do NOT build a
  browser tool** (choose the higher-assurance option).
- Contain the capture run: pre-registration execution runs **under a
  draft catalog entry with production-identical containment**
  (interception / ephemeral profile / audit — all under the same
  contract).

---

## 15. Registration walkthrough

1. **HTTP reduction gate** (§14, under draft-catalog-entry
   containment).
2. **Tier decision** (§5 — can an independent anchor be formed?).
3. **Build the verification contract** (from §4's closed vocabulary;
   B1 requires at least one independent anchor).
4. **Independent reviewer — independent grounds + counterexample
   fixture required**:
   Common-cause failure (a single LLM writing both flow and
   verification embeds the same misunderstanding in both) is NOT
   preventable by actor separation alone (agreement on
   misunderstanding grounded in the same screen and same normal
   fixture). The registration-gate condition is NOT "an independent
   actor" — it is "**presentation of independent grounds + a
   counterexample fixture**." The reviewer prepares a false-success
   fixture (selector drift, same-shape screen on a different
   tenant, filter-not-applied, dropped pagination, partial
   export, etc.) and registration passes only when the
   verification contract **rejects all cases**.
5. **doctor** (login → navigate → selector-exists).
6. **prepare → approve → execute** — one full loop.

Fixtures are NOT satisfied by one normal case — prepare a
**false-success corpus** and record per-mutation rejection rates
plus normal-case false rejections to quantitatively evaluate
v1 vocabulary sufficiency.

### B1 prototype measurement (Step 7)

Verification contract = 5 checks (`filter_readback`,
`row_count_range`, `ui_total_vs_file_rows`, `primary_key_unique`,
`tenant_id_match` — 3 correctness + 3 independent anchor, some
overlap). Measured against 8 false-success mutations in
`test_browser_extract_corpus.py`. Fake-injected the flow's execution
outcome (`ExtractionResult`) to measure verification-contract
enforcement (browser-independent pure logic).

| False-success mutation | Catching check | Reject reason |
|---|---|---|
| Filter not applied | `filter_readback` | `readback_mismatch` |
| Same-shape screen on different tenant (bad selector) | `tenant_id_match` | `tenant_mismatch` |
| Dropped pagination | `ui_total_vs_file_rows` | `ui_total_mismatch` |
| Partial download | `ui_total_vs_file_rows` | `ui_total_mismatch` |
| Truncation | `ui_total_vs_file_rows` | `ui_total_mismatch` |
| HTML error page 200 | `row_count_range` | `row_count_out_of_range` |
| Empty result | `row_count_range` | `row_count_out_of_range` |
| Duplicate primary key (double fetch) | `primary_key_unique` | `duplicate_primary_key` |

**Result**: 8/8 mutations rejected (100% rejection rate), 0
normal-case false rejections.

**Observations on v1 vocabulary sufficiency**:

- **`ui_total_vs_file_rows` alone catches the 3 completeness
  mutations** (pagination / partial / truncation). Evidence that
  the independent oracle "the total shown in UI" is doing the work.
  Screens without a UI total cannot form this anchor and drop to
  B2 (matches the B1 precondition).
- **"Different value only — shape, count, tenant all normal — bad
  selector" is a residual not caught by vocabulary alone.**
  `tenant_id_match` catches tenant-boundary errors, but a bad
  selector that grabs different normal data inside the same tenant
  is out of scope. Narrow this class via the registration-time
  independent-reviewer step (independent grounds + counterexample
  fixture) plus concurrent `filter_readback` / `row_count_range`.
  Recorded as a v1 known limit.
- `artifact_hash` / `screen_fingerprint` are unused in this corpus
  (the former is bundle→delivery completeness, the latter needs a
  catalog-declared baseline fingerprint). At real-site adoption,
  the baseline-acquisition procedure for `screen_fingerprint` is
  worked out in the next cycle.

---

## 16. Known limits (honest scoping)

Residuals recorded in the guide and accepted:

- **Read-only NOT enforced**: honest scoping — "don't act outside
  the declared flow" + provenance (same style as MariaDB out of
  scope). Mechanical **enforcement** of read-only is a v1 Non-Goal.
- **DNS rebinding**: allowlist matches by hostname. Rebinding risk
  on long-lived sessions remains.
- **WebRTC residual**: closed via launch args; the remaining path
  is on par with DNS rebinding.
- **Doctor's login side effect**: doctor does NOT claim data
  non-contact — login and navigation carry side effects (session
  creation, last-login update, etc.). Declared narrowly as "**does
  NOT extract / does NOT produce artifacts / does NOT perform
  explicit destructive actions**." During doctor, trace /
  screenshot / DOM saving are disabled.
- **Audit JSONL writability** (§0 residual): the bar rises to "the
  attacker must also tamper with audit history," no higher.
- **Structural containment of in-process Python is unachievable**
  (§2): hash pin + AST gate + PR review are accident prevention
  and review support — do NOT claim a structural boundary against
  malicious flows.
- **OS-layer traffic containment via egress proxy / network
  namespace**: doubling up on the interception-layer limit is a
  future option (recorded here only).

---

## 17. Bootstrap (Playwright)

Browser system does NOT pollute the main `requirements.txt`. Opt-in
uses a separate file:

```
# Install dependencies (separate file; flat requirements has no
# extras mechanism, so we separate).
uv pip install -r requirements-browser.txt

# Fetch browser binaries (first time only).
python -m playwright install chromium
```

`playwright` is declared with a lower bound ≥ 1.48 (`route_web_socket`
required) + `major.minor` upper bound.

Real E2E browser tests are gated by the same opt-in env-var scheme as
DB smoke (`BROWSER_EXTRACT_SMOKE` unset → skip). Browser-independent
decision logic (allowlist match, URL canonicalization, state
machine, retention-policy judgment, janitor file operations, AST
gate) runs on every test invocation.

### Measured (2026-07-17 / WSL2 Ubuntu, Python 3.12.3, uv-managed `.venv`)

```
uv pip install --python .venv/bin/python -r requirements-browser.txt
.venv/bin/python -m playwright install chromium
```

- `requirements-browser.txt`'s `playwright>=1.48,<1.55` resolved to
  **1.54.0** (within the upper bound, no bump needed).
- **On WSL2, missing system deps did NOT surface.**
  `chromium.launch(headless=True)` and the runner's actual
  `launch_persistent_context(service_workers="block",
  fixed locale/timezone/viewport, args=WebRTC disabled)` +
  `context.route("**/*")` + `context.route_web_socket("**/*")` all
  launched and worked without additional libs. `route_web_socket`
  is provided in 1.54 (consistent with the ≥ 1.48 lower bound).
- **On environments where system deps are missing** (`error while
  loading shared libraries: lib*.so`), the equivalent of
  `python -m playwright install-deps chromium` is required. Since
  that calls `apt-get`, it **requires sudo**. In that case, note
  the missing lib names from the error and **ask a human to run**
  `sudo python -m playwright install-deps chromium` (or
  `sudo apt-get install` per lib) and rerun the smoke after
  installation (the agent does NOT run sudo).
- Browser binaries land at `~/.cache/ms-playwright/` (outside
  `.venv`, globally shared, under HOME — not covered by gitignore).

### Items measured under smoke (2026-07-17, `BROWSER_EXTRACT_SMOKE=1`)

`lib/service/test_browser_flow_runner.py`'s smoke class measures
against the fixture server:

- **interception**: declared origins continue; undeclared origins
  abort + `on_block` notification; because it is **context-scoped**,
  undeclared requests from new tabs (`context.new_page()`) are
  also caught.
- **`service_workers='block'`**: after navigation,
  `context.service_workers == []`.
- **teardown**: on both normal exit and mid-flow exception,
  context close + ephemeral user-data-dir removal.
- **hard timeout**: navigation to unresponsive `/hang` exceeds the
  page default timeout → `TimeoutError` → sanitized to
  `flow_timeout`; user-data-dir purged.
- **download**: real download of the fixture's Export CSV — saved
  with a runner-generated name (NOT the server-supplied name),
  atomic placement in spool (`.part` → `os.replace`), bytes
  matching the CSV.

Honest scoping (NOT measured):

- **Live WebSocket rejection is NOT tested against a real
  socket**. Sync Playwright's dispatcher can deadlock if the page
  Promise is awaited inside a route handler; WS-deny is confined
  to the **mechanism test** (`install_interception` places
  `route_web_socket("**/*")` context-scoped — verified against a
  fake context, always-on). Consistent with the §7 policy of not
  admitting WS-required tools to v1.
- **SIGINT force-kill / rejection of `data:` / `blob:` navigation**:
  the former is structurally covered by the teardown `finally` +
  process exit (hard to make a deterministic automated smoke);
  the latter is covered by capability-API `goto` that only builds
  catalog origins plus `canonicalize_request_url`'s scheme
  rejection (always-on unit).

### Canonicalize insights measured (surfaced by smoke and fixed)

`canonicalize_request_url` borrows the http connector's segment
normalization, but smoke surfaced two browser-specific points
(both now pinned by always-on unit tests):

- **IP literal hosts** (`127.0.0.1` / `[::1]` in the local fixture)
  cannot be IDNA-encoded. Pass numeric hosts through without IDNA;
  wrap IPv6 in `[...]` at origin.
- **Root `/` and single trailing slash** are emitted normally by
  browsers, but the http connector's segment normalization rejects
  them as empty segments. Absorb this on the browser side before
  matching (multi-slashes like `//` still reject as a path
  confusion attack surface).

### Operational positioning of smoke (accepting review feedback)

The security-core real-browser interfaces (real interception
abort, real form-login capture, teardown/udd purge, real E2E
rejection of false-success mutations) exist **only** under the
`BROWSER_EXTRACT_SMOKE` gate. Always-on tests verify decision
logic against fakes, but Playwright wiring regressions are only
detectable by smoke. **Before committing changes that touch the
browser system, run smoke locally** (CI is not set up in this
repo; procedure lives in the "Browser Extract" section of
AGENTS.md).

### Catalog-author footgun notes

- `account.origin` matches by **plain string equality** (only the
  request side is normalized). Write origin in lowercase with the
  default port (:443 / :80) omitted — uppercase or explicit `:443`
  will falsely block legitimate requests (fail-closed direction —
  accident, not leak).
- Same-origin resources not enumerated under `origin_allowlist`'s
  `resource_type` (script / stylesheet / image, etc.) also abort.
  Failing to enumerate the resource types the real page needs
  breaks the screen (surfaces in doctor / smoke).

### Supply-chain notes

Chromium binaries are fetched from Playwright's official CDN
(`playwright.download.prss.microsoft.com` family). Their integrity
verification depends on Playwright itself (separate from pip
package hash verification). pip narrows supply with the
`major.minor` upper bound in `requirements-browser.txt`; browser-
binary authenticity is entrusted to the CDN + Playwright's
verification (recorded here and accepted).
