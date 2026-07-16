"""Tests for tool_catalog.py — tool catalog のロード・検証・entry 解決.

catalog は実行契約の真実源（git 管理 JSON）。schema-of-record は
``.wiki/schema/tool-catalog-schema.json`` で、hand-rolled validator の
全制約（required / enum / type / additionalProperties / bounds）との
同期をこのテストが機械検証する。
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
    CATALOG_REQUIRED_TOP,
    CATALOG_SCHEMA_VERSION,
    CONNECTOR_TYPES,
    ENTRY_REQUIRED,
    LIMIT_BOUNDS,
    LIMITS_REQUIRED,
    TABLE_PATTERN,
    CatalogError,
    load_catalog,
    resolve_db_path,
    resolve_entry,
    validate_catalog,
)
from lib.service.tool_paths import ToolPathError


def make_entry(**overrides) -> dict:
    entry = {
        "tool_id": "events-db",
        "type": "sqlite",
        "connection": {"path": "data/events.sqlite3"},
        "allowed_tables": ["users", "registrations", "refunds"],
        "limits": {
            "max_rows": 10000,
            "max_result_bytes": 10485760,
            "max_cell_bytes": 65536,
            "timeout_sec": 30,
        },
        "allowed_statements": ["select"],
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
        for field in ENTRY_REQUIRED:
            entry = make_entry()
            del entry[field]
            errors = validate_catalog(make_catalog_data([entry]))
            assert errors != [], f"{field} 欠損が検出されない"

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
        entry = make_entry(type="postgres")
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
        for field, (lo, hi) in LIMIT_BOUNDS.items():
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

    def test_top_level_required_matches(self, schema: dict) -> None:
        assert set(schema["required"]) == set(CATALOG_REQUIRED_TOP)
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_version"]["const"] == CATALOG_SCHEMA_VERSION

    def test_entry_required_matches(self, schema: dict) -> None:
        tool = schema["definitions"]["tool"]
        assert set(tool["required"]) == set(ENTRY_REQUIRED)
        assert tool["additionalProperties"] is False

    def test_entry_optional_fields_are_known(self, schema: dict) -> None:
        """schema の properties がコード側の required + optional の和と一致する。"""
        tool = schema["definitions"]["tool"]
        assert set(tool["properties"]) == set(ENTRY_REQUIRED) | {"credential_ref"}

    def test_type_enum_matches(self, schema: dict) -> None:
        tool = schema["definitions"]["tool"]
        assert tool["properties"]["type"]["enum"] == list(CONNECTOR_TYPES)

    def test_allowed_statements_const_matches(self, schema: dict) -> None:
        tool = schema["definitions"]["tool"]
        assert tool["properties"]["allowed_statements"]["const"] == list(
            ALLOWED_STATEMENTS
        )

    def test_limits_required_and_bounds_match(self, schema: dict) -> None:
        limits = schema["definitions"]["tool"]["properties"]["limits"]
        assert set(limits["required"]) == set(LIMITS_REQUIRED)
        assert limits["additionalProperties"] is False
        for field, (lo, hi) in LIMIT_BOUNDS.items():
            prop = limits["properties"][field]
            assert prop["type"] == "integer"
            assert prop["minimum"] == lo
            assert prop["maximum"] == hi

    def test_id_patterns_match_sanitize_id(self, schema: dict) -> None:
        tool = schema["definitions"]["tool"]
        assert tool["properties"]["tool_id"]["pattern"] == ID_PATTERN
        assert tool["properties"]["credential_ref"]["pattern"] == ID_PATTERN

    def test_table_pattern_matches(self, schema: dict) -> None:
        tool = schema["definitions"]["tool"]
        assert (
            tool["properties"]["allowed_tables"]["items"]["pattern"] == TABLE_PATTERN
        )

    def test_nested_objects_forbid_unknown_keys(self, schema: dict) -> None:
        tool = schema["definitions"]["tool"]
        for name in ("connection", "delivery"):
            assert tool["properties"][name]["additionalProperties"] is False

    def test_full_schema_structure_matches_validator_constants(
        self, schema: dict
    ) -> None:
        """記述キーを除く schema 全体を validator 定数から機械構築して照合する。

        required / properties 集合 / type / additionalProperties / minItems /
        minLength / pattern / enum / const / bounds のどれが schema 側から
        欠落・変更されても検出される（部分照合では抜けが残るため全体照合）。
        """
        id_schema = {"type": "string", "pattern": ID_PATTERN}
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
                    "items": {"$ref": "#/definitions/tool"},
                },
            },
            "definitions": {
                "tool": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(ENTRY_REQUIRED),
                    "properties": {
                        "tool_id": id_schema,
                        "type": {"enum": list(CONNECTOR_TYPES)},
                        "connection": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["path"],
                            "properties": {
                                "path": {"type": "string", "minLength": 1},
                                "base_dir": {"type": "string", "minLength": 1},
                            },
                        },
                        "credential_ref": id_schema,
                        "allowed_tables": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "pattern": TABLE_PATTERN},
                        },
                        "limits": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": list(LIMITS_REQUIRED),
                            "properties": {
                                field: {
                                    "type": "integer",
                                    "minimum": lo,
                                    "maximum": hi,
                                }
                                for field, (lo, hi) in LIMIT_BOUNDS.items()
                            },
                        },
                        "allowed_statements": {"const": list(ALLOWED_STATEMENTS)},
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
                    },
                },
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
