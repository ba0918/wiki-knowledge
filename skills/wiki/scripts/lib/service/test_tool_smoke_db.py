"""実 DB smoke — opt-in profile（計画 Acceptance Criteria で裁定済み）.

環境変数に接続情報 JSON を設定した場合のみ実行される。未設定時の skip は
「失敗テストの回避」ではなく、通常 CI に実 PostgreSQL / MySQL を要求しない
ための opt-in 分離。fake driver では検証できない「実 driver・実サーバー」の
接続 → read-only session → SELECT → cursor streaming を確認する。

例::

    TOOL_QUERY_SMOKE_PG='{"host":"localhost","port":5432,"dbname":"postgres",
        "user":"readonly","password":"secret","allow_insecure_tls":true}' \\
    TOOL_QUERY_SMOKE_MYSQL='{"host":"localhost","port":3306,"dbname":"appdb",
        "user":"readonly","password":"secret","allow_insecure_tls":true}' \\
    .venv/bin/python -m pytest lib/service/test_tool_smoke_db.py -v
"""

from __future__ import annotations

import json
import os
import time

import pytest

from lib.domain.types import is_ok
from lib.service.tool_catalog import MySqlConnectionConfig, PostgresConnectionConfig

PG_ENV = "TOOL_QUERY_SMOKE_PG"
MYSQL_ENV = "TOOL_QUERY_SMOKE_MYSQL"

pg_smoke = pytest.mark.skipif(
    not os.environ.get(PG_ENV),
    reason=f"opt-in smoke: {PG_ENV} 未設定（実 PostgreSQL が必要）",
)
mysql_smoke = pytest.mark.skipif(
    not os.environ.get(MYSQL_ENV),
    reason=f"opt-in smoke: {MYSQL_ENV} 未設定（実 MySQL が必要）",
)


def _env_config(name: str) -> dict:
    data = json.loads(os.environ[name])
    data.setdefault("port", 5432 if name == PG_ENV else 3306)
    return data


def _deadline(seconds: float = 30.0) -> float:
    return time.monotonic() + seconds


@pg_smoke
class TestPostgresSmoke:
    def _open(self):
        from lib.service.tool_connector_pg import open_postgres_connector

        raw = _env_config(PG_ENV)
        password = raw.pop("password")
        tls_ca = raw.pop("tls_ca_file", None)
        config = PostgresConnectionConfig(**raw)
        result = open_postgres_connector(
            config=config,
            password=password,
            tls_ca_path=tls_ca,
            deadline_monotonic=_deadline(),
        )
        assert is_ok(result), getattr(result, "detail", None)
        return result.value

    def test_named_cursor_runs_in_readonly_transaction(self) -> None:
        connector = self._open()
        try:
            # 実クエリと同じ経路（named cursor が属する transaction）で
            # read-only 状態を introspection する。SHOW は named cursor で
            # 実行できないため current_setting（SELECT）を使う（doctor と同じ）
            with connector.execute_stream(
                "SELECT current_setting('transaction_read_only')"
            ).value as s:
                assert list(s) == [("on",)]
        finally:
            connector.close()

    def test_select_streams_and_early_termination_is_fast(self) -> None:
        connector = self._open()
        started = time.monotonic()
        try:
            result = connector.execute_stream(
                "SELECT g FROM generate_series(1, 1000000) AS g"
            )
            assert is_ok(result)
            stream = result.value
            got = []
            for row in stream:
                got.append(row)
                if len(got) >= 5:
                    break
            stream.close()
        finally:
            connector.close()
        assert got[0] == (1,)
        assert time.monotonic() - started < 10.0


@mysql_smoke
class TestMySqlSmoke:
    def _open(self):
        from lib.service.tool_connector_mysql import open_mysql_connector

        raw = _env_config(MYSQL_ENV)
        password = raw.pop("password")
        tls_ca = raw.pop("tls_ca_file", None)
        config = MySqlConnectionConfig(**raw)
        result = open_mysql_connector(
            config=config,
            password=password,
            tls_ca_path=tls_ca,
            deadline_monotonic=_deadline(),
        )
        assert is_ok(result), getattr(result, "detail", None)
        return result.value

    def test_session_is_read_only(self) -> None:
        connector = self._open()
        try:
            with connector.execute_stream(
                "SELECT @@session.transaction_read_only"
            ).value as s:
                assert list(s) == [(1,)]
        finally:
            connector.close()

    def test_large_result_early_termination_does_not_drain(self) -> None:
        """数行で打ち切り、残結果（100 万行）を全受信せず規定時間内に終わること。

        SSCursor.close() 経由だと残結果のネットワーク全消費が起きる —
        connection 即 discard の契約が実サーバーで効いていることの確認。
        """
        connector = self._open()
        big = (
            "WITH RECURSIVE cte AS ("
            "SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 1000"
            ") SELECT a.n FROM cte a JOIN cte b"
        )
        started = time.monotonic()
        result = connector.execute_stream(big)
        assert is_ok(result), getattr(result, "detail", None)
        stream = result.value
        got = []
        for row in stream:
            got.append(row)
            if len(got) >= 5:
                break
        stream.close()
        connector.close()
        assert len(got) == 5
        assert time.monotonic() - started < 10.0
