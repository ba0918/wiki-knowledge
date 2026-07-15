#!/usr/bin/env python3
"""source_scan CLI — classify repository source files into domain-knowledge categories.

Thin handler only: argument parsing, layer composition, Result → exit code
mapping, and table/json output. All logic lives in ``lib/domain/source_scan.py``
(pure) and ``lib/service/source_scan_io.py`` (I/O with DI).

Usage::

    python source_scan.py --wiki-root .wiki --slug myapp \
        [--categories schema,routes,tests] [--max-files 100] [--format table|json]

Exit codes: 0 = success (including empty results), 1 = failure
(manifest not found, clone path not found, etc.), 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from lib.domain.source_scan import (
    SourceCategory,
    classify_source_files,
)
from lib.domain.types import Err, Ok
from lib.service.source_scan_io import (
    SubprocessRunner,
    get_doc_paths_from_manifest,
    list_files_with_sizes,
    load_manifest,
    resolve_clone_path,
)


def _print_table(result) -> None:
    if not result.candidates:
        print(f"[{result.slug}] 候補なし（skipped: {result.skipped_count}）")
        return

    print(f"[{result.slug} @{result.revision[:12]}] "
          f"{len(result.candidates)} candidates, {result.skipped_count} skipped")
    print()

    max_path = max(len(c.path) for c in result.candidates)
    max_path = min(max_path, 60)
    header = f"{'PATH':<{max_path}}  {'CATEGORY':<10}  {'CONF':>5}  {'SIZE':>8}  {'WARN'}"
    print(header)
    print("-" * len(header))

    for c in result.candidates:
        path_display = c.path if len(c.path) <= max_path else "..." + c.path[-(max_path - 3):]
        warn = "⚠ LARGE" if c.large_file_warning else ""
        size_display = f"{c.size_bytes:>8}"
        print(f"{path_display:<{max_path}}  {c.category.value:<10}  {c.confidence:>5.2f}  {size_display}  {warn}")

    print()
    print("Stats:")
    for cat, count in sorted(result.stats.items()):
        if count > 0:
            print(f"  {cat}: {count}")


def _print_json(result) -> None:
    output = {
        "slug": result.slug,
        "revision": result.revision,
        "candidates": [
            {
                "path": c.path,
                "category": c.category.value,
                "confidence": c.confidence,
                "size_bytes": c.size_bytes,
                "large_file_warning": c.large_file_warning,
            }
            for c in result.candidates
        ],
        "stats": result.stats,
        "skipped_count": result.skipped_count,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify repository source files into domain-knowledge categories",
    )
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--slug", required=True, help="repository slug from repo_ingest manifest")
    parser.add_argument(
        "--categories",
        help="comma-separated category filter (default: all)",
    )
    parser.add_argument("--max-files", type=int, default=100, help="max files per category")
    parser.add_argument("--format", choices=["table", "json"], default="table")

    args = parser.parse_args(argv)

    manifest_result = load_manifest(args.wiki_root, args.slug)
    if isinstance(manifest_result, Err):
        print(f"error: {manifest_result.error.value}: {manifest_result.detail}", file=sys.stderr)
        return 1

    manifest = manifest_result.value

    clone_result = resolve_clone_path(manifest)
    if isinstance(clone_result, Err):
        print(f"error: {clone_result.error.value}: {clone_result.detail}", file=sys.stderr)
        return 1

    clone_path = clone_result.value

    runner = SubprocessRunner()
    files_result = list_files_with_sizes(clone_path, runner)
    if isinstance(files_result, Err):
        print(f"error: {files_result.error.value}: {files_result.detail}", file=sys.stderr)
        return 1

    file_list = [(e.path, e.size_bytes) for e in files_result.value]
    exclude_paths = get_doc_paths_from_manifest(manifest)

    categories = None
    if args.categories:
        try:
            categories = frozenset(
                SourceCategory(c.strip()) for c in args.categories.split(",")
            )
        except ValueError as exc:
            print(f"error: invalid category: {exc}", file=sys.stderr)
            return 2

    scan_result = classify_source_files(
        file_list,
        slug=args.slug,
        revision=manifest.get("revision", "unknown"),
        exclude_paths=exclude_paths,
        categories=categories,
        max_files_per_category=args.max_files,
    )

    if args.format == "json":
        _print_json(scan_result)
    else:
        _print_table(scan_result)

    return 0


if __name__ == "__main__":
    sys.exit(main())
