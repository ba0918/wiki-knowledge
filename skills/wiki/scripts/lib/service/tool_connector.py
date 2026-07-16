"""Connector protocol + SqliteConnector + FakeConnector.

Connector の責務（enforcement の所有権）:

* typed row stream（column metadata 付き iterator、close/例外時 cleanup 契約、
  値型は None/int/float/str/bytes）
* read-only 三重防御: ① read-only URI ② ``PRAGMA query_only=ON``
  ③ ``set_authorizer`` の action matrix
* DB 側 wall-clock deadline（progress handler + monotonic clock、DI 可能）
* ``setlimit()`` による巨大値の**割り当て前**遮断（``randomblob()`` 等は既定で
  最大 10^9 bytes を生成でき、事後検査では手遅れのため）

出力上限（max_rows / max_result_bytes / max_cell_bytes の計数）は Runner の
責務 — 同じ上限を二箇所で数えない。

connector invariant（``SQLITE_FUNCTION`` を許可できる根拠）:
extension loading は無効のまま（``enable_load_extension`` を呼ばない）・
UDF を一切登録しない・接続は当該 DB ファイルのみ。呼べるのは sqlite 組み込み
関数だけで、未知関数への fail-open が構造的に存在しない。

WITH RECURSIVE は action matrix（SELECT / READ / FUNCTION のみ許可）により
拒否される。正当クエリの表現力として必要になったら matrix の拡張として
レビュー付きで判断する。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterator, Protocol, Sequence, runtime_checkable
from urllib.parse import quote
from enum import Enum

from lib.domain.types import Err, Ok


PROGRESS_HANDLER_OPS = 1000
MAX_SQL_LENGTH = 1_000_000
MAX_COLUMNS = 256

# SQLITE_LIMIT_LENGTH は返却値だけでなく**スキャン中に読む格納値**にも効くため、
# max_cell_bytes（ユーザー policy）に直結すると WHERE 句で触れただけの列で
# 誤って落ちる。DB 側の遮断は「巨大値の割り当て前防止」が目的なので、床値は
# catalog schema の max_cell_bytes 実用上限（1 MiB）に固定し、それ未満の
# policy は runner の事後計測（cell_size_bytes）が enforcement する。
DB_LENGTH_LIMIT_FLOOR = 1_048_576

ROW_CHUNK_SIZE = 500

Value = None | int | float | str | bytes
Row = tuple[Value, ...]


class ToolConnectorError(str, Enum):
    """Discriminator for connector failures."""

    CONNECT_FAILED = "connect_failed"
    NOT_AUTHORIZED = "not_authorized"
    EXECUTION_FAILED = "execution_failed"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    # SQLITE_LIMIT_LENGTH による遮断（生成値・格納値とも）。runner の
    # max_cell_bytes 事後検査と同じ policy 違反なので同じ reason code 値を使う
    VALUE_TOO_BIG = "cell_bytes_exceeded"


class ConnectorStreamError(Exception):
    """行 stream の反復中に発生した失敗（deadline 中断・遅延評価エラー）。

    execute_stream() の Result では表現できない「開始後」の失敗チャネル。
    """

    def __init__(self, reason: ToolConnectorError, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@runtime_checkable
class RowStream(Protocol):
    """column metadata 付き row iterator。context manager として使い、
    例外時も close が保証される。"""

    @property
    def columns(self) -> tuple[str, ...]: ...  # pragma: no cover - protocol

    @property
    def closed(self) -> bool: ...  # pragma: no cover - protocol

    def __iter__(self) -> Iterator[Row]: ...  # pragma: no cover - protocol

    def close(self) -> None: ...  # pragma: no cover - protocol

    def __enter__(self) -> "RowStream": ...  # pragma: no cover - protocol

    def __exit__(self, *exc) -> None: ...  # pragma: no cover - protocol


@runtime_checkable
class Connector(Protocol):
    """実行スクリプトが依存する接続抽象。PoC は sqlite のみ、postgres adapter が
    第2段階でこの protocol に差し込まれる。"""

    def execute_stream(
        self, sql: str
    ) -> "Ok[RowStream] | Err[ToolConnectorError]": ...  # pragma: no cover

    def close(self) -> None: ...  # pragma: no cover - protocol


# ---------------------------------------------------------------------------
# sqlite 実装
# ---------------------------------------------------------------------------


def _classify_sqlite_error(exc: BaseException) -> ToolConnectorError:
    # sqlite_errorcode（DB エンジン自身のエラーコード）を第一判定にする。
    # メッセージ文字列は表現が action ごとに揺れる（"not authorized" /
    # "authorization denied" / "prohibited"）ため fallback に留める
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        if code == sqlite3.SQLITE_AUTH:
            return ToolConnectorError.NOT_AUTHORIZED
        if code == sqlite3.SQLITE_INTERRUPT:
            return ToolConnectorError.DEADLINE_EXCEEDED
        if code == sqlite3.SQLITE_TOOBIG:
            return ToolConnectorError.VALUE_TOO_BIG
    message = str(exc).lower()
    if "not authorized" in message or "prohibited" in message or "denied" in message:
        return ToolConnectorError.NOT_AUTHORIZED
    if "interrupted" in message:
        return ToolConnectorError.DEADLINE_EXCEEDED
    if "too big" in message:
        return ToolConnectorError.VALUE_TOO_BIG
    return ToolConnectorError.EXECUTION_FAILED


class _SqliteRowStream:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor
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
                chunk = self._cursor.fetchmany(ROW_CHUNK_SIZE)
                if not chunk:
                    return
                yield from chunk
        except sqlite3.Error as exc:
            raise ConnectorStreamError(
                _classify_sqlite_error(exc), str(exc)
            ) from exc

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._cursor.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "_SqliteRowStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SqliteConnector:
    """三重防御付き read-only sqlite 接続。:func:`open_sqlite_connector` で作る。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        deadline_monotonic: float,
        monotonic: Callable[[], float],
    ) -> None:
        self._conn = conn
        self._deadline = deadline_monotonic
        self._monotonic = monotonic
        self._closed = False

    def execute_stream(self, sql: str) -> Ok[_SqliteRowStream] | Err[ToolConnectorError]:
        # progress handler は一定 opcode ごとにしか発火しないため、軽量クエリは
        # handler だけでは期限を守れない。実行開始前に必ず期限を確認する
        if self._monotonic() >= self._deadline:
            return Err(
                error=ToolConnectorError.DEADLINE_EXCEEDED,
                detail="deadline を超過しているため実行しません",
            )
        try:
            cursor = self._conn.execute(sql)
        except (sqlite3.Error, sqlite3.Warning) as exc:
            return Err(error=_classify_sqlite_error(exc), detail=str(exc))
        return Ok(value=_SqliteRowStream(cursor))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def _build_authorizer(
    allowed_tables: frozenset[str],
) -> Callable[[int, str | None, str | None, str | None, str | None], int]:
    def authorizer(
        action: int,
        arg1: str | None,
        arg2: str | None,
        dbname: str | None,
        source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table = arg1 or ""
            # システム表は allowlist に書かれていても拒否する
            if table.lower().startswith("sqlite_"):
                return sqlite3.SQLITE_DENY
            if table in allowed_tables:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            # connector invariant により組み込み関数しか存在しない
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorizer


def open_sqlite_connector(
    *,
    db_path: Path,
    allowed_tables: Sequence[str],
    max_cell_bytes: int,
    deadline_monotonic: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> Ok[SqliteConnector] | Err[ToolConnectorError]:
    """検証済み DB path から read-only 接続を組み立てる。

    ``db_path`` は tool_paths wrapper で封じ込め検証済みであること（本関数は
    パス検証をしない — 所有権は catalog / tool_paths 側）。
    """

    # 期限切れなら接続そのものをしない（busy timeout に floor を与えると
    # 期限切れ後も軽量クエリが実行できてしまう）
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        return Err(
            error=ToolConnectorError.DEADLINE_EXCEEDED,
            detail="deadline を超過しているため接続しません",
        )

    # path 内の `?` `#` が URI の query string として解釈されないよう
    # percent-encoding してから read-only URI を組み立てる
    uri = f"file:{quote(str(db_path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=remaining)
        # 防御② query_only は authorizer 設置前に設定する（PRAGMA 自体が
        # authorizer の action matrix で拒否されるため）
        conn.execute("PRAGMA query_only=ON")
        conn.setlimit(
            sqlite3.SQLITE_LIMIT_LENGTH, max(max_cell_bytes, DB_LENGTH_LIMIT_FLOOR)
        )
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_LENGTH)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS)
        conn.set_authorizer(_build_authorizer(frozenset(allowed_tables)))

        def _progress() -> int:
            return 1 if monotonic() >= deadline_monotonic else 0

        conn.set_progress_handler(_progress, PROGRESS_HANDLER_OPS)
    except sqlite3.Error as exc:
        return Err(error=ToolConnectorError.CONNECT_FAILED, detail=str(exc))
    return Ok(
        value=SqliteConnector(
            conn, deadline_monotonic=deadline_monotonic, monotonic=monotonic
        )
    )


# ---------------------------------------------------------------------------
# FakeConnector — service テスト用の決定的 double
# ---------------------------------------------------------------------------


class _FakeRowStream:
    def __init__(
        self,
        columns: tuple[str, ...],
        rows: list[Row],
        raise_after: int | None,
        raise_reason: ToolConnectorError,
    ) -> None:
        self._columns = columns
        self._rows = rows
        self._raise_after = raise_after
        self._raise_reason = raise_reason
        self._closed = False

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    @property
    def closed(self) -> bool:
        return self._closed

    def __iter__(self) -> Iterator[Row]:
        for i, row in enumerate(self._rows):
            if self._raise_after is not None and i >= self._raise_after:
                raise ConnectorStreamError(self._raise_reason, "fake midstream failure")
            yield row

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "_FakeRowStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class FakeConnector:
    """Connector protocol の決定的 fake。

    * ``executed`` — execute_stream に渡された SQL の履歴
    * ``fail_with`` — execute_stream が返すエラー
    * ``raise_after`` — N 行 yield 後に :class:`ConnectorStreamError` を送出
    """

    def __init__(
        self,
        *,
        columns: tuple[str, ...],
        rows: list[Row],
        fail_with: ToolConnectorError | None = None,
        raise_after: int | None = None,
        raise_reason: ToolConnectorError = ToolConnectorError.EXECUTION_FAILED,
    ) -> None:
        # protocol の値型契約（None/int/float/str/bytes）を fake でも強制する —
        # 実装が返さない型でテストが通ることを防ぐ（bool は int の subclass だが対象外）
        for row in rows:
            for value in row:
                if value is not None and type(value) not in (int, float, str, bytes):
                    raise ValueError(
                        f"FakeConnector: 値型契約外の値です: {value!r} ({type(value).__name__})"
                    )
        self._columns = columns
        self._rows = rows
        self._fail_with = fail_with
        self._raise_after = raise_after
        self._raise_reason = raise_reason
        self.executed: list[str] = []
        self.streams: list[_FakeRowStream] = []
        self.closed = False

    def execute_stream(self, sql: str) -> Ok[_FakeRowStream] | Err[ToolConnectorError]:
        self.executed.append(sql)
        if self._fail_with is not None:
            return Err(error=self._fail_with, detail="fake failure")
        stream = _FakeRowStream(
            self._columns, list(self._rows), self._raise_after, self._raise_reason
        )
        self.streams.append(stream)
        return Ok(value=stream)

    def close(self) -> None:
        self.closed = True
