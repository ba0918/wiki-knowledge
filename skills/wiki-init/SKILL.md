---
name: wiki-init
description: >
  Initialize the wiki structure (directories, templates, AGENTS.md) in a
  project. Trigger phrases: "wiki init", "initialize wiki", "create a new
  wiki", "set up a knowledge base".
---

# Wiki Init

Bootstrap the wiki structure in a project.

Path resolution follows [paths.md](../wiki/references/paths.md).

## Preflight

If the project root's `AGENTS.md` (or `CLAUDE.md`) already has
`wiki_root`, ask whether to reinitialize. (Skip the confirmation only if
neither file exists.) A pre-existing wiki directory alone — without
`wiki_root` in either file — does NOT count as initialized: proceed,
merging any existing files per the rules below.

## Procedure

1. Decide the wiki path (default: `.wiki`; user-overridable). `wiki_root`
   is a project-root-relative path.
2. Create directories:
   ```
   {wiki_root}/
   ├── raw/articles/
   ├── raw/files/
   ├── concepts/
   ├── outputs/queries/
   ├── outputs/reports/
   └── schema/
   ```
   Note: `index.md` and `log.md` are files created by step 3's template
   copy.
3. Place template files (originals live under
   `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/`):
   - `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/page-template.json` →
     `{wiki_root}/schema/page-template.json` (copy verbatim)
   - `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/categories.json` →
     `{wiki_root}/schema/categories.json` (copy verbatim)
   - `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/index-template.md` →
     `{wiki_root}/index.md` (copy verbatim)
   - `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/log-template.md` →
     `{wiki_root}/log.md` (substitute `[YYYY-MM-DD]` with today's date)
   - `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/wiki-gitignore-template`
     → `{wiki_root}/.gitignore`
     - If a `.gitignore` already exists, do not overwrite. Merge:
       append only the lines that are not yet present.
4. Configure the project root's `AGENTS.md`:
   - **If `AGENTS.md` does not exist**: create it from
     `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/agents-md-template.md`
     and fill in all placeholders:
     - Set `wiki_root` to the real path.
     - Expand `{wiki_root}` in the body to the real path. (Leave other
       placeholders like `{slug}` alone.)
     - `SCOPE_DESCRIPTION`: default to the exact fallback
       "_Scope not set. Fill this in on first ingest._". Write 1–2
       sentences instead ONLY when the user's request itself states
       the wiki's purpose — never infer scope from repo contents.
   - **If `AGENTS.md` exists but has no `wiki_root`**: keep the
     existing content untouched and append the template's wiki
     sections (`## Wiki Knowledge Base` and everything below it) to
     the end of the file, filling placeholders the same way. Skip the
     template's preamble — the file already has its own heading.
   - **If `AGENTS.md` already has `wiki_root`**: keep the existing value.
   - **Regardless of the branch taken**: if `CLAUDE.md` does not
     exist, create a `CLAUDE.md` that only contains `@AGENTS.md`.
5. Point the user to the next step (`wiki-ingest`) in the completion
   message.

## Completion message

```
── init complete ──
Wiki root: {wiki_root}/
Created: raw/articles/, raw/files/, concepts/, outputs/queries/, outputs/reports/, schema/
Next: `wiki-ingest <URL or file>` to bring in a source
```

If some items already existed and were merged instead of created,
say so on the `Created:` line (e.g. `Created: … (.gitignore merged)`).
