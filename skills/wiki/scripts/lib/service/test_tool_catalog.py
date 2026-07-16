"""Tests for tool_catalog.py — tool catalog のロード・検証・entry 解決.

catalog は実行契約の真実源（git 管理 JSON）。schema-of-record は
``.wiki/schema/tool-catalog-schema.json`` で、hand-rolled validator の
**構造制約**（required / enum / type / additionalProperties / minItems /
minLength / pattern / const / bounds）との同期をこのテストが機械検証する。
相関制約（allow_insecure_tls の localhost 限定・tls_ca_file 相互排他・http
の allow_insecure など cross-field 制約）は schema には持たず validator が
所有するため、TestValidateCatalog* の個別テストが enforcement を担保する。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.path_validator import ID_PATTERN
from lib.service.tool_catalog import (
    ALLOWED_STATEMENTS,
    BASE_URL_PATTERN,
    CATALOG_REQUIRED_TOP,
    CATALOG_SCHEMA_VERSION,
    CONNECTION_OPTIONAL_BY_TYPE,
    CONNECTION_REQUIRED_BY_TYPE,
    CONNECTOR_TYPES,
    DBNAME_PATTERN,
    ENTRY_OPTIONAL_BY_TYPE,
    ENTRY_REQUIRED_BY_TYPE,
    HEADER_NAME_PATTERN,
    HEADER_TEMPLATE_PATTERN,
    HOST_PATTERN,
    HTTP_METHODS,
    LIMIT_BOUNDS,
    LIMITS_REQUIRED_BY_TYPE,
    LOCALHOST_HOSTS,
    PATH_PREFIX_PATTERN,
    PG_SCHEMA_PATTERN,
    PORT_BOUNDS,
    QUALIFIED_TABLE_PATTERN,
    TABLE_PATTERN,
    USER_PATTERN,
    CatalogError,
    HttpConnectionConfig,
    HttpEndpointRule,
    MySqlConnectionConfig,
    PostgresConnectionConfig,
    SqliteConnectionConfig,
    load_catalog,
    resolve_db_path,
    resolve_entry,
    validate_catalog,
)
from lib.service.tool_catalog import CredentialError, load_credential
from lib.service.tool_paths import ToolPathError


BASE_LIMITS = {
    "max_rows": 10000,
    "max_result_bytes": 10485760,
    "max_cell_bytes": 65536,
    "timeout_sec": 30,
}


def make_entry(**overrides) -> dict:
    entry = {
        "tool_id": "events-db",
        "type": "sqlite",
        "connection": {"path": "data/events.sqlite3"},
        "allowed_tables": ["users", "registrations", "refunds"],
        "limits": dict(BASE_LIMITS),
        "allowed_statements": ["select"],
        "delivery": {"allowed_dirs": ["outputs/deliveries"]},
    }
    entry.update(overrides)
    return entry


def make_pg_entry(**overrides) -> dict:
    entry = {
        "tool_id": "pg-db",
        "type": "postgres",
        "connection": {
            "host": "db.example.com",
            "port": 5432,
            "dbname": "appdb",
            "user": "readonly",
        },
        "credential_ref": "pg-ro",
        "allowed_tables": ["users", "analytics.events"],
        "limits": dict(BASE_LIMITS),
        "allowed_statements": ["select"],
        "delivery": {"allowed_dirs": ["outputs/deliveries"]},
    }
    entry.update(overrides)
    return entry


def make_mysql_entry(**overrides) -> dict:
    entry = {
        "tool_id": "mysql-db",
        "type": "mysql",
        "connection": {
            "host": "db.example.com",
            "port": 3306,
            "dbname": "appdb",
            "user": "readonly",
        },
        "credential_ref": "mysql-ro",
        "allowed_tables": ["users", "otherdb.events"],
        "limits": dict(BASE_LIMITS),
        "allowed_statements": ["select"],
        "delivery": {"allowed_dirs": ["outputs/deliveries"]},
    }
    entry.update(overrides)
    return entry


def make_http_entry(**overrides) -> dict:
    entry = {
        "tool_id": "redash-api",
        "type": "http",
        "connection": {
            "base_url": "https://redash.example.com",
            "allowed_endpoints": [
                {"method": "POST", "path_prefix": "/api/queries"},
                {"method": "GET", "path_prefix": "/api/jobs"},
            ],
            "auth_header_name": "Authorization",
            "auth_header_template": "Key {credential}",
        },
        "credential_ref": "redash-key",
        "limits": {**BASE_LIMITS, "max_response_bytes": 8388608},
        "delivery": {"allowed_dirs": ["outputs/deliveries"]},
    }
    entry.update(overrides)
    return entry


def make_catalog_data(entries: list[dict] | None = None) -> dict:
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "tools": entries if entries is not None else [make_entry()],
    }


def write_catalog(wiki_root: Path, data: dict) -> Path:
    path = wiki_root / "tools" / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _strip_docs(obj: object) -> object:
    """schema から記述専用キーを除去して構造だけを比較可能にする。"""
    if isinstance(obj, dict):
        return {
            k: _strip_docs(v)
            for k, v in obj.items()
            if k not in ("description", "title", "$schema")
        }
    if isinstance(obj, list):
        return [_strip_docs(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# validate_catalog（純粋検証）
# ---------------------------------------------------------------------------


class TestValidateCatalog:
    def test_valid_catalog_has_no_errors(self) -> None:
        assert validate_catalog(make_catalog_data()) == []

    def test_non_object_is_rejected(self) -> None:
        assert validate_catalog([]) != []
        assert validate_catalog("x") != []

    def test_missing_top_level_field_is_rejected(self) -> None:
        data = make_catalog_data()
        del data["tools"]
        assert validate_catalog(data) != []

    def test_unknown_top_level_key_is_rejected(self) -> None:
        data = make_catalog_data()
        data["extra"] = 1
        assert validate_catalog(data) != []

    def test_wrong_schema_version_is_rejected(self) -> None:
        data = make_catalog_data()
        data["schema_version"] = 99
        assert validate_catalog(data) != []

    def test_non_integer_schema_version_is_rejected(self) -> None:
        """JSON の true/1.0 は Python では 1 と等価だが version 1 としては不正。"""
        for bad in (True, False, "1", None, 1.0):
            data = make_catalog_data()
            data["schema_version"] = bad
            assert validate_catalog(data) != [], repr(bad)

    def test_missing_entry_field_is_rejected(self) -> None:
        for field in ENTRY_REQUIRED_BY_TYPE["sqlite"]:
            if field == "type":
                continue  # type 欠損は下の専用テスト（dispatch 不能）で検証
            entry = make_entry()
            del entry[field]
            errors = validate_catalog(make_catalog_data([entry]))
            assert errors != [], f"{field} 欠損が検出されない"

    def test_missing_type_field_is_rejected(self) -> None:
        entry = make_entry()
        del entry["type"]
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_unknown_entry_key_is_rejected(self) -> None:
        entry = make_entry(mystery_flag=True)
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_unknown_connection_key_is_rejected(self) -> None:
        entry = make_entry(connection={"path": "a.db", "extra": 1})
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_unknown_limits_key_is_rejected(self) -> None:
        entry = make_entry()
        entry["limits"]["extra"] = 1
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_unknown_delivery_key_is_rejected(self) -> None:
        entry = make_entry(delivery={"allowed_dirs": ["x"], "extra": 1})
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_bad_tool_id_pattern_is_rejected(self) -> None:
        for bad in ("UPPER", "has space", "-leading", "trailing-", "a/../b", ""):
            entry = make_entry(tool_id=bad)
            assert validate_catalog(make_catalog_data([entry])) != [], bad

    def test_duplicate_tool_id_is_rejected(self) -> None:
        data = make_catalog_data([make_entry(), make_entry()])
        assert validate_catalog(data) != []

    def test_unknown_connector_type_is_rejected(self) -> None:
        entry = make_entry(type="oracle")
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_credential_ref_is_optional_for_sqlite(self) -> None:
        entry = make_entry()
        assert "credential_ref" not in entry
        assert validate_catalog(make_catalog_data([entry])) == []

    def test_credential_ref_format_is_validated(self) -> None:
        assert validate_catalog(
            make_catalog_data([make_entry(credential_ref="events-ro")])
        ) == []
        for bad in ("UPPER", "../x", "", "a b"):
            errors = validate_catalog(
                make_catalog_data([make_entry(credential_ref=bad)])
            )
            assert errors != [], bad

    def test_allowed_tables_must_be_nonempty(self) -> None:
        entry = make_entry(allowed_tables=[])
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_allowed_tables_must_be_identifiers(self) -> None:
        for bad in ("users; drop", "a b", "", 'x"y', 1):
            entry = make_entry(allowed_tables=[bad])
            assert validate_catalog(make_catalog_data([entry])) != [], bad

    def test_allowed_statements_must_be_exactly_select(self) -> None:
        for bad in ([], ["select", "insert"], ["insert"], "select"):
            entry = make_entry(allowed_statements=bad)
            assert validate_catalog(make_catalog_data([entry])) != [], bad

    def test_limit_bounds_are_enforced(self) -> None:
        for field in LIMITS_REQUIRED_BY_TYPE["sqlite"]:
            lo, hi = LIMIT_BOUNDS[field]
            for bad in (0, -1, hi + 1, "10", 1.5, True):
                entry = make_entry()
                entry["limits"][field] = bad
                errors = validate_catalog(make_catalog_data([entry]))
                assert errors != [], f"{field}={bad!r} が拒否されない"
            for good in (lo, hi):
                entry = make_entry()
                entry["limits"][field] = good
                assert validate_catalog(make_catalog_data([entry])) == [], (
                    f"{field}={good} 境界値が通らない"
                )

    def test_delivery_allowed_dirs_must_be_nonempty(self) -> None:
        entry = make_entry(delivery={"allowed_dirs": []})
        assert validate_catalog(make_catalog_data([entry])) != []
        entry = make_entry(delivery={"allowed_dirs": [""]})
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_connection_path_must_be_nonempty_string(self) -> None:
        for bad_conn in ({"path": ""}, {"path": 1}, {}):
            entry = make_entry(connection=bad_conn)
            assert validate_catalog(make_catalog_data([entry])) != [], bad_conn


# ---------------------------------------------------------------------------
# postgres entry の type 別検証
# ---------------------------------------------------------------------------


class TestValidateCatalogPostgres:
    def test_valid_postgres_entry_passes(self) -> None:
        assert validate_catalog(make_catalog_data([make_pg_entry()])) == []

    def test_credential_ref_is_required(self) -> None:
        entry = make_pg_entry()
        del entry["credential_ref"]
        assert validate_catalog(make_catalog_data([entry])) != []

    @pytest.mark.parametrize("bad", [None, "", 1, True, "UPPER", "a/b"])
    def test_credential_ref_null_or_nonstring_is_rejected(self, bad) -> None:
        """存在するが値が null / 非文字列の credential_ref を通さない
        （schema の "type": "string" との乖離を防ぐ）。"""
        entry = make_pg_entry(credential_ref=bad)
        assert validate_catalog(make_catalog_data([entry])) != [], repr(bad)

    @pytest.mark.parametrize("field", ["host", "port", "dbname", "user"])
    def test_connection_fields_are_required(self, field: str) -> None:
        entry = make_pg_entry()
        del entry["connection"][field]
        assert validate_catalog(make_catalog_data([entry])) != [], field

    def test_port_bounds_are_enforced(self) -> None:
        for bad in (0, -1, 65536, "5432", True, 1.5):
            entry = make_pg_entry()
            entry["connection"]["port"] = bad
            assert validate_catalog(make_catalog_data([entry])) != [], repr(bad)
        for good in PORT_BOUNDS:
            entry = make_pg_entry()
            entry["connection"]["port"] = good
            assert validate_catalog(make_catalog_data([entry])) == [], good

    def test_qualified_allowed_tables_are_accepted(self) -> None:
        entry = make_pg_entry(allowed_tables=["users", "analytics.events"])
        assert validate_catalog(make_catalog_data([entry])) == []

    def test_bad_allowed_tables_are_rejected(self) -> None:
        for bad in ("a.b.c", "a..b", ".users", "users.", "users; drop", ""):
            entry = make_pg_entry(allowed_tables=[bad])
            assert validate_catalog(make_catalog_data([entry])) != [], bad

    def test_sqlite_allowed_tables_stay_unqualified(self) -> None:
        entry = make_entry(allowed_tables=["main.users"])
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_default_schema_pattern_is_validated(self) -> None:
        entry = make_pg_entry()
        entry["connection"]["default_schema"] = "analytics"
        assert validate_catalog(make_catalog_data([entry])) == []
        for bad in ("", "1abc", "a.b", "a b"):
            entry = make_pg_entry()
            entry["connection"]["default_schema"] = bad
            assert validate_catalog(make_catalog_data([entry])) != [], bad

    def test_allow_insecure_tls_requires_localhost(self) -> None:
        for host in LOCALHOST_HOSTS:
            entry = make_pg_entry()
            entry["connection"]["host"] = host
            entry["connection"]["allow_insecure_tls"] = True
            assert validate_catalog(make_catalog_data([entry])) == [], host
        entry = make_pg_entry()
        entry["connection"]["allow_insecure_tls"] = True  # host は外部のまま
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_allow_insecure_tls_conflicts_with_tls_ca_file(self) -> None:
        entry = make_pg_entry()
        entry["connection"]["host"] = "localhost"
        entry["connection"]["allow_insecure_tls"] = True
        entry["connection"]["tls_ca_file"] = "ca.pem"
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_canary_relation_pattern_is_validated(self) -> None:
        entry = make_pg_entry()
        entry["connection"]["canary_relation"] = "ops.doctor_canary"
        assert validate_catalog(make_catalog_data([entry])) == []
        entry = make_pg_entry()
        entry["connection"]["canary_relation"] = "a.b.c"
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_unknown_connection_key_is_rejected(self) -> None:
        entry = make_pg_entry()
        entry["connection"]["sslmode"] = "disable"
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_host_pattern_rejects_dsn_metacharacters(self) -> None:
        for bad in ("", "host with space", "host'--", "a\nb", "host?x=1"):
            entry = make_pg_entry()
            entry["connection"]["host"] = bad
            assert validate_catalog(make_catalog_data([entry])) != [], repr(bad)

    def test_max_response_bytes_is_unknown_for_postgres(self) -> None:
        entry = make_pg_entry()
        entry["limits"]["max_response_bytes"] = 1024
        assert validate_catalog(make_catalog_data([entry])) != []


class TestValidateCatalogMysql:
    def test_valid_mysql_entry_passes(self) -> None:
        assert validate_catalog(make_catalog_data([make_mysql_entry()])) == []

    def test_credential_ref_is_required(self) -> None:
        entry = make_mysql_entry()
        del entry["credential_ref"]
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_default_schema_is_unknown_for_mysql(self) -> None:
        entry = make_mysql_entry()
        entry["connection"]["default_schema"] = "public"
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_allow_insecure_tls_requires_localhost(self) -> None:
        entry = make_mysql_entry()
        entry["connection"]["allow_insecure_tls"] = True
        assert validate_catalog(make_catalog_data([entry])) != []


# ---------------------------------------------------------------------------
# http entry の type 別検証
# ---------------------------------------------------------------------------


class TestValidateCatalogHttp:
    def test_valid_http_entry_passes(self) -> None:
        assert validate_catalog(make_catalog_data([make_http_entry()])) == []

    def test_credential_ref_is_required(self) -> None:
        entry = make_http_entry()
        del entry["credential_ref"]
        assert validate_catalog(make_catalog_data([entry])) != []

    @pytest.mark.parametrize("bad", [None, "", 1, True])
    def test_credential_ref_null_or_nonstring_is_rejected(self, bad) -> None:
        entry = make_http_entry(credential_ref=bad)
        assert validate_catalog(make_catalog_data([entry])) != [], repr(bad)

    def test_allowed_tables_is_forbidden(self) -> None:
        entry = make_http_entry(allowed_tables=["users"])
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_allowed_statements_is_forbidden(self) -> None:
        entry = make_http_entry(allowed_statements=["select"])
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_max_response_bytes_is_required(self) -> None:
        entry = make_http_entry()
        del entry["limits"]["max_response_bytes"]
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_plain_http_base_url_requires_explicit_optin_and_localhost(self) -> None:
        entry = make_http_entry()
        entry["connection"]["base_url"] = "http://redash.example.com"
        assert validate_catalog(make_catalog_data([entry])) != []

        entry = make_http_entry()
        entry["connection"]["base_url"] = "http://localhost:5000"
        entry["connection"]["allow_insecure"] = True
        assert validate_catalog(make_catalog_data([entry])) == []

        entry = make_http_entry()
        entry["connection"]["base_url"] = "http://internal.example.com"
        entry["connection"]["allow_insecure"] = True
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_allow_insecure_with_https_is_rejected(self) -> None:
        entry = make_http_entry()
        entry["connection"]["allow_insecure"] = True
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_base_url_must_be_origin_only(self) -> None:
        for bad in (
            "https://redash.example.com/api",
            "https://redash.example.com?x=1",
            "https://redash.example.com#f",
            "https://user:pass@redash.example.com",
            "https://",
            "ftp://redash.example.com",
            "redash.example.com",
        ):
            entry = make_http_entry()
            entry["connection"]["base_url"] = bad
            assert validate_catalog(make_catalog_data([entry])) != [], bad

    def test_base_url_with_port_and_trailing_slash_is_accepted(self) -> None:
        for good in ("https://redash.example.com:8443", "https://redash.example.com/"):
            entry = make_http_entry()
            entry["connection"]["base_url"] = good
            assert validate_catalog(make_catalog_data([entry])) == [], good

    def test_allowed_endpoints_must_be_nonempty(self) -> None:
        entry = make_http_entry()
        entry["connection"]["allowed_endpoints"] = []
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_endpoint_method_must_be_in_enum(self) -> None:
        for bad in ("DELETE", "PUT", "PATCH", "get", ""):
            entry = make_http_entry()
            entry["connection"]["allowed_endpoints"] = [
                {"method": bad, "path_prefix": "/api"}
            ]
            assert validate_catalog(make_catalog_data([entry])) != [], bad

    def test_endpoint_path_prefix_must_be_canonical(self) -> None:
        for bad in (
            "api/queries",
            "/api/../admin",
            "//api",
            "/api\\queries",
            "/api%2Fqueries",
            "/api?x=1",
            "/api#f",
            "/api queries",
            "",
        ):
            entry = make_http_entry()
            entry["connection"]["allowed_endpoints"] = [
                {"method": "GET", "path_prefix": bad}
            ]
            assert validate_catalog(make_catalog_data([entry])) != [], bad

    def test_endpoint_unknown_key_is_rejected(self) -> None:
        entry = make_http_entry()
        entry["connection"]["allowed_endpoints"] = [
            {"method": "GET", "path_prefix": "/api", "extra": 1}
        ]
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_auth_header_template_requires_placeholder(self) -> None:
        entry = make_http_entry()
        entry["connection"]["auth_header_template"] = "Key hardcoded"
        assert validate_catalog(make_catalog_data([entry])) != []

    def test_auth_header_name_is_token(self) -> None:
        for bad in ("", "X Header", "X:Header", "X\nHeader"):
            entry = make_http_entry()
            entry["connection"]["auth_header_name"] = bad
            assert validate_catalog(make_catalog_data([entry])) != [], repr(bad)

    def test_auth_header_template_rejects_control_chars(self) -> None:
        entry = make_http_entry()
        entry["connection"]["auth_header_template"] = "Key {credential}\r\nEvil: 1"
        assert validate_catalog(make_catalog_data([entry])) != []


# ---------------------------------------------------------------------------
# tagged config parse（load_catalog が type 別 config を確定させる）
# ---------------------------------------------------------------------------


class TestTaggedConfigParse:
    def _load_entries(self, tmp_path: Path, entries: list[dict]):
        write_catalog(tmp_path, make_catalog_data(entries))
        result = load_catalog(wiki_root=tmp_path)
        assert is_ok(result), getattr(result, "detail", None)
        return result.value.entries

    def test_sqlite_entry_has_sqlite_config(self, tmp_path: Path) -> None:
        (entry,) = self._load_entries(tmp_path, [make_entry()])
        assert isinstance(entry.connection, SqliteConnectionConfig)
        assert entry.connection.path == "data/events.sqlite3"
        assert entry.connection.base_dir is None
        # 後方互換 property（sqlite のみ）
        assert entry.connection_path == "data/events.sqlite3"
        assert entry.connection_base_dir is None

    def test_postgres_entry_has_pg_config_with_defaults(self, tmp_path: Path) -> None:
        (entry,) = self._load_entries(tmp_path, [make_pg_entry()])
        conn = entry.connection
        assert isinstance(conn, PostgresConnectionConfig)
        assert (conn.host, conn.port, conn.dbname, conn.user) == (
            "db.example.com",
            5432,
            "appdb",
            "readonly",
        )
        assert conn.default_schema == "public"
        assert conn.tls_ca_file is None
        assert conn.allow_insecure_tls is False
        assert conn.canary_relation is None
        assert entry.credential_ref == "pg-ro"

    def test_mysql_entry_has_mysql_config(self, tmp_path: Path) -> None:
        (entry,) = self._load_entries(tmp_path, [make_mysql_entry()])
        conn = entry.connection
        assert isinstance(conn, MySqlConnectionConfig)
        assert conn.port == 3306
        assert conn.allow_insecure_tls is False

    def test_http_entry_has_http_config(self, tmp_path: Path) -> None:
        (entry,) = self._load_entries(tmp_path, [make_http_entry()])
        conn = entry.connection
        assert isinstance(conn, HttpConnectionConfig)
        assert conn.base_url == "https://redash.example.com"
        assert conn.allowed_endpoints == (
            HttpEndpointRule(method="POST", path_prefix="/api/queries"),
            HttpEndpointRule(method="GET", path_prefix="/api/jobs"),
        )
        assert conn.auth_header_name == "Authorization"
        assert conn.auth_header_template == "Key {credential}"
        assert conn.allow_insecure is False
        assert entry.limits.max_response_bytes == 8388608
        assert entry.allowed_tables == ()
        assert entry.allowed_statements == ()

    def test_sql_entries_have_no_max_response_bytes(self, tmp_path: Path) -> None:
        (entry,) = self._load_entries(tmp_path, [make_entry()])
        assert entry.limits.max_response_bytes is None

    def test_non_sqlite_config_rejects_legacy_path_properties(
        self, tmp_path: Path
    ) -> None:
        (entry,) = self._load_entries(tmp_path, [make_pg_entry()])
        with pytest.raises(TypeError):
            entry.connection_path
        with pytest.raises(TypeError):
            entry.connection_base_dir

    def test_mixed_catalog_loads_all_types(self, tmp_path: Path) -> None:
        entries = self._load_entries(
            tmp_path,
            [make_entry(), make_pg_entry(), make_mysql_entry(), make_http_entry()],
        )
        assert [e.type for e in entries] == ["sqlite", "postgres", "mysql", "http"]


# ---------------------------------------------------------------------------
# schema-of-record との同期（required / enum / type / additionalProperties）
# ---------------------------------------------------------------------------


class TestSchemaSync:
    @pytest.fixture()
    def schema(self) -> dict:
        schema_path = (
            Path(__file__).resolve().parents[5]
            / ".wiki"
            / "schema"
            / "tool-catalog-schema.json"
        )
        return json.loads(schema_path.read_text(encoding="utf-8"))

    # -- 期待 schema を validator 定数から機械構築する ----------------------

    def _connection_schema(self, ctype: str) -> dict:
        all_props: dict[str, dict] = {
            "sqlite": {
                "path": {"type": "string", "minLength": 1},
                "base_dir": {"type": "string", "minLength": 1},
            },
            "postgres": {
                "host": {"type": "string", "pattern": HOST_PATTERN},
                "port": {
                    "type": "integer",
                    "minimum": PORT_BOUNDS[0],
                    "maximum": PORT_BOUNDS[1],
                },
                "dbname": {"type": "string", "pattern": DBNAME_PATTERN},
                "user": {"type": "string", "pattern": USER_PATTERN},
                "default_schema": {"type": "string", "pattern": PG_SCHEMA_PATTERN},
                "tls_ca_file": {"type": "string", "minLength": 1},
                "allow_insecure_tls": {"type": "boolean"},
                "canary_relation": {
                    "type": "string",
                    "pattern": QUALIFIED_TABLE_PATTERN,
                },
            },
            "mysql": {
                "host": {"type": "string", "pattern": HOST_PATTERN},
                "port": {
                    "type": "integer",
                    "minimum": PORT_BOUNDS[0],
                    "maximum": PORT_BOUNDS[1],
                },
                "dbname": {"type": "string", "pattern": DBNAME_PATTERN},
                "user": {"type": "string", "pattern": USER_PATTERN},
                "tls_ca_file": {"type": "string", "minLength": 1},
                "allow_insecure_tls": {"type": "boolean"},
                "canary_relation": {
                    "type": "string",
                    "pattern": QUALIFIED_TABLE_PATTERN,
                },
            },
            "http": {
                "base_url": {"type": "string", "pattern": BASE_URL_PATTERN},
                "allowed_endpoints": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["method", "path_prefix"],
                        "properties": {
                            "method": {"enum": list(HTTP_METHODS)},
                            "path_prefix": {
                                "type": "string",
                                "pattern": PATH_PREFIX_PATTERN,
                            },
                        },
                    },
                },
                "auth_header_name": {
                    "type": "string",
                    "pattern": HEADER_NAME_PATTERN,
                },
                "auth_header_template": {
                    "type": "string",
                    "pattern": HEADER_TEMPLATE_PATTERN,
                },
                "allow_insecure": {"type": "boolean"},
            },
        }
        fields = (
            CONNECTION_REQUIRED_BY_TYPE[ctype] + CONNECTION_OPTIONAL_BY_TYPE[ctype]
        )
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(CONNECTION_REQUIRED_BY_TYPE[ctype]),
            "properties": {f: all_props[ctype][f] for f in fields},
        }

    def _tool_schema(self, ctype: str) -> dict:
        id_schema = {"type": "string", "pattern": ID_PATTERN}
        known = set(ENTRY_REQUIRED_BY_TYPE[ctype]) | set(ENTRY_OPTIONAL_BY_TYPE[ctype])
        props: dict = {
            "tool_id": id_schema,
            "type": {"const": ctype},
            "connection": self._connection_schema(ctype),
            "limits": {
                "type": "object",
                "additionalProperties": False,
                "required": list(LIMITS_REQUIRED_BY_TYPE[ctype]),
                "properties": {
                    f: {
                        "type": "integer",
                        "minimum": LIMIT_BOUNDS[f][0],
                        "maximum": LIMIT_BOUNDS[f][1],
                    }
                    for f in LIMITS_REQUIRED_BY_TYPE[ctype]
                },
            },
            "delivery": {
                "type": "object",
                "additionalProperties": False,
                "required": ["allowed_dirs"],
                "properties": {
                    "allowed_dirs": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        }
        if "credential_ref" in known:
            props["credential_ref"] = id_schema
        if "allowed_tables" in known:
            table_pattern = (
                TABLE_PATTERN if ctype == "sqlite" else QUALIFIED_TABLE_PATTERN
            )
            props["allowed_tables"] = {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "pattern": table_pattern},
            }
        if "allowed_statements" in known:
            props["allowed_statements"] = {"const": list(ALLOWED_STATEMENTS)}
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(ENTRY_REQUIRED_BY_TYPE[ctype]),
            "properties": props,
        }

    # -- 照合 ---------------------------------------------------------------

    def test_top_level_required_matches(self, schema: dict) -> None:
        assert set(schema["required"]) == set(CATALOG_REQUIRED_TOP)
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_version"]["const"] == CATALOG_SCHEMA_VERSION

    def test_definitions_cover_all_connector_types(self, schema: dict) -> None:
        assert set(schema["definitions"]) == {f"tool_{t}" for t in CONNECTOR_TYPES}
        refs = [
            ref["$ref"] for ref in schema["properties"]["tools"]["items"]["oneOf"]
        ]
        assert refs == [f"#/definitions/tool_{t}" for t in CONNECTOR_TYPES]

    @pytest.mark.parametrize("ctype", CONNECTOR_TYPES)
    def test_entry_required_matches_by_type(self, schema: dict, ctype: str) -> None:
        tool = schema["definitions"][f"tool_{ctype}"]
        assert set(tool["required"]) == set(ENTRY_REQUIRED_BY_TYPE[ctype])
        assert tool["additionalProperties"] is False
        known = set(ENTRY_REQUIRED_BY_TYPE[ctype]) | set(
            ENTRY_OPTIONAL_BY_TYPE[ctype]
        )
        assert set(tool["properties"]) == known

    @pytest.mark.parametrize("ctype", CONNECTOR_TYPES)
    def test_connection_fields_match_by_type(self, schema: dict, ctype: str) -> None:
        conn = schema["definitions"][f"tool_{ctype}"]["properties"]["connection"]
        assert set(conn["required"]) == set(CONNECTION_REQUIRED_BY_TYPE[ctype])
        known = set(CONNECTION_REQUIRED_BY_TYPE[ctype]) | set(
            CONNECTION_OPTIONAL_BY_TYPE[ctype]
        )
        assert set(conn["properties"]) == known
        assert conn["additionalProperties"] is False

    @pytest.mark.parametrize("ctype", CONNECTOR_TYPES)
    def test_limits_required_and_bounds_match_by_type(
        self, schema: dict, ctype: str
    ) -> None:
        limits = schema["definitions"][f"tool_{ctype}"]["properties"]["limits"]
        assert set(limits["required"]) == set(LIMITS_REQUIRED_BY_TYPE[ctype])
        assert limits["additionalProperties"] is False
        for field in LIMITS_REQUIRED_BY_TYPE[ctype]:
            lo, hi = LIMIT_BOUNDS[field]
            prop = limits["properties"][field]
            assert prop["type"] == "integer"
            assert prop["minimum"] == lo
            assert prop["maximum"] == hi

    def test_correlation_constraints_are_validator_owned_not_in_schema(
        self, schema: dict
    ) -> None:
        """相関制約（cross-field）は構造 schema に持たず validator が所有する
        という契約境界を機械検証する。

        allow_insecure_tls は schema 上「単なる boolean」であり、localhost
        限定や tls_ca_file 相互排他は表現されない — これは validator が真実源
        だから。この境界が崩れて schema 側に if/then 等が混入したら検出する。
        """
        for ctype in ("postgres", "mysql"):
            conn = schema["definitions"][f"tool_{ctype}"]["properties"]["connection"]
            assert _strip_docs(conn["properties"]["allow_insecure_tls"]) == {
                "type": "boolean"
            }
            assert "if" not in conn and "allOf" not in conn and "not" not in conn
        http_conn = schema["definitions"]["tool_http"]["properties"]["connection"]
        assert _strip_docs(http_conn["properties"]["allow_insecure"]) == {
            "type": "boolean"
        }
        assert "if" not in http_conn and "allOf" not in http_conn

    def test_full_schema_structure_matches_validator_constants(
        self, schema: dict
    ) -> None:
        """記述キーを除く schema 全体を validator 定数から機械構築して照合する。

        required / properties 集合 / type / additionalProperties / minItems /
        minLength / pattern / enum / const / bounds のどれが schema 側から
        欠落・変更されても検出される（部分照合では抜けが残るため全体照合）。
        """
        expected = {
            "type": "object",
            "additionalProperties": False,
            "required": list(CATALOG_REQUIRED_TOP),
            "properties": {
                "schema_version": {
                    "type": "integer",
                    "const": CATALOG_SCHEMA_VERSION,
                },
                "tools": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"$ref": f"#/definitions/tool_{t}"}
                            for t in CONNECTOR_TYPES
                        ]
                    },
                },
            },
            "definitions": {
                f"tool_{t}": self._tool_schema(t) for t in CONNECTOR_TYPES
            },
        }
        assert _strip_docs(schema) == expected


# ---------------------------------------------------------------------------
# load_catalog / resolve_entry
# ---------------------------------------------------------------------------


class TestLoadCatalog:
    def test_load_valid_catalog(self, tmp_path: Path) -> None:
        path = write_catalog(tmp_path, make_catalog_data())
        result = load_catalog(wiki_root=tmp_path)
        assert is_ok(result)
        catalog = result.value
        assert catalog.schema_version == CATALOG_SCHEMA_VERSION
        assert len(catalog.entries) == 1
        entry = catalog.entries[0]
        assert entry.tool_id == "events-db"
        assert entry.type == "sqlite"
        assert entry.connection_path == "data/events.sqlite3"
        assert entry.credential_ref is None
        assert entry.allowed_tables == ("users", "registrations", "refunds")
        assert entry.allowed_statements == ("select",)
        assert entry.delivery_allowed_dirs == ("outputs/deliveries",)
        assert entry.limits.max_rows == 10000
        expected_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert catalog.digest == expected_digest

    def test_missing_file_is_not_found(self, tmp_path: Path) -> None:
        result = load_catalog(wiki_root=tmp_path)
        assert is_err(result)
        assert result.error == CatalogError.NOT_FOUND

    def test_invalid_json_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "tools" / "catalog.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        result = load_catalog(wiki_root=tmp_path)
        assert is_err(result)
        assert result.error == CatalogError.INVALID_JSON

    def test_schema_violation_is_reported_with_detail(self, tmp_path: Path) -> None:
        data = make_catalog_data([make_entry(type="postgres")])
        write_catalog(tmp_path, data)
        result = load_catalog(wiki_root=tmp_path)
        assert is_err(result)
        assert result.error == CatalogError.SCHEMA_VIOLATION
        assert result.detail != ""

    def test_digest_changes_when_bytes_change(self, tmp_path: Path) -> None:
        write_catalog(tmp_path, make_catalog_data())
        first = load_catalog(wiki_root=tmp_path).value.digest
        data = make_catalog_data([make_entry(tool_id="other-db")])
        write_catalog(tmp_path, data)
        second = load_catalog(wiki_root=tmp_path).value.digest
        assert first != second


class TestResolveEntry:
    def test_known_tool_id_resolves(self, tmp_path: Path) -> None:
        write_catalog(tmp_path, make_catalog_data())
        catalog = load_catalog(wiki_root=tmp_path).value
        result = resolve_entry(catalog, "events-db")
        assert is_ok(result)
        assert result.value.tool_id == "events-db"

    def test_unknown_tool_id_is_rejected(self, tmp_path: Path) -> None:
        write_catalog(tmp_path, make_catalog_data())
        catalog = load_catalog(wiki_root=tmp_path).value
        result = resolve_entry(catalog, "no-such-tool")
        assert is_err(result)
        assert result.error == CatalogError.UNKNOWN_TOOL


# ---------------------------------------------------------------------------
# resolve_db_path（DB path の封じ込め）
# ---------------------------------------------------------------------------


class TestResolveDbPath:
    def _entry_for(self, tmp_path: Path, connection: dict):
        data = make_catalog_data([make_entry(connection=connection)])
        write_catalog(tmp_path, data)
        catalog = load_catalog(wiki_root=tmp_path).value
        return catalog.entries[0]

    def test_path_under_wiki_root_resolves(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "events.sqlite3").write_bytes(b"")
        entry = self._entry_for(tmp_path, {"path": "data/events.sqlite3"})
        result = resolve_db_path(entry=entry, wiki_root=tmp_path)
        assert is_ok(result)
        assert result.value == tmp_path.resolve() / "data" / "events.sqlite3"

    def test_traversal_outside_wiki_root_is_rejected(self, tmp_path: Path) -> None:
        entry = self._entry_for(tmp_path, {"path": "../outside.db"})
        result = resolve_db_path(entry=entry, wiki_root=tmp_path)
        assert is_err(result)

    def test_symlink_inside_base_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "real.db").write_bytes(b"")
        (tmp_path / "link.db").symlink_to(tmp_path / "real.db")
        entry = self._entry_for(tmp_path, {"path": "link.db"})
        result = resolve_db_path(entry=entry, wiki_root=tmp_path)
        assert is_err(result)
        assert result.error == ToolPathError.SYMLINK_COMPONENT

    def test_declared_base_dir_confines_path(self, tmp_path: Path) -> None:
        base = tmp_path / "dbs"
        base.mkdir()
        (base / "events.sqlite3").write_bytes(b"")
        entry = self._entry_for(
            tmp_path, {"path": "events.sqlite3", "base_dir": str(base)}
        )
        result = resolve_db_path(entry=entry, wiki_root=tmp_path)
        assert is_ok(result)
        assert result.value == base.resolve() / "events.sqlite3"

    def test_symlinked_absolute_base_dir_is_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "real_dbs"
        real.mkdir()
        (real / "events.sqlite3").write_bytes(b"")
        link = tmp_path / "dbs_link"
        link.symlink_to(real)
        entry = self._entry_for(
            tmp_path, {"path": "events.sqlite3", "base_dir": str(link)}
        )
        result = resolve_db_path(entry=entry, wiki_root=tmp_path)
        assert is_err(result)
        assert result.error == ToolPathError.SYMLINK_COMPONENT

    def test_escape_from_declared_base_dir_is_rejected(self, tmp_path: Path) -> None:
        base = tmp_path / "dbs"
        base.mkdir()
        entry = self._entry_for(
            tmp_path, {"path": "../secret.db", "base_dir": str(base)}
        )
        result = resolve_db_path(entry=entry, wiki_root=tmp_path)
        assert is_err(result)

    def test_relative_base_dir_is_resolved_under_wiki_root(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "dbs").mkdir()
        (tmp_path / "dbs" / "events.sqlite3").write_bytes(b"")
        entry = self._entry_for(
            tmp_path, {"path": "events.sqlite3", "base_dir": "dbs"}
        )
        result = resolve_db_path(entry=entry, wiki_root=tmp_path)
        assert is_ok(result)
        assert result.value == tmp_path.resolve() / "dbs" / "events.sqlite3"


# ---------------------------------------------------------------------------
# load_credential（credential_ref 解決の enforcement）
# ---------------------------------------------------------------------------


class TestLoadCredential:
    def _write_credentials(
        self, tmp_path: Path, content: str, *, mode: int = 0o600
    ) -> Path:
        local = tmp_path / ".local"
        local.mkdir(exist_ok=True)
        path = local / "credentials.json"
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        return path

    def test_resolves_value_by_ref(self, tmp_path: Path) -> None:
        self._write_credentials(tmp_path, '{"events-ro": "hunter2"}')
        result = load_credential(wiki_root=tmp_path, ref="events-ro")
        assert is_ok(result)
        assert result.value == "hunter2"

    def test_missing_file_is_not_found(self, tmp_path: Path) -> None:
        result = load_credential(wiki_root=tmp_path, ref="events-ro")
        assert is_err(result)
        assert result.error == CredentialError.NOT_FOUND

    def test_group_or_world_readable_is_rejected(self, tmp_path: Path) -> None:
        for mode in (0o644, 0o640, 0o606, 0o660):
            self._write_credentials(
                tmp_path, '{"events-ro": "hunter2"}', mode=mode
            )
            result = load_credential(wiki_root=tmp_path, ref="events-ro")
            assert is_err(result), oct(mode)
            assert result.error == CredentialError.BAD_PERMISSIONS

    def test_stricter_than_0600_is_accepted(self, tmp_path: Path) -> None:
        self._write_credentials(tmp_path, '{"events-ro": "hunter2"}', mode=0o400)
        assert is_ok(load_credential(wiki_root=tmp_path, ref="events-ro"))

    def test_symlink_is_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "real-creds.json"
        real.write_text('{"events-ro": "hunter2"}', encoding="utf-8")
        real.chmod(0o600)
        local = tmp_path / ".local"
        local.mkdir()
        (local / "credentials.json").symlink_to(real)
        result = load_credential(wiki_root=tmp_path, ref="events-ro")
        assert is_err(result)
        assert result.error == CredentialError.NOT_REGULAR_FILE

    def test_symlinked_parent_directory_is_rejected(self, tmp_path: Path) -> None:
        """`.local` 自体が symlink でも拒否（終端だけの lstat では抜けられる穴）。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        creds = outside / "credentials.json"
        creds.write_text('{"events-ro": "hunter2"}', encoding="utf-8")
        creds.chmod(0o600)
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / ".local").symlink_to(outside)
        result = load_credential(wiki_root=wiki, ref="events-ro")
        assert is_err(result)
        assert result.error == CredentialError.NOT_REGULAR_FILE

    def test_non_regular_file_is_rejected(self, tmp_path: Path) -> None:
        local = tmp_path / ".local"
        local.mkdir()
        (local / "credentials.json").mkdir()  # directory
        result = load_credential(wiki_root=tmp_path, ref="events-ro")
        assert is_err(result)
        assert result.error == CredentialError.NOT_REGULAR_FILE

    def test_malformed_structure_is_rejected(self, tmp_path: Path) -> None:
        for bad in ("not json", "[1,2]", '{"ref": 123}'):
            self._write_credentials(tmp_path, bad)
            result = load_credential(wiki_root=tmp_path, ref="ref")
            assert is_err(result), bad
            assert result.error == CredentialError.MALFORMED

    def test_unknown_ref_detail_names_ref_only(self, tmp_path: Path) -> None:
        """エラー detail には ref 名のみ — 秘密値は決して載せない。"""
        self._write_credentials(tmp_path, '{"events-ro": "hunter2"}')
        result = load_credential(wiki_root=tmp_path, ref="no-such-ref")
        assert is_err(result)
        assert result.error == CredentialError.UNKNOWN_REF
        assert "hunter2" not in result.detail
        assert "no-such-ref" in result.detail
