# Shared post-processing

Post-processing shared by `compile` and `query(promote)`.

## Backlink Audit (required)

After article generation, `grep` every existing article and identify
places that should link to the new article. Add `[[new-slug]]` links
and `related` frontmatter entries in the matching articles, and bump
their `updated` frontmatter to today.

Skipping this step turns the wiki into a one-directional blog. **Do
NOT skip.**

## Update index / AGENTS.md

1. Add the new article to `{wiki_root}/index.md` (categorized, one-line
   summary).
2. Update the Articles section of `AGENTS.md`.

## Wikilink rendering

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/wikilink_render.py --write {wiki_root}/concepts/
```

Appends the GitHub-Web-UI-clickable form to each `[[slug]]`, producing
`[[slug]] ([↗](slug.md))`. Idempotent.

**Note**: `[[wikilink]]`s inside `outputs/queries/` do NOT need the
GitHub companion (the render script targets `concepts/` only).

## log_append

Append to `log.md` via the relevant subcommand (the script owns the
format):

```bash
# compile
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/log_append.py compile --wiki-root {wiki_root} --title "{Title}" --word-count {N} --sources {N}

# promote
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/log_append.py promote --wiki-root {wiki_root} --title "{Title}"

# discover
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/log_append.py discover --wiki-root {wiki_root} --slug {slug} --articles N
```
