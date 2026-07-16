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
    AuditRegistry,
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
            if event == "doctor":
                assert is_ok(
                    log.append(
                        make_event(event="doctor", plan_id=None, subcommand="doctor")
                    )
                )
                continue
            assert is_ok(log.append(make_event(event=event))), event

    def test_doctor_event_is_plan_independent(self, tmp_path: Path) -> None:
        """doctor は plan 非依存の診断イベント — plan_id なしで記録できる。"""
        log = make_log(tmp_path)
        result = log.append(
            AuditEvent(
                event="doctor",
                plan_id=None,
                tool_id="pg-db",
                subcommand="doctor",
            )
        )
        assert is_ok(result)
        entry = read_lines(tmp_path)[0]
        assert entry["event"] == "doctor"
        assert "plan_id" not in entry
        assert entry["tool_id"] == "pg-db"

    def test_doctor_event_with_plan_id_is_rejected(self, tmp_path: Path) -> None:
        """doctor に plan_id を載せるのは種別違反（plan と紐付けない）。"""
        log = make_log(tmp_path)
        result = log.append(
            AuditEvent(
                event="doctor",
                plan_id=PLAN_ID,
                tool_id="pg-db",
                subcommand="doctor",
            )
        )
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT

    def test_non_doctor_event_requires_plan_id(self, tmp_path: Path) -> None:
        """状態遷移イベントは plan_id 必須（None は種別違反）。"""
        log = make_log(tmp_path)
        result = log.append(make_event(event="prepared", plan_id=None))
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT


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

    def test_sql_gate_and_registry_reasons_are_allowed(self) -> None:
        """Phase A2 で追加された reason 空間（SQL gate / registry / http）が
        ALLOWED_REASONS に含まれる — 自由文字列を経由せず監査に載せるため。"""
        from lib.service.tool_audit import ALLOWED_REASONS
        from lib.service.tool_connector_http import HttpConnectorError
        from lib.service.tool_connector_registry import RegistryError
        from lib.service.tool_sql_gate import SqlGateError

        assert {e.value for e in SqlGateError} <= ALLOWED_REASONS
        assert {e.value for e in RegistryError} <= ALLOWED_REASONS
        assert {e.value for e in HttpConnectorError} <= ALLOWED_REASONS

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


# ---------------------------------------------------------------------------
# 監査の一般化 — 許可 enum レジストリ + 出力パス + 汎用 digest の注入
# ---------------------------------------------------------------------------


BROWSER_REGISTRY = AuditRegistry(
    events={
        "prepared": True,
        "approved": True,
        "delivering": True,
        "delivered": True,
        "failed": True,
        "expired": True,
        "execute_attempted": True,
        "rejected": True,
        # login は plan 非依存の診断イベント（plan_id を載せない）
        "login": False,
    },
    subcommands=frozenset(
        {"prepare", "approve", "execute", "doctor", "login", "catalog-validate"}
    ),
    allowed_reasons=frozenset({"seal_mismatch", "session_expired", "origin_blocked"}),
    allowed_digest_keys=frozenset({"artifact_digest", "manifest_digest"}),
    relative_path="outputs/browser-audit.jsonl",
)


def browser_event(**overrides) -> AuditEvent:
    base = dict(
        event="prepared",
        plan_id=PLAN_ID,
        tool_id="events-web",
        subcommand="prepare",
        row_count=42,
        digests={"artifact_digest": "a" * 64, "manifest_digest": "b" * 64},
    )
    base.update(overrides)
    return AuditEvent(**base)


class TestInjectableRegistry:
    def test_browser_registry_writes_to_its_own_path(self, tmp_path: Path) -> None:
        log = make_log(tmp_path, registry=BROWSER_REGISTRY)
        result = log.append(browser_event())
        assert is_ok(result), result
        assert not (tmp_path / AUDIT_RELATIVE_PATH).exists()
        path = tmp_path / "outputs" / "browser-audit.jsonl"
        entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["event"] == "prepared"
        assert entry["artifact_digest"] == "a" * 64
        assert entry["manifest_digest"] == "b" * 64
        assert entry["row_count"] == 42

    def test_browser_login_event_is_plan_independent(self, tmp_path: Path) -> None:
        log = make_log(tmp_path, registry=BROWSER_REGISTRY)
        result = log.append(
            AuditEvent(
                event="login", plan_id=None, tool_id="events-web", subcommand="login"
            )
        )
        assert is_ok(result), result

    def test_browser_login_with_plan_id_is_rejected(self, tmp_path: Path) -> None:
        log = make_log(tmp_path, registry=BROWSER_REGISTRY)
        result = log.append(
            AuditEvent(
                event="login",
                plan_id=PLAN_ID,
                tool_id="events-web",
                subcommand="login",
            )
        )
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT

    def test_sql_event_name_is_unknown_to_browser_registry(
        self, tmp_path: Path
    ) -> None:
        """別系統の分離: SQL 固有イベントは browser レジストリでは未知。"""
        log = make_log(tmp_path, registry=BROWSER_REGISTRY)
        result = log.append(browser_event(event="published"))
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT

    def test_unknown_digest_key_is_rejected(self, tmp_path: Path) -> None:
        log = make_log(tmp_path, registry=BROWSER_REGISTRY)
        result = log.append(
            browser_event(digests={"sql_digest": "a" * 64})
        )
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT

    def test_non_sha256_digest_value_is_rejected(self, tmp_path: Path) -> None:
        log = make_log(tmp_path, registry=BROWSER_REGISTRY)
        result = log.append(
            browser_event(digests={"artifact_digest": "SELECT * FROM x"})
        )
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT

    def test_browser_reason_is_allowed(self, tmp_path: Path) -> None:
        log = make_log(tmp_path, registry=BROWSER_REGISTRY)
        result = log.append(
            browser_event(event="rejected", reason="seal_mismatch")
        )
        assert is_ok(result), result

    def test_sql_reason_is_unknown_to_browser_registry(self, tmp_path: Path) -> None:
        log = make_log(tmp_path, registry=BROWSER_REGISTRY)
        result = log.append(browser_event(event="rejected", reason="ttl_expired"))
        assert is_err(result)
        assert result.error == AuditError.INVALID_EVENT

    def test_default_registry_preserves_sql_behavior(self, tmp_path: Path) -> None:
        """registry を渡さない既定は SQL レジストリ（振る舞い不変）。"""
        log = make_log(tmp_path)
        assert is_ok(log.append(make_event()))
        entry = read_lines(tmp_path)[0]
        assert entry["sql_digest"] == "d" * 64
