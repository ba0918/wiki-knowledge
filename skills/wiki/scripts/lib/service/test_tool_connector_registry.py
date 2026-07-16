"""Tests for tool_connector_registry.py — type → ConnectorProvider の解決.

runner に type 分岐を増やさないための registry 層。provider が所有するもの:

* type 別の connector 構築（driver DI を含む）
* credential 解決（pg / mysql は password 必須、sqlite は enforcement 素通し）
* tls_ca_file の封じ込め解決（wiki_root 相対 / 絶対、symlink 全拒否）
* SQL gate の適用要否の宣言（sqlite は authorizer 持ちなので不要と答える）
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.tool_catalog import load_catalog
from lib.service.tool_connector_mysql import FakeMySqlDriver
from lib.service.tool_connector_pg import FakePgDriver
from lib.service.tool_connector_registry import (
    RegistryError,
    default_registry,
)


def make_wiki(tmp_path: Path, *, pg_conn_extra: dict | None = None) -> Path:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "tools").mkdir(parents=True)
    (wiki_root / "data").mkdir()
    (wiki_root / ".local").mkdir()

    db = wiki_root / "data" / "events.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO users VALUES (1)")
    conn.commit()
    conn.close()

    creds = wiki_root / ".local" / "credentials.json"
    creds.write_text(
        json.dumps({"pg-ro": "pg-hunter2", "mysql-ro": "my-hunter2"}),
        encoding="utf-8",
    )
    creds.chmod(0o600)

    limits = {
        "max_rows": 1000,
        "max_result_bytes": 1048576,
        "max_cell_bytes": 4096,
        "timeout_sec": 30,
    }
    catalog = {
        "schema_version": 1,
        "tools": [
            {
                "tool_id": "events-db",
                "type": "sqlite",
                "connection": {"path": "data/events.sqlite3"},
                "allowed_tables": ["users"],
                "limits": limits,
                "allowed_statements": ["select"],
                "delivery": {"allowed_dirs": ["deliveries"]},
            },
            {
                "tool_id": "pg-db",
                "type": "postgres",
                "connection": {
                    "host": "db.example.com",
                    "port": 5432,
                    "dbname": "appdb",
                    "user": "readonly",
                    "default_schema": "analytics",
                    **(pg_conn_extra or {}),
                },
                "credential_ref": "pg-ro",
                "allowed_tables": ["users"],
                "limits": limits,
                "allowed_statements": ["select"],
                "delivery": {"allowed_dirs": ["deliveries"]},
            },
            {
                "tool_id": "mysql-db",
                "type": "mysql",
                "connection": {
                    "host": "db.example.com",
                    "port": 3306,
                    "dbname": "appdb",
                    "user": "readonly",
                },
                "credential_ref": "mysql-ro",
                "allowed_tables": ["users"],
                "limits": limits,
                "allowed_statements": ["select"],
                "delivery": {"allowed_dirs": ["deliveries"]},
            },
        ],
    }
    (wiki_root / "tools" / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return wiki_root


def entry_of(wiki_root: Path, tool_id: str):
    catalog = load_catalog(wiki_root=wiki_root).value
    return next(e for e in catalog.entries if e.tool_id == tool_id)


def open_via(registry, entry, wiki_root):
    provider = registry.resolve(entry.type).value
    return provider.open(
        entry=entry,
        wiki_root=wiki_root,
        deadline_monotonic=30.0,
        monotonic=lambda: 0.0,
    )


class TestResolve:
    def test_known_types_resolve(self, tmp_path: Path) -> None:
        registry = default_registry()
        for type_name in ("sqlite", "postgres", "mysql"):
            assert is_ok(registry.resolve(type_name)), type_name

    def test_unknown_type_is_rejected(self) -> None:
        registry = default_registry()
        result = registry.resolve("oracle")
        assert is_err(result)
        assert result.error == RegistryError.UNKNOWN_TYPE


class TestGatePolicy:
    def test_sqlite_does_not_require_gate(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        registry = default_registry()
        entry = entry_of(wiki_root, "events-db")
        policy = registry.resolve("sqlite").value.gate_policy(entry)
        assert policy.required is False

    def test_postgres_requires_gate_with_default_schema(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        registry = default_registry()
        entry = entry_of(wiki_root, "pg-db")
        policy = registry.resolve("postgres").value.gate_policy(entry)
        assert policy.required is True
        assert policy.dialect == "postgres"
        assert policy.default_namespace == "analytics"

    def test_mysql_requires_gate_with_dbname_namespace(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        registry = default_registry()
        entry = entry_of(wiki_root, "mysql-db")
        policy = registry.resolve("mysql").value.gate_policy(entry)
        assert policy.required is True
        assert policy.dialect == "mysql"
        assert policy.default_namespace == "appdb"


class TestSqliteProvider:
    def test_open_returns_working_connector(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        registry = default_registry()
        entry = entry_of(wiki_root, "events-db")
        result = open_via(registry, entry, wiki_root)
        assert is_ok(result)
        connector = result.value
        try:
            with connector.execute_stream("SELECT id FROM users").value as stream:
                assert list(stream) == [(1,)]
        finally:
            connector.close()

    def test_injected_factory_is_used(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        calls = {"n": 0}

        def factory(**kwargs):
            calls["n"] += 1
            from lib.service.tool_connector import open_sqlite_connector

            return open_sqlite_connector(**kwargs)

        registry = default_registry(sqlite_factory=factory)
        entry = entry_of(wiki_root, "events-db")
        assert is_ok(open_via(registry, entry, wiki_root))
        assert calls["n"] == 1


class TestPostgresProvider:
    def test_credential_is_resolved_and_passed_to_driver(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        driver = FakePgDriver()
        registry = default_registry(pg_driver=driver)
        entry = entry_of(wiki_root, "pg-db")
        result = open_via(registry, entry, wiki_root)
        assert is_ok(result), getattr(result, "detail", None)
        assert driver.connect_kwargs["password"] == "pg-hunter2"
        assert driver.connect_kwargs["user"] == "readonly"

    def test_missing_credential_fails_before_driver(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        (wiki_root / ".local" / "credentials.json").unlink()
        driver = FakePgDriver()
        registry = default_registry(pg_driver=driver)
        entry = entry_of(wiki_root, "pg-db")
        result = open_via(registry, entry, wiki_root)
        assert is_err(result)
        assert driver.connect_kwargs is None

    def test_relative_tls_ca_file_is_resolved_under_wiki_root(
        self, tmp_path: Path
    ) -> None:
        wiki_root = make_wiki(tmp_path, pg_conn_extra={"tls_ca_file": "ca.pem"})
        (wiki_root / "ca.pem").write_text("cert", encoding="utf-8")
        driver = FakePgDriver()
        registry = default_registry(pg_driver=driver)
        entry = entry_of(wiki_root, "pg-db")
        assert is_ok(open_via(registry, entry, wiki_root))
        assert driver.connect_kwargs["sslrootcert"] == str(
            wiki_root.resolve() / "ca.pem"
        )

    def test_symlinked_tls_ca_file_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, pg_conn_extra={"tls_ca_file": "ca.pem"})
        real = tmp_path / "real-ca.pem"
        real.write_text("cert", encoding="utf-8")
        (wiki_root / "ca.pem").symlink_to(real)
        driver = FakePgDriver()
        registry = default_registry(pg_driver=driver)
        entry = entry_of(wiki_root, "pg-db")
        result = open_via(registry, entry, wiki_root)
        assert is_err(result)
        assert driver.connect_kwargs is None

    def test_missing_tls_ca_file_fails_fast(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path, pg_conn_extra={"tls_ca_file": "ca.pem"})
        driver = FakePgDriver()
        registry = default_registry(pg_driver=driver)
        entry = entry_of(wiki_root, "pg-db")
        result = open_via(registry, entry, wiki_root)
        assert is_err(result)
        assert driver.connect_kwargs is None


class TestMySqlProvider:
    def test_credential_is_resolved_and_passed_to_driver(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        driver = FakeMySqlDriver()
        registry = default_registry(mysql_driver=driver)
        entry = entry_of(wiki_root, "mysql-db")
        result = open_via(registry, entry, wiki_root)
        assert is_ok(result), getattr(result, "detail", None)
        assert driver.connect_kwargs["password"] == "my-hunter2"


# ---------------------------------------------------------------------------
# provider.precheck（テキスト検査の所有を provider に移す）
# ---------------------------------------------------------------------------


class TestProviderPrecheck:
    def test_sqlite_precheck_accepts_select_and_rejects_others(
        self, tmp_path: Path
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        registry = default_registry()
        entry = entry_of(wiki_root, "events-db")
        provider = registry.resolve("sqlite").value
        assert is_ok(provider.precheck(entry, "SELECT * FROM users"))
        result = provider.precheck(entry, "DELETE FROM users")
        assert is_err(result)

    def test_pg_precheck_applies_sql_gate(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        registry = default_registry()
        entry = entry_of(wiki_root, "pg-db")
        provider = registry.resolve("postgres").value
        assert is_ok(provider.precheck(entry, "SELECT * FROM users"))
        result = provider.precheck(entry, "SELECT * FROM secrets")
        assert is_err(result)
        assert result.error == "sql_gate_relation_not_allowed"

    def test_precheck_label_prefixes_detail(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        registry = default_registry()
        entry = entry_of(wiki_root, "pg-db")
        provider = registry.resolve("postgres").value
        result = provider.precheck(entry, "SELECT * FROM secrets", label="全件")
        assert is_err(result)
        assert "全件" in result.detail


# ---------------------------------------------------------------------------
# http provider
# ---------------------------------------------------------------------------


def add_http_entry(wiki_root: Path) -> None:
    catalog_path = wiki_root / "tools" / "catalog.json"
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    data["tools"].append(
        {
            "tool_id": "redash-api",
            "type": "http",
            "connection": {
                "base_url": "https://redash.example.com",
                "allowed_endpoints": [
                    {"method": "GET", "path_prefix": "/api/data"}
                ],
                "auth_header_name": "Authorization",
                "auth_header_template": "Key {credential}",
            },
            "credential_ref": "redash-key",
            "limits": {
                "max_rows": 1000,
                "max_result_bytes": 1048576,
                "max_cell_bytes": 4096,
                "timeout_sec": 30,
                "max_response_bytes": 8388608,
            },
            "delivery": {"allowed_dirs": ["deliveries"]},
        }
    )
    catalog_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    creds_path = wiki_root / ".local" / "credentials.json"
    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    creds["redash-key"] = "redash-hunter2"
    creds_path.write_text(json.dumps(creds), encoding="utf-8")
    creds_path.chmod(0o600)


HTTP_SPEC = (
    '{"method": "GET", "path": "/api/data", '
    '"records_path": "rows", "columns": ["a"]}'
)


class TestHttpProvider:
    def test_http_resolves(self) -> None:
        registry = default_registry()
        assert is_ok(registry.resolve("http"))

    def test_gate_policy_is_not_required(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        add_http_entry(wiki_root)
        registry = default_registry()
        entry = entry_of(wiki_root, "redash-api")
        policy = registry.resolve("http").value.gate_policy(entry)
        assert policy.required is False

    def test_precheck_validates_spec_and_endpoint(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        add_http_entry(wiki_root)
        registry = default_registry()
        entry = entry_of(wiki_root, "redash-api")
        provider = registry.resolve("http").value
        assert is_ok(provider.precheck(entry, HTTP_SPEC))
        # SQL テキストは spec として不正 → 拒否
        assert is_err(provider.precheck(entry, "SELECT 1"))
        # allowlist 外 endpoint は precheck（送信前 dry-run）で拒否
        bad = HTTP_SPEC.replace("/api/data", "/admin/keys")
        result = provider.precheck(entry, bad)
        assert is_err(result)
        assert result.error == "http_endpoint_not_allowed"

    def test_open_resolves_credential_and_limits(self, tmp_path: Path) -> None:
        from lib.service.tool_connector_http import FakeTransport

        wiki_root = make_wiki(tmp_path)
        add_http_entry(wiki_root)
        transport = FakeTransport()
        registry = default_registry(http_transport=transport)
        entry = entry_of(wiki_root, "redash-api")
        result = registry.resolve("http").value.open(
            entry=entry,
            wiki_root=wiki_root,
            deadline_monotonic=30.0,
            monotonic=lambda: 0.0,
        )
        assert is_ok(result), getattr(result, "detail", None)

    def test_open_without_credential_fails(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        add_http_entry(wiki_root)
        (wiki_root / ".local" / "credentials.json").unlink()
        registry = default_registry()
        entry = entry_of(wiki_root, "redash-api")
        result = registry.resolve("http").value.open(
            entry=entry,
            wiki_root=wiki_root,
            deadline_monotonic=30.0,
            monotonic=lambda: 0.0,
        )
        assert is_err(result)
