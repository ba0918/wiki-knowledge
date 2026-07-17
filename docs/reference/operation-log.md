# Operation Log (`log.md`)

Append-only operations log. Entries follow the format
`## [YYYY-MM-DD] {op} | ...`.

A script owns the formatting so that things like singular vs plural
drift are prevented:

```bash
python3 skills/wiki/scripts/log_append.py \
  {ingest|compile|promote|query|lint} \
  --wiki-root .wiki <op-specific fields>
```

Common options:

- `--date` — defaults to today.
- `--note` — free-form suffix appended as ` — {note}`.
