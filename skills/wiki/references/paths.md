# Path resolution rules

The same conventions apply to every operation. Mixing them up produces
broken links — always follow this table.

## Resolving `wiki_root`

Before any operation, read `AGENTS.md` (or `CLAUDE.md`) at the project
root and pick up the base path from the `wiki_root` field. If missing,
prompt the user to run `wiki-init`.

```
AGENTS.md → wiki_root: .wiki (default)
```

## Path resolution table

Frontmatter and body text use different bases.

| Location | Base | Example (writing from concepts/foo.md) |
|---|---|---|
| Frontmatter `source_refs` | Relative to `{wiki_root}` | `raw/articles/20260405-bar.md` |
| Frontmatter `related` | Relative to `{wiki_root}` | `concepts/bar.md` |
| Body `[[wikilink]]` | Slug → `concepts/{slug}.md` | `[[bar]]` |
| Body Markdown link | **Relative to the file being written** | `[source](../raw/articles/20260405-bar.md)` |

## Script paths

Scripts and templates ship with the plugin. Every skill uses
`${CLAUDE_PLUGIN_ROOT}`-based paths (this env var points to the plugin
install root):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/xxx.py --wiki-root {wiki_root}
```

If `${CLAUDE_PLUGIN_ROOT}` is not set (e.g. when developing directly in
this repository rather than going through the plugin), fall back to the
repo-relative path `skills/wiki/scripts/xxx.py`.
