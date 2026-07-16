"""Tests for tool_connector_mysql.py — MySQLConnector（driver DI、SSCursor）.

実 DB は使わない — PyMySQL の surface を模した FakeMySqlDriver で契約を固定する:

* read-only 順序契約: autocommit 状態（transaction 開始前）で
  ``SET SESSION TRANSACTION READ ONLY`` + ``max_execution_time`` を発行し、
  実 SELECT はその後に開始される transaction 内で実行される
* TLS 既定 CA + hostname 検証 / allow_insecure_tls → ssl_disabled
* SSCursor（unbuffered）必須 + fetchmany chunk
* 後始末の分岐: 正常 EOF のみ cursor close。**早期終了時は SSCursor.close()
  を経由しない**（PyMySQL の SSCursor.close は残結果をネットワーク越しに
  全消費するため、close 経由では打ち切りと deadline が無効化される）—
  connection を即 discard する
* sanitized error envelope（errno で分類、メッセージ・password を透過しない）
"""

from __future__ import annotations

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.tool_catalog import MySqlConnectionConfig
from lib.service.tool_connector import ConnectorStreamError, ToolConnectorError
from lib.service.tool_connector_mysql import (
    FakeMySqlDriver,
    FakeMySqlError,
    open_mysql_connector,
)


def make_config(**overrides) -> MySqlConnectionConfig:
    args = dict(host="db.example.com", port=3306, dbname="appdb", user="readonly")
    args.update(overrides)
    return MySqlConnectionConfig(**args)




def open_ok(driver: FakeMySqlDriver, *, config=None, now=0.0, deadline=30.0, **kwargs):
    result = open_mysql_connector(
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
        driver = FakeMySqlDriver()
        open_ok(driver)
        kw = driver.connect_kwargs
        assert kw["host"] == "db.example.com"
        assert kw["port"] == 3306
        assert kw["database"] == "appdb"
        assert kw["user"] == "readonly"
        assert kw["password"] == "hunter2"
        assert kw["autocommit"] is True
        assert kw["cursorclass"] is driver.cursors.SSCursor

    def test_tls_uses_ssl_context_with_hostname_verification(self) -> None:
        """CA 省略時も check_hostname / CERT_REQUIRED が有効な SSLContext を渡す
        （PyMySQL の hasnoca による検証無効化を回避する）。"""
        import ssl as _ssl

        driver = FakeMySqlDriver()
        open_ok(driver)
        kw = driver.connect_kwargs
        ctx = kw["ssl"]
        assert isinstance(ctx, _ssl.SSLContext)
        assert ctx.check_hostname is True
        assert ctx.verify_mode == _ssl.CERT_REQUIRED
        assert "ssl_disabled" not in kw
        # 検証を無効化する古い経路（ssl_verify_* の素通し）は使わない
        assert "ssl_verify_cert" not in kw
        assert "ssl_ca" not in kw

    def test_tls_ca_path_is_threaded_into_context(self, monkeypatch) -> None:
        """指定 CA が create_default_context(cafile=...) に渡り、hostname 検証も
        維持される（実 PEM を用意せず cafile 引数の受け渡しで検証）。"""
        import ssl as _ssl
        import lib.service.tool_connector_mysql as mod

        captured: dict = {}
        real = _ssl.create_default_context

        def spy(*, cafile=None, **kw):
            captured["cafile"] = cafile
            return real()

        monkeypatch.setattr(mod.ssl, "create_default_context", spy)
        driver = FakeMySqlDriver()
        open_ok(driver, tls_ca_path="/wiki/ca.pem")
        assert captured["cafile"] == "/wiki/ca.pem"
        ctx = driver.connect_kwargs["ssl"]
        assert ctx.check_hostname is True
        assert ctx.verify_mode == _ssl.CERT_REQUIRED

    def test_allow_insecure_tls_disables_ssl(self) -> None:
        driver = FakeMySqlDriver()
        open_ok(driver, config=make_config(host="localhost", allow_insecure_tls=True))
        kw = driver.connect_kwargs
        assert kw["ssl_disabled"] is True
        assert "ssl" not in kw

    def test_connect_and_read_timeouts_use_remaining_budget(self) -> None:
        driver = FakeMySqlDriver()
        open_ok(driver, now=0.0, deadline=30.0)
        kw = driver.connect_kwargs
        assert 1 <= kw["connect_timeout"] <= 30
        # blocking fetch / session I/O にも上限を与える
        assert 1 <= kw["read_timeout"] <= 30
        assert 1 <= kw["write_timeout"] <= 30


class TestReadOnlySessionOrdering:
    def test_session_setup_happens_before_transaction_and_select(self) -> None:
        driver = FakeMySqlDriver(columns=("id",), rows=[(1,)])
        connector = open_ok(driver, deadline=30.0)
        with connector.execute_stream("SELECT id FROM users").value as stream:
            list(stream)
        conn = driver.connections[0]
        executed = [e[1] for e in conn.events if e[0] == "execute"]
        assert executed[0] == "SET SESSION TRANSACTION READ ONLY"
        assert executed[1].startswith("SET SESSION max_execution_time = ")
        assert executed[2] == "START TRANSACTION"
        assert executed[3] == "SELECT id FROM users"
        # SET 2 文は transaction 開始前（autocommit 状態）に発行される
        begin = next(i for i, e in enumerate(conn.events) if e == ("begin",))
        set_events = [
            i
            for i, e in enumerate(conn.events)
            if e[0] == "execute" and e[1].startswith("SET SESSION")
        ]
        assert all(i < begin for i in set_events)

    def test_max_execution_time_is_milliseconds_of_remaining(self) -> None:
        driver = FakeMySqlDriver(columns=("id",), rows=[(1,)])
        connector = open_ok(driver, now=0.0, deadline=30.0)
        with connector.execute_stream("SELECT 1").value as stream:
            list(stream)
        conn = driver.connections[0]
        stmt = next(
            e[1]
            for e in conn.events
            if e[0] == "execute" and "max_execution_time" in e[1]
        )
        ms = int(stmt.rsplit("=", 1)[1])
        assert 0 < ms <= 30000


def _cursor_closes_with_pending_rows(conn) -> list[tuple]:
    """残結果が残ったままの SSCursor.close()（= 全消費経路）の発生一覧。"""

    return [e for e in conn.events if e[0] == "cursor_close" and e[1] > 0]


def _data_cursor_closed(conn) -> bool:
    """SELECT の後に cursor_close が発生したか（setup cursor の close は除外）。"""

    select_idx = next(
        i
        for i, e in enumerate(conn.events)
        if e[0] == "execute" and e[1].startswith("SELECT")
    )
    return any(
        e[0] == "cursor_close" for e in conn.events[select_idx + 1 :]
    )


class TestCleanupPaths:
    def test_normal_eof_closes_cursor_then_rollback_then_connection(self) -> None:
        driver = FakeMySqlDriver(columns=("id",), rows=[(1,), (2,)])
        connector = open_ok(driver)
        with connector.execute_stream("SELECT 1").value as stream:
            assert list(stream) == [(1,), (2,)]
        connector.close()
        conn = driver.connections[0]
        assert _data_cursor_closed(conn)
        assert _cursor_closes_with_pending_rows(conn) == []
        kinds = [e[0] for e in conn.events]
        assert kinds.index("rollback") < kinds.index("close")

    def test_early_close_discards_connection_without_cursor_close(self) -> None:
        """SSCursor.close() は残結果を全消費する — 早期終了は connection 即 discard。"""
        driver = FakeMySqlDriver(columns=("id",), rows=[(i,) for i in range(1000)])
        connector = open_ok(driver)
        stream = connector.execute_stream("SELECT 1").value
        next(iter(stream))
        stream.close()
        conn = driver.connections[0]
        assert not _data_cursor_closed(conn)
        assert _cursor_closes_with_pending_rows(conn) == []
        assert conn.closed  # socket ごと discard
        connector.close()  # 冪等（二重 close で例外を出さない）

    def test_midstream_error_discards_connection_without_cursor_close(self) -> None:
        driver = FakeMySqlDriver(
            columns=("id",),
            rows=[(i,) for i in range(600)],
            fetch_error=FakeMySqlError(2013, "Lost connection hunter2"),
            fetch_error_after=1,
        )
        connector = open_ok(driver)
        stream = connector.execute_stream("SELECT 1").value
        with pytest.raises(ConnectorStreamError) as exc_info:
            list(stream)
        assert "hunter2" not in exc_info.value.detail
        stream.close()
        conn = driver.connections[0]
        assert not _data_cursor_closed(conn)
        assert conn.closed

    def test_deadline_midstream_discards_connection(self) -> None:
        rows = [(i,) for i in range(1000)]
        clock = {"now": 0.0}
        driver = FakeMySqlDriver(columns=("id",), rows=rows)
        connector = open_ok(driver, monotonic=lambda: clock["now"], deadline=30.0)
        stream = connector.execute_stream("SELECT 1").value
        it = iter(stream)
        for _ in range(500):
            next(it)
        clock["now"] = 99.0
        with pytest.raises(ConnectorStreamError) as exc_info:
            for _ in it:
                pass
        assert exc_info.value.reason == ToolConnectorError.DEADLINE_EXCEEDED
        stream.close()
        conn = driver.connections[0]
        assert not _data_cursor_closed(conn)
        assert _cursor_closes_with_pending_rows(conn) == []
        assert conn.closed


class TestDeadline:
    def test_expired_before_connect_does_not_touch_driver(self) -> None:
        driver = FakeMySqlDriver()
        result = open_mysql_connector(
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
        times = iter([0.0, 0.5, 99.0])
        driver = FakeMySqlDriver()
        connector = open_ok(driver, monotonic=lambda: next(times), deadline=30.0)
        result = connector.execute_stream("SELECT 1")
        assert is_err(result)
        assert result.error == ToolConnectorError.DEADLINE_EXCEEDED


class TestErrorEnvelope:
    def test_connect_failure_is_classified_and_sanitized(self) -> None:
        driver = FakeMySqlDriver(
            connect_error=FakeMySqlError(1045, "Access denied for 'readonly' hunter2")
        )
        result = open_mysql_connector(
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
        assert "readonly" not in result.detail

    @pytest.mark.parametrize(
        ("errno", "expected"),
        [
            (1290, ToolConnectorError.NOT_AUTHORIZED),
            (1142, ToolConnectorError.NOT_AUTHORIZED),
            (1044, ToolConnectorError.NOT_AUTHORIZED),
            (3024, ToolConnectorError.DEADLINE_EXCEEDED),
            (1146, ToolConnectorError.EXECUTION_FAILED),
        ],
    )
    def test_execute_errors_map_by_errno(
        self, errno: int, expected: ToolConnectorError
    ) -> None:
        driver = FakeMySqlDriver(
            execute_error=FakeMySqlError(errno, "secret detail hunter2")
        )
        connector = open_ok(driver)
        result = connector.execute_stream("SELECT 1")
        assert is_err(result)
        assert result.error == expected
        assert "hunter2" not in result.detail
        assert str(errno) in result.detail  # 分類根拠の errno だけは出す

    def test_missing_driver_is_connect_failed(self) -> None:
        result = open_mysql_connector(
            config=make_config(),
            password="hunter2",
            tls_ca_path=None,
            deadline_monotonic=30.0,
            monotonic=lambda: 0.0,
            driver=None,
            driver_importer=lambda: (_ for _ in ()).throw(ImportError("no pymysql")),
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.CONNECT_FAILED


class TestTypedRows:
    def test_typed_rows_are_normalized(self) -> None:
        from decimal import Decimal
        import datetime

        driver = FakeMySqlDriver(
            columns=("d", "t", "n"),
            rows=[(Decimal("2.25"), datetime.date(2026, 7, 16), None)],
        )
        connector = open_ok(driver)
        with connector.execute_stream("SELECT *").value as stream:
            (row,) = list(stream)
        assert row == ("2.25", "2026-07-16", None)
