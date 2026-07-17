---
name: wiki-query
description: >
  Answer questions using wiki knowledge. Synthesize the answer from the
  wiki as source material — not general knowledge — with citations.
  Trigger phrases: "look it up in the wiki", "query", "ask the wiki",
  "answer from the knowledge base", "use the wiki to answer".
---

# Wiki Query

Answer questions from the wiki, not from general knowledge.

**Resolving `wiki_root`**: read the `wiki_root:` field from `AGENTS.md`.
If missing, point the user at `wiki-init`. Details in
[paths.md](../wiki/references/paths.md).

## Procedure

1. **Candidate selection (retrieval pre-pass)**: extract keywords from
   the question. If the question could hit content in either Japanese
   or English, include both.
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/query_retrieve.py \
     --wiki-root {wiki_root} --keywords <kw1> <kw2> ...
   ```
   Returns a candidate list with score, trust, and selection rationale,
   built from the graph layer + Trust Score. If `outputs/graph.json` is
   missing the script exits 2 — run
   `python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/graph_gen.py --wiki-root {wiki_root}`
   first, then rerun `query_retrieve.py`.
2. **Read related articles**: from the top of the candidate list, read
   the full text of articles that will actually improve accuracy. You
   may pull in articles outside the candidate list from
   `{wiki_root}/index.md` if needed.
3. **Synthesize the answer**:
   - Every claim must carry a `[[slug]]` citation.
   - **Trust-aware citation**: when citing an article with trust
     **below 0.30**, annotate as "(low trust: {trust})".
   - Make agreements and contradictions between articles explicit.
   - Call out uncovered areas as "gaps" and **name the topic**.
   - Choose the format based on the question: prose for facts, a table
     for comparisons, numbered lists for procedures.
4. **Offer to save**: after answering, ask whether to save as a wiki
   article.

**Do NOT answer from general knowledge.** Read wiki articles first. If
articles conflict, present both.

## Saving the answer (Wiki Promote)

If the user approves saving:

1. Save as `{wiki_root}/concepts/{slug}.md` with
   `tags: [query, synthesis]`.
2. Run post-processing per
   [post-processing.md](../wiki/references/post-processing.md) (Backlink
   Audit → index/AGENTS.md update → wikilink rendering → log_append
   promote).

If not saving:

1. Save the answer to
   `{wiki_root}/outputs/queries/{YYYYMMDD}-{slug}.md`.
   - Derive `{slug}` from the question's subject as kebab-case English.
   - Store the full answer verbatim (do not summarize).
   - Frontmatter:
   ```yaml
   ---
   title: Question summary
   type: query
   question: Original question text
   answered: YYYY-MM-DD
   sources_consulted:
     - "concepts/xxx.md"
   promoted: false
   ---
   ```
2. Append to `log.md`:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/log_append.py query \
     --wiki-root {wiki_root} --summary "{question summary}"
   ```

## QueryLog append (always run after the save decision)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/querylog_append.py \
  --wiki-root {wiki_root} \
  --question "{user's original question}" \
  --consulted concepts/{slug1}.md concepts/{slug2}.md \
  --answer-file {path to the saved answer file} \
  [--gap-topics "{topic1}" "{topic2}"] \
  [--promoted --promoted-to concepts/{slug}.md]
```

- `--consulted`: every article path read (relative to `{wiki_root}`).
- `--answer-file`: path to the saved answer text. `sources_cited` is
  extracted from it.
- `--gap-topics`: gap topic names (omit if none).
- Exit codes: `0` = success / `1` = validation error / `2` = argument
  error.

**⚠** `querylog.jsonl` stores the user's original question verbatim.
It is git-ignored by default.

## Completion message

```
── query complete ──
Consulted: {N} article(s) ({slug}, ...)
Gaps: {gap_topics or "none"}
Saved: {save path}
Next: {omit if promoted; else `wiki-query` for follow-ups}
```

`{N}` and `{slug}` come from `sources_consulted` (articles actually
read).
