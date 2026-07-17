# Graph Layer

A read-only graph derived from `concepts/*.md` (nodes / edges /
`metadata.dangling_links`).

- Script: `skills/wiki/scripts/graph_gen.py`.
- Output: `.wiki/outputs/graph.json`.
- Role: substrate for `dead_link` / `orphan` detection in lint. This
  eliminates double implementation of the detection logic and prevents
  layer-crossing.

## Regeneration

```bash
python3 skills/wiki/scripts/graph_gen.py --wiki-root .wiki
```

## Pipeline placement

Cycle runs `compile → graph_gen → lint` in that order, explicitly
orchestrated.

## Git tracking

The graph layer is derived. Running with it git-ignored is supported
(add to `.wiki/.gitignore` if desired).
