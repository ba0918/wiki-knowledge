#!/usr/bin/env python3
"""log.md 操作ログの定型追記。

SKILL.md の各ワークフロー（ingest / compile / promote / query / lint / discover）に
散在していた ``## [YYYY-MM-DD] {op} | ...`` テンプレートを単一の真実源として
ここに抽出したもの。単複の使い分け（1 source / 2 sources）等のフォーマット
ドリフトをスクリプト側で吸収する。

Usage:
    python3 log_append.py ingest  --wiki-root .wiki --slug foo-bar --source-kind article
    python3 log_append.py compile --wiki-root .wiki --title "Trust Score" \
        --word-count 320 --sources 2
    python3 log_append.py promote --wiki-root .wiki --title "RAG アーキテクチャ"
    python3 log_append.py query   --wiki-root .wiki --summary "Trust Score の計算方法"
    python3 log_append.py lint    --wiki-root .wiki --errors 0 --warnings 3 --info 1
    python3 log_append.py discover --wiki-root .wiki --slug myapp --articles 4

共通オプション: --date YYYY-MM-DD（省略時はローカル今日）/ --note（末尾に「 — {note}」を付す）

Exit codes:
    0 = 追記成功
    1 = log.md 不在（wiki-init 未実行）
    2 = 引数エラー
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

OPS = ("ingest", "compile", "promote", "query", "lint", "discover")


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

def format_entry(op: str, entry_date: str, fields: dict, note: str | None = None) -> str:
    """SKILL.md 指定の log.md エントリ 1 行を組み立てる。"""
    if op == "ingest":
        body = f"{fields['slug']} ({fields['source_kind']})"
    elif op == "compile":
        n = fields["sources"]
        unit = "source" if n == 1 else "sources"
        body = f"{fields['title']} ({fields['word_count']} words, {n} {unit})"
    elif op == "promote":
        body = f"{fields['title']} (from query)"
    elif op == "query":
        body = fields["summary"]
    elif op == "lint":
        body = f"{fields['errors']} errors, {fields['warnings']} warnings, {fields['info']} info"
    elif op == "discover":
        n = fields["articles"]
        unit = "article" if n == 1 else "articles"
        body = f"{fields['slug']} ({n} {unit})"
    else:
        raise ValueError(f"未知の op: {op!r}（{'/'.join(OPS)} のいずれか）")

    line = f"## [{entry_date}] {op} | {body}"
    if note:
        line += f" — {note}"
    return line


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def append_line(log_path: Path, line: str) -> None:
    """log.md 末尾にエントリを追記する。EOF に改行がなければ補う。"""
    if not log_path.exists():
        raise FileNotFoundError(f"{log_path} が存在しません（wiki-init 未実行？）")
    tail = log_path.read_bytes()[-1:]
    with log_path.open("a", encoding="utf-8") as f:
        if tail and tail != b"\n":
            f.write("\n")
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append a log.md operation entry")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--wiki-root", required=True, help="wiki ルート（例: .wiki）")
    common.add_argument("--date", default=None, help="YYYY-MM-DD（省略時はローカル今日）")
    common.add_argument("--note", default=None, help="末尾に「 — {note}」を付す自由記述")

    subparsers = parser.add_subparsers(dest="op", required=True)

    p_ingest = subparsers.add_parser("ingest", parents=[common])
    p_ingest.add_argument("--slug", required=True)
    p_ingest.add_argument("--source-kind", required=True, help="article / file / repo @ {hash} 等")

    p_compile = subparsers.add_parser("compile", parents=[common])
    p_compile.add_argument("--title", required=True)
    p_compile.add_argument("--word-count", type=int, required=True)
    p_compile.add_argument("--sources", type=int, required=True)

    p_promote = subparsers.add_parser("promote", parents=[common])
    p_promote.add_argument("--title", required=True)

    p_query = subparsers.add_parser("query", parents=[common])
    p_query.add_argument("--summary", required=True, help="質問の短い要約（保存ファイル title と同一）")

    p_lint = subparsers.add_parser("lint", parents=[common])
    p_lint.add_argument("--errors", type=int, required=True)
    p_lint.add_argument("--warnings", type=int, required=True)
    p_lint.add_argument("--info", type=int, required=True)

    p_discover = subparsers.add_parser("discover", parents=[common])
    p_discover.add_argument("--slug", required=True)
    p_discover.add_argument("--articles", type=int, required=True, help="生成された記事数")

    args = parser.parse_args(argv)

    entry_date = args.date if args.date else date.today().isoformat()
    if not DATE_RE.match(entry_date):
        print(f"error: --date は YYYY-MM-DD 形式で指定してください: {entry_date!r}", file=sys.stderr)
        return 2

    fields = {
        k: v
        for k, v in vars(args).items()
        if k not in ("op", "wiki_root", "date", "note")
    }
    line = format_entry(args.op, entry_date, fields, note=args.note)

    log_path = Path(args.wiki_root) / "log.md"
    try:
        append_line(log_path, line)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
