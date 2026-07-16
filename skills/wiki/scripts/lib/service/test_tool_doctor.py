"""Tests for tool_doctor.py — DoctorCheck registry と判定表.

doctor は「実データに触れない診断」— COUNT すら実行しない。read-only の検証は
単一の write 試行ではなく独立 check に分解する（同一接続では session
read-only と role 拒否を区別できないため）。実 DB は使わず fake driver で
introspection 応答をスクリプトして判定ロジックを固定する。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.clock import FixedClock
from lib.service.file_lock import RealFileLock
from lib.service.tool_catalog import load_catalog
from lib.service.tool_connector_mysql import FakeMySqlDriver
from lib.service.tool_connector_pg import FakePgDriver
from lib.service.tool_connector_registry import default_registry
from lib.service.tool_connector_http import FakeTransport, FakeTransportResponse
from lib.service.tool_doctor import (
    CHECK_NAMES,
    CHECK_REGISTRY,
    CheckStatus,
    Doctor,
    DoctorReport,
)


NOW = "2026-07-16T12:00:00Z"


def make_wiki(tmp_path: Path, tools: list[dict]) -> Path:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "tools").mkdir(parents=True)
    (wiki_root / "deliveries").mkdir()
    (wiki_root / "outputs").mkdir()
    (wiki_root / ".local").mkdir()
    creds = wiki_root / ".local" / "credentials.json"
    creds.write_text(
        json.dumps({"pg-ro": "pg-secret", "mysql-ro": "my-secret", "http-key": "h-secret"}),
        encoding="utf-8",
    )
    creds.chmod(0o600)
    (wiki_root / "tools" / "catalog.json").write_text(
        json.dumps({"schema_version": 1, "tools": tools}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return wiki_root


LIMITS = {
    "max_rows": 1000,
    "max_result_bytes": 1048576,
    "max_cell_bytes": 4096,
    "timeout_sec": 30,
}
HTTP_LIMITS = {**LIMITS, "max_response_bytes": 8388608}


def pg_tool(**conn_extra) -> dict:
    return {
        "tool_id": "pg-db",
        "type": "postgres",
        "connection": {
            "host": "db.example.com",
            "port": 5432,
            "dbname": "appdb",
            "user": "readonly",
            "default_schema": "public",
            **conn_extra,
        },
        "credential_ref": "pg-ro",
        "allowed_tables": ["users"],
        "limits": LIMITS,
        "allowed_statements": ["select"],
        "delivery": {"allowed_dirs": ["deliveries"]},
    }


def mysql_tool(**conn_extra) -> dict:
    return {
        "tool_id": "mysql-db",
        "type": "mysql",
        "connection": {
            "host": "db.example.com",
            "port": 3306,
            "dbname": "appdb",
            "user": "readonly",
            **conn_extra,
        },
        "credential_ref": "mysql-ro",
        "allowed_tables": ["users"],
        "limits": LIMITS,
        "allowed_statements": ["select"],
        "delivery": {"allowed_dirs": ["deliveries"]},
    }


def http_tool() -> dict:
    return {
        "tool_id": "redash-api",
        "type": "http",
        "connection": {
            "base_url": "https://redash.example.com",
            "allowed_endpoints": [{"method": "GET", "path_prefix": "/api/data"}],
            "auth_header_name": "Authorization",
            "auth_header_template": "Key {credential}",
        },
        "credential_ref": "http-key",
        "limits": HTTP_LIMITS,
        "delivery": {"allowed_dirs": ["deliveries"]},
    }


def sqlite_tool(tmp_path: Path, wiki_root: Path) -> dict:
    (wiki_root / "data").mkdir(exist_ok=True)
    db = wiki_root / "data" / "events.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return {
        "tool_id": "events-db",
        "type": "sqlite",
        "connection": {"path": "data/events.sqlite3"},
        "allowed_tables": ["users"],
        "limits": LIMITS,
        "allowed_statements": ["select"],
        "delivery": {"allowed_dirs": ["deliveries"]},
    }


def pg_healthy_script() -> dict:
    """read-only + SELECT のみ権限を持つ健全な pg role の introspection 応答。"""
    script = {
        "SELECT current_setting('transaction_read_only')": (("current_setting",), [("on",)]),
    }
    # role_grants: allowlist relation ごとの has_table_privilege（全 write 権限が false）
    for rel in ("public.users",):
        script[
            f"SELECT has_table_privilege(current_user, '{rel}', 'INSERT'),"
            f" has_table_privilege(current_user, '{rel}', 'UPDATE'),"
            f" has_table_privilege(current_user, '{rel}', 'DELETE'),"
            f" has_table_privilege(current_user, '{rel}', 'TRUNCATE')"
        ] = (("insert", "update", "delete", "truncate"), [(False, False, False, False)])
    return script


def mysql_healthy_script() -> dict:
    return {
        "SELECT @@session.transaction_read_only": (("ro",), [(1,)]),
        "SHOW GRANTS FOR CURRENT_USER()": (
            ("Grants",),
            [("GRANT SELECT ON `appdb`.* TO `readonly`@`%`",)],
        ),
    }


def run_doctor(wiki_root: Path, *, registry=None, **kwargs) -> DoctorReport:
    doctor = Doctor(
        wiki_root=wiki_root,
        clock=FixedClock(now=NOW),
        lock=RealFileLock(),
        registry=registry or default_registry(),
        monotonic=lambda: 0.0,
    )
    return doctor.run(**kwargs)


def status_of(report: DoctorReport, tool_id: str, check: str) -> CheckStatus:
    for diag in report.diagnoses:
        if diag.tool_id == tool_id:
            for outcome in diag.outcomes:
                if outcome.check == check:
                    return outcome.status
    raise AssertionError(f"check {check!r} not found for {tool_id!r}")


def checks_of(report: DoctorReport, tool_id: str) -> set[str]:
    for diag in report.diagnoses:
        if diag.tool_id == tool_id:
            return {o.check for o in diag.outcomes}
    raise AssertionError(f"tool {tool_id!r} not found")


def _outcomes(report: DoctorReport, tool_id: str):
    for diag in report.diagnoses:
        if diag.tool_id == tool_id:
            return diag.outcomes
    raise AssertionError(f"tool {tool_id!r} not found")


# ---------------------------------------------------------------------------
# postgres の check 分解
# ---------------------------------------------------------------------------


class TestPostgresChecks:
    def test_healthy_pg_role_passes_readonly_and_grants(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        driver = FakePgDriver(script=pg_healthy_script())
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        assert status_of(report, "pg-db", "connectivity") == CheckStatus.OK
        assert status_of(report, "pg-db", "credential_resolves") == CheckStatus.OK
        assert status_of(report, "pg-db", "session_readonly") == CheckStatus.OK
        assert status_of(report, "pg-db", "role_grants") == CheckStatus.OK

    def test_read_write_session_is_ng(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        script = pg_healthy_script()
        script["SELECT current_setting('transaction_read_only')"] = (
            ("current_setting",),
            [("off",)],
        )
        driver = FakePgDriver(script=script)
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        assert status_of(report, "pg-db", "session_readonly") == CheckStatus.NG
        assert report.has_ng()

    def test_write_grant_present_is_ng(self, tmp_path: Path) -> None:
        """INSERT なし・UPDATE あり の grant で role_grants が NG になる。"""
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        script = pg_healthy_script()
        for key in list(script):
            if key.startswith("SELECT has_table_privilege"):
                script[key] = (
                    ("insert", "update", "delete", "truncate"),
                    [(False, True, False, False)],
                )
        driver = FakePgDriver(script=script)
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        assert status_of(report, "pg-db", "role_grants") == CheckStatus.NG

    def test_role_write_denial_is_skip_by_default(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        driver = FakePgDriver(script=pg_healthy_script())
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        assert status_of(report, "pg-db", "role_write_denial") == CheckStatus.SKIP

    def test_uninspected_privileges_are_skipped_not_ok(self, tmp_path: Path) -> None:
        """CREATE / TEMPORARY / EXECUTE は機械検証外 — SKIP として明示される。"""
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        driver = FakePgDriver(script=pg_healthy_script())
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        assert status_of(report, "pg-db", "role_uninspected_privileges") == (
            CheckStatus.SKIP
        )

    def test_connect_failure_marks_connectivity_ng(self, tmp_path: Path) -> None:
        from lib.service.tool_connector_pg import FakePgError

        wiki_root = make_wiki(tmp_path, [pg_tool()])
        driver = FakePgDriver(connect_error=FakePgError("boom", sqlstate="08006"))
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        assert status_of(report, "pg-db", "connectivity") == CheckStatus.NG
        # 接続不能なら以降の introspection check は SKIP（NG を伝播させない）
        assert status_of(report, "pg-db", "session_readonly") == CheckStatus.SKIP

    def test_missing_credential_marks_credential_ng(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        (wiki_root / ".local" / "credentials.json").unlink()
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=FakePgDriver()))
        assert status_of(report, "pg-db", "credential_resolves") == CheckStatus.NG


class TestTlsCheck:
    def test_default_tls_is_verified(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        driver = FakePgDriver(script=pg_healthy_script())
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        assert status_of(report, "pg-db", "tls") == CheckStatus.OK

    def test_insecure_tls_optin_is_reported_as_skip(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(
            tmp_path, [pg_tool(host="localhost", allow_insecure_tls=True)]
        )
        driver = FakePgDriver(script=pg_healthy_script())
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        # 緩和が宣言されている場合は検証を SKIP（警告付き）— OK 扱いにしない
        assert status_of(report, "pg-db", "tls") == CheckStatus.SKIP

    def test_tls_is_skipped_when_connect_fails(self, tmp_path: Path) -> None:
        """接続不能なら TLS ネゴシエーション成立は判定できない → SKIP（OK にしない）。"""
        from lib.service.tool_connector_pg import FakePgError

        wiki_root = make_wiki(tmp_path, [pg_tool()])
        driver = FakePgDriver(connect_error=FakePgError("boom", sqlstate="08006"))
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        assert status_of(report, "pg-db", "connectivity") == CheckStatus.NG
        assert status_of(report, "pg-db", "tls") == CheckStatus.SKIP


# ---------------------------------------------------------------------------
# mysql
# ---------------------------------------------------------------------------


class TestMysqlChecks:
    def test_healthy_mysql_passes(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [mysql_tool()])
        driver = FakeMySqlDriver(script=mysql_healthy_script())
        report = run_doctor(wiki_root, registry=default_registry(mysql_driver=driver))
        assert status_of(report, "mysql-db", "session_readonly") == CheckStatus.OK
        assert status_of(report, "mysql-db", "role_grants") == CheckStatus.OK

    def test_non_readonly_session_is_ng(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [mysql_tool()])
        script = mysql_healthy_script()
        script["SELECT @@session.transaction_read_only"] = (("ro",), [(0,)])
        driver = FakeMySqlDriver(script=script)
        report = run_doctor(wiki_root, registry=default_registry(mysql_driver=driver))
        assert status_of(report, "mysql-db", "session_readonly") == CheckStatus.NG

    def test_grant_with_insert_is_ng(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [mysql_tool()])
        script = mysql_healthy_script()
        script["SHOW GRANTS FOR CURRENT_USER()"] = (
            ("Grants",),
            [("GRANT SELECT, INSERT ON `appdb`.* TO `readonly`@`%`",)],
        )
        driver = FakeMySqlDriver(script=script)
        report = run_doctor(wiki_root, registry=default_registry(mysql_driver=driver))
        assert status_of(report, "mysql-db", "role_grants") == CheckStatus.NG

    def test_all_privileges_grant_is_ng(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [mysql_tool()])
        script = mysql_healthy_script()
        script["SHOW GRANTS FOR CURRENT_USER()"] = (
            ("Grants",),
            [("GRANT ALL PRIVILEGES ON `appdb`.* TO `readonly`@`%`",)],
        )
        driver = FakeMySqlDriver(script=script)
        report = run_doctor(wiki_root, registry=default_registry(mysql_driver=driver))
        assert status_of(report, "mysql-db", "role_grants") == CheckStatus.NG

    def test_role_grant_is_incomplete_not_ok(self, tmp_path: Path) -> None:
        """role 付与（ON なし）は実効権限を確定できない → fail-open せず SKIP。"""
        wiki_root = make_wiki(tmp_path, [mysql_tool()])
        script = mysql_healthy_script()
        script["SHOW GRANTS FOR CURRENT_USER()"] = (
            ("Grants",),
            [
                ("GRANT USAGE ON *.* TO `readonly`@`%`",),
                ("GRANT `app_writer`@`%` TO `readonly`@`%`",),
            ],
        )
        driver = FakeMySqlDriver(script=script)
        report = run_doctor(wiki_root, registry=default_registry(mysql_driver=driver))
        for o in _outcomes(report, "mysql-db"):
            if o.check == "role_grants":
                assert o.status == CheckStatus.SKIP
                assert o.reason_code == "role_grants_incomplete"

    def test_unparseable_grant_line_is_incomplete_not_ok(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [mysql_tool()])
        script = mysql_healthy_script()
        script["SHOW GRANTS FOR CURRENT_USER()"] = (
            ("Grants",),
            [("REVOKE nonsense line",)],
        )
        driver = FakeMySqlDriver(script=script)
        report = run_doctor(wiki_root, registry=default_registry(mysql_driver=driver))
        assert status_of(report, "mysql-db", "role_grants") == CheckStatus.SKIP


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------


class TestHttpChecks:
    def test_http_allowlist_dryrun_does_not_send(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [http_tool()])
        transport = FakeTransport(FakeTransportResponse(body=b"{}"))
        report = run_doctor(
            wiki_root, registry=default_registry(http_transport=transport)
        )
        assert status_of(report, "redash-api", "http_allowlist") == CheckStatus.OK
        # dry-run — 実送信しない
        assert transport.requests == []

    def test_http_has_no_sql_specific_checks(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [http_tool()])
        transport = FakeTransport(FakeTransportResponse(body=b"{}"))
        report = run_doctor(
            wiki_root, registry=default_registry(http_transport=transport)
        )
        checks = checks_of(report, "redash-api")
        assert "session_readonly" not in checks
        assert "role_grants" not in checks
        assert "credential_resolves" in checks
        assert "delivery_writable" in checks


# ---------------------------------------------------------------------------
# delivery probe
# ---------------------------------------------------------------------------


class TestDeliveryCheck:
    def test_writable_delivery_dir_is_ok_and_leaves_no_probe(
        self, tmp_path: Path
    ) -> None:
        wiki_root = make_wiki(tmp_path, [http_tool()])
        transport = FakeTransport(FakeTransportResponse(body=b"{}"))
        report = run_doctor(
            wiki_root, registry=default_registry(http_transport=transport)
        )
        assert status_of(report, "redash-api", "delivery_writable") == CheckStatus.OK
        # temp probe は削除される（成果物を残さない）
        assert list((wiki_root / "deliveries").iterdir()) == []

    def test_missing_delivery_dir_is_ng(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [http_tool()])
        import shutil

        shutil.rmtree(wiki_root / "deliveries")
        transport = FakeTransport(FakeTransportResponse(body=b"{}"))
        report = run_doctor(
            wiki_root, registry=default_registry(http_transport=transport)
        )
        assert status_of(report, "redash-api", "delivery_writable") == CheckStatus.NG


# ---------------------------------------------------------------------------
# --probe-write（二重 opt-in）
# ---------------------------------------------------------------------------


class TestWriteProbe:
    def test_probe_write_requires_canary_relation(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])  # canary_relation 未宣言
        driver = FakePgDriver(script=pg_healthy_script())
        report = run_doctor(
            wiki_root,
            registry=default_registry(pg_driver=driver),
            probe_write="pg-db",
        )
        # canary 未宣言なら probe 自体を拒否（NG）— 実 INSERT はしない
        assert status_of(report, "pg-db", "write_probe") == CheckStatus.NG

    def test_probe_write_expects_insert_to_be_denied(self, tmp_path: Path) -> None:
        from lib.service.tool_connector_pg import FakePgError

        wiki_root = make_wiki(tmp_path, [pg_tool(canary_relation="doctor_canary")])
        script = pg_healthy_script()
        # INSERT が role で拒否される（期待される正常系）
        driver = FakePgDriver(
            script=script,
            execute_error=FakePgError("permission denied", sqlstate="42501"),
        )
        report = run_doctor(
            wiki_root,
            registry=default_registry(pg_driver=driver),
            probe_write="pg-db",
        )
        # 拒否されれば write_probe は OK（role が書込を防いでいる）
        assert status_of(report, "pg-db", "write_probe") == CheckStatus.OK

    def test_probe_write_only_targets_named_tool(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(
            tmp_path, [pg_tool(canary_relation="doctor_canary"), mysql_tool()]
        )
        pg = FakePgDriver(script=pg_healthy_script())
        my = FakeMySqlDriver(script=mysql_healthy_script())
        report = run_doctor(
            wiki_root,
            registry=default_registry(pg_driver=pg, mysql_driver=my),
            probe_write="pg-db",
        )
        assert "write_probe" in checks_of(report, "pg-db")
        assert "write_probe" not in checks_of(report, "mysql-db")

    def test_probe_inconclusive_when_failure_is_not_authorization(
        self, tmp_path: Path
    ) -> None:
        """接続断・構文エラー等の「あらゆる失敗」を書込拒否成功と誤認しない。"""
        from lib.service.tool_connector_pg import FakePgError

        wiki_root = make_wiki(tmp_path, [pg_tool(canary_relation="doctor_canary")])
        script = pg_healthy_script()
        # INSERT が relation 不在（42P01 → EXECUTION_FAILED）で失敗するケース
        driver = FakePgDriver(
            script=script,
            execute_error=FakePgError("relation missing", sqlstate="42P01"),
        )
        report = run_doctor(
            wiki_root,
            registry=default_registry(pg_driver=driver),
            probe_write="pg-db",
        )
        for o in _outcomes(report, "pg-db"):
            if o.check == "write_probe":
                assert o.status == CheckStatus.NG
                assert o.reason_code == "probe_inconclusive"

    def test_probe_write_unknown_target_is_error(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        doctor = Doctor(
            wiki_root=wiki_root,
            clock=FixedClock(now=NOW),
            lock=RealFileLock(),
            registry=default_registry(pg_driver=FakePgDriver()),
            monotonic=lambda: 0.0,
        )
        assert is_err(doctor.run_checked(probe_write="no-such"))

    def test_probe_write_on_http_type_is_error(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [http_tool()])
        doctor = Doctor(
            wiki_root=wiki_root,
            clock=FixedClock(now=NOW),
            lock=RealFileLock(),
            registry=default_registry(),
            monotonic=lambda: 0.0,
        )
        assert is_err(doctor.run_checked(probe_write="redash-api"))


# ---------------------------------------------------------------------------
# tool 選択・出力契約・監査
# ---------------------------------------------------------------------------


class TestReportContract:
    def test_tool_filter_restricts_to_one_tool(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool(), mysql_tool()])
        pg = FakePgDriver(script=pg_healthy_script())
        my = FakeMySqlDriver(script=mysql_healthy_script())
        report = run_doctor(
            wiki_root,
            registry=default_registry(pg_driver=pg, mysql_driver=my),
            tool="pg-db",
        )
        assert [d.tool_id for d in report.diagnoses] == ["pg-db"]

    def test_unknown_tool_filter_is_error(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        doctor = Doctor(
            wiki_root=wiki_root,
            clock=FixedClock(now=NOW),
            lock=RealFileLock(),
            registry=default_registry(pg_driver=FakePgDriver()),
            monotonic=lambda: 0.0,
        )
        result = doctor.run_checked(tool="no-such")
        assert is_err(result)

    def test_skip_summary_counts_skips(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        driver = FakePgDriver(script=pg_healthy_script())
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        summary = report.skip_summary()
        assert sum(summary.values()) >= 2  # role_write_denial + uninspected 等

    def test_every_ng_has_a_hint(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        script = pg_healthy_script()
        script["SELECT current_setting('transaction_read_only')"] = (
            ("current_setting",),
            [("off",)],
        )
        driver = FakePgDriver(script=script)
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        for diag in report.diagnoses:
            for outcome in diag.outcomes:
                if outcome.status == CheckStatus.NG:
                    assert outcome.hint

    def test_credential_value_never_appears_in_report(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        driver = FakePgDriver(script=pg_healthy_script())
        report = run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        blob = json.dumps(
            [
                [
                    [o.check, o.status.value, o.reason_code, o.hint]
                    for o in d.outcomes
                ]
                for d in report.diagnoses
            ],
            ensure_ascii=False,
        )
        assert "pg-secret" not in blob

    def test_audit_write_failure_surfaces_as_ng(self, tmp_path: Path) -> None:
        """監査が書けないと exit 0 で流さず NG として計上する（fail closed）。"""
        from lib.service.file_lock import FakeFileLock

        wiki_root = make_wiki(tmp_path, [pg_tool()])
        driver = FakePgDriver(script=pg_healthy_script())
        doctor = Doctor(
            wiki_root=wiki_root,
            clock=FixedClock(now=NOW),
            lock=FakeFileLock(always_times_out=True),
            registry=default_registry(pg_driver=driver),
            monotonic=lambda: 0.0,
        )
        report = doctor.run()
        assert status_of(report, "pg-db", "audit") == CheckStatus.NG
        assert report.has_ng()

    def test_doctor_writes_plan_independent_audit_event(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool()])
        driver = FakePgDriver(script=pg_healthy_script())
        run_doctor(wiki_root, registry=default_registry(pg_driver=driver))
        audit = (wiki_root / "outputs" / "toolquery-audit.jsonl").read_text(
            encoding="utf-8"
        )
        events = [json.loads(line) for line in audit.splitlines() if line]
        doctor_events = [e for e in events if e["event"] == "doctor"]
        assert doctor_events
        assert all("plan_id" not in e for e in doctor_events)
        assert any(e["tool_id"] == "pg-db" for e in doctor_events)


class TestSqliteChecks:
    def test_sqlite_connectivity_and_delivery(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [])
        tool = sqlite_tool(tmp_path, wiki_root)
        (wiki_root / "tools" / "catalog.json").write_text(
            json.dumps({"schema_version": 1, "tools": [tool]}, ensure_ascii=False),
            encoding="utf-8",
        )
        report = run_doctor(wiki_root)
        assert status_of(report, "events-db", "connectivity") == CheckStatus.OK
        assert status_of(report, "events-db", "delivery_writable") == CheckStatus.OK
        # sqlite は authorizer が read-only を保証 — role introspection は持たない
        assert "role_grants" not in checks_of(report, "events-db")


# ---------------------------------------------------------------------------
# DoctorCheck registry（emit と登録の網羅性）
# ---------------------------------------------------------------------------


class TestCheckRegistry:
    def test_every_emitted_check_is_registered(self, tmp_path: Path) -> None:
        """doctor が emit する全 check は CHECK_REGISTRY に登録済みであること。"""
        wiki_root = make_wiki(
            tmp_path,
            [pg_tool(canary_relation="doctor_canary"), mysql_tool(), http_tool()],
        )
        pg = FakePgDriver(script=pg_healthy_script())
        my = FakeMySqlDriver(script=mysql_healthy_script())
        transport = FakeTransport(FakeTransportResponse(body=b"{}"))
        report = run_doctor(
            wiki_root,
            registry=default_registry(
                pg_driver=pg, mysql_driver=my, http_transport=transport
            ),
            probe_write="pg-db",
        )
        emitted = {o.check for d in report.diagnoses for o in d.outcomes}
        assert emitted <= CHECK_NAMES, emitted - CHECK_NAMES

    def test_required_checks_are_present_per_type(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, [pg_tool(), mysql_tool(), http_tool()])
        pg = FakePgDriver(script=pg_healthy_script())
        my = FakeMySqlDriver(script=mysql_healthy_script())
        transport = FakeTransport(FakeTransportResponse(body=b"{}"))
        report = run_doctor(
            wiki_root,
            registry=default_registry(
                pg_driver=pg, mysql_driver=my, http_transport=transport
            ),
        )
        for diag in report.diagnoses:
            emitted = {o.check for o in diag.outcomes}
            required = {
                c.name
                for c in CHECK_REGISTRY
                if c.required and diag.type in c.applies
            }
            assert required <= emitted, (diag.type, required - emitted)
