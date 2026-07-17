# User Support Investigation Guide

> A canonical procedure for corroborating user-support inquiries (e.g.
> "I didn't get event points after the match") from a user ID against
> related data. Composed of: three-layer decision decomposition, a
> five-section report shape, an evidence-route priority order, a
> canonical form for "cannot investigate," and anonymization rules for
> feeding results back into the wiki.
> **This guide is tool-agnostic** — specific investigation tools are
> registered per environment by users; this canon does not
> structurally depend on any of them.

## Positioning

Support-inquiry corroboration is "a question to collective knowledge."
Support inquiries are frequent and formulaic, so a procedure established
once is reused repeatedly — the more it accumulates as knowledge, the
faster the loop spins. This guide defines the procedure as **a
reproducible canon**.

The canon covers three things.

1. **Decision structure** — three-layer separation of observation,
   rule application, and bug inference, enforced by the five-section
   report shape.
2. **Route selection** — priority for where evidence is pulled from
   (a selection rule over registered tools).
3. **Result canonicalization** — "cannot investigate" is also a
   first-class result, classified formally.

The specific tools (which API to call, which DB to query) **are
registered per environment**. This canon carries no tool names — it
speaks in abstract categories (official API family / data query family
/ screen-mediated family). Mentions of registered tool examples are
isolated to the "One application example in this repository" section
at the end.

## Three-layer decomposition

The one-liner "points didn't come in" folds three qualitatively
different judgments into one sentence. Failing to separate them is
**the biggest false-success mode**.

| Layer | Content | Owner |
|---|---|---|
| (1) Observation | Raw facts read from a data source (no attribution record, etc.) | Data query (tool run under human approval) |
| (2) Business rule application | Apply business rules to the observation (invalid match, out-of-scope, cap, delay, cancellation) | Business-rule knowledge (articles, staff) |
| (3) Bug inference | The residual not explained by rules classifies as "suspected defect" | Human |

**Do NOT jump from (1) to (3).** The report shape enforces this. An
observation (no attribution record) only takes on meaning after a
business rule (that match was invalid) is applied; only then, if no
rule explains it, the investigation moves to bug inference. Make it
explicit in the report at which layer the conclusion firmed up.

## Five-section report shape

Do NOT reduce the outcome to a binary ("bug / not a bug"). Split into
these five sections. The shape itself is the device that enforces
three-layer decomposition:

1. **Observations** — raw facts read from a data source. No
   interpretation mixed in.
2. **Applied rules** — the business rules applied and the outcome
   of applying them.
3. **Inferred cause** — whether rules did or did not explain it. Bug
   inference goes ONLY here.
4. **Confidence** — high / medium / low (rubric below).
5. **Missing information** — what would raise confidence if
   available, or classification as "cannot investigate."

The report is **for support staff** — not customer-facing text.
Internal implementation terms (table names, internal flag names) may
remain to the extent staff use them for judgment, but do NOT write it
assuming direct forwarding to the customer.

## Route priority — evidence authority (SoT) is the primary criterion

The investigation route is NOT a fixed tool list — it is **a selection
rule over the registered tool set**. "Official API family / data query
family / screen-mediated family" are abstract access categories, NOT
the priority itself.

### Primary criterion: evidence authority — how close is this route to the Source of Truth

Do NOT conflate the access method (API vs DB vs screen) with whether
the data is a **Source of Truth (SoT)**. Internal APIs can be
**derivations** of internal DBs. Screen displays can be derivations of
APIs. So the primary criterion for a route is not "is access easy" but
**how close is this route to the SoT (evidence authority)**.

- Pull evidence from the route closest to SoT. Derived views (caches,
  aggregated screens, replicas) do not necessarily agree with SoT.
- Facts confirmed by an external service (e.g. payment confirmation)
  are SoT in **the service's authoritative route**, not in your own
  screen.
- Distinguish "confirmed on screen" from "confirmed inside SoT" in the
  report.

### Tiebreaker: minimum privilege

When multiple routes are equally authoritative, pick **the route
reachable with the least privilege**. Prefer read-only, minimum-field
routes. Avoid routes requiring broad privileges.

## Mandatory gate before bug inference — settlement window elapsed?

Concluding "bug" before waiting for asynchronous processing or batch
confirmation is **the biggest false positive**. Event attribution,
payment confirmation, and point application all have delays between
event occurrence and confirmation. Elapsed time of that
**confirmation-wait window (settlement window)** is a required
precondition for bug inference.

- Know each processing kind's settlement window (e.g. "up to N minutes
  from match end to attribution confirmation").
- If inquiry time / observation time is inside the settlement window,
  the conclusion is "in progress / re-check pending" — not a bug.
- The timeline shape carries a settlement-window column: "start-event
  time + window = confirmation deadline" placed alongside the
  observation time.

Reports that jumped to bug inference without confirming the settlement
window bounce back as shape violations.

## Canonical forms for "cannot investigate" — six categories

"Couldn't figure it out" is also a first-class result. Classify into
one of six. The aggregation itself becomes a gauge for prioritizing
**API promotion, audit-view creation, and tool registration**.

| Category | Meaning | Feedback destination |
|---|---|---|
| Insufficient privilege | No access to the required data source | Access design; investigation-only views |
| Missing join key | Cross-boundary common key (order ID, purchase token) missing — cannot correlate | Build a mapping table |
| In-progress / re-check pending | Settlement window not yet elapsed; judgment on hold | Re-check (retry after some time) |
| No registered tool route | **No registered tool** reaches the required data source | Tool registration backlog (first-class signal) |
| SoT mismatch | Multiple SoT-grade routes disagree; cannot decide authoritative | Data-consistency investigation; owner confirmation |
| Data expired by retention | Data has aged out and cannot be restored | Retention policy review |

"No registered tool route" is an especially important **first-class
signal**. Via Gap Detection, it measures "which data sources lack a
registered investigation tool" and feeds back into the tool-registration
priority.

## Cross-boundary investigation — common join keys and reference-time timelines

Cross-system investigation is NOT "search two systems in parallel" —
it is **cross-checking via a common join key + reference time**.

- **Common join key**: the internal user ID ↔ order ID / purchase
  token mapping table is the linchpin. The mapping table itself
  (permissions, retention, audit) is also in scope. If the key is
  missing, the investigation is "missing join key" and cannot
  proceed.
- **Reference-time timeline**: align event times and observation times
  at each boundary on a single time axis.

### Time hazards (required subsection)

Every timeline entry must make these two explicit — leaving them
ambiguous invites reading a false causality:

- **TZ normalization**: normalize every time to a single time zone
  (UTC recommended). Do NOT line up local times as-is. Annotate the
  original time zone alongside each time.
- **event-time vs processing-time distinction**: "when the event
  happened (event-time)" and "when the system processed / recorded it
  (processing-time)" are different things. Attach a **time-kind
  label** (event / processing) to each timeline entry. Skipping this
  reads processing delay as event ordering.

## Confidence rubric

The report's three-valued confidence is defined by the number of
independent corroborations.

| Confidence | Definition |
|---|---|
| High | At least **two SoT-grade** independent evidences agree (corroboration present) |
| Medium | Supported by **a single SoT** (no independent second evidence) |
| Low | **Indirect inference only** (screen of a derived view; circumstantial evidence) |

Corroboration means evidence obtained via mutually independent routes
pointing to the same conclusion. Reading the same SoT twice is NOT
corroboration. Confidence is a mechanical way of conveying "how firmly
the staff member can assert it."

## Two-log separation

To avoid conflating signal sources of the growth loop, separate two
logs by role:

| Log | What it records | Role |
|---|---|---|
| Article reference log (QueryLog) | Which articles were consulted to build the investigation procedure | Input to knowledge-gap detection (Gap Detection) |
| Tool execution log (audit log) | Which tool ran what and when (execution provenance) | Audit / traceability of execution |

These are **different systems**. Do NOT write tool-execution traces
(who queried what when) into QueryLog. QueryLog accumulates "which
articles were consulted," and treats articles that were referenced but
do not exist as gaps — it is a signal source for the growth loop, NOT
execution provenance. Conversely, writing article-consultation facts
into the execution audit log is also wrong.

## Wiki feedback rule — anonymized decision rules only

Results feed back into the wiki as **anonymized decision rules
(class-level) only**. Do NOT feed back individual-case outcomes (this
user_id turned out to be X). Turning the wiki into a case archive
generates search noise, and when a conclusion flows back after
someone trusted the article and skipped the actual investigation,
**mis-knowledge self-reinforces**.

- Feed back class-level abstractions like "under which branch condition
  + with which missing data + what anomaly occurs."
- QueryLog `question` / `gap_topics` must also be **class-level
  anonymized**. Do NOT include real user IDs.
- **Anonymization boundary is on the "entering collective knowledge"
  side**: investigation reports need to identify "whose case was
  investigated" as a business requirement, so **real user IDs and
  ticket IDs may appear**. Compensate by keeping the output location
  git-ignored and maintaining minimum-field retrieval. Do NOT
  transcribe real IDs into wiki articles, QueryLog, or the operation
  log (`log.md`) — report (individual-case provenance) and knowledge
  (class-level collective knowledge) are handled differently.

### Minimum generality bar

Overly specific decision rules derived from rare cases are effectively
case records (k-anonymity broken). Feed back only rules with
**generality that applies to multiple cases**. Turning "a condition
that matches only one case" straight into a rule points at the
individual case even after "anonymization." Before feedback, ask
"does this rule apply to multiple cases?" as the minimum-generality
bar.

## Minimum-field retrieval and read-only principle

- **Read-only is the top principle.** Do NOT use write paths for
  investigation.
- **Minimum fields**: do NOT retrieve sensitive information (e.g.
  payment method) not needed for the answer. Narrow columns and do
  NOT leave them in artifacts.
- **Output caps**: cap rows retrieved per run.
- **Exit conditions**: separate the investigation account (shared
  accounts blur execution-actor audit). Prepare an
  **investigation-only view** (only necessary columns, masked) rather
  than touching PII tables directly.

## Closed-set evolution governance

Design investigation-procedure knowledge as a **closed set by mismatch
pattern** — NOT by inquiry phrasing (phrasing varies infinitely; a
phrasing-based split fractures gaps across synonymous inquiries).

When a new mismatch pattern is found, decide "new category or variant"
by this rule:

- **New category** when the investigation route and judgment
  rationale are **structurally different** from every existing
  category (different business rule applied, different join key).
- **Variant of an existing category** when the investigation route
  and judgment rationale match an existing category, and **only the
  entry-symptom differs**.

Record the decision (which one, why) inside the relevant knowledge
article. Grow the closed set too easily and its discriminability is
lost. Shove everything into variants and a single article balloons,
burying the investigation route.

## User co-presence steps for real-data queries

Do NOT run steps that require credentials or a real-case URL headless.
Fix this sequence:

1. Select the route per priority (evidence authority → minimum
   privilege).
2. Sanity-check the read-only connection.
3. **Present** the target (one real case) and the retrieval plan.
4. **Human approval** (the execution gate is always human — the LLM
   only proposes candidates).
5. Run; emit the five-section report (output location is
   git-ignored — PII present).
6. Record in QueryLog (question / identifiers anonymized).

---

## One application example in this repository

> Below is **one example** of tools that could be registered against
> this repository (a wiki substrate). The canon above does NOT depend
> on these — other environments will register other tools.

The abstract categories above materialize as follows in this
repository (one example only):

- **Official API family** — one option is the HTTP connector
  (`tool-query`'s `http` type). App Store Server API / Google Play
  Developer API-style official APIs are typical here.
- **Data query family** — one option is `tool-query`'s direct DB
  query (sqlite / postgres / mysql). The multi-layered defense
  (read-only role + relation allowlist + static SQL gate + session
  read-only + output caps) is an example of mechanically upholding
  the canon's "read-only top principle" and "minimum-field
  retrieval."
- **Screen-mediated family** — one option is `browser-extract`. A
  "bridge until an official API grows" for in-house tools without an
  API. Seal-at-prepare containment + human TTY approval mechanically
  uphold "the execution gate is human."

For the explanation layer of "what to fetch and how to decide," this
repository has the Selection Recipe mechanism (`selection-recipe`
tag). However, the investigation knowledge in this guide (tool-agnostic
articles under the `practices` category) does NOT structurally
depend on these tool mechanisms. Selection Recipes describe execution
contracts for registered tools — they are not the investigation canon
itself.
