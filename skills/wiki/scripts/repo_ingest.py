#!/usr/bin/env python3
"""repo ingest CLI — clone git repositories and emit snapshot manifests.

Thin handler only: argument parsing, layer composition, Result → exit code
mapping, and file/stdout output. All logic lives in ``lib/domain/repo.py``
(pure) and ``lib/service/repo_clone.py`` (I/O with DI).

Usage::

    python repo_ingest.py <source>... --wiki-root .wiki \
        [--max-docs 50] [--full-clone] [--refresh] [--output DIR]

Sources may be https:// URLs, ssh:// URLs, scp-like ``git@host:path`` or
existing local paths. Manifests are written to
``{wiki_root}/.cache/manifests/{slug}.json`` (override with ``--output``);
stdout carries only a per-repo summary. A machine-generated
``repo-inventory.md`` is written to ``{wiki_root}/raw/files/{slug}/``.

Exit codes: 0 = all sources succeeded, 1 = at least one source failed,
2 = usage error, 130 = interrupted (SIGINT).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

# Support both "python repo_ingest.py" and "python -m repo_ingest".
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from lib.domain.repo import DEFAULT_MAX_DOCS, RepoManifest, parse_repo_source
from lib.domain.types import Err, Ok
from lib.service.repo_clone import (
    SubprocessRunner,
    resolve_and_snapshot,
    write_repo_inventory,
)


def _manifest_json(manifest: RepoManifest) -> str:
    """Canonical (deterministic) JSON form of a manifest."""

    return (
        json.dumps(
            dataclasses.asdict(manifest),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def _process_source(
    text: str,
    *,
    wiki_root: Path,
    manifests_dir: Path,
    max_docs: int,
    full_clone: bool,
    refresh: bool,
) -> dict:
    """Handle one source; returns a per-repo status record (never raises
    for expected failures)."""

    parsed = parse_repo_source(text)
    if isinstance(parsed, Err):
        return {
            "source": text,
            "ok": False,
            "reason": f"parse_error: {parsed.error.value}",
        }

    if parsed.value.removed_userinfo:
        print(
            f"warning: credentials were removed from the URL for {parsed.value.host}",
            file=sys.stderr,
        )

    result = resolve_and_snapshot(
        parsed.value,
        wiki_root=wiki_root,
        runner=SubprocessRunner(),
        max_docs=max_docs,
        full_clone=full_clone,
        refresh=refresh,
    )
    if isinstance(result, Err):
        detail = f" ({result.detail})" if result.detail else ""
        return {
            "source": text,
            "ok": False,
            "reason": f"{result.error.value}{detail}",
        }
    manifest = result.value

    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifests_dir / f"{manifest.slug}.json"
    manifest_path.write_text(_manifest_json(manifest), encoding="utf-8")

    inventory = write_repo_inventory(manifest, wiki_root=wiki_root)
    if isinstance(inventory, Err):
        return {
            "source": text,
            "ok": False,
            "reason": f"inventory: {inventory.error.value}",
        }

    return {
        "source": text,
        "ok": True,
        "slug": manifest.slug,
        "revision": manifest.revision[:12],
        "docs": len(manifest.docs),
        "docs_total": manifest.docs_total,
        "docs_truncated": manifest.docs_truncated,
        "manifest_path": str(manifest_path),
        "inventory_path": str(inventory.value),
    }


def _print_summary(statuses: list[dict]) -> None:
    for status in statuses:
        if status["ok"]:
            truncated = "（truncated）" if status["docs_truncated"] else ""
            print(
                f"[ok] {status['slug']} @{status['revision']} — "
                f"docs {status['docs']}/{status['docs_total']}{truncated} — "
                f"manifest: {status['manifest_path']}"
            )
        else:
            print(f"[failed] {status['source']} — {status['reason']}")
    ok_count = sum(1 for s in statuses if s["ok"])
    failed = len(statuses) - ok_count
    print(f"── repo ingest 完了: {ok_count} ok / {failed} failed ──")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clone git repositories and emit snapshot manifests",
    )
    parser.add_argument("source", nargs="+", help="repo URL or local path")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--max-docs", type=int, default=DEFAULT_MAX_DOCS)
    parser.add_argument(
        "--full-clone",
        action="store_true",
        help="disable the shallow-clone default",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="update an existing clone before snapshotting",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="manifest output dir (default: {wiki_root}/.cache/manifests)",
    )
    args = parser.parse_args(argv)

    if args.max_docs < 1:
        print("error: --max-docs must be >= 1", file=sys.stderr)
        return 2

    wiki_root: Path = args.wiki_root
    manifests_dir: Path = args.output or (wiki_root / ".cache" / "manifests")

    statuses: list[dict] = []
    try:
        for text in args.source:
            statuses.append(
                _process_source(
                    text,
                    wiki_root=wiki_root,
                    manifests_dir=manifests_dir,
                    max_docs=args.max_docs,
                    full_clone=args.full_clone,
                    refresh=args.refresh,
                )
            )
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    _print_summary(statuses)
    return 0 if all(s["ok"] for s in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
