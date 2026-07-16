#!/usr/bin/env python3
"""wiki-tool-query CLI — 制約・監査付きアドホック集計の実行入口.

Thin handler only: 引数解析・layer composition・Result → exit code 変換。
ロジックは ``lib/domain/tool_query.py``（純粋）と ``lib/service/tool_*.py``
（I/O、DI 境界付き）に置く。

Usage::

    python3 tool_query_run.py catalog-validate --wiki-root .wiki [--format table|json]

Exit codes: 0 = 成功, 1 = policy 拒否・実行失敗, 2 = usage・引数不備,
130 = SIGINT 中断。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Support both "python tool_query_run.py" and "python -m tool_query_run".
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from lib.domain.types import is_err
from lib.service.tool_catalog import load_catalog


def _cmd_catalog_validate(args: argparse.Namespace) -> int:
    result = load_catalog(wiki_root=Path(args.wiki_root))
    if is_err(result):
        errors = [f"{result.error.value}: {result.detail}"]
        for line in errors:
            print(f"error: {line}", file=sys.stderr)
        if args.format == "json":
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False))
        return 1

    catalog = result.value
    tool_ids = [entry.tool_id for entry in catalog.entries]
    if args.format == "json":
        print(
            json.dumps(
                {"ok": True, "tools": tool_ids, "digest": catalog.digest},
                ensure_ascii=False,
            )
        )
    else:
        print("── catalog 検証 ──")
        print(f"tools: {len(tool_ids)} 件 ({', '.join(tool_ids) or 'なし'})")
        print(f"digest: {catalog.digest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tool_query_run.py",
        description="制約・監査付きアドホック集計（wiki-tool-query）",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_validate = subparsers.add_parser(
        "catalog-validate", help="catalog の schema 適合検証"
    )
    p_validate.add_argument("--wiki-root", required=True)
    p_validate.add_argument("--format", choices=["table", "json"], default="table")
    p_validate.set_defaults(handler=_cmd_catalog_validate)

    args = parser.parse_args(argv)

    # setlimit() による巨大値遮断（防御層）は Python 3.11+ 必須。
    # 満たさない環境で防御層を黙って欠落させないため起動を拒否する。
    if sys.version_info < (3, 11):
        print(
            "error: Python 3.11 以上が必要です"
            "（sqlite3 setlimit による防御層が使えません）",
            file=sys.stderr,
        )
        return 2

    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
