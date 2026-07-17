# Security Scan (Ingest)

The three ingest-time security checks — path traversal, sensitive data,
prompt injection — have a single source of truth in the scan script.

```bash
python3 skills/wiki/scripts/security_scan.py <file>... --filename <stored-name>
```

Direct text input via `--stdin`.

Exit codes:
- 0 = clean
- 1 = detection (ingest aborted)
- 2 = argument error

`--format json` is available.
