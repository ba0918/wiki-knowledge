# Gap Detection

Aggregates `gap_topics` from QueryLog and cross-checks them against
existing articles to surface knowledge gaps.

```bash
python3 skills/wiki/scripts/gap_detect.py --wiki-root .wiki
```

- Output formats: `--format table` (default) / `json` / `report`
  (Markdown).
- Report path: `.wiki/outputs/reports/{YYYYMMDD}-gap-detect.md`.
- Adjust coverage threshold with `--threshold` (default: 0.8).
