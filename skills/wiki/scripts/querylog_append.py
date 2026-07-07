#!/usr/bin/env python3
"""QueryLog エントリの組み立て・検証・追記。

SKILL.md の query「QueryLog 追記」節に散文として埋まっていた手順
（id 生成 / sources_cited の正規表現抽出 / concepts フィルタ / JSONL 追記）を
テスト済みスクリプトに抽出したもの。schema-of-record は
``{wiki_root}/schema/querylog-schema.json``。

Usage:
    python3 querylog_append.py --wiki-root .wiki \
        --question "Trust Score はどう計算される？" \
        --consulted concepts/trust-score.md concepts/querylog.md \
        --answer-file /path/to/answer.md \
        [--gap-topics "RAG architecture" ...] \
        [--promoted --promoted-to concepts/new-article.md] \
        [--format table|json]

Exit codes:
    0 = 追記成功
    1 = エントリ検証エラー（追記しない）
    2 = 引数エラー / 入力ファイル不在

Design: pure core (extract_cited / build_entry / validate_entry) + thin CLI。
時刻は lib.service.clock（DI 境界）から取得し、``--now`` で再現可能に上書きできる。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from lib.service.clock import SystemClock


WIKILINK_RE = re.compile(r"\[\[([a-z0-9-]+)\]\]")
ID_RE = re.compile(r"^q_\d{8}T\d{6}$")
ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z$")
CONSULTED_RE = re.compile(r"^concepts/[^/]+\.md$")

# schema-of-record（querylog-schema.json）の required と同期。
# test_querylog_append.py が機械的に同期を検証する。
REQUIRED_FIELDS = (
    "id",
    "timestamp",
    "question",
    "sources_consulted",
    "sources_cited",
    "gap_noted",
    "gap_topics",
    "promoted",
    "promoted_to",
)


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

def extract_cited(answer_text: str) -> list[str]:
    """回答テキストから ``[[wikilink]]`` を抽出し ``concepts/{slug}.md`` に変換する。

    重複は初出順を保って除去する。
    """
    seen: list[str] = []
    for slug in WIKILINK_RE.findall(answer_text):
        path = f"concepts/{slug}.md"
        if path not in seen:
            seen.append(path)
    return seen


def entry_id_from_iso(iso: str) -> str:
    """ISO 8601 UTC タイムスタンプから ``q_{YYYYMMDDTHHMMSS}`` 形式の id を生成する。"""
    m = ISO_RE.match(iso)
    if not m:
        raise ValueError(f"ISO 8601 UTC (Z) 形式ではありません: {iso!r}")
    y, mo, d, h, mi, s = m.groups()
    return f"q_{y}{mo}{d}T{h}{mi}{s}"


def build_entry(
    question: str,
    consulted: list[str],
    answer_text: str,
    gap_topics: list[str],
    promoted: bool,
    promoted_to: str | None,
    now: str,
) -> dict:
    """QueryLog エントリを組み立てる。

    * ``sources_consulted`` は ``concepts/*.md`` のみ残す（index.md 等は除外）
    * ``gap_noted`` は ``gap_topics`` の有無から導出する
    """
    filtered = [src for src in consulted if CONSULTED_RE.match(src)]
    return {
        "id": entry_id_from_iso(now),
        "timestamp": now,
        "question": question,
        "sources_consulted": filtered,
        "sources_cited": extract_cited(answer_text),
        "gap_noted": bool(gap_topics),
        "gap_topics": list(gap_topics),
        "promoted": promoted,
        "promoted_to": promoted_to,
    }


def validate_entry(entry: dict) -> list[str]:
    """schema-of-record 準拠と整合性を検証し、エラーメッセージのリストを返す。"""
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in entry:
            errors.append(f"必須フィールド欠損: {field}")
    if errors:
        return errors

    if not isinstance(entry["id"], str) or not ID_RE.match(entry["id"]):
        errors.append(f"id が q_YYYYMMDDTHHMMSS 形式ではない: {entry['id']!r}")
    if not isinstance(entry["question"], str) or not entry["question"]:
        errors.append("question が空")
    for field in ("sources_consulted", "sources_cited", "gap_topics"):
        if not isinstance(entry[field], list):
            errors.append(f"{field} が配列ではない")
    for field in ("gap_noted", "promoted"):
        if not isinstance(entry[field], bool):
            errors.append(f"{field} が boolean ではない")
    if entry["promoted"] and not entry["promoted_to"]:
        errors.append("promoted=true なのに promoted_to が null")
    if not entry["promoted"] and entry["promoted_to"] is not None:
        errors.append("promoted=false なのに promoted_to が設定されている")
    return errors


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def append_jsonl(path: Path, entry: dict) -> None:
    """エントリを JSON 1 行として排他ロック付きで追記する。"""
    line = json.dumps(entry, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        try:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except ImportError:  # 非 POSIX 環境はロックなしで追記
            pass
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append a QueryLog entry (JSONL)")
    parser.add_argument("--wiki-root", required=True, help="wiki ルート（例: .wiki）")
    parser.add_argument("--question", required=True, help="ユーザの元の質問文")
    parser.add_argument(
        "--consulted", nargs="*", default=[],
        help="読み込んだ記事パス（{wiki_root} 相対。concepts/*.md のみ記録される）",
    )
    answer_group = parser.add_mutually_exclusive_group(required=True)
    answer_group.add_argument("--answer-file", help="回答全文のファイルパス（cited 抽出元）")
    answer_group.add_argument(
        "--answer-stdin", action="store_true", help="回答全文を標準入力から読む"
    )
    parser.add_argument("--gap-topics", nargs="*", default=[], help="指摘したギャップのトピック名")
    parser.add_argument("--promoted", action="store_true", help="回答を concepts/ に昇格した")
    parser.add_argument("--promoted-to", default=None, help="昇格先パス（--promoted 時に必須）")
    parser.add_argument(
        "--now", default=None,
        help="ISO 8601 UTC タイムスタンプの上書き（テスト・リプレイ用。省略時は現在時刻）",
    )
    parser.add_argument("--format", choices=["table", "json"], default="table")
    args = parser.parse_args(argv)

    if args.answer_file:
        try:
            answer_text = Path(args.answer_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"error: {args.answer_file} を読めません: {e}", file=sys.stderr)
            return 2
    else:
        answer_text = sys.stdin.read()

    now = args.now if args.now else SystemClock().now()
    try:
        entry = build_entry(
            question=args.question,
            consulted=args.consulted,
            answer_text=answer_text,
            gap_topics=args.gap_topics,
            promoted=args.promoted,
            promoted_to=args.promoted_to,
            now=now,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    errors = validate_entry(entry)
    if errors:
        for err in errors:
            print(f"validation error: {err}", file=sys.stderr)
        return 1

    logfile = Path(args.wiki_root) / "outputs" / "querylog.jsonl"
    append_jsonl(logfile, entry)

    if args.format == "json":
        print(json.dumps(entry, ensure_ascii=False, indent=2))
    else:
        print("── querylog 追記 ──")
        print(f"id: {entry['id']}")
        print(
            f"consulted: {len(entry['sources_consulted'])} 件, "
            f"cited: {len(entry['sources_cited'])} 件, "
            f"gaps: {entry['gap_topics'] or 'なし'}, "
            f"promoted: {entry['promoted']}"
        )
        print(f"→ {logfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
