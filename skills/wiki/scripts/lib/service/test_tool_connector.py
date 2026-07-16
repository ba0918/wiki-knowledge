"""Tests for tool_connector.py — Connector protocol と sqlite read-only 三重防御.

read-only enforcement は ① read-only URI ② PRAGMA query_only ③ authorizer
action matrix の三重。真実源は authorizer（DB エンジン自身の判定）で、
SQLITE_FUNCTION を許可できる根拠は connector invariant（extension 無効・
UDF 非登録・接続は当該 DB のみ）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.tool_connector import (
    ConnectorStreamError,
    FakeConnector,
    SqliteConnector,
    ToolConnectorError,
    open_sqlite_connector,
)


ALLOWED = ("users", "registrations", "refunds")


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "events.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE registrations (user_id INTEGER, event TEXT);
        CREATE TABLE refunds (user_id INTEGER, amount INTEGER);
        CREATE TABLE secrets (token TEXT);
        INSERT INTO users VALUES (1, 'alice'), (2, 'bob'), (3, 'carol');
        INSERT INTO registrations VALUES (1, 'ev1'), (2, 'ev1'), (3, 'ev2');
        INSERT INTO refunds VALUES (2, 500);
        INSERT INTO secrets VALUES ('do-not-read');
        """
    )
    conn.commit()
    conn.close()
    return path


def make_connector(db_path: Path, **overrides) -> SqliteConnector:
    args = dict(
        db_path=db_path,
        allowed_tables=ALLOWED,
        max_cell_bytes=65536,
        deadline_monotonic=10_000.0,
        monotonic=lambda: 0.0,
    )
    args.update(overrides)
    result = open_sqlite_connector(**args)
    assert is_ok(result), f"接続に失敗: {result}"
    return result.value


# ---------------------------------------------------------------------------
# 接続
# ---------------------------------------------------------------------------


class TestOpen:
    def test_missing_db_file_is_connect_failed(self, tmp_path: Path) -> None:
        result = open_sqlite_connector(
            db_path=tmp_path / "no-such.sqlite3",
            allowed_tables=ALLOWED,
            max_cell_bytes=65536,
            deadline_monotonic=10.0,
            monotonic=lambda: 0.0,
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.CONNECT_FAILED

    def test_special_chars_in_path_do_not_leak_into_uri_query(
        self, tmp_path: Path
    ) -> None:
        """`?` `#` を含むパスは percent-encoding で URI query string 汚染を防ぐ。"""
        weird = tmp_path / "we?ird#.sqlite3"
        conn = sqlite3.connect(weird)
        conn.execute("CREATE TABLE users (user_id INTEGER)")
        conn.commit()
        conn.close()
        connector = make_connector(weird)
        try:
            result = connector.execute_stream("SELECT count(*) FROM users")
            assert is_ok(result)
            with result.value as stream:
                assert list(stream) == [(0,)]
        finally:
            connector.close()

    def test_read_only_uri_blocks_writes_even_without_authorizer_reason(
        self, db_path: Path
    ) -> None:
        """三重防御のうちどの層かは問わないが、書き込みは必ず失敗する。"""
        connector = make_connector(db_path)
        try:
            result = connector.execute_stream("INSERT INTO users VALUES (9, 'mallory')")
            assert is_err(result)
        finally:
            connector.close()
        check = sqlite3.connect(db_path)
        assert check.execute("SELECT count(*) FROM users").fetchone() == (3,)
        check.close()


# ---------------------------------------------------------------------------
# authorizer action matrix
# ---------------------------------------------------------------------------


class TestAuthorizer:
    @pytest.fixture()
    def connector(self, db_path: Path):
        connector = make_connector(db_path)
        yield connector
        connector.close()

    def test_direct_read_of_unlisted_table_is_denied(self, connector) -> None:
        result = connector.execute_stream("SELECT * FROM secrets")
        assert is_err(result)
        assert result.error == ToolConnectorError.NOT_AUTHORIZED

    def test_join_with_unlisted_table_is_denied(self, connector) -> None:
        result = connector.execute_stream(
            "SELECT u.name FROM users u JOIN secrets s ON 1=1"
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.NOT_AUTHORIZED

    def test_subquery_on_unlisted_table_is_denied(self, connector) -> None:
        result = connector.execute_stream(
            "SELECT * FROM users WHERE name IN (SELECT token FROM secrets)"
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.NOT_AUTHORIZED

    def test_cte_wrapping_unlisted_table_is_denied(self, connector) -> None:
        result = connector.execute_stream(
            "WITH s AS (SELECT token FROM secrets) SELECT * FROM s"
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.NOT_AUTHORIZED

    def test_system_table_read_is_denied(self, connector) -> None:
        result = connector.execute_stream("SELECT * FROM sqlite_master")
        assert is_err(result)
        assert result.error == ToolConnectorError.NOT_AUTHORIZED

    def test_system_table_denied_even_if_allowlisted(self, db_path: Path) -> None:
        """catalog が誤って sqlite_master を allowlist しても authorizer は拒否する。"""
        connector = make_connector(
            db_path, allowed_tables=("users", "sqlite_master")
        )
        try:
            result = connector.execute_stream("SELECT * FROM sqlite_master")
            assert is_err(result)
            assert result.error == ToolConnectorError.NOT_AUTHORIZED
        finally:
            connector.close()

    @pytest.mark.parametrize(
        "sql",
        [
            "ATTACH DATABASE ':memory:' AS x",
            "DETACH DATABASE main",
            "PRAGMA table_info(users)",
            "CREATE TABLE t (x)",
            "CREATE TEMP TABLE t (x)",
            "DROP TABLE users",
            "ALTER TABLE users ADD COLUMN x",
            "UPDATE users SET name = 'x'",
            "DELETE FROM users",
            "BEGIN",
            "VACUUM",
        ],
    )
    def test_non_select_actions_are_denied(self, connector, sql: str) -> None:
        """authorizer 拒否は NOT_AUTHORIZED に分類される（誤分類の回帰検出）。"""
        result = connector.execute_stream(sql)
        assert is_err(result)
        assert result.error == ToolConnectorError.NOT_AUTHORIZED, sql

    def test_load_extension_is_unavailable(self, connector) -> None:
        """connector invariant: extension loading は無効のまま。"""
        result = connector.execute_stream("SELECT load_extension('evil')")
        assert is_err(result)
        assert result.error == ToolConnectorError.NOT_AUTHORIZED

    def test_multi_statement_is_rejected(self, connector) -> None:
        result = connector.execute_stream("SELECT 1; DROP TABLE users")
        assert is_err(result)
        assert result.error == ToolConnectorError.EXECUTION_FAILED

    def test_writing_cte_that_passes_precheck_is_denied(self, connector) -> None:
        """precheck（WITH 開始）を通過する書込み CTE も authorizer が拒否する。"""
        result = connector.execute_stream(
            "WITH x AS (SELECT 1) INSERT INTO users SELECT 9, 'm', 'm' FROM x"
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.NOT_AUTHORIZED

    def test_view_without_base_table_in_allowlist_is_denied(
        self, db_path: Path
    ) -> None:
        """view を許可しても基底 table が allowlist になければ拒否される
        （authorizer は基底 table の READ を発火する）。"""
        add_view = sqlite3.connect(db_path)
        add_view.execute("CREATE VIEW v_users AS SELECT user_id, name FROM users")
        add_view.execute("CREATE VIEW v_secrets AS SELECT token FROM secrets")
        add_view.commit()
        add_view.close()

        view_only = make_connector(db_path, allowed_tables=("v_users",))
        try:
            result = view_only.execute_stream("SELECT * FROM v_users")
            assert is_err(result)
            assert result.error == ToolConnectorError.NOT_AUTHORIZED
        finally:
            view_only.close()

        with_base = make_connector(db_path, allowed_tables=("v_users", "users"))
        try:
            result = with_base.execute_stream("SELECT count(*) FROM v_users")
            assert is_ok(result)
            with result.value as stream:
                assert list(stream) == [(3,)]
        finally:
            with_base.close()

        # 未許可 table を読む view は view 自体を許可しても拒否
        leaky = make_connector(db_path, allowed_tables=("v_secrets", "users"))
        try:
            result = leaky.execute_stream("SELECT * FROM v_secrets")
            assert is_err(result)
            assert result.error == ToolConnectorError.NOT_AUTHORIZED
        finally:
            leaky.close()

    def test_cte_count_join_query_passes(self, connector) -> None:
        """正当な集計クエリ（CTE + COUNT + JOIN）は authorizer を通過する。"""
        sql = """
        WITH refunded AS (SELECT user_id FROM refunds)
        SELECT count(*)
        FROM registrations r
        JOIN users u ON u.user_id = r.user_id
        WHERE r.user_id NOT IN (SELECT user_id FROM refunded)
        """
        result = connector.execute_stream(sql)
        assert is_ok(result)
        with result.value as stream:
            assert list(stream) == [(2,)]

    def test_builtin_functions_are_allowed(self, connector) -> None:
        result = connector.execute_stream("SELECT upper(name) FROM users ORDER BY 1")
        assert is_ok(result)
        with result.value as stream:
            assert [row[0] for row in stream] == ["ALICE", "BOB", "CAROL"]


# ---------------------------------------------------------------------------
# setlimit（巨大値の割り当て前遮断）
# ---------------------------------------------------------------------------


class TestSetlimit:
    @pytest.mark.parametrize("expr", ["randomblob(1000000000)", "zeroblob(1000000000)"])
    def test_giant_value_generation_is_blocked_before_allocation(
        self, db_path: Path, expr: str
    ) -> None:
        connector = make_connector(db_path, max_cell_bytes=65536)
        try:
            result = connector.execute_stream(f"SELECT {expr}")
            if is_ok(result):
                with pytest.raises(ConnectorStreamError):
                    with result.value as stream:
                        list(stream)
            else:
                assert result.error == ToolConnectorError.VALUE_TOO_BIG
        finally:
            connector.close()

    def test_values_within_limit_pass(self, db_path: Path) -> None:
        connector = make_connector(db_path, max_cell_bytes=65536)
        try:
            result = connector.execute_stream("SELECT randomblob(1024)")
            assert is_ok(result)
            with result.value as stream:
                rows = list(stream)
            assert len(rows[0][0]) == 1024
        finally:
            connector.close()


# ---------------------------------------------------------------------------
# DB wall-clock deadline
# ---------------------------------------------------------------------------


class TestDeadline:
    def test_long_query_is_interrupted_at_deadline(self, db_path: Path) -> None:
        clock = {"t": 0.0}

        def fake_monotonic() -> float:
            clock["t"] += 1.0
            return clock["t"]

        connector = make_connector(
            db_path, deadline_monotonic=5.0, monotonic=fake_monotonic
        )
        try:
            # cross join で progress handler が確実に発火する演算量を作る
            sql = """
            SELECT count(*)
            FROM users a, users b, registrations c, registrations d,
                 users e, users f, registrations g, registrations h
            """
            result = connector.execute_stream(sql)
            if is_ok(result):
                with pytest.raises(ConnectorStreamError) as exc:
                    with result.value as stream:
                        list(stream)
                assert exc.value.reason == ToolConnectorError.DEADLINE_EXCEEDED
            else:
                assert result.error == ToolConnectorError.DEADLINE_EXCEEDED
        finally:
            connector.close()

    def test_query_within_deadline_completes(self, db_path: Path) -> None:
        connector = make_connector(db_path, deadline_monotonic=10_000.0)
        try:
            result = connector.execute_stream("SELECT count(*) FROM users")
            assert is_ok(result)
            with result.value as stream:
                assert list(stream) == [(3,)]
        finally:
            connector.close()

    def test_expired_deadline_refuses_to_connect(self, db_path: Path) -> None:
        """期限切れなら接続そのものをしない（floor による猶予を与えない）。"""
        result = open_sqlite_connector(
            db_path=db_path,
            allowed_tables=ALLOWED,
            max_cell_bytes=65536,
            deadline_monotonic=5.0,
            monotonic=lambda: 5.0,  # ちょうど期限
        )
        assert is_err(result)
        assert result.error == ToolConnectorError.DEADLINE_EXCEEDED

    def test_lightweight_query_after_deadline_is_refused(
        self, db_path: Path
    ) -> None:
        """progress handler が発火しない軽量クエリ（SELECT 1）も、
        実行前チェックで期限を守る。"""
        clock = {"t": 0.0}
        connector = make_connector(
            db_path, deadline_monotonic=5.0, monotonic=lambda: clock["t"]
        )
        try:
            clock["t"] = 6.0  # 接続後に期限が過ぎた
            result = connector.execute_stream("SELECT 1")
            assert is_err(result)
            assert result.error == ToolConnectorError.DEADLINE_EXCEEDED
        finally:
            connector.close()


# ---------------------------------------------------------------------------
# Connector protocol contract（SqliteConnector / FakeConnector 共通）
# ---------------------------------------------------------------------------


class ConnectorContract:
    """両実装が満たすべき protocol contract。

    サブクラスが ``make(rows, columns)`` を提供する。データは
    users(user_id, name) 相当の 3 行。
    """

    def make(self, tmp_path: Path):  # pragma: no cover - abstract
        raise NotImplementedError

    def test_stream_exposes_column_metadata(self, tmp_path: Path) -> None:
        connector = self.make(tmp_path)
        try:
            result = connector.execute_stream("SELECT user_id, name FROM users")
            assert is_ok(result)
            with result.value as stream:
                assert stream.columns == ("user_id", "name")
        finally:
            connector.close()

    def test_stream_yields_typed_rows(self, tmp_path: Path) -> None:
        connector = self.make(tmp_path)
        try:
            result = connector.execute_stream("SELECT user_id, name FROM users")
            with result.value as stream:
                rows = list(stream)
            assert (1, "alice") in rows
            for row in rows:
                assert isinstance(row, tuple)
                assert all(
                    value is None or isinstance(value, (int, float, str, bytes))
                    for value in row
                )
        finally:
            connector.close()

    def test_stream_close_is_idempotent(self, tmp_path: Path) -> None:
        connector = self.make(tmp_path)
        try:
            result = connector.execute_stream("SELECT user_id, name FROM users")
            stream = result.value
            stream.close()
            stream.close()
        finally:
            connector.close()

    def test_context_manager_closes_on_exception(self, tmp_path: Path) -> None:
        connector = self.make(tmp_path)
        try:
            result = connector.execute_stream("SELECT user_id, name FROM users")
            stream = result.value
            with pytest.raises(RuntimeError):
                with stream:
                    raise RuntimeError("boom")
            assert stream.closed is True
        finally:
            connector.close()

    def test_connector_close_is_idempotent(self, tmp_path: Path) -> None:
        connector = self.make(tmp_path)
        connector.close()
        connector.close()

    def test_stream_values_cover_full_type_contract(self, tmp_path: Path) -> None:
        """値型契約の全 5 型（None/int/float/str/bytes）が protocol を通る。"""
        connector = self.make_typed(tmp_path)
        try:
            result = connector.execute_stream("SELECT i, f, s, b, n FROM typed")
            assert is_ok(result)
            with result.value as stream:
                rows = list(stream)
            assert rows == [(1, 1.5, "x", b"\x01\x02", None)]
        finally:
            connector.close()


class TestSqliteConnectorContract(ConnectorContract):
    def _open(self, path: Path, tables: tuple[str, ...]):
        result = open_sqlite_connector(
            db_path=path,
            allowed_tables=tables,
            max_cell_bytes=65536,
            deadline_monotonic=10_000.0,
            monotonic=lambda: 0.0,
        )
        assert is_ok(result)
        return result.value

    def make(self, tmp_path: Path):
        path = tmp_path / "contract.sqlite3"
        if not path.exists():
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE users (user_id INTEGER, name TEXT)")
            conn.executemany(
                "INSERT INTO users VALUES (?, ?)",
                [(1, "alice"), (2, "bob"), (3, "carol")],
            )
            conn.commit()
            conn.close()
        return self._open(path, ("users",))

    def make_typed(self, tmp_path: Path):
        path = tmp_path / "typed.sqlite3"
        if not path.exists():
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE typed (i INTEGER, f REAL, s TEXT, b BLOB, n TEXT)")
            conn.execute(
                "INSERT INTO typed VALUES (?, ?, ?, ?, ?)",
                (1, 1.5, "x", b"\x01\x02", None),
            )
            conn.commit()
            conn.close()
        return self._open(path, ("typed",))


class TestFakeConnectorContract(ConnectorContract):
    def make(self, tmp_path: Path):
        return FakeConnector(
            columns=("user_id", "name"),
            rows=[(1, "alice"), (2, "bob"), (3, "carol")],
        )

    def make_typed(self, tmp_path: Path):
        return FakeConnector(
            columns=("i", "f", "s", "b", "n"),
            rows=[(1, 1.5, "x", b"\x01\x02", None)],
        )


class TestFakeConnector:
    def test_rejects_values_outside_type_contract(self) -> None:
        """protocol の値型契約外（bool・dict 等）の fixture を構築時に拒否する。"""
        for bad_row in [(True,), ({},), ([1],)]:
            with pytest.raises(ValueError):
                FakeConnector(columns=("x",), rows=[bad_row])

    def test_records_executed_sql(self) -> None:
        fake = FakeConnector(columns=("x",), rows=[(1,)])
        fake.execute_stream("SELECT 1")
        assert fake.executed == ["SELECT 1"]

    def test_configured_error_is_returned(self) -> None:
        fake = FakeConnector(
            columns=(),
            rows=[],
            fail_with=ToolConnectorError.NOT_AUTHORIZED,
        )
        result = fake.execute_stream("SELECT * FROM secrets")
        assert is_err(result)
        assert result.error == ToolConnectorError.NOT_AUTHORIZED

    def test_raise_after_n_rows_simulates_midstream_failure(self) -> None:
        fake = FakeConnector(
            columns=("x",),
            rows=[(1,), (2,), (3,)],
            raise_after=1,
            raise_reason=ToolConnectorError.DEADLINE_EXCEEDED,
        )
        result = fake.execute_stream("SELECT x FROM t")
        received: list = []
        with pytest.raises(ConnectorStreamError) as exc:
            with result.value as stream:
                for row in stream:
                    received.append(row)
        assert received == [(1,)]
        assert exc.value.reason == ToolConnectorError.DEADLINE_EXCEEDED
