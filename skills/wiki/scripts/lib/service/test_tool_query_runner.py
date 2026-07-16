"""Tests for tool_query_runner.py — prepare/approve/execute の application service.

状態遷移 acceptance test（成功経路 + 全拒否経路）、出力上限 enforcement、
fault-injection（クラッシュポイント表の検証）、並行 CAS、そして
**実案件 replay fixture**（イベント補填対象者抽出を模した合成 fixture と
「手動集計結果」役の期待 CSV との機械照合）を含む。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from lib.domain.tool_query import RejectReason
from lib.domain.types import Err, Ok, is_err, is_ok
from lib.service.clock import FixedClock
from lib.service.file_lock import FakeFileLock, RealFileLock
from lib.service.tool_audit import AuditLog
from lib.service import tool_query_runner
from lib.service.tool_query_runner import (
    CountSql,
    RunnerReason,
    ToolQueryRunner,
)


NOW = "2026-07-16T12:00:00Z"


class SimulatedCrash(BaseException):
    """fault-injection 用 — 通常の except 節に吸収されない中断。"""


def _seq_nonce(*values: str):
    it = iter(values)
    return lambda: next(it)


def make_wiki(
    tmp_path: Path,
    *,
    limits: dict | None = None,
    expected_key: str = "user_id",
) -> Path:
    """catalog + sqlite fixture + delivery dir を持つ wiki_root を組み立てる。"""

    wiki_root = tmp_path / "wiki"
    (wiki_root / "tools").mkdir(parents=True)
    (wiki_root / "data").mkdir()
    (wiki_root / "deliveries").mkdir()
    (wiki_root / "outputs").mkdir()

    db = wiki_root / "data" / "events.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, name TEXT, email TEXT);
        CREATE TABLE registrations (user_id INTEGER, event TEXT);
        CREATE TABLE refunds (user_id INTEGER, amount INTEGER);
        INSERT INTO users VALUES
            (1, 'alice', 'alice@example.com'),
            (2, 'bob', 'bob@example.com'),
            (3, 'carol', 'carol@example.com'),
            (4, 'dave', 'dave@example.com'),
            (5, 'erin', 'erin@example.com');
        INSERT INTO registrations VALUES
            (1, 'ev-2026'), (2, 'ev-2026'), (3, 'ev-2026'), (4, 'ev-2026'),
            (5, 'other-event');
        INSERT INTO refunds VALUES (2, 500);
        """
    )
    conn.commit()
    conn.close()

    catalog = {
        "schema_version": 1,
        "tools": [
            {
                "tool_id": "events-db",
                "type": "sqlite",
                "connection": {"path": "data/events.sqlite3"},
                "allowed_tables": ["users", "registrations", "refunds"],
                "limits": limits
                or {
                    "max_rows": 1000,
                    "max_result_bytes": 1048576,
                    "max_cell_bytes": 4096,
                    "timeout_sec": 30,
                },
                "allowed_statements": ["select"],
                "delivery": {"allowed_dirs": ["deliveries"]},
            }
        ],
    }
    (wiki_root / "tools" / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return wiki_root


MAIN_SQL = """\
SELECT u.user_id, u.name, u.email
FROM registrations r
JOIN users u ON u.user_id = r.user_id
WHERE r.event = 'ev-2026'
  AND r.user_id NOT IN (SELECT user_id FROM refunds)
ORDER BY u.user_id
"""

COUNT_ALL_SQL = "SELECT count(*) FROM registrations WHERE event = 'ev-2026'"
COUNT_NO_REFUND_SQL = """\
SELECT count(*) FROM registrations
WHERE event = 'ev-2026' AND user_id NOT IN (SELECT user_id FROM refunds)
"""

# 「手動集計結果」役の期待値: ev-2026 登録者のうち返金なし = 1, 3, 4
EXPECTED_CSV = (
    "user_id,name,email\r\n"
    "1,alice,alice@example.com\r\n"
    "3,carol,carol@example.com\r\n"
    "4,dave,dave@example.com\r\n"
)


def write_sqls(tmp_path: Path) -> tuple[Path, list[CountSql]]:
    sql_dir = tmp_path / "sqls"
    sql_dir.mkdir(exist_ok=True)
    main = sql_dir / "main.sql"
    main.write_text(MAIN_SQL, encoding="utf-8")
    c1 = sql_dir / "count_all.sql"
    c1.write_text(COUNT_ALL_SQL, encoding="utf-8")
    c2 = sql_dir / "count_no_refund.sql"
    c2.write_text(COUNT_NO_REFUND_SQL, encoding="utf-8")
    return main, [
        CountSql(label="ev-2026 登録者", path=c1),
        CountSql(label="返金なし", path=c2),
    ]


def make_runner(wiki_root: Path, **overrides) -> ToolQueryRunner:
    clock = overrides.pop("clock", FixedClock(now=NOW))
    args = dict(
        wiki_root=wiki_root,
        clock=clock,
        lock=RealFileLock(),
        monotonic=lambda: 0.0,
        nonce=_seq_nonce("aa00", "aa01", "aa02", "aa03", "aa04", "aa05"),
        lock_timeout=5.0,
    )
    args.update(overrides)
    return ToolQueryRunner(**args)


def do_prepare(runner: ToolQueryRunner, tmp_path: Path, **overrides):
    main, counts = write_sqls(tmp_path)
    args = dict(
        tool_id="events-db",
        sql_path=main,
        count_sqls=counts,
        key_columns=("user_id",),
        expected_rows=(1, 10),
        deliver_to="deliveries",
    )
    args.update(overrides)
    return runner.prepare(**args)


def approve(runner: ToolQueryRunner, plan_id: str):
    preview = runner.approve_preview(plan_id)
    assert is_ok(preview), preview
    return runner.approve_commit(
        plan_id,
        approved_by="mizumi",
        expected_proposal_digest=preview.value.proposal_digest,
    )


def audit_events(wiki_root: Path) -> list[str]:
    path = wiki_root / "outputs" / "toolquery-audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)["event"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def read_state(wiki_root: Path, plan_id: str) -> dict:
    path = wiki_root / "outputs" / "toolquery-plans" / plan_id / "state.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


class TestPrepare:
    def test_creates_immutable_bundle_with_draft_state(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        result = do_prepare(runner, tmp_path)
        assert is_ok(result), result
        outcome = result.value

        bundle = wiki_root / "outputs" / "toolquery-plans" / outcome.plan_id
        assert bundle.is_dir()
        assert not bundle.name.startswith(".staging-")
        proposal = json.loads((bundle / "proposal.json").read_text(encoding="utf-8"))
        assert proposal["plan_id"] == outcome.plan_id
        assert proposal["tool_id"] == "events-db"
        assert proposal["delivery_dir"] == "deliveries"
        assert proposal["expected_rows_min"] == 1
        assert proposal["expected_rows_max"] == 10

        sql_bytes = (bundle / "query.sql").read_bytes()
        assert sql_bytes == MAIN_SQL.encode("utf-8")
        assert proposal["sql_digest"] == hashlib.sha256(sql_bytes).hexdigest()

        count_files = sorted((bundle / "counts").iterdir())
        assert [f.name for f in count_files] == ["00.sql", "01.sql"]

        state = read_state(wiki_root, outcome.plan_id)
        assert state["status"] == "draft"

    def test_funnel_counts_come_from_dry_run(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        outcome = do_prepare(runner, tmp_path).value
        assert [(s.label, s.row_count) for s in outcome.funnel] == [
            ("ev-2026 登録者", 4),
            ("返金なし", 3),
        ]

    def test_prepare_records_attempted_then_prepared(self, tmp_path: Path) -> None:
        """COUNT 実行も execute と同じ「attempted → 結果」の監査契約を通す。"""
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        do_prepare(runner, tmp_path)
        assert audit_events(wiki_root) == ["prepare_attempted", "prepared"]

    def test_prepare_attempted_write_failure_stops_before_db(
        self, tmp_path: Path
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        audit = FailingAudit(
            fail_events={"prepare_attempted"},
            wiki_root=wiki_root,
            lock=RealFileLock(),
            clock=FixedClock(now=NOW),
            lock_timeout=5.0,
        )
        calls = {"n": 0}

        def counting_factory(**kwargs):
            calls["n"] += 1
            return tool_query_runner.open_sqlite_connector(**kwargs)

        runner = make_runner(
            wiki_root, audit=audit, connector_factory=counting_factory
        )
        result = do_prepare(runner, tmp_path)
        assert is_err(result)
        assert result.error == RunnerReason.AUDIT_WRITE_FAILED.value
        assert calls["n"] == 0  # DB アクセス（COUNT 含む）が発生していない

    def test_expires_at_is_24h_after_created(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        outcome = do_prepare(runner, tmp_path).value
        assert outcome.expires_at == "2026-07-17T12:00:00Z"

    def test_unknown_tool_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        result = do_prepare(runner, tmp_path, tool_id="no-such-tool")
        assert is_err(result)
        assert result.error == "unknown_tool"

    def test_deliver_to_outside_allowed_dirs_is_rejected(
        self, tmp_path: Path
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        for bad in ("elsewhere", "../deliveries", "/tmp/deliveries"):
            result = do_prepare(runner, tmp_path, deliver_to=bad)
            assert is_err(result), bad
            assert result.error == RunnerReason.DELIVERY_NOT_ALLOWED.value

    def test_non_select_main_sql_fails_precheck(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        bad = tmp_path / "bad.sql"
        bad.write_text("UPDATE users SET name = 'x'", encoding="utf-8")
        result = do_prepare(runner, tmp_path, sql_path=bad)
        assert is_err(result)
        assert result.error == RejectReason.SQL_PRECHECK_FAILED.value
        assert not (wiki_root / "outputs" / "toolquery-plans").exists() or not any(
            (wiki_root / "outputs" / "toolquery-plans").iterdir()
        )

    def test_duplicate_count_labels_are_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        main, counts = write_sqls(tmp_path)
        dup = [
            CountSql(label="同じ", path=counts[0].path),
            CountSql(label="同じ", path=counts[1].path),
        ]
        result = do_prepare(runner, tmp_path, count_sqls=dup)
        assert is_err(result)
        assert result.error == RejectReason.INVALID_LABEL.value

    def test_count_sql_returning_non_scalar_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        bad_count = tmp_path / "bad_count.sql"
        bad_count.write_text("SELECT user_id FROM users", encoding="utf-8")
        result = do_prepare(
            runner,
            tmp_path,
            count_sqls=[CountSql(label="複数行", path=bad_count)],
        )
        assert is_err(result)
        assert result.error == RunnerReason.COUNT_RESULT_INVALID.value

    def test_count_sql_returning_non_numeric_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        bad_count = tmp_path / "text_count.sql"
        bad_count.write_text("SELECT 'many'", encoding="utf-8")
        result = do_prepare(
            runner,
            tmp_path,
            count_sqls=[CountSql(label="非数値", path=bad_count)],
        )
        assert is_err(result)
        assert result.error == RunnerReason.COUNT_RESULT_INVALID.value

    def test_staging_collision_retries_with_new_nonce(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        plans = wiki_root / "outputs" / "toolquery-plans"
        plans.mkdir(parents=True)
        (plans / ".staging-20260716120000-aa00-events-db").mkdir()
        runner = make_runner(wiki_root)
        result = do_prepare(runner, tmp_path)
        assert is_ok(result)
        assert result.value.plan_id == "20260716120000-aa01-events-db"

    def test_final_bundle_name_collision_fails(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        plans = wiki_root / "outputs" / "toolquery-plans"
        plans.mkdir(parents=True)
        (plans / "20260716120000-aa00-events-db").mkdir()  # 既存の空 directory
        runner = make_runner(wiki_root, nonce=lambda: "aa00")
        result = do_prepare(runner, tmp_path)
        assert is_err(result)
        # 部分 bundle（staging）が残らず、既存 directory も無傷
        names = [p.name for p in plans.iterdir()]
        assert not any(n.startswith(".staging-") for n in names)
        assert "20260716120000-aa00-events-db" in names


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


class TestApprove:
    def test_preview_then_commit_transitions_to_approved(
        self, tmp_path: Path
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path).value.plan_id
        result = approve(runner, plan_id)
        assert is_ok(result), result
        state = read_state(wiki_root, plan_id)
        assert state["status"] == "approved"
        assert state["approved_by"] == "mizumi"
        assert state["proposal_digest"]
        assert audit_events(wiki_root) == [
            "prepare_attempted",
            "prepared",
            "approved",
        ]

    def test_approve_audit_failure_keeps_state_draft(self, tmp_path: Path) -> None:
        """audit-first: approved イベントが書けなければ状態を変えない。"""
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path).value.plan_id
        failing = FailingAudit(
            fail_events={"approved"},
            wiki_root=wiki_root,
            lock=RealFileLock(),
            clock=FixedClock(now=NOW),
            lock_timeout=5.0,
        )
        failing_runner = make_runner(wiki_root, audit=failing)
        preview = failing_runner.approve_preview(plan_id)
        result = failing_runner.approve_commit(
            plan_id,
            approved_by="mizumi",
            expected_proposal_digest=preview.value.proposal_digest,
        )
        assert is_err(result)
        assert result.error == RunnerReason.AUDIT_WRITE_FAILED.value
        assert read_state(wiki_root, plan_id)["status"] == "draft"

    def test_commit_with_stale_digest_is_rejected(self, tmp_path: Path) -> None:
        """表示と確定の間に proposal が書き換わったら CAS が拒否する。"""
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path).value.plan_id
        result = runner.approve_commit(
            plan_id, approved_by="mizumi", expected_proposal_digest="0" * 64
        )
        assert is_err(result)
        assert result.error == RejectReason.PROPOSAL_DIGEST_MISMATCH.value
        assert read_state(wiki_root, plan_id)["status"] == "draft"

    def test_second_commit_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path).value.plan_id
        assert is_ok(approve(runner, plan_id))
        result = approve(runner, plan_id)
        assert is_err(result)
        assert result.error == RejectReason.ALREADY_APPROVED.value

    def test_expired_plan_cannot_be_approved(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        clock = FixedClock(now=NOW)
        runner = make_runner(wiki_root, clock=clock)
        plan_id = do_prepare(runner, tmp_path).value.plan_id
        clock.advance("2026-07-18T00:00:00Z")
        preview = runner.approve_preview(plan_id)
        result = runner.approve_commit(
            plan_id,
            approved_by="mizumi",
            expected_proposal_digest=preview.value.proposal_digest,
        )
        assert is_err(result)
        assert result.error == RejectReason.TTL_EXPIRED.value

    def test_invalid_approved_by_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path).value.plan_id
        preview = runner.approve_preview(plan_id)
        result = runner.approve_commit(
            plan_id,
            approved_by="   ",
            expected_proposal_digest=preview.value.proposal_digest,
        )
        assert is_err(result)
        assert result.error == RejectReason.INVALID_APPROVED_BY.value

    def test_concurrent_approve_only_one_wins(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path).value.plan_id
        preview = runner.approve_preview(plan_id).value
        barrier = threading.Barrier(2)
        results: list = [None, None]

        def worker(i: int) -> None:
            barrier.wait()
            results[i] = runner.approve_commit(
                plan_id,
                approved_by=f"user{i}",
                expected_proposal_digest=preview.proposal_digest,
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [r for r in results if is_ok(r)]
        errs = [r for r in results if is_err(r)]
        assert len(oks) == 1
        assert len(errs) == 1
        assert errs[0].error == RejectReason.ALREADY_APPROVED.value


# ---------------------------------------------------------------------------
# execute — 成功経路（replay fixture）
# ---------------------------------------------------------------------------


class TestExecuteReplayFixture:
    def test_replay_matches_manual_aggregation(self, tmp_path: Path) -> None:
        """実案件（補填対象者抽出）を模した fixture で、publish された CSV が
        「手動集計結果」役の期待値と bytes 一致する（説明不能な差分 0 件）。"""
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path, expected_rows=(3, 3)).value.plan_id
        assert is_ok(approve(runner, plan_id))

        result = runner.execute(plan_id)
        assert is_ok(result), result
        outcome = result.value

        final = wiki_root / "deliveries" / outcome.run_id
        # read_text は universal newline 変換で \r\n を潰すため bytes で照合する
        csv_text = (final / "result.csv").read_bytes().decode("utf-8")
        assert csv_text == EXPECTED_CSV

        manifest = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["row_count"] == 3
        assert manifest["duplicate_key_count"] == 0
        assert manifest["null_counts"] == {"user_id": 0, "name": 0, "email": 0}
        assert manifest["sanitized_cell_count"] == 0
        assert manifest["csv_sha256"] == hashlib.sha256(
            (final / "result.csv").read_bytes()
        ).hexdigest()
        assert manifest["data_as_of"] == NOW

        assert outcome.row_count == 3
        assert outcome.csv_sha256 == manifest["csv_sha256"]
        assert outcome.published_at == NOW  # 監査 published / receipt と同じ時刻源

        state = read_state(wiki_root, plan_id)
        assert state["status"] == "consumed"
        assert state["run_id"] == outcome.run_id

        receipt = json.loads(
            (
                wiki_root / "outputs" / "toolquery-plans" / plan_id / "receipt.json"
            ).read_text(encoding="utf-8")
        )
        assert receipt["run_id"] == outcome.run_id
        assert receipt["row_count"] == 3
        assert receipt["delivery_dir"] == "deliveries"

        assert audit_events(wiki_root) == [
            "prepare_attempted",
            "prepared",
            "approved",
            "execute_attempted",
            "executed",
            "published",
        ]

    def test_empty_result_publishes_header_only_csv(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        empty_sql = tmp_path / "empty.sql"
        empty_sql.write_text(
            "SELECT user_id FROM users WHERE user_id < 0", encoding="utf-8"
        )
        plan_id = do_prepare(
            runner, tmp_path, sql_path=empty_sql, expected_rows=(0, 0)
        ).value.plan_id
        assert is_ok(approve(runner, plan_id))
        result = runner.execute(plan_id)
        assert is_ok(result)
        final = wiki_root / "deliveries" / result.value.run_id
        assert (final / "result.csv").read_bytes() == b"user_id\r\n"

    def test_null_keys_and_duplicates_are_counted(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        sql = tmp_path / "dups.sql"
        sql.write_text(
            "SELECT event, user_id FROM registrations "
            "UNION ALL SELECT NULL, NULL "
            "UNION ALL SELECT NULL, NULL",
            encoding="utf-8",
        )
        plan_id = do_prepare(
            runner,
            tmp_path,
            sql_path=sql,
            key_columns=("event",),
            expected_rows=(0, 100),
        ).value.plan_id
        assert is_ok(approve(runner, plan_id))
        result = runner.execute(plan_id)
        assert is_ok(result)
        final = wiki_root / "deliveries" / result.value.run_id
        manifest = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
        # event 値: ev-2026 ×4, other-event ×1, NULL ×2 → 7 行で distinct 3 → 重複 4
        assert manifest["row_count"] == 7
        assert manifest["duplicate_key_count"] == 4
        assert manifest["null_counts"]["event"] == 2

    def test_csv_injection_cells_are_sanitized_and_counted(
        self, tmp_path: Path
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        sql = tmp_path / "inj.sql"
        sql.write_text(
            "SELECT '=cmd()' AS a, 'safe' AS b, '@import' AS c", encoding="utf-8"
        )
        plan_id = do_prepare(
            runner,
            tmp_path,
            sql_path=sql,
            key_columns=("a",),
            expected_rows=(1, 1),
        ).value.plan_id
        assert is_ok(approve(runner, plan_id))
        result = runner.execute(plan_id)
        assert is_ok(result)
        final = wiki_root / "deliveries" / result.value.run_id
        manifest = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["sanitized_cell_count"] == 2
        assert "'=cmd()" in (final / "result.csv").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# execute — 拒否経路
# ---------------------------------------------------------------------------


def prepared_and_approved(tmp_path: Path, **prepare_overrides):
    wiki_root = make_wiki(tmp_path)
    runner = make_runner(wiki_root)
    plan_id = do_prepare(runner, tmp_path, **prepare_overrides).value.plan_id
    assert is_ok(approve(runner, plan_id))
    return wiki_root, runner, plan_id


class TestExecuteRejections:
    def test_execute_before_approve_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path).value.plan_id
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RejectReason.NOT_APPROVED.value
        assert audit_events(wiki_root)[-1] == "rejected"

    def test_double_execute_is_replay_rejected(self, tmp_path: Path) -> None:
        wiki_root, runner, plan_id = prepared_and_approved(
            tmp_path, expected_rows=(3, 3)
        )
        assert is_ok(runner.execute(plan_id))
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RejectReason.ALREADY_CONSUMED.value

    def test_missing_bundle_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        result = runner.execute("20260716120000-zz99-events-db")
        assert is_err(result)
        assert result.error == RejectReason.BUNDLE_MISSING.value

    def test_tampered_bundle_sql_is_rejected(self, tmp_path: Path) -> None:
        wiki_root, runner, plan_id = prepared_and_approved(tmp_path)
        bundle = wiki_root / "outputs" / "toolquery-plans" / plan_id
        (bundle / "query.sql").write_text("SELECT 'tampered'", encoding="utf-8")
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RejectReason.SQL_DIGEST_MISMATCH.value

    def test_tampered_count_sql_is_rejected(self, tmp_path: Path) -> None:
        wiki_root, runner, plan_id = prepared_and_approved(tmp_path)
        bundle = wiki_root / "outputs" / "toolquery-plans" / plan_id
        (bundle / "counts" / "00.sql").write_text("SELECT 0", encoding="utf-8")
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RejectReason.COUNT_SQL_DIGEST_MISMATCH.value

    def test_tampered_proposal_after_approval_is_rejected(
        self, tmp_path: Path
    ) -> None:
        wiki_root, runner, plan_id = prepared_and_approved(tmp_path)
        bundle = wiki_root / "outputs" / "toolquery-plans" / plan_id
        proposal = json.loads((bundle / "proposal.json").read_text(encoding="utf-8"))
        proposal["delivery_dir"] = "deliveries"  # 同値でも bytes が変われば検出
        proposal["expected_rows_max"] = 999999
        (bundle / "proposal.json").write_text(
            json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RejectReason.PROPOSAL_DIGEST_MISMATCH.value

    def test_whitespace_only_proposal_rewrite_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """proposal_digest は再直列化ではなく**生 bytes**から計算される —
        意味的に同一でも空白・キー順を変えた書き換えは mismatch になる。"""
        wiki_root, runner, plan_id = prepared_and_approved(tmp_path)
        bundle = wiki_root / "outputs" / "toolquery-plans" / plan_id
        proposal = json.loads((bundle / "proposal.json").read_text(encoding="utf-8"))
        (bundle / "proposal.json").write_text(
            json.dumps(proposal, ensure_ascii=False, indent=4, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RejectReason.PROPOSAL_DIGEST_MISMATCH.value

    def test_bundle_name_and_proposal_plan_id_must_match(
        self, tmp_path: Path
    ) -> None:
        """別 plan の proposal を差し込んで監査・receipt を偽装できない。"""
        wiki_root, runner, plan_id = prepared_and_approved(tmp_path)
        bundle = wiki_root / "outputs" / "toolquery-plans" / plan_id
        proposal = json.loads((bundle / "proposal.json").read_text(encoding="utf-8"))
        proposal["plan_id"] = "20260716120000-zz99-events-db"
        (bundle / "proposal.json").write_text(
            json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RejectReason.MALFORMED_PROPOSAL.value

    def test_expired_overall_deadline_rejects_without_consuming_approval(
        self, tmp_path: Path
    ) -> None:
        """全体 deadline 超過は承認消費の前に拒否される（承認は温存）。"""
        wiki_root = make_wiki(
            tmp_path,
            limits={
                "max_rows": 1000,
                "max_result_bytes": 1048576,
                "max_cell_bytes": 4096,
                "timeout_sec": 1,
            },
        )
        clock = {"t": 0.0}

        def jumping_monotonic() -> float:
            # 最初の呼び出し（started）だけ 0、以降は deadline(=1) を大きく超える
            value = clock["t"]
            clock["t"] = 100.0
            return value

        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path, expected_rows=(3, 3)).value.plan_id
        assert is_ok(approve(runner, plan_id))

        expired_runner = make_runner(
            wiki_root,
            monotonic=jumping_monotonic,
            nonce=_seq_nonce("cc00", "cc01"),
        )
        result = expired_runner.execute(plan_id)
        assert is_err(result)
        assert result.error == "deadline_exceeded"
        # 承認は消費されていない（approved のまま）
        assert read_state(wiki_root, plan_id)["status"] == "approved"
        assert audit_events(wiki_root)[-1] == "rejected"

    def test_catalog_change_after_prepare_is_rejected(self, tmp_path: Path) -> None:
        wiki_root, runner, plan_id = prepared_and_approved(tmp_path)
        catalog_path = wiki_root / "tools" / "catalog.json"
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        data["tools"][0]["limits"]["max_rows"] = 999
        catalog_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RejectReason.CATALOG_DIGEST_MISMATCH.value

    def test_expired_plan_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        clock = FixedClock(now=NOW)
        runner = make_runner(wiki_root, clock=clock)
        plan_id = do_prepare(runner, tmp_path).value.plan_id
        assert is_ok(approve(runner, plan_id))
        clock.advance("2026-07-18T00:00:00Z")
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RejectReason.TTL_EXPIRED.value

    def test_rows_out_of_range_is_not_published(self, tmp_path: Path) -> None:
        wiki_root, runner, plan_id = prepared_and_approved(
            tmp_path, expected_rows=(10, 100)  # 実際は 3 行
        )
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RejectReason.ROWS_OUT_OF_RANGE.value
        deliveries = wiki_root / "deliveries"
        assert list(deliveries.iterdir()) == []  # publish も staging 残骸もない
        assert audit_events(wiki_root)[-1] == "rejected"

    def test_rows_range_boundary_exactly_passes(self, tmp_path: Path) -> None:
        wiki_root, runner, plan_id = prepared_and_approved(
            tmp_path, expected_rows=(3, 3)
        )
        assert is_ok(runner.execute(plan_id))

    def test_max_rows_limit_interrupts_execution(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(
            tmp_path,
            limits={
                "max_rows": 2,
                "max_result_bytes": 1048576,
                "max_cell_bytes": 4096,
                "timeout_sec": 30,
            },
        )
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path, expected_rows=(0, 100)).value.plan_id
        assert is_ok(approve(runner, plan_id))
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RunnerReason.ROW_LIMIT_EXCEEDED.value
        assert list((wiki_root / "deliveries").iterdir()) == []

    def test_max_result_bytes_limit_interrupts_execution(
        self, tmp_path: Path
    ) -> None:
        wiki_root = make_wiki(
            tmp_path,
            limits={
                "max_rows": 1000,
                "max_result_bytes": 30,  # header + 1 行で超える
                "max_cell_bytes": 4096,
                "timeout_sec": 30,
            },
        )
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path, expected_rows=(0, 100)).value.plan_id
        assert is_ok(approve(runner, plan_id))
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RunnerReason.RESULT_BYTES_EXCEEDED.value

    def test_max_cell_bytes_limit_interrupts_execution(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(
            tmp_path,
            limits={
                "max_rows": 1000,
                "max_result_bytes": 1048576,
                "max_cell_bytes": 8,  # email が超える
                "timeout_sec": 30,
            },
        )
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path, expected_rows=(0, 100)).value.plan_id
        assert is_ok(approve(runner, plan_id))
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RunnerReason.CELL_BYTES_EXCEEDED.value

    def test_key_column_missing_from_result_is_rejected(
        self, tmp_path: Path
    ) -> None:
        wiki_root, runner, plan_id = prepared_and_approved(
            tmp_path, key_columns=("no_such_column",)
        )
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RunnerReason.KEY_COLUMN_MISSING.value

    def test_duplicate_result_columns_are_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        sql = tmp_path / "dupcol.sql"
        sql.write_text(
            "SELECT user_id, user_id FROM users", encoding="utf-8"
        )
        plan_id = do_prepare(
            runner, tmp_path, sql_path=sql, expected_rows=(0, 100)
        ).value.plan_id
        assert is_ok(approve(runner, plan_id))
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == RunnerReason.DUPLICATE_COLUMNS.value

    def test_delivery_run_dir_conflict_fails(self, tmp_path: Path) -> None:
        wiki_root, runner, plan_id = prepared_and_approved(
            tmp_path, expected_rows=(3, 3)
        )
        # execute が使う run_id（nonce 続き）を先取りして衝突させる
        (wiki_root / "deliveries" / "20260716120000-aa01-events-db").mkdir()
        result = runner.execute(plan_id)
        assert is_err(result)
        assert result.error == "delivery_conflict"
        # staging は finally で削除される
        leftovers = [
            p
            for p in (wiki_root / "deliveries").iterdir()
            if p.name.startswith(".staging-")
        ]
        assert leftovers == []


# ---------------------------------------------------------------------------
# execute — fail closed / fault injection
# ---------------------------------------------------------------------------


class FailingAudit(AuditLog):
    """特定イベントの追記だけ失敗させる監査ログ（fail closed 検証用）。"""

    def __init__(self, *, fail_events: set[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._fail_events = fail_events

    def append(self, event):
        if event.event in self._fail_events:
            from lib.service.tool_audit import AuditError

            return Err(error=AuditError.WRITE_FAILED, detail="fake audit failure")
        return super().append(event)


class TestFailClosed:
    def _runner_with_failing_audit(
        self, wiki_root: Path, fail_events: set[str]
    ) -> ToolQueryRunner:
        audit = FailingAudit(
            fail_events=fail_events,
            wiki_root=wiki_root,
            lock=RealFileLock(),
            clock=FixedClock(now=NOW),
            lock_timeout=5.0,
        )
        return make_runner(wiki_root, audit=audit)

    def test_execute_attempted_write_failure_stops_before_db(
        self, tmp_path: Path
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path, expected_rows=(3, 3)).value.plan_id
        assert is_ok(approve(runner, plan_id))

        failing = self._runner_with_failing_audit(wiki_root, {"execute_attempted"})
        result = failing.execute(plan_id)
        assert is_err(result)
        assert result.error == RunnerReason.AUDIT_WRITE_FAILED.value
        # plan は approved のまま（承認は消費されていない）
        assert read_state(wiki_root, plan_id)["status"] == "approved"
        # DB アクセスも delivery も発生していない
        assert list((wiki_root / "deliveries").iterdir()) == []

    def test_executed_write_failure_discards_staging_and_does_not_publish(
        self, tmp_path: Path
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path, expected_rows=(3, 3)).value.plan_id
        assert is_ok(approve(runner, plan_id))

        failing = self._runner_with_failing_audit(wiki_root, {"executed"})
        result = failing.execute(plan_id)
        assert is_err(result)
        assert result.error == RunnerReason.AUDIT_WRITE_FAILED.value
        assert read_state(wiki_root, plan_id)["status"] == "consumed"
        assert list((wiki_root / "deliveries").iterdir()) == []

    def test_crash_after_execute_attempted_leaves_plan_approved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """クラッシュポイント表 #2: attempted 監査のみ・plan は approved のまま。"""
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path, expected_rows=(3, 3)).value.plan_id
        assert is_ok(approve(runner, plan_id))

        original = tool_query_runner._write_json_durable

        def crash_on_state_write(path: Path, data: dict) -> None:
            if path.name == "state.json":
                raise SimulatedCrash("crash before consumed write")
            original(path, data)

        monkeypatch.setattr(
            tool_query_runner, "_write_json_durable", crash_on_state_write
        )
        with pytest.raises(SimulatedCrash):
            runner.execute(plan_id)
        assert read_state(wiki_root, plan_id)["status"] == "approved"
        assert audit_events(wiki_root)[-1] == "execute_attempted"
        assert list((wiki_root / "deliveries").iterdir()) == []

    def test_crash_after_consumed_leaves_authorization_spent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """クラッシュポイント表 #3-4: consumed + attempted 監査 + 成果物なし。"""
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path, expected_rows=(3, 3)).value.plan_id
        assert is_ok(approve(runner, plan_id))

        def crash_factory(**kwargs):
            raise SimulatedCrash("crash after consumed, before DB")

        monkeypatch.setattr(runner, "_connector_factory", crash_factory)
        with pytest.raises(SimulatedCrash):
            runner.execute(plan_id)
        assert read_state(wiki_root, plan_id)["status"] == "consumed"
        assert audit_events(wiki_root)[-1] == "execute_attempted"
        assert list((wiki_root / "deliveries").iterdir()) == []

    def test_crash_before_rename_leaves_consumed_and_unpublished(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """クラッシュポイント表 #6 rename 前: consumed / executed 監査 / 非 publish。"""
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root)
        plan_id = do_prepare(runner, tmp_path, expected_rows=(3, 3)).value.plan_id
        assert is_ok(approve(runner, plan_id))

        def crash_publish(**kwargs):
            raise SimulatedCrash("crash before rename")

        monkeypatch.setattr(tool_query_runner, "publish_run_dir", crash_publish)
        with pytest.raises(SimulatedCrash):
            runner.execute(plan_id)
        assert read_state(wiki_root, plan_id)["status"] == "consumed"
        events = audit_events(wiki_root)
        assert "executed" in events
        assert "published" not in events
        assert list((wiki_root / "deliveries").iterdir()) == []

    def test_consumed_after_failure_requires_new_plan(self, tmp_path: Path) -> None:
        """consumed は「承認の消費」— 実行失敗後も承認は復活しない。"""
        wiki_root, runner, plan_id = prepared_and_approved(
            tmp_path, expected_rows=(10, 100)  # rows range で失敗させる
        )
        first = runner.execute(plan_id)
        assert is_err(first)
        assert read_state(wiki_root, plan_id)["status"] == "consumed"
        second = runner.execute(plan_id)
        assert is_err(second)
        assert second.error == RejectReason.ALREADY_CONSUMED.value


class TestConcurrentExecute:
    def test_only_one_of_concurrent_executes_reaches_db(
        self, tmp_path: Path
    ) -> None:
        """plan lock 連続保持の CAS により並行 execute は一方のみ成功する。"""
        wiki_root, runner_a, plan_id = prepared_and_approved(
            tmp_path, expected_rows=(3, 3)
        )
        runner_b = make_runner(
            wiki_root, nonce=_seq_nonce("bb00", "bb01", "bb02")
        )
        barrier = threading.Barrier(2)
        results: list = [None, None]

        def worker(i: int, runner: ToolQueryRunner) -> None:
            barrier.wait()
            results[i] = runner.execute(plan_id)

        threads = [
            threading.Thread(target=worker, args=(0, runner_a)),
            threading.Thread(target=worker, args=(1, runner_b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = [r for r in results if is_ok(r)]
        errs = [r for r in results if is_err(r)]
        assert len(oks) == 1
        assert len(errs) == 1
        assert errs[0].error == RejectReason.ALREADY_CONSUMED.value
        # 成果物は勝者の 1 つだけ
        finals = [
            p
            for p in (wiki_root / "deliveries").iterdir()
            if not p.name.startswith(".")
        ]
        assert len(finals) == 1
