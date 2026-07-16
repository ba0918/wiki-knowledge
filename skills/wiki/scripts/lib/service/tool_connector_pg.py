"""PostgresConnector — psycopg 3 / named cursor / read-only transaction.

read-only semantics の固定（計画「read-only enforcement」節）:
psycopg は既定で最初の DB 操作から transaction を暗黙開始する。``SET
default_transaction_read_only`` を SQL として発行する方式は「設定 SQL 自体が
read-write transaction を開始し、その同じ transaction 内で named cursor が
実行される」順序を許すため、契約は**接続直後・transaction 未開始の時点で
``Connection.read_only = True`` を設定し、その後に開始される明示 transaction
内で named cursor を開く**に一本化する。``statement_timeout`` は接続オプション
（``-c statement_timeout=..``）で渡し、``search_path`` も静的 SQL gate が
未修飾 relation を解決する ``default_schema`` に固定する。

named cursor（server-side cursor）が必須契約: デフォルト cursor は結果全体を
client にバッファするため、行数上限まで fetch した時点で打ち切る防御が
成立しない。後始末は正常・異常とも cursor close → rollback → connection
close（named cursor の close は server 側 portal の破棄で軽量）。

sanitized error envelope: driver 例外のメッセージ・接続文字列・password を
上位へ透過しない。detail に載せるのは分類根拠（sqlstate / errno）のみ。

read-only の第1防御は DB 側 role（catalog 契約）、第2防御は tool_sql_gate、
本 connector の session 属性は第3防御。
"""

from __future__ import annotations

import datetime
import decimal
import math
import time
import uuid
from typing import Callable, Iterator

from lib.domain.types import Err, Ok
from lib.service.tool_catalog import PostgresConnectionConfig
from lib.service.tool_connector import (
    ROW_CHUNK_SIZE,
    ConnectorStreamError,
    Row,
    ToolConnectorError,
)


_CURSOR_NAME_PREFIX = "wikitoolquery"

# sqlstate → reason の分類表。ここにない code は EXECUTION_FAILED
_SQLSTATE_MAP = {
    "42501": ToolConnectorError.NOT_AUTHORIZED,  # insufficient_privilege
    "25006": ToolConnectorError.NOT_AUTHORIZED,  # read_only_sql_transaction
    "57014": ToolConnectorError.DEADLINE_EXCEEDED,  # query_canceled
}


def _import_psycopg():
    import psycopg

    return psycopg


def _classify(exc: BaseException, *, default: ToolConnectorError) -> ToolConnectorError:
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate in _SQLSTATE_MAP:
        return _SQLSTATE_MAP[sqlstate]
    return default


def _sanitized_detail(exc: BaseException) -> str:
    """例外メッセージを透過しない — 分類根拠の sqlstate だけを載せる。"""

    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        return f"driver error (sqlstate={sqlstate})"
    return "driver error（詳細はサーバー側ログ参照）"


def normalize_value(value: object):
    """driver の返す型を Connector 契約（None/int/float/str/bytes）に正規化する。

    方針の固定: Decimal は精度を落とさず str / 日付時刻は ISO 8601 str /
    bool は int / memoryview は bytes。既知外の型は str 化（数値の顔をした
    別型を下流の集計に流さない — 文字列なら CSV 層でそのまま安全に扱える）。
    """

    if value is None or type(value) in (int, float, str, bytes):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return str(value)


def normalize_row(raw: tuple) -> Row:
    return tuple(normalize_value(v) for v in raw)


class _PgRowStream:
    def __init__(
        self,
        cursor,
        *,
        error_class: type[BaseException],
        deadline_monotonic: float,
        monotonic: Callable[[], float],
    ) -> None:
        self._cursor = cursor
        self._error_class = error_class
        self._deadline = deadline_monotonic
        self._monotonic = monotonic
        self._closed = False
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
                    return
                for raw in chunk:
                    yield normalize_row(raw)
        except self._error_class as exc:
            # driver 例外メッセージには password / host / DSN が混ざり得る。
            # __cause__ として保持すると未捕捉 traceback で秘密が stderr に
            # 出るため切断する
            raise ConnectorStreamError(
                _classify(exc, default=ToolConnectorError.EXECUTION_FAILED),
                _sanitized_detail(exc),
            ) from None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._cursor.close()
            except self._error_class:
                pass

    def __enter__(self) -> "_PgRowStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class PostgresConnector:
    """:func:`open_postgres_connector` で作る read-only 接続。"""

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
        self._cursor_seq = 0

    def execute_stream(self, sql: str) -> Ok[_PgRowStream] | Err[ToolConnectorError]:
        if self._monotonic() >= self._deadline:
            return Err(
                error=ToolConnectorError.DEADLINE_EXCEEDED,
                detail="deadline を超過しているため実行しません",
            )
        self._cursor_seq += 1
        name = f"{_CURSOR_NAME_PREFIX}_{self._cursor_seq}"
        cursor = None
        try:
            # named cursor の execute が暗黙 transaction を開始する。明示 BEGIN は
            # 発行しないが、read_only は接続直後（transaction 開始前）に設定済みの
            # ため、ここで始まる暗黙 transaction も read-only になる — read-only の
            # 実効性は明示/暗黙のどちらでも成立する（正式契約として暗黙を採る）
            cursor = self._conn.cursor(name=name)
            cursor.execute(sql)
        except self._error_class as exc:
            # execute 失敗時も生成済み cursor（server 側 portal）を閉じてから返す
            if cursor is not None:
                try:
                    cursor.close()
                except self._error_class:
                    pass
            return Err(
                error=_classify(exc, default=ToolConnectorError.EXECUTION_FAILED),
                detail=_sanitized_detail(exc),
            )
        return Ok(
            value=_PgRowStream(
                cursor,
                error_class=self._error_class,
                deadline_monotonic=self._deadline,
                monotonic=self._monotonic,
            )
        )

    def execute_probe(self, sql: str) -> Ok[None] | Err[ToolConnectorError]:
        if self._monotonic() >= self._deadline:
            return Err(
                error=ToolConnectorError.DEADLINE_EXCEEDED,
                detail="deadline を超過しているため実行しません",
            )
        cursor = None
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql)
        except self._error_class as exc:
            return Err(
                error=_classify(exc, default=ToolConnectorError.EXECUTION_FAILED),
                detail=_sanitized_detail(exc),
            )
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except self._error_class:
                    pass
        return Ok(value=None)

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


def open_postgres_connector(
    *,
    config: PostgresConnectionConfig,
    password: str,
    tls_ca_path: str | None = None,
    deadline_monotonic: float,
    monotonic: Callable[[], float] = time.monotonic,
    driver=None,
    driver_importer: Callable[[], object] = _import_psycopg,
) -> Ok[PostgresConnector] | Err[ToolConnectorError]:
    """field から keyword 引数のみで接続を組み立てる（DSN 文字列は受けない）。

    ``driver`` は psycopg module 互換の DI ポイント（テストは
    :class:`FakePgDriver`。実接続の検証は doctor と opt-in smoke の責務）。
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
                detail="psycopg が見つかりません（requirements.txt を .venv に導入してください）",
            )

    kwargs: dict[str, object] = {
        "host": config.host,
        "port": config.port,
        "dbname": config.dbname,
        "user": config.user,
        "password": password,
        "sslmode": "prefer" if config.allow_insecure_tls else "verify-full",
        "connect_timeout": max(1, math.ceil(remaining)),
        "options": (
            f"-c statement_timeout={math.ceil(remaining * 1000)}"
            f" -c search_path={config.default_schema}"
        ),
        # statement_timeout は server 側の実行時間だけを縛る。ネットワーク停止
        # （TCP 応答なし）で fetch がハングする経路を tcp_user_timeout で縛る
        # （送信データが未 ACK のまま許容される最大 ms）
        "tcp_user_timeout": math.ceil(remaining * 1000),
        "keepalives": 1,
    }
    if not config.allow_insecure_tls:
        # libpq の既定 CA は ~/.postgresql/root.crt であり**システム CA ストア
        # ではない**。CA 未指定で verify-full にすると通常環境で root.crt 不在の
        # ため繋がらない。CA 指定時はそれを、省略時はシステム CA ストア
        # （sslrootcert=system、libpq 16+）を明示する
        kwargs["sslrootcert"] = str(tls_ca_path) if tls_ca_path is not None else "system"

    try:
        conn = driver.connect(**kwargs)
    except driver.Error as exc:
        return Err(
            error=_classify(exc, default=ToolConnectorError.CONNECT_FAILED),
            detail=_sanitized_detail(exc),
        )
    try:
        # transaction 未開始のこの時点で read-only を設定する（以降に開始
        # される transaction はすべて read-only になる）
        conn.read_only = True
    except driver.Error as exc:
        # read_only 設定に失敗したら開いた接続を捨ててから返す（接続リーク防止）
        try:
            conn.close()
        except driver.Error:
            pass
        return Err(
            error=_classify(exc, default=ToolConnectorError.CONNECT_FAILED),
            detail=_sanitized_detail(exc),
        )
    return Ok(
        value=PostgresConnector(
            conn,
            error_class=driver.Error,
            deadline_monotonic=deadline_monotonic,
            monotonic=monotonic,
        )
    )


# ---------------------------------------------------------------------------
# FakePgDriver — service テスト用の決定的 double（psycopg surface を模す）
# ---------------------------------------------------------------------------


class FakePgError(Exception):
    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakePgCursor:
    def __init__(self, conn: "_FakePgConnection", name: str | None) -> None:
        self._conn = conn
        self.name = name
        self.description = None
        self._rows: list[tuple] = []
        self._pos = 0
        self._fetched = 0
        self.closed = False

    def execute(self, sql: str) -> None:
        self._conn._begin_if_needed()
        self._conn.events.append(("execute", self.name, sql))
        driver = self._conn.driver
        statement = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if self.name is not None and statement not in {"SELECT", "VALUES"}:
            raise FakePgError(
                "named cursor query must be SELECT or VALUES", sqlstate="42601"
            )
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
        self._conn.events.append(("cursor_close", self.name))


class _FakePgConnection:
    def __init__(self, driver: "FakePgDriver") -> None:
        self.driver = driver
        self.events: list[tuple] = []
        self._read_only = False
        self.in_transaction = False
        self.closed = False

    @property
    def read_only(self) -> bool:
        return self._read_only

    @read_only.setter
    def read_only(self, value: bool) -> None:
        if self.driver.read_only_error is not None:
            raise self.driver.read_only_error
        if self.in_transaction:
            raise FakePgError("read_only は transaction 内で変更できない")
        self._read_only = value
        self.events.append(("set_read_only", value))

    def _begin_if_needed(self) -> None:
        if not self.in_transaction:
            self.in_transaction = True
            # transaction が read-only で開始されたかを記録する — 順序契約の検証点
            self.events.append(("begin", self._read_only))

    def cursor(self, name: str | None = None):
        self.events.append(("cursor", name))
        return _FakePgCursor(self, name)

    def rollback(self) -> None:
        self.events.append(("rollback",))
        self.in_transaction = False

    def close(self) -> None:
        self.closed = True
        self.events.append(("close",))


class FakePgDriver:
    """psycopg module 互換の決定的 fake。

    * ``connect_kwargs`` / ``connect_args`` — connect に渡された引数
    * ``script`` — SQL 文字列 → ``(columns, rows)`` の対応（未登録は既定値）
    * ``connect_error`` / ``execute_error`` / ``fetch_error`` — 失敗注入
    """

    Error = FakePgError

    def __init__(
        self,
        *,
        columns: tuple[str, ...] = ("value",),
        rows: list[tuple] | None = None,
        script: dict[str, tuple[tuple[str, ...], list[tuple]]] | None = None,
        connect_error: BaseException | None = None,
        read_only_error: BaseException | None = None,
        execute_error: BaseException | None = None,
        fetch_error: BaseException | None = None,
        fetch_error_after: int | None = None,
    ) -> None:
        self.default_columns = columns
        self.default_rows = rows if rows is not None else [(1,)]
        self.script = script or {}
        self.connect_error = connect_error
        self.read_only_error = read_only_error
        self.execute_error = execute_error
        self.fetch_error = fetch_error
        self.fetch_error_after = fetch_error_after if fetch_error is not None else None
        self.connect_kwargs: dict | None = None
        self.connect_args: tuple = ()
        self.connections: list[_FakePgConnection] = []

    def result_for(self, sql: str) -> tuple[tuple[str, ...], list[tuple]]:
        if sql in self.script:
            return self.script[sql]
        return self.default_columns, self.default_rows

    def connect(self, *args, **kwargs) -> _FakePgConnection:
        self.connect_args = args
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error
        conn = _FakePgConnection(self)
        self.connections.append(conn)
        return conn
