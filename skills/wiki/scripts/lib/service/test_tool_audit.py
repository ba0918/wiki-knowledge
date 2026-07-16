"""Tests for tool_audit.py — 監査ログの排他追記（fail closed）.

監査ログは値を含まないメタデータのみ: plan_id・tool_id・subcommand・
sql_digest・件数・時刻・delivery 先（catalog 相対）・reason。
SQL 全文・条件値・結果行・絶対パスは書かない。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.clock import FixedClock
from lib.service.file_lock import FakeFileLock, RealFileLock
from lib.service.tool_audit import (
    AUDIT_EVENTS,
    AUDIT_RELATIVE_PATH,
    AuditError,
    AuditEvent,
    AuditLog,
)


NOW = "2026-07-16T12:00:00Z"
PLAN_ID = "20260716120000-ab12-events-db"


def make_log(wiki_root: Path, **overrides) -> AuditLog:
    args = dict(
        wiki_root=wiki_root,
        lock=FakeFileLock(),
        clock=FixedClock(now=NOW),
        lock_timeout=5.0,
    )
    args.update(overrides)
    return AuditLog(**args)


def make_event(**overrides) -> AuditEvent:
    base = dict(
        event="prepared",
        plan_id=PLAN_ID,
        tool_id="events-db",
        subcommand="prepare",
        sql_digest="d" * 64,
        row_count=120,
        delivery_dir="outputs/deliveries",
        reason=None,
    )
    base.update(overrides)
    return AuditEvent(**base)


def read_lines(wiki_root: Path) -> list[dict]:
    path = wiki_root / AUDIT_RELATIVE_PATH
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


class TestAppend:
    def test_appends_one_json_line_with_timestamp(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        result = log.append(make_event())
        assert is_ok(result)
        lines = read_lines(tmp_path)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["event"] == "prepared"
        assert entry["at"] == NOW
        assert entry["plan_id"] == PLAN_ID
        assert entry["tool_id"] == "events-db"
        assert entry["sql_digest"] == "d" * 64
        assert entry["row_count"] == 120

    def test_appends_are_cumulative(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        log.append(make_event(event="prepared"))
        log.append(make_event(event="approved", subcommand="approve"))
        events = [entry["event"] for entry in read_lines(tmp_path)]
        assert events == ["prepared", "approved"]

    def test_none_fields_are_omitted(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        log.append(
            make_event(sql_digest=None, row_count=None, delivery_dir=None)
        )
        entry = read_lines(tmp_path)[0]
        assert "sql_digest" not in entry
        assert "row_count" not in entry
        assert "delivery_dir" not in entry

    def test_rejected_event_records_reason(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        log.append(make_event(event="rejected", reason="ttl_expired"))
        entry = read_lines(tmp_path)[0]
        assert entry["event"] == "rejected"
        assert entry["reason"] == "ttl_expired"

    def test_all_state_events_are_accepted(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        for event in AUDIT_EVENTS:
            assert is_ok(log.append(make_event(event=event))), event
        assert [e["event"] for e in read_lines(tmp_path)] == list(AUDIT_EVENTS)


class TestFailClosed:
    def test_unknown_event_name_is_rejected(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        result = log.append(make_event(event="banana"))
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT
        assert not (tmp_path / AUDIT_RELATIVE_PATH).exists()

    @pytest.mark.parametrize(
        "bad_dir",
        [
            "/etc/deliveries",
            "outputs/../../home/user",
            "outputs//deliveries",
            "./deliveries",
            "C:\\deliveries",
            "outputs\\deliveries",
            "",
        ],
    )
    def test_non_clean_relative_delivery_dir_is_rejected(
        self, tmp_path: Path, bad_dir: str
    ) -> None:
        """監査ログに絶対パス・traversal 表記を書かない invariant を API 側で強制する。"""
        log = make_log(tmp_path)
        result = log.append(make_event(delivery_dir=bad_dir))
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT

    def test_free_text_reason_is_rejected(self, tmp_path: Path) -> None:
        """reason は列挙値のみ — 例外メッセージや SQL 全文の混入経路を塞ぐ。"""
        log = make_log(tmp_path)
        for bad in (
            "SELECT * FROM users WHERE email = 'a@example.com'",
            "OSError: /home/mizumi/secret.csv",
            "",
        ):
            result = log.append(make_event(event="rejected", reason=bad))
            assert is_err(result), bad
            assert result.error == AuditError.INVALID_EVENT

    def test_sql_digest_must_be_sha256_form(self, tmp_path: Path) -> None:
        """digest フィールドに SQL 全文を流し込めない。"""
        log = make_log(tmp_path)
        result = log.append(make_event(sql_digest="SELECT * FROM users"))
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT

    def test_malformed_plan_id_is_rejected(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        for bad in ("../x", "/abs", "free text"):
            result = log.append(make_event(plan_id=bad))
            assert is_err(result), bad
            assert result.error == AuditError.INVALID_EVENT

    def test_unknown_subcommand_is_rejected(self, tmp_path: Path) -> None:
        log = make_log(tmp_path)
        result = log.append(make_event(subcommand="rm -rf"))
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT

    def test_runner_reason_values_stay_in_sync(self) -> None:
        """循環 import 回避のため列挙した RUNNER_REASON_VALUES と
        RunnerReason enum の同期を機械検証する。"""
        from lib.service.tool_audit import RUNNER_REASON_VALUES
        from lib.service.tool_query_runner import RunnerReason

        assert set(RUNNER_REASON_VALUES) == {e.value for e in RunnerReason}

    def test_lock_timeout_is_write_failed(self, tmp_path: Path) -> None:
        log = make_log(tmp_path, lock=FakeFileLock(always_times_out=True))
        result = log.append(make_event())
        assert is_err(result)
        assert result.error == AuditError.WRITE_FAILED
        assert not (tmp_path / AUDIT_RELATIVE_PATH).exists()

    def test_unwritable_target_is_write_failed(self, tmp_path: Path) -> None:
        # outputs を regular file にして mkdir/open を失敗させる
        (tmp_path / "outputs").write_text("not a directory", encoding="utf-8")
        log = make_log(tmp_path)
        result = log.append(make_event())
        assert is_err(result)
        assert result.error == AuditError.WRITE_FAILED


class TestLockUsage:
    def test_append_goes_through_file_lock(self, tmp_path: Path) -> None:
        lock = FakeFileLock()
        log = make_log(tmp_path, lock=lock)
        log.append(make_event())
        assert len(lock.history) == 1
        lock_path, timeout = lock.history[0]
        assert lock_path.endswith(".lock")
        assert timeout == 5.0

    def test_concurrent_appends_do_not_interleave(self, tmp_path: Path) -> None:
        log = make_log(tmp_path, lock=RealFileLock())
        errors: list = []

        def worker(n: int) -> None:
            for i in range(20):
                result = log.append(make_event(row_count=n * 100 + i))
                if is_err(result):
                    errors.append(result)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        lines = read_lines(tmp_path)  # 全行が独立した JSON として parse できる
        assert len(lines) == 80
