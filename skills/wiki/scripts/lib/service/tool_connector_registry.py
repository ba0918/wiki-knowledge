"""ConnectorProvider registry — type → provider の解決と構築の所有.

runner に type 分岐を増やさないための層。provider が所有するもの:

* type 別の connector 構築（driver DI を含む）
* credential 解決 — pg / mysql は password を接続に使う。sqlite はファイル
  接続で値を使わないが、Phase A からの enforcement（0600 / regular file /
  symlink 拒否 / 構造検証）を必ず通す
* ``tls_ca_file`` の封じ込め解決（wiki_root 相対 / 絶対、symlink 全拒否、
  存在しない CA は接続前に fail fast）
* SQL gate の適用要否の宣言（:meth:`gate_policy`）— sqlite は authorizer
  持ちなので不要、pg / mysql は必須と**provider 側が答える**
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from lib.domain.tool_query import precheck_sql
from lib.domain.types import Err, Ok, is_err
from lib.service.tool_catalog import (
    MySqlConnectionConfig,
    PostgresConnectionConfig,
    SqliteConnectionConfig,
    ToolEntry,
    load_credential,
    resolve_db_path,
)
from lib.service.tool_connector import open_sqlite_connector
from lib.service.tool_connector_http import (
    HttpConnector,
    UrllibTransport,
    build_request_url,
    parse_request_spec,
)
from lib.service.tool_connector_mysql import open_mysql_connector
from lib.service.tool_connector_pg import open_postgres_connector
from lib.service.tool_paths import resolve_declared_dir
from lib.service.tool_sql_gate import check_sql


class RegistryError(str, Enum):
    UNKNOWN_TYPE = "unknown_connector_type"
    CA_FILE_MISSING = "tls_ca_file_missing"


@dataclass(frozen=True)
class GatePolicy:
    """SQL gate の適用宣言。required=True なら dialect / namespace も確定する。"""

    required: bool
    dialect: str | None = None
    default_namespace: str | None = None


@runtime_checkable
class ConnectorProvider(Protocol):
    type_name: str

    def gate_policy(self, entry: ToolEntry) -> GatePolicy: ...  # pragma: no cover

    def precheck(
        self, entry: ToolEntry, text: str, *, label: str | None = None
    ): ...  # pragma: no cover - protocol

    def open(
        self,
        *,
        entry: ToolEntry,
        wiki_root: Path,
        deadline_monotonic: float,
        monotonic: Callable[[], float],
    ): ...  # pragma: no cover - protocol


def _reason_value(reason: object) -> str:
    return reason.value if isinstance(reason, Enum) else str(reason)


def _resolve_tls_ca(
    *, wiki_root: Path, declared: str | None
) -> Ok[str | None] | Err:
    """catalog 宣言の CA ファイルを封じ込め解決する（symlink 全拒否・存在必須）。"""

    if declared is None:
        return Ok(value=None)
    resolved = resolve_declared_dir(wiki_root=wiki_root, declared=declared)
    if is_err(resolved):
        return Err(error=_reason_value(resolved.error), detail=resolved.detail)
    path = resolved.value
    if not os.path.isfile(path):
        return Err(
            error=RegistryError.CA_FILE_MISSING.value,
            detail="tls_ca_file が存在しません（catalog 宣言を確認してください）",
        )
    return Ok(value=str(path))


def _prefixed(detail: str, label: str | None) -> str:
    return f"count {label!r}: {detail}" if label is not None else detail


class _SqlPrecheckMixin:
    """SQL 系 provider 共通の precheck（UX 早期拒否 + 必要なら静的 gate）。"""

    def precheck(
        self, entry: ToolEntry, text: str, *, label: str | None = None
    ) -> Ok[None] | Err:
        pre = precheck_sql(text)
        if is_err(pre):
            return Err(
                error=_reason_value(pre.error), detail=_prefixed(pre.detail, label)
            )
        policy = self.gate_policy(entry)
        if policy.required:
            checked = check_sql(
                text,
                dialect=policy.dialect,
                default_namespace=policy.default_namespace,
                allowed_tables=entry.allowed_tables,
            )
            if is_err(checked):
                return Err(
                    error=_reason_value(checked.error),
                    detail=_prefixed(checked.detail, label),
                )
        return Ok(value=None)


class SqliteConnectorProvider(_SqlPrecheckMixin):
    type_name = "sqlite"

    def __init__(self, factory: Callable[..., object] = open_sqlite_connector) -> None:
        self._factory = factory

    def gate_policy(self, entry: ToolEntry) -> GatePolicy:
        # 三重防御（read-only URI / PRAGMA / authorizer）は実行エンジン自身の
        # 判定なので静的 gate は不要
        return GatePolicy(required=False)

    def open(
        self,
        *,
        entry: ToolEntry,
        wiki_root: Path,
        deadline_monotonic: float,
        monotonic: Callable[[], float],
    ):
        db_path = resolve_db_path(entry=entry, wiki_root=wiki_root)
        if is_err(db_path):
            return Err(error=_reason_value(db_path.error), detail=db_path.detail)
        if entry.credential_ref is not None:
            # 値は使わないが enforcement は必ず通す（Phase A 契約の維持）
            credential = load_credential(wiki_root=wiki_root, ref=entry.credential_ref)
            if is_err(credential):
                return Err(
                    error=_reason_value(credential.error), detail=credential.detail
                )
        result = self._factory(
            db_path=db_path.value,
            allowed_tables=entry.allowed_tables,
            max_cell_bytes=entry.limits.max_cell_bytes,
            deadline_monotonic=deadline_monotonic,
            monotonic=monotonic,
        )
        if is_err(result):
            return Err(error=_reason_value(result.error), detail=result.detail)
        return Ok(value=result.value)


class _RemoteDbProviderBase(_SqlPrecheckMixin):
    """pg / mysql provider の共通部（credential + CA 解決 → opener 委譲）。"""

    def __init__(self, driver=None) -> None:
        self._driver = driver

    def _open_remote(
        self,
        opener,
        *,
        entry: ToolEntry,
        wiki_root: Path,
        deadline_monotonic: float,
        monotonic: Callable[[], float],
    ):
        config = entry.connection
        credential = load_credential(wiki_root=wiki_root, ref=entry.credential_ref)
        if is_err(credential):
            return Err(
                error=_reason_value(credential.error), detail=credential.detail
            )
        ca = _resolve_tls_ca(wiki_root=wiki_root, declared=config.tls_ca_file)
        if is_err(ca):
            return ca
        result = opener(
            config=config,
            password=credential.value,
            tls_ca_path=ca.value,
            deadline_monotonic=deadline_monotonic,
            monotonic=monotonic,
            **({"driver": self._driver} if self._driver is not None else {}),
        )
        if is_err(result):
            return Err(error=_reason_value(result.error), detail=result.detail)
        return Ok(value=result.value)


class PostgresConnectorProvider(_RemoteDbProviderBase):
    type_name = "postgres"

    def gate_policy(self, entry: ToolEntry) -> GatePolicy:
        config = entry.connection
        assert isinstance(config, PostgresConnectionConfig)
        return GatePolicy(
            required=True,
            dialect="postgres",
            default_namespace=config.default_schema,
        )

    def open(self, **kwargs):
        return self._open_remote(open_postgres_connector, **kwargs)


class MySqlConnectorProvider(_RemoteDbProviderBase):
    type_name = "mysql"

    def gate_policy(self, entry: ToolEntry) -> GatePolicy:
        config = entry.connection
        assert isinstance(config, MySqlConnectionConfig)
        return GatePolicy(
            required=True, dialect="mysql", default_namespace=config.dbname
        )

    def open(self, **kwargs):
        return self._open_remote(open_mysql_connector, **kwargs)


class HttpConnectorProvider:
    type_name = "http"

    def __init__(self, transport=None) -> None:
        self._transport = transport

    def gate_policy(self, entry: ToolEntry) -> GatePolicy:
        # SQL を持たない — テキスト検査は precheck（request spec 検証）が担う
        return GatePolicy(required=False)

    def precheck(
        self, entry: ToolEntry, text: str, *, label: str | None = None
    ) -> Ok[None] | Err:
        """request spec の schema 検証 + endpoint allowlist の dry-run 検査
        （送信しない）。"""

        spec_result = parse_request_spec(text)
        if is_err(spec_result):
            return Err(
                error=_reason_value(spec_result.error),
                detail=_prefixed(spec_result.detail, label),
            )
        url_result = build_request_url(entry.connection, spec_result.value)
        if is_err(url_result):
            return Err(
                error=_reason_value(url_result.error),
                detail=_prefixed(url_result.detail, label),
            )
        return Ok(value=None)

    def open(
        self,
        *,
        entry: ToolEntry,
        wiki_root: Path,
        deadline_monotonic: float,
        monotonic: Callable[[], float],
    ):
        credential = load_credential(wiki_root=wiki_root, ref=entry.credential_ref)
        if is_err(credential):
            return Err(
                error=_reason_value(credential.error), detail=credential.detail
            )
        return Ok(
            value=HttpConnector(
                config=entry.connection,
                credential=credential.value,
                max_response_bytes=entry.limits.max_response_bytes,
                deadline_monotonic=deadline_monotonic,
                monotonic=monotonic,
                transport=self._transport
                if self._transport is not None
                else UrllibTransport(),
            )
        )


class ConnectorRegistry:
    def __init__(self, providers: tuple[ConnectorProvider, ...]) -> None:
        self._by_type = {p.type_name: p for p in providers}

    def resolve(self, type_name: str) -> Ok[ConnectorProvider] | Err[RegistryError]:
        provider = self._by_type.get(type_name)
        if provider is None:
            return Err(
                error=RegistryError.UNKNOWN_TYPE,
                detail=f"connector type が registry にありません: {type_name!r}",
            )
        return Ok(value=provider)


def default_registry(
    *,
    sqlite_factory: Callable[..., object] | None = None,
    pg_driver=None,
    mysql_driver=None,
    http_transport=None,
) -> ConnectorRegistry:
    return ConnectorRegistry(
        providers=(
            SqliteConnectorProvider(factory=sqlite_factory or open_sqlite_connector),
            PostgresConnectorProvider(driver=pg_driver),
            MySqlConnectorProvider(driver=mysql_driver),
            HttpConnectorProvider(transport=http_transport),
        )
    )
