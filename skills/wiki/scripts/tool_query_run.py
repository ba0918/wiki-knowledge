#!/usr/bin/env python3
"""wiki-tool-query CLI — 制約・監査付きアドホック集計の実行入口.

Thin handler only: 引数解析・layer composition・Result → exit code 変換。
ロジックは ``lib/domain/tool_query.py``（純粋）と ``lib/service/tool_*.py``
（I/O、DI 境界付き）に置く。

Usage::

    python3 tool_query_run.py catalog-validate --wiki-root .wiki
    python3 tool_query_run.py prepare --wiki-root .wiki --tool <id> \
        --sql-file q.sql --count-sql "<label>=<path>" ... \
        --key-columns <col>... --expected-rows <min>:<max> --deliver-to <dir>
    python3 tool_query_run.py approve --wiki-root .wiki --plan <plan_id> \
        --approved-by <name>
    python3 tool_query_run.py execute --wiki-root .wiki --plan <plan_id>

stdout = 結果データ（table/json）、stderr = 進捗・診断・承認プロンプト。
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

from lib.domain.tool_query import RejectReason
from lib.domain.types import is_err, is_ok
from lib.service.clock import SystemClock
from lib.service.file_lock import RealFileLock
from lib.service.tool_catalog import load_catalog, resolve_entry
from lib.service.tool_doctor import CheckStatus, Doctor, DoctorError
from lib.service.tool_query_runner import CountSql, RunnerReason, ToolQueryRunner


# 「引数の直しようがある」失敗は usage エラー（exit 2）に分類する
_USAGE_REASONS = frozenset(
    {
        RejectReason.INVALID_PLAN_ID.value,
        RejectReason.INVALID_LABEL.value,
        RejectReason.INVALID_ROWS_RANGE.value,
        RejectReason.INVALID_APPROVED_BY.value,
        RunnerReason.SQL_FILE_UNREADABLE.value,
        "unknown_tool",
    }
)


def _progress(message: str) -> None:
    print(message, file=sys.stderr)


def _fail(result) -> int:
    reason = result.error.value if hasattr(result.error, "value") else str(result.error)
    print(f"error: {reason}: {result.detail}", file=sys.stderr)
    return 2 if reason in _USAGE_REASONS else 1


def _build_runner(wiki_root: str) -> ToolQueryRunner:
    return ToolQueryRunner(
        wiki_root=Path(wiki_root),
        clock=SystemClock(),
        lock=RealFileLock(),
    )


def _build_doctor(wiki_root: str) -> Doctor:
    return Doctor(
        wiki_root=Path(wiki_root),
        clock=SystemClock(),
        lock=RealFileLock(),
    )


def _entry_type(wiki_root: str, tool_id: str) -> str | None:
    """CLI 表示・flag 検証用の type 参照（失敗時は None — 実行時検証は runner）。"""

    catalog = load_catalog(wiki_root=Path(wiki_root))
    if is_err(catalog):
        return None
    entry = resolve_entry(catalog.value, tool_id)
    return entry.value.type if is_ok(entry) else None


# ---------------------------------------------------------------------------
# catalog-validate
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def _parse_count_sql(raw: str) -> CountSql:
    label, sep, path = raw.partition("=")
    if not sep or not label or not path:
        raise argparse.ArgumentTypeError(
            f"<label>=<path> 形式が必要: {raw!r}"
        )
    return CountSql(label=label, path=Path(path))


def _parse_expected_rows(raw: str) -> tuple[int, int]:
    lo, sep, hi = raw.partition(":")
    if not sep or not lo.isdigit() or not hi.isdigit():
        raise argparse.ArgumentTypeError(
            f"--expected-rows は <min>:<max>（非負整数）形式が必要: {raw!r}"
        )
    return (int(lo), int(hi))


def _funnel_lines(funnel) -> list[str]:
    return [f"  {step.label}: {step.row_count} 件" for step in funnel]


def _resolve_prepare_payload(
    args: argparse.Namespace,
) -> tuple[Path, list[CountSql]] | int:
    """tool type と payload flag（SQL 系 / http 系）の整合を検証して解決する。

    catalog が読めない・tool が未知の場合は type 不明として SQL 系 flag を
    そのまま通し、実行時検証（runner）に委ねる。
    """

    tool_type = _entry_type(args.wiki_root, args.tool)
    is_http = tool_type == "http"
    has_sql = args.sql_file is not None or args.count_sql
    has_request = args.request_file is not None or args.count_request

    if is_http:
        if has_sql:
            print(
                "error: http tool では --request-file / --count-request を"
                "使ってください（SQL ではなく request spec を渡す）",
                file=sys.stderr,
            )
            return 2
        if args.request_file is None or not args.count_request:
            print(
                "error: http tool には --request-file と --count-request が必要です",
                file=sys.stderr,
            )
            return 2
        return Path(args.request_file), list(args.count_request)

    if has_request:
        print(
            "error: --request-file / --count-request は http tool 専用です"
            "（SQL 系 tool では --sql-file / --count-sql を使用）",
            file=sys.stderr,
        )
        return 2
    if args.sql_file is None or not args.count_sql:
        print(
            "error: --sql-file と --count-sql が必要です",
            file=sys.stderr,
        )
        return 2
    return Path(args.sql_file), list(args.count_sql)


def _cmd_prepare(args: argparse.Namespace) -> int:
    payload = _resolve_prepare_payload(args)
    if isinstance(payload, int):
        return payload
    sql_path, count_sqls = payload
    is_http = _entry_type(args.wiki_root, args.tool) == "http"
    digest_label = "request_digest" if is_http else "sql_digest"

    _progress("catalog 検証 → 接続 → ファネル COUNT 実行 → bundle 生成")
    runner = _build_runner(args.wiki_root)
    result = runner.prepare(
        tool_id=args.tool,
        sql_path=sql_path,
        count_sqls=count_sqls,
        key_columns=tuple(args.key_columns),
        expected_rows=args.expected_rows,
        deliver_to=args.deliver_to,
    )
    if is_err(result):
        return _fail(result)
    outcome = result.value
    if args.format == "json":
        payload_json = {
            "plan_id": outcome.plan_id,
            "tool_id": outcome.tool_id,
            "funnel": [
                {"label": s.label, "row_count": s.row_count}
                for s in outcome.funnel
            ],
            "sql_digest": outcome.sql_digest,
            "sql_display_digest": outcome.sql_display_digest,
            "expected_rows": {
                "min": outcome.expected_rows[0],
                "max": outcome.expected_rows[1],
            },
            "delivery_dir": outcome.delivery_dir,
            "expires_at": outcome.expires_at,
        }
        if is_http:
            # bundle 内部 field は Phase A 互換の sql_digest のまま、
            # 表示層でのみ中立名を併記する
            payload_json["request_digest"] = outcome.sql_digest
        print(json.dumps(payload_json, ensure_ascii=False))
    else:
        print("── proposal 生成 ──")
        print(f"plan_id: {outcome.plan_id}")
        print(f"tool: {outcome.tool_id}")
        print("funnel:")
        for line in _funnel_lines(outcome.funnel):
            print(line)
        print(
            f"想定件数: {outcome.expected_rows[0]}〜{outcome.expected_rows[1]} 件"
        )
        print(f"delivery: {outcome.delivery_dir}")
        print(f"{digest_label}: {outcome.sql_digest}")
        print(f"expires_at: {outcome.expires_at}")
    return 0


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def _cmd_approve(args: argparse.Namespace) -> int:
    # パイプ越しの自動承認を作らない — 確認プロンプトは TTY 必須
    if not sys.stdin.isatty():
        print(
            "error: approve は対話 TTY でのみ実行できます"
            "（パイプ・リダイレクト経由の承認は無効）",
            file=sys.stderr,
        )
        return 2

    runner = _build_runner(args.wiki_root)
    preview_result = runner.approve_preview(args.plan)
    if is_err(preview_result):
        return _fail(preview_result)
    preview = preview_result.value

    digest_label = (
        "request_digest"
        if _entry_type(args.wiki_root, preview.tool_id) == "http"
        else "sql_digest"
    )
    # summary と確認プロンプトは stderr（--format json の stdout を汚染しない）
    print("── 承認対象 ──", file=sys.stderr)
    print(f"plan_id: {preview.plan_id}", file=sys.stderr)
    print(f"tool: {preview.tool_id}", file=sys.stderr)
    print(f"{digest_label}: {preview.sql_digest}", file=sys.stderr)
    print(
        f"想定件数: {preview.expected_rows[0]}〜{preview.expected_rows[1]} 件",
        file=sys.stderr,
    )
    print(f"delivery: {preview.delivery_dir}", file=sys.stderr)
    print(f"expires_at: {preview.expires_at}", file=sys.stderr)
    for line in _funnel_lines(preview.funnel):
        print(line, file=sys.stderr)

    print("承認しますか？（yes と入力で承認）: ", file=sys.stderr, end="", flush=True)
    answer = sys.stdin.readline()
    if answer.strip() != "yes":
        print("未承認のまま終了します（plan は draft のまま）", file=sys.stderr)
        return 1

    result = runner.approve_commit(
        args.plan,
        approved_by=args.approved_by,
        expected_proposal_digest=preview.proposal_digest,
    )
    if is_err(result):
        return _fail(result)
    state = result.value
    if args.format == "json":
        print(
            json.dumps(
                {
                    "approved": True,
                    "plan_id": preview.plan_id,
                    "approved_by": state.approved_by,
                    "approved_at": state.approved_at,
                },
                ensure_ascii=False,
            )
        )
    else:
        print("── 承認完了 ──")
        print(f"plan_id: {preview.plan_id}")
        print(f"approved_by: {state.approved_by} at {state.approved_at}")
    return 0


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def _cmd_execute(args: argparse.Namespace) -> int:
    _progress("catalog 検証 → bundle 検証 → 接続 → 実行 → 検証 → publish")
    runner = _build_runner(args.wiki_root)
    result = runner.execute(args.plan)
    if is_err(result):
        return _fail(result)
    outcome = result.value
    for warning in outcome.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "run_id": outcome.run_id,
                    "row_count": outcome.row_count,
                    "duplicate_key_count": outcome.duplicate_key_count,
                    "null_counts": outcome.null_counts,
                    "csv_sha256": outcome.csv_sha256,
                    "sanitized_cell_count": outcome.sanitized_cell_count,
                    "delivery_dir": outcome.delivery_dir,
                    "published_at": outcome.published_at,
                    "data_as_of": outcome.data_as_of,
                    "warnings": list(outcome.warnings),
                },
                ensure_ascii=False,
            )
        )
    else:
        print("── 実行完了 ──")
        print(f"run_id: {outcome.run_id}")
        print(f"取得件数: {outcome.row_count} 件")
        print(
            f"manifest: 重複 key {outcome.duplicate_key_count} 件 / "
            f"無害化セル {outcome.sanitized_cell_count} 件"
        )
        print(f"csv_sha256: {outcome.csv_sha256}")
        print(f"delivery: {outcome.delivery_dir}/{outcome.run_id}/")
        print(f"published_at: {outcome.published_at}")
        print(f"data_as_of: {outcome.data_as_of}")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


_STATUS_LABEL = {
    CheckStatus.OK: "OK",
    CheckStatus.NG: "NG",
    CheckStatus.SKIP: "SKIP",
}


def _cmd_doctor(args: argparse.Namespace) -> int:
    doctor = _build_doctor(args.wiki_root)
    result = doctor.run_checked(
        tool=args.tool, probe_write=args.probe_write, announce=_progress
    )
    if is_err(result):
        print(f"error: {result.error.value}: {result.detail}", file=sys.stderr)
        return (
            2
            if result.error in (DoctorError.UNKNOWN_TOOL, DoctorError.INVALID_PROBE)
            else 1
        )
    report = result.value

    if args.format == "json":
        print(
            json.dumps(
                {
                    "diagnoses": [
                        {
                            "tool_id": d.tool_id,
                            "type": d.type,
                            "checks": [
                                {
                                    "check": o.check,
                                    "status": o.status.value,
                                    "reason_code": o.reason_code,
                                    "hint": o.hint,
                                }
                                for o in d.outcomes
                            ],
                        }
                        for d in report.diagnoses
                    ],
                    "skip_summary": report.skip_summary(),
                    "required_skips": report.required_skips(),
                    "ok": not report.has_ng(),
                },
                ensure_ascii=False,
            )
        )
    else:
        # 固定列: tool / check / status / reason_code / hint
        rows = [
            (
                diag.tool_id,
                o.check,
                _STATUS_LABEL[o.status],
                o.reason_code,
                o.hint if o.status != CheckStatus.OK else "",
            )
            for diag in report.diagnoses
            for o in diag.outcomes
        ]
        headers = ("tool", "check", "status", "reason_code", "hint")
        widths = [len(h) for h in headers]
        for r in rows:
            for i, cell in enumerate(r[:4]):
                widths[i] = max(widths[i], len(cell))
        print(
            "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers[:4]))
            + "  hint"
        )
        for r in rows:
            line = "  ".join(r[i].ljust(widths[i]) for i in range(4))
            print(f"{line}  {r[4]}")
        skips = report.skip_summary()
        skip_total = sum(skips.values())
        print(f"SKIP: {skip_total} 件" + (f" ({skips})" if skips else ""))
        required_skips = report.required_skips()
        if report.has_ng():
            verdict = "NG あり"
        elif required_skips:
            verdict = (
                f"NG なし（必須 check の SKIP {len(required_skips)} 件あり"
                " — 未検証項目を確認）"
            )
        else:
            verdict = "必須 check 全 OK"
        print("判定: " + verdict)

    return 1 if report.has_ng() else 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wiki-root", required=True)
    parser.add_argument("--format", choices=["table", "json"], default="table")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tool_query_run.py",
        description="制約・監査付きアドホック集計（wiki-tool-query）",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_validate = subparsers.add_parser(
        "catalog-validate", help="catalog の schema 適合検証"
    )
    _add_common(p_validate)
    p_validate.set_defaults(handler=_cmd_catalog_validate)

    p_prepare = subparsers.add_parser(
        "prepare", help="ファネル COUNT 実行 + proposal bundle 生成"
    )
    _add_common(p_prepare)
    p_prepare.add_argument("--tool", required=True)
    p_prepare.add_argument("--sql-file", help="SQL 系 tool（sqlite/postgres/mysql）用")
    p_prepare.add_argument(
        "--count-sql",
        action="append",
        type=_parse_count_sql,
        help="<label>=<path>（複数可、順序保持。SQL 系 tool 用）",
    )
    p_prepare.add_argument(
        "--request-file", help="http tool 用の request spec（JSON）"
    )
    p_prepare.add_argument(
        "--count-request",
        action="append",
        type=_parse_count_sql,
        help="<label>=<path>（http tool 用。spec は count_path を持つ）",
    )
    p_prepare.add_argument("--key-columns", nargs="+", required=True)
    p_prepare.add_argument(
        "--expected-rows",
        required=True,
        type=_parse_expected_rows,
        help="<min>:<max>",
    )
    p_prepare.add_argument("--deliver-to", required=True)
    p_prepare.set_defaults(handler=_cmd_prepare)

    p_approve = subparsers.add_parser(
        "approve", help="人間による承認記録（draft → approved）"
    )
    _add_common(p_approve)
    p_approve.add_argument("--plan", required=True)
    p_approve.add_argument("--approved-by", required=True)
    p_approve.set_defaults(handler=_cmd_approve)

    p_execute = subparsers.add_parser(
        "execute", help="bundle 検証 → 本実行 → CSV + manifest → 監査 → consumed"
    )
    _add_common(p_execute)
    p_execute.add_argument("--plan", required=True)
    p_execute.set_defaults(handler=_cmd_execute)

    p_doctor = subparsers.add_parser(
        "doctor", help="接続疎通・read-only 状態・delivery 書込可否のローカル診断"
    )
    _add_common(p_doctor)
    p_doctor.add_argument("--tool", help="診断対象を 1 tool に絞る")
    p_doctor.add_argument(
        "--probe-write",
        metavar="TOOL_ID",
        help="canary relation への INSERT を試行し拒否を確認（二重 opt-in）",
    )
    p_doctor.set_defaults(handler=_cmd_doctor)

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
        # staging cleanup は runner の finally が実施済み。状態を告知して 130
        if args.subcommand in ("prepare", "execute"):
            print(
                "中断しました（staging は削除済み。plan の状態は state.json を参照）",
                file=sys.stderr,
            )
        else:
            print("中断しました", file=sys.stderr)
        return 130
    except OSError as exc:
        # 未捕捉の I/O 失敗（durable 書込み等）でも exit code 契約（0/1/2/130）を守る
        print(f"error: io_failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
