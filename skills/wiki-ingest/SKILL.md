---
name: wiki-ingest
description: >
  Stage source documents (URLs, files, articles, git repositories) into
  the wiki's raw/ directory. Trigger phrases: "ingest a source", "add to
  wiki", "put this URL into the wiki", "ingest this repository".
---

# Wiki Ingest

Stage source documents under `{wiki_root}/raw/`. `raw/` is immutable —
once saved, do not modify.

**Resolving `wiki_root`**: read the `wiki_root:` field from `AGENTS.md`.
If missing, point the user at `wiki-init`. Details in
[paths.md](../wiki/references/paths.md).

## Inputs

Dispatch by input type:

```
input → git URL / git repo path? → repo flow (see "Ingesting a repo source")
      → URL?               → WebFetch → save as article
      → file path?         → Read     → save as file
      → inline text?       → use as-is → save as article
```

Git URL detection: `https://…/owner/repo(.git)`, `ssh://…`,
`git@host:owner/repo.git`, or any local path containing a `.git`
directory.

## Security scan (required)

Run `security_scan.py` per [security.md](../wiki/references/security.md).
Exit 1 aborts the ingest: save nothing under `raw/`, append nothing to
`log.md`, and do not print the completion message. Instead print the
scan output verbatim (the ✅/❌ summary plus its Findings lines), state
that the ingest was aborted because of them, and propose remediation
(redact or remove the flagged lines, then re-ingest).

Print the script's ✅/❌ summary verbatim — the script's actual output
wins; the block below only illustrates the shape:

```
✅ Path traversal: OK
✅ Sensitive data: OK
✅ Prompt injection: OK
```

## Procedure

1. **Determine save destination and filename**:
   | Input type | Destination | Filename |
   |---|---|---|
   | URL / inline text | `raw/articles/` | `{YYYYMMDD}-{slug}.md` |
   | Single local file | `raw/files/` | Original filename verbatim |
   | Repo flow | `raw/files/{repo-slug}/` | Original filename verbatim |

   `{slug}`: kebab-case English derived from the title (step 3).
2. Run the security scan (pass the filename from step 1 to
   `--filename`).
3. Attach frontmatter:
   ```yaml
   ---
   title: Document title              # required. Base on the source H1, refined to reflect content; inline text without a heading: derive a short title from the text's subject (typically its first sentence)
   scraped: YYYY-MM-DD                # required (ingest date)
   source_url: https://example.com    # attach only for URL inputs
   source_path: path/to/original.md   # attach only for local-file inputs (tracks the origin)
   tags: [auto-inferred tags]         # inferred from body; empty array [] if inference fails
   ---
   ```
4. Save to the destination decided in step 1.
5. Append to `log.md`:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/log_append.py ingest \
     --wiki-root {wiki_root} --slug {slug} --source-kind {source_kind}
   ```
   `{source_kind}`: `url` / `file` / `inline` (the repo flow uses
   `repo @ {short-hash}`).

## Completion message

```
── ingest complete ──
{security scan ✅/❌ summary, verbatim}
Saved to: {destination decided in step 1}/{filename}
Frontmatter:
  title: {title}
  source_url: {url}        ← only for URL inputs
  scraped: {date}
  tags: [{tags}]
Next: `wiki-compile` to generate articles, or `wiki-cycle --compile-only` to run compile + lint together
```

## Ingesting a repo source

Ingest a git repository (URL or local path). **Multiple repositories go
through three passes** — cross-repo wikilinks only resolve after every
repo is on disk, so the pass order matters:

**Pass 1 — clone every repository and generate manifests** (batchable in
one command):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/repo_ingest.py <url-or-path>... --wiki-root {wiki_root}
```

- Clone is automatic: `ghq get --shallow` if `ghq` is available,
  otherwise `git clone --depth 1` into `{wiki_root}/.cache/repos/`.
- Manifest is written to `{wiki_root}/.cache/manifests/{slug}.json`.
  **Don't read the whole thing — Read only the tier you need.**
- A machine-generated `repo-inventory.md` is saved under
  `raw/files/{slug}/`.

**Pass 2 — pick docs + ingest for every repository**:

1. Start from the manifest's tier 1 (README / architecture / adr), and
   confirm the selection with the user.
2. Save each file through the existing file-ingest flow (security scan
   included) into `raw/files/{slug}/`.
3. Attach frontmatter with `source_url` + `source_revision` (commit
   hash) + `source_path`. See the repo section of
   [frontmatter-schemas.md](../wiki/references/frontmatter-schemas.md).
4. Append to `log.md`:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/log_append.py ingest \
     --wiki-root {wiki_root} --slug {slug} --source-kind "repo @ {short-hash}"
   ```

**Pass 3 — bulk compile**:

Run `wiki-compile` once every repository has finished ingest. The
procedure lives in the "Ingesting a repo source" section of
[compilation-guide.md](../wiki/references/compilation-guide.md).
