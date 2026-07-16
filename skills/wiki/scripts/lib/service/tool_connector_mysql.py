"""MySQLConnector — PyMySQL / SSCursor（unbuffered）/ read-only session.

read-only semantics の固定（計画「read-only enforcement」節）:
``SET SESSION TRANSACTION READ ONLY`` は発行後に**開始される** transaction に
適用され、進行中の transaction には効かない。契約は接続直後（autocommit
状態、transaction 開始前）に read-only + ``max_execution_time`` を発行し、
実 SELECT はその後の明示 ``START TRANSACTION`` 内で実行する。

後始末の分岐が pg と異なる（計画「cursor / streaming 契約」節）:
PyMySQL の ``SSCursor.close()`` は残結果を**ネットワーク越しに全消費**する。
close 経由では巨大結果の打ち切りと deadline が無効化されるため、早期終了時
（max_rows・deadline・cancel・stream error）は SSCursor を通常 close せず
connection/socket を即 discard する。正常 EOF のみ cursor close → rollback →
connection close。

MySQL の read-only transaction は一時テーブルへの DML を許容する — この穴は
session 層では塞げないため、DB 側 role（CREATE TEMPORARY TABLES 権限を
付与しない）で防ぐことを guide が保証範囲の限定として明記する。

sanitized error envelope: driver 例外メッセージ・password を透過しない。
detail に載せるのは分類根拠（errno）のみ。
"""

from __future__ import annotations

import math
import ssl
import time
from typing import Callable, Iterator

from lib.domain.types import Err, Ok
from lib.service.tool_catalog import MySqlConnectionConfig
from lib.service.tool_connector import (
    ROW_CHUNK_SIZE,
    ConnectorStreamError,
    Row,
    ToolConnectorError,
)
from lib.service.tool_connector_pg import normalize_row


# errno → reason の分類表。ここにない code は EXECUTION_FAILED
_ERRNO_MAP = {
    1044: ToolConnectorError.NOT_AUTHORIZED,  # ER_DBACCESS_DENIED_ERROR
    1142: ToolConnectorError.NOT_AUTHORIZED,  # ER_TABLEACCESS_DENIED_ERROR
    1290: ToolConnectorError.NOT_AUTHORIZED,  # ER_OPTION_PREVENTS_STATEMENT (read only)
    3024: ToolConnectorError.DEADLINE_EXCEEDED,  # ER_QUERY_TIMEOUT (max_execution_time)
}


def _import_pymysql():
    import pymysql

    return pymysql


def _errno_of(exc: BaseException) -> int | None:
    args = getattr(exc, "args", ())
    if args and type(args[0]) is int:
        return args[0]
    return None


def _classify(exc: BaseException, *, default: ToolConnectorError) -> ToolConnectorError:
    errno = _errno_of(exc)
    if errno in _ERRNO_MAP:
        return _ERRNO_MAP[errno]
    return default


def _sanitized_detail(exc: BaseException) -> str:
    """例外メッセージを透過しない — 分類根拠の errno だけを載せる。"""

    errno = _errno_of(exc)
    if errno is not None:
        return f"driver error (errno={errno})"
    return "driver error（詳細はサーバー側ログ参照）"


class _MySqlRowStream:
    """正常 EOF と早期終了で後始末経路が分かれる row stream。

    * EOF まで消費 → close() は cursor close（残結果なしなので安全）
    * 早期終了 → close() は connection.discard()（SSCursor.close の
      残結果全消費を経由しない）
    """

    def __init__(
        self,
        cursor,
        connector: "MySQLConnector",
        *,
        error_class: type[BaseException],
        deadline_monotonic: float,
        monotonic: Callable[[], float],
    ) -> None:
        self._cursor = cursor
        self._connector = connector
        self._error_class = error_class
        self._deadline = deadline_monotonic
        self._monotonic = monotonic
        self._closed = False
        self._eof = False
        description = cursor.description or ()
        self._columns = tuple(col[0] for col in description)

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    @property
    def closed(self) -> bool:
        return self._closed

    def __iter__(self) -> Iterator[Row]:
        try:
            while True:
                if self._monotonic() >= self._deadline:
                    raise ConnectorStreamError(
                        ToolConnectorError.DEADLINE_EXCEEDED,
                        "処理全体の deadline を超過しました",
                    )
                chunk = self._cursor.fetchmany(ROW_CHUNK_SIZE)
                if not chunk:
                    self._eof = True
                    return
                for raw in chunk:
                    yield normalize_row(raw)
        except self._error_class as exc:
            # driver 例外メッセージには password / host が混ざり得る。__cause__
            # として保持すると未捕捉 traceback で秘密が stderr に出るため切断する
            raise ConnectorStreamError(
                _classify(exc, default=ToolConnectorError.EXECUTION_FAILED),
                _sanitized_detail(exc),
            ) from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._eof:
            try:
                self._cursor.close()
            except self._error_class:
                pass
        else:
            # 残結果が残っている — cursor.close() は全消費するため通さない
            self._connector.discard()

    def __enter__(self) -> "_MySqlRowStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class MySQLConnector:
    """:func:`open_mysql_connector` で作る read-only 接続。"""

    def __init__(
        self,
        conn,
        *,
        error_class: type[BaseException],
        deadline_monotonic: float,
        monotonic: Callable[[], float],
    ) -> None:
        self._conn = conn
        self._error_class = error_class
        self._deadline = deadline_monotonic
        self._monotonic = monotonic
        self._closed = False
        self._discarded = False

    def execute_stream(self, sql: str) -> Ok[_MySqlRowStream] | Err[ToolConnectorError]:
        if self._monotonic() >= self._deadline:
            return Err(
                error=ToolConnectorError.DEADLINE_EXCEEDED,
                detail="deadline を超過しているため実行しません",
            )
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql)
        except self._error_class as exc:
            return Err(
                error=_classify(exc, default=ToolConnectorError.EXECUTION_FAILED),
                detail=_sanitized_detail(exc),
            )
        return Ok(
            value=_MySqlRowStream(
                cursor,
                self,
                error_class=self._error_class,
                deadline_monotonic=self._deadline,
                monotonic=self._monotonic,
            )
        )

    def discard(self) -> None:
        """早期終了用 — rollback / cursor close を経ず connection を即破棄する。"""

        if not self._discarded:
            self._discarded = True
            self._closed = True
            try:
                self._conn.close()
            except self._error_class:
                pass

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._conn.rollback()
            except self._error_class:
                pass
            try:
                self._conn.close()
            except self._error_class:
                pass


def open_mysql_connector(
    *,
    config: MySqlConnectionConfig,
    password: str,
    tls_ca_path: str | None = None,
    deadline_monotonic: float,
    monotonic: Callable[[], float] = time.monotonic,
    driver=None,
    driver_importer: Callable[[], object] = _import_pymysql,
) -> Ok[MySQLConnector] | Err[ToolConnectorError]:
    """field から keyword 引数のみで接続を組み立てる（DSN 文字列は受けない）。

    ``driver`` は PyMySQL module 互換の DI ポイント（テストは
    :class:`FakeMySqlDriver`。実接続の検証は doctor と opt-in smoke の責務）。
    """

    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        return Err(
            error=ToolConnectorError.DEADLINE_EXCEEDED,
            detail="deadline を超過しているため接続しません",
        )

    if driver is None:
        try:
            driver = driver_importer()
        except ImportError:
            return Err(
                error=ToolConnectorError.CONNECT_FAILED,
                detail="PyMySQL が見つかりません（requirements.txt を .venv に導入してください）",
            )

    budget = max(1, math.ceil(remaining))
    kwargs: dict[str, object] = {
        "host": config.host,
        "port": config.port,
        "database": config.dbname,
        "user": config.user,
        "password": password,
        "autocommit": True,  # transaction は read-only 設定後に明示開始する
        "cursorclass": driver.cursors.SSCursor,
        "connect_timeout": budget,
        # blocking な fetch / session I/O にも残時間の上限を与える。
        # connect_timeout だけでは fetchmany / SET の停止を期限内に切れない
        "read_timeout": budget,
        "write_timeout": budget,
    }
    if config.allow_insecure_tls:
        kwargs["ssl_disabled"] = True
    else:
        # PyMySQL は ssl_ca 省略時（hasnoca）に check_hostname=False +
        # verify_mode=CERT_NONE へ落ちるため、ssl_verify_cert/identity を渡す
        # だけでは CA 省略時に検証が無効になる。自前の SSLContext を渡して
        # 「システム CA（or 指定 CA）+ hostname 検証」を必ず有効にする
        ctx = ssl.create_default_context(
            cafile=str(tls_ca_path) if tls_ca_path is not None else None
        )
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        kwargs["ssl"] = ctx

    try:
        conn = driver.connect(**kwargs)
    except driver.Error as exc:
        return Err(
            error=_classify(exc, default=ToolConnectorError.CONNECT_FAILED),
            detail=_sanitized_detail(exc),
        )

    # autocommit 状態（transaction 開始前）で session を設定してから明示開始する
    ms = math.ceil((deadline_monotonic - monotonic()) * 1000)
    try:
        setup = conn.cursor()
        setup.execute("SET SESSION TRANSACTION READ ONLY")
        setup.execute(f"SET SESSION max_execution_time = {max(ms, 1)}")
        setup.execute("START TRANSACTION")
        setup.close()
    except driver.Error as exc:
        try:
            conn.close()
        except driver.Error:
            pass
        return Err(
            error=_classify(exc, default=ToolConnectorError.CONNECT_FAILED),
            detail=_sanitized_detail(exc),
        )
    return Ok(
        value=MySQLConnector(
            conn,
            error_class=driver.Error,
            deadline_monotonic=deadline_monotonic,
            monotonic=monotonic,
        )
    )


# ---------------------------------------------------------------------------
# FakeMySqlDriver — service テスト用の決定的 double（PyMySQL surface を模す）
# ---------------------------------------------------------------------------


class FakeMySqlError(Exception):
    """PyMySQL 互換: args[0] が errno。"""


class _FakeSSCursor:
    def __init__(self, conn: "_FakeMySqlConnection") -> None:
        self._conn = conn
        self.description = None
        self._rows: list[tuple] = []
        self._pos = 0
        self._fetched = 0
        self.closed = False

    def execute(self, sql: str) -> None:
        driver = self._conn.driver
        if not sql.startswith(("SET SESSION", "START TRANSACTION")):
            self._conn._begin_if_needed()
        elif sql == "START TRANSACTION":
            self._conn.in_transaction = True
            self._conn.events.append(("begin",))
        self._conn.events.append(("execute", sql))
        if sql.startswith(("SET SESSION", "START TRANSACTION")):
            return
        if driver.execute_error is not None:
            raise driver.execute_error
        columns, rows = driver.result_for(sql)
        self.description = [(c, None, None, None, None, None, None) for c in columns]
        self._rows = list(rows)
        self._pos = 0

    def fetchmany(self, n: int) -> list[tuple]:
        self._conn.events.append(("fetchmany", n))
        driver = self._conn.driver
        if (
            driver.fetch_error is not None
            and driver.fetch_error_after is not None
            and self._fetched >= driver.fetch_error_after
        ):
            raise driver.fetch_error
        chunk = self._rows[self._pos : self._pos + n]
        self._pos += len(chunk)
        self._fetched += 1
        return chunk

    def close(self) -> None:
        self.closed = True
        # PyMySQL の SSCursor.close() は残結果を全消費する — 残行数を記録し、
        # 「残結果ありの close を経由しない」契約をテストで検証可能にする
        self._conn.events.append(("cursor_close", len(self._rows) - self._pos))


class _FakeCursors:
    SSCursor = _FakeSSCursor


class _FakeMySqlConnection:
    def __init__(self, driver: "FakeMySqlDriver") -> None:
        self.driver = driver
        self.events: list[tuple] = []
        self.in_transaction = False
        self.closed = False

    def _begin_if_needed(self) -> None:
        # autocommit=True 前提: 明示 START TRANSACTION なしの文は単文 transaction
        if not self.in_transaction:
            self.events.append(("autocommit_stmt",))

    def cursor(self):
        self.events.append(("cursor",))
        return _FakeSSCursor(self)

    def rollback(self) -> None:
        self.events.append(("rollback",))
        self.in_transaction = False

    def close(self) -> None:
        self.closed = True
        self.events.append(("close",))


class FakeMySqlDriver:
    """PyMySQL module 互換の決定的 fake（cursors.SSCursor を含む）。"""

    Error = FakeMySqlError
    cursors = _FakeCursors

    def __init__(
        self,
        *,
        columns: tuple[str, ...] = ("value",),
        rows: list[tuple] | None = None,
        script: dict[str, tuple[tuple[str, ...], list[tuple]]] | None = None,
        connect_error: BaseException | None = None,
        execute_error: BaseException | None = None,
        fetch_error: BaseException | None = None,
        fetch_error_after: int | None = None,
    ) -> None:
        self.default_columns = columns
        self.default_rows = rows if rows is not None else [(1,)]
        self.script = script or {}
        self.connect_error = connect_error
        self.execute_error = execute_error
        self.fetch_error = fetch_error
        self.fetch_error_after = fetch_error_after if fetch_error is not None else None
        self.connect_kwargs: dict | None = None
        self.connections: list[_FakeMySqlConnection] = []

    def result_for(self, sql: str) -> tuple[tuple[str, ...], list[tuple]]:
        if sql in self.script:
            return self.script[sql]
        return self.default_columns, self.default_rows

    def connect(self, **kwargs) -> _FakeMySqlConnection:
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error
        conn = _FakeMySqlConnection(self)
        self.connections.append(conn)
        return conn
