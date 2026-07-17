# Repo Ingest

Entry point for turning git repositories (URL or local path) into
knowledge sources.

```bash
python3 skills/wiki/scripts/repo_ingest.py <url-or-path>... --wiki-root .wiki
```

Multiple repositories can be ingested in one call.

## Clone behavior

- Prefers `ghq get --shallow`. Falls back to `git clone --depth 1`.
- Cache path: `{wiki_root}/.cache/repos/` (gitignored).

## Output

- Manifest: `{wiki_root}/.cache/manifests/{slug}.json` — structural
  metadata plus tiered document candidates.
- Machine-generated `repo-inventory.md` under `raw/files/{slug}/`.
- Raw frontmatter is extended with `source_revision` (commit hash) and
  `source_path`. Note: `source_version` collides with the pipeline's
  int type and is not used.

## Multi-repo handling

Multiple repositories are processed in three passes: clone all → ingest
all → compile all. This is required so cross-repo wikilinks resolve.
See the `wiki-ingest` skill for the "repo source ingest" procedure and
`references/compilation-guide.md` for the compile procedure.

## Security

- Positive-match protocol allowlist (rejects `ext://` and `file://`).
- `GIT_ALLOW_PROTOCOL` restriction.
- Userinfo stripped from URLs.
- Two base-path containment for clone destinations.

## Options and exit codes

- `--max-docs` (default: 50) / `--full-clone` / `--refresh` / `--output`.
- Exit codes: 0 = all succeeded / 1 = some failed / 2 = argument error /
  130 = interrupted.
