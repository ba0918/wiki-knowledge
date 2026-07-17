---
name: wiki-cycle
description: >
  Orchestrator that runs Ingest → Compile → Graph Gen → Lint end to end.
  Trigger phrases: "wiki cycle", "run everything", "ingest through lint".
---

# Wiki Cycle

Orchestrator that runs Ingest → Compile → Lint end to end. Holds no
business logic — delegates to each leaf skill.

Script paths resolve per
[paths.md](../wiki/references/paths.md).

## Why use it

- Security-scan failure aborts the whole flow automatically.
- Compile errors auto-skip the lint step.
- If the flow stops midway, the summary tells you where.

## Arguments

| Argument | Meaning |
|---|---|
| Source spec | File path or URL (ingest target) |
| `--compile-only` | Skip ingest; run compile + graph_gen + lint |
| `--lint-only` | Run graph_gen + lint only |
| `--discover` | Run discover on repo sources before compile |

## Flow definitions

Dispatch on arguments. `graph_gen` **always** runs between compile and
lint (skipping it makes lint exit 2).

### Default flow (source specified)

```
1. Skill tool: wiki:wiki-ingest (stage source under raw/)
2. [only with --discover] Skill tool: wiki:wiki-compile (args: discover {slug} --yes)
3. Skill tool: wiki:wiki-compile (generate articles)
4. python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/graph_gen.py --wiki-root {wiki_root}
5. Skill tool: wiki:wiki-lint
6. Print summary
```

`discover` only applies to repo sources (with a manifest). Inside cycle
it always runs non-interactively (`--yes`).

### `--compile-only` flow

```
1. Skill tool: wiki:wiki-compile (auto-detect uncompiled sources)
2. python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/graph_gen.py --wiki-root {wiki_root}
3. Skill tool: wiki:wiki-lint
4. Print summary
```

### `--lint-only` flow

```
1. python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/graph_gen.py --wiki-root {wiki_root}
2. Skill tool: wiki:wiki-lint
3. Print summary
```

## Abort rules

- Ingest security check fails → abort the whole flow.
- Compile errors → skip lint.
- Lint 🔴 Error → propose a re-lint after fixes.

## Completion message

```
── cycle complete ──
ingest:  {ok/skipped/aborted} — {slug} ({source_kind})
compile: {ok/skipped} — {N} article(s) generated
lint:    {ok/skipped} — 🔴 {N}, 🟡 {N}, 🔵 {N}
Next: {show fix procedure if Error/Warning present; else `wiki-query` to use the knowledge}
```
