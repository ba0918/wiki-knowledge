#!/usr/bin/env python3
"""Ingest security check: path traversal / secret data / prompt injection.

SKILL.md の ingest「セキュリティチェック（必須）」節に散文として埋まっていた
正規表現群を単一の真実源としてここに抽出したもの。LLM が目視でパターン照合する
代わりに本スクリプトを実行し、✅/❌ サマリーをそのまま提示する。

Usage:
    python3 security_scan.py <file>... [--filename NAME] [--format table|json]
    python3 security_scan.py --stdin [--filename NAME]   # テキスト直接入力

Exit codes:
    0 = クリーン（保存に進んでよい）
    1 = 検出あり（ingest を中断する）
    2 = 引数エラー / 入力ファイル不在

Design: pure core (scan_text / check_filename / render_table) + thin CLI,
following the query_retrieve.py / graph_gen.py precedent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass


# ---------------------------------------------------------------------------
# Patterns — SKILL.md「セキュリティチェック（必須）」と同一
# ---------------------------------------------------------------------------

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_key", re.compile(r"(sk-|api[_-]?key|token)[a-zA-Z0-9_\-]{20,}")),
    ("email", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
    ("phone", re.compile(r"\b0[0-9]{1,4}-?[0-9]{1,4}-?[0-9]{4}\b")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
)

INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override_instructions",
        re.compile(
            r"(?i)(ignore|disregard|forget)\s+(all\s+)?(previous|above|prior)"
            r"\s+(instructions?|prompts?)"
        ),
    ),
    ("role_hijack", re.compile(r"(?i)you\s+are\s+now\s+")),
    ("system_prompt", re.compile(r"(?i)system\s*:\s*")),
)

# ファイル名セグメント: 英数字+ハイフン、拡張子のドットは許可、隠しファイル不可
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*$")

MAX_VALUE_LEN = 60

CHECK_LABELS = {
    "path": "パス traversal",
    "secret": "機密データ",
    "injection": "プロンプトインジェクション",
}


@dataclass(frozen=True)
class Finding:
    """1 件の検出結果。"""

    check: str  # "path" | "secret" | "injection"
    pattern: str  # パターン名（path の場合は拒否理由コード）
    line: int  # 1-indexed。path チェックは 0
    value: str  # 検出値（MAX_VALUE_LEN で切り詰め）
    file: str | None  # 走査対象の表示名（--stdin は None）


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

def scan_text(text: str, source: str | None = None) -> list[Finding]:
    """テキスト全体を機密データ + プロンプトインジェクションで走査する。"""
    findings: list[Finding] = []
    for line_num, line in enumerate(text.splitlines(), start=1):
        for check, patterns in (("secret", SECRET_PATTERNS), ("injection", INJECTION_PATTERNS)):
            for name, pattern in patterns:
                for m in pattern.finditer(line):
                    findings.append(
                        Finding(
                            check=check,
                            pattern=name,
                            line=line_num,
                            value=m.group(0)[:MAX_VALUE_LEN],
                            file=source,
                        )
                    )
    return findings


def check_filename(name: str) -> list[Finding]:
    """保存ファイル名（相対サブパス可）を検証する。

    先頭の ``./`` は正規化して除去。``..`` セグメント・絶対パス・
    英数字+ハイフン+拡張子ドット以外の文字を拒否する。
    """

    def _finding(code: str) -> Finding:
        return Finding(check="path", pattern=code, line=0, value=name, file=None)

    if not name:
        return [_finding("empty")]
    if name.startswith("/"):
        return [_finding("absolute_path")]

    normalized = name
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        return [_finding("empty")]

    segments = normalized.split("/")
    if ".." in segments:
        return [_finding("parent_traversal")]
    for segment in segments:
        if not _SEGMENT_RE.match(segment):
            return [_finding("invalid_chars")]
    return []


def render_table(findings: list[Finding], path_checked: bool) -> str:
    """SKILL.md 指定の ✅/❌ サマリー + 検出詳細を組み立てる。"""
    by_check = {"path": 0, "secret": 0, "injection": 0}
    for f in findings:
        by_check[f.check] += 1

    lines: list[str] = []
    for check in ("path", "secret", "injection"):
        label = CHECK_LABELS[check]
        if check == "path" and not path_checked:
            lines.append(f"⏭ {label}: SKIP（--filename 未指定）")
        elif by_check[check] == 0:
            lines.append(f"✅ {label}: OK")
        else:
            lines.append(f"❌ {label}: NG（{by_check[check]} 件検出）")

    if findings:
        lines.append("")
        lines.append("検出内容:")
        for f in findings:
            location = f"{f.file or '-'}:{f.line}"
            lines.append(f"  - {location} [{f.pattern}] {f.value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest security check (path traversal / secrets / prompt injection)"
    )
    parser.add_argument("files", nargs="*", help="走査対象ファイル")
    parser.add_argument("--stdin", action="store_true", help="標準入力からテキストを走査")
    parser.add_argument("--filename", help="保存予定のファイル名（パス traversal 検証）")
    parser.add_argument("--format", choices=["table", "json"], default="table")
    args = parser.parse_args(argv)

    if not args.files and not args.stdin and args.filename is None:
        parser.print_usage(sys.stderr)
        print("error: ファイル・--stdin・--filename のいずれかを指定してください", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    path_checked = args.filename is not None
    if path_checked:
        findings.extend(check_filename(args.filename))

    for filepath in args.files:
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            print(f"error: {filepath} を読めません: {e}", file=sys.stderr)
            return 2
        findings.extend(scan_text(text, source=filepath))

    if args.stdin:
        findings.extend(scan_text(sys.stdin.read(), source=None))

    if args.format == "json":
        counts = {"path": 0, "secret": 0, "injection": 0}
        for f in findings:
            counts[f.check] += 1
        print(
            json.dumps(
                {
                    "ok": not findings,
                    "path_checked": path_checked,
                    "counts": counts,
                    "findings": [asdict(f) for f in findings],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render_table(findings, path_checked=path_checked))

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
