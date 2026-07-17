# Trust Score

Per-article trust score in [0.0, 1.0] computed from four factors: source
count, freshness, citation frequency, and backlink count.

## v2: absolute scale

Each factor uses an absolute saturation curve, not min-max normalization:

- Source count: `n / (n + 1)`
- Citation frequency: `c / (c + 2)`
- Backlink count: `b / (b + 2)`

The 0.30 threshold has meaning for a single article, independent of the
distribution over the corpus.

Freshness decays exponentially with a 365-day half-life (1 year = 0.50,
2 years = 0.25, never zero). This is aligned with the snapshot policy —
`source_revision` pinning.

## Running

```bash
python3 skills/wiki/scripts/trust_score.py --wiki-root .wiki
```

Output formats: `--format table` (default) / `json` / `report` (Markdown).

Report path: `.wiki/outputs/reports/{YYYYMMDD}-trust-score.md`.

## Edge cases

- If QueryLog is empty, the citation-frequency factor is excluded and the
  remaining three factors are re-weighted.
- Trust Score is a derived value; do NOT persist it in article
  frontmatter.
