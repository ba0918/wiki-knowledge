"""Tests for tool_connector_pg.py — PostgresConnector（driver DI、named cursor）.

実 DB は使わない — psycopg の surface を模した FakePgDriver で契約を固定する:

* read-only 順序契約: ``Connection.read_only = True`` は **transaction 開始前**
  に設定され、named cursor はその read-only transaction 内で開かれる
* TLS 既定 verify-full / tls_ca_file → sslrootcert / allow_insecure_tls → prefer
* end-to-end deadline（connect 段階の消費を含む）と chunk 境界の超過検査
* server-side cursor（named）+ fetchmany chunk
* 後始末: 正常・異常とも cursor close → rollback → connection close
* typed rows 正規化（Decimal → str / datetime → ISO / bool → int / memoryview → bytes）
* sanitized error envelope（driver 例外メッセージ・password を透過しない）

実接続の検証は doctor と opt-in smoke（test_tool_smoke_db.py）の責務。
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.tool_catalog import PostgresConnectionConfig
from lib.service.tool_connector import ConnectorStreamError, ToolConnectorError
from lib.service.tool_connector_pg import (
    FakePgDriver,
    FakePgError,
    open_postgres_connector,
)


def make_config(**overrides) -> PostgresConnectionConfig:
    args = dict(host="db.example.com", port=5432, dbname="appdb", user="readonly")
    args.update(overrides)
    return PostgresConnectionConfig(**args)


def open_ok(driver: FakePgDriver, *, config=None, now=0.0, deadline=30.0, **kwargs):
    result = open_postgres_connector(
        config=config or make_config(),
        password="hunter2",
        tls_ca_path=kwargs.pop("tls_ca_path", None),
        deadline_monotonic=deadline,
        monotonic=kwargs.pop("monotonic", lambda: now),
        driver=driver,
    )
    assert is_ok(result), getattr(result, "detail", None)
    return result.value


class TestConnectKwargs:
    def test_connection_fields_and_password_are_passed(self) -> None:
        driver = FakePgDriver()
        open_ok(driver)
        kw = driver.connect_kwargs
        assert kw["host"] == "db.example.com"
        assert kw["port"] == 5432
        assert kw["dbname"] == "appdb"
        assert kw["user"] == "readonly"
        assert kw["password"] == "hunter2"

    def test_tls_defaults_to_verify_full_with_system_ca(self) -> None:
        """CA 省略時は verify-full + sslrootcert=system。libpq 既定の
        ~/.postgresql/root.crt 依存を避け、システム CA ストアを明示する。"""
        driver = FakePgDriver()
        open_ok(driver)
        assert driver.connect_kwargs["sslmode"] == "verify-full"
        assert driver.connect_kwargs["sslrootcert"] == "system"

    def test_tls_ca_path_is_passed_as_sslrootcert(self) -> None:
        driver = FakePgDriver()
        open_ok(driver, tls_ca_path="/wiki/ca.pem")
        assert driver.connect_kwargs["sslmode"] == "verify-full"
        assert driver.connect_kwargs["sslrootcert"] == "/wiki/ca.pem"

    def test_allow_insecure_tls_relaxes_sslmode(self) -> None:
        driver = FakePgDriver()
        open_ok(driver, config=make_config(host="localhost", allow_insecure_tls=True))
        assert driver.connect_kwargs["sslmode"] == "prefer"
        # 緩和時は CA 検証設定を渡さない（root.crt/system を強制しない）
        assert "sslrootcert" not in driver.connect_kwargs

    def test_connect_timeout_and_statement_timeout_use_remaining_budget(self) -> None:
        driver = FakePgDriver()
        open_ok(driver, now=0.0, deadline=30.0)
        kw = driver.connect_kwargs
        assert 1 <= kw["connect_timeout"] <= 30
        assert kw["options"] == "-c statement_timeout=30000"

    def test_user_supplied_dsn_string_is_not_accepted(self) -> None:
        """接続は field からの keyword 引数組み立てのみ — conninfo 文字列を渡す
        経路がないことを surface で固定する。"""
        driver = FakePgDriver()
        open_ok(driver)
        assert driver.connect_args == ()  # 位置引数（DSN 文字列）なし


class TestReadOnlyOrdering:
    def test_read_only_is_set_before_transaction_begins(self) -> None:
        driver = FakePgDriver(columns=("id",), rows=[(1,)])
        connector = open_ok(driver)
        result = connector.execute_stream("SELECT id FROM users")
        assert is_ok(result)
        with result.value as stream:
            list(stream)
        conn = driver.connections[0]
        set_ro = conn.events.index(("set_read_only", True))
        begin = next(i for i, e in enumerate(conn.events) if e[0] == "begin")
        assert set_ro < begin
        # begin イベントは「read-only で transaction が開始されたか」を記録する
        assert conn.events[begin] == ("begin", True)

    def test_cursor_is_named_server_side_cursor(self) -> None:
        driver = FakePgDriver(columns=("id",), rows=[(1,)])
        connector = open_ok(driver)
        with connector.execute_stream("SELECT 1").value as stream:
            list(stream)
        conn = driver.connections[0]
        names = [e[1] for e in conn.events if e[0] == "cursor"]
        assert len(names) == 1
        assert names[0]  # 無名（client-side buffered）cursor ではない

    def test_repeated_streams_use_unique_cursor_names(self) -> None:
        driver = FakePgDriver(columns=("id",), rows=[(1,)])
        connector = open_ok(driver)
        with connector.execute_stream("SELECT 1").value as s:
            list(s)
        with connector.execute_stream("SELECT 2").value as s:
            list(s)
        conn = driver.connections[0]
        names = [e[1] for e in conn.events if e[0] == "cursor"]
        assert len(names) == 2 and names[0] != names[1]


class TestStreaming:
    def test_columns_and_rows_are_streamed_in_chunks(self) -> None:
        rows = [(i, f"u{i}") for i in range(1200)]
        driver = FakePgDriver(columns=("id", "name"), rows=rows)
        connector = open_ok(driver)
        with connector.execute_stream("SELECT id, name FROM users").value as stream:
            assert stream.columns == ("id", "name")
            got = list(stream)
        assert got == rows
        conn = driver.connections[0]
        fetches = [e for e in conn.events if e[0] == "fetchmany"]
        assert len(fetches) >= 3  # 1200 行は 500 行 chunk では一括で来ない

    def test_typed_rows_are_normalized(self) -> None:
        moment = datetime.datetime(2026, 7, 16, 12, 0, 0)
        driver = FakePgDriver(
            columns=("d", "t", "b", "m", "n"),
            rows=[(Decimal("1.50"), moment, True, memoryview(b"\x01\x02"), None)],
        )
        connector = open_ok(driver)
        with connector.execute_stream("SELECT *").value as stream:
            (row,) = list(stream)
        assert row == ("1.50", "2026-07-16T12:00:00", 1, b"\x01\x02", None)
        assert type(row[2]) is int  # bool を int の顔で流さない


class TestDeadline:
    def test_expired_before_connect_does_not_touch_driver(self) -> None:
        driver = FakePgDriver()
        result = open_postgres_connector(
            config=make_config(),
            password="hunter2",
            tls_ca_path=None,
            deadline_monotonic=10.0,
            monotonic=lambda: 10.0,
            driver=driver,
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.DEADLINE_EXCEEDED
        assert driver.connect_kwargs is None

    def test_expired_before_execute_is_rejected(self) -> None:
        times = iter([0.0, 99.0])
        driver = FakePgDriver()
        connector = open_ok(driver, monotonic=lambda: next(times), deadline=30.0)
        result = connector.execute_stream("SELECT 1")
        assert is_err(result)
        assert result.error == ToolConnectorError.DEADLINE_EXCEEDED

    def test_deadline_is_checked_at_chunk_boundaries(self) -> None:
        rows = [(i,) for i in range(1000)]
        clock = {"now": 0.0}
        driver = FakePgDriver(columns=("id",), rows=rows)
        connector = open_ok(driver, monotonic=lambda: clock["now"], deadline=30.0)
        stream = connector.execute_stream("SELECT id FROM big").value
        it = iter(stream)
        for _ in range(500):
            next(it)
        clock["now"] = 99.0  # 次の chunk 境界で超過
        with pytest.raises(ConnectorStreamError) as exc_info:
            for _ in it:
                pass
        assert exc_info.value.reason == ToolConnectorError.DEADLINE_EXCEEDED
        stream.close()
        connector.close()
        conn = driver.connections[0]
        kinds = [e[0] for e in conn.events]
        assert "cursor_close" in kinds and "rollback" in kinds and "close" in kinds


class TestCleanup:
    def test_normal_eof_closes_cursor_rollback_and_connection(self) -> None:
        driver = FakePgDriver(columns=("id",), rows=[(1,)])
        connector = open_ok(driver)
        with connector.execute_stream("SELECT 1").value as stream:
            list(stream)
        connector.close()
        conn = driver.connections[0]
        kinds = [e[0] for e in conn.events]
        assert kinds.index("cursor_close") < kinds.index("rollback") < kinds.index(
            "close"
        )

    def test_early_close_also_closes_cursor(self) -> None:
        """pg の named cursor close は server 側 portal の破棄で軽量 —
        早期終了でも通常 close 経路でよい（mysql と対照的）。"""
        driver = FakePgDriver(columns=("id",), rows=[(i,) for i in range(1000)])
        connector = open_ok(driver)
        stream = connector.execute_stream("SELECT 1").value
        next(iter(stream))
        stream.close()
        connector.close()
        conn = driver.connections[0]
        kinds = [e[0] for e in conn.events]
        assert "cursor_close" in kinds and "rollback" in kinds and "close" in kinds


class TestErrorEnvelope:
    def test_connect_failure_is_classified_and_sanitized(self) -> None:
        driver = FakePgDriver(
            connect_error=FakePgError(
                'connection failed: host "db.example.com" password "hunter2"'
            )
        )
        result = open_postgres_connector(
            config=make_config(),
            password="hunter2",
            tls_ca_path=None,
            deadline_monotonic=30.0,
            monotonic=lambda: 0.0,
            driver=driver,
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.CONNECT_FAILED
        assert "hunter2" not in result.detail
        assert "db.example.com" not in result.detail

    @pytest.mark.parametrize(
        ("sqlstate", "expected"),
        [
            ("42501", ToolConnectorError.NOT_AUTHORIZED),
            ("25006", ToolConnectorError.NOT_AUTHORIZED),
            ("57014", ToolConnectorError.DEADLINE_EXCEEDED),
            ("42P01", ToolConnectorError.EXECUTION_FAILED),
        ],
    )
    def test_execute_errors_map_by_sqlstate(
        self, sqlstate: str, expected: ToolConnectorError
    ) -> None:
        driver = FakePgDriver(
            execute_error=FakePgError("secret detail hunter2", sqlstate=sqlstate)
        )
        connector = open_ok(driver)
        result = connector.execute_stream("SELECT 1")
        assert is_err(result)
        assert result.error == expected
        assert "hunter2" not in result.detail
        assert sqlstate in result.detail  # 分類根拠の sqlstate だけは出す

    def test_midstream_error_is_wrapped_and_sanitized(self) -> None:
        driver = FakePgDriver(
            columns=("id",),
            rows=[(i,) for i in range(600)],
            fetch_error=FakePgError("fetch broke: password=hunter2"),
            fetch_error_after=1,
        )
        connector = open_ok(driver)
        stream = connector.execute_stream("SELECT 1").value
        with pytest.raises(ConnectorStreamError) as exc_info:
            list(stream)
        assert exc_info.value.reason == ToolConnectorError.EXECUTION_FAILED
        assert "hunter2" not in exc_info.value.detail

    def test_missing_driver_is_connect_failed(self) -> None:
        result = open_postgres_connector(
            config=make_config(),
            password="hunter2",
            tls_ca_path=None,
            deadline_monotonic=30.0,
            monotonic=lambda: 0.0,
            driver=None,
            driver_importer=lambda: (_ for _ in ()).throw(ImportError("no psycopg")),
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.CONNECT_FAILED

    def test_read_only_setup_failure_closes_connection(self) -> None:
        """read_only 設定に失敗したら開いた接続を捨てる（接続リーク防止）。"""
        driver = FakePgDriver(
            read_only_error=FakePgError("cannot set read_only", sqlstate="25001")
        )
        result = open_postgres_connector(
            config=make_config(),
            password="hunter2",
            tls_ca_path=None,
            deadline_monotonic=30.0,
            monotonic=lambda: 0.0,
            driver=driver,
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.CONNECT_FAILED
        assert driver.connections[0].closed

    def test_execute_failure_closes_cursor(self) -> None:
        driver = FakePgDriver(
            execute_error=FakePgError("relation missing", sqlstate="42P01")
        )
        connector = open_ok(driver)
        result = connector.execute_stream("SELECT 1")
        assert is_err(result)
        conn = driver.connections[0]
        assert any(e[0] == "cursor_close" for e in conn.events)

    def test_midstream_error_does_not_chain_original_exception(self) -> None:
        """秘密を含み得る driver 例外を __cause__ に残さない（from None）。"""
        driver = FakePgDriver(
            columns=("id",),
            rows=[(i,) for i in range(600)],
            fetch_error=FakePgError("boom password=hunter2 host=db"),
            fetch_error_after=1,
        )
        connector = open_ok(driver)
        stream = connector.execute_stream("SELECT 1").value
        with pytest.raises(ConnectorStreamError) as exc_info:
            list(stream)
        assert exc_info.value.__cause__ is None
        assert "hunter2" not in str(exc_info.value)
