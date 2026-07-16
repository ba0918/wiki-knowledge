"""Structural tests for the browser-extract schema-of-record JSON files.

catalog schema と params meta-schema は browser tool 宣言の真実源。ここでは
draft-07 として妥当か・閉集合の語彙（tier / auth profile / resource type /
検証 check）が enum として固定されているか・params meta-schema が各パラメータに
enum/pattern/maxLength のいずれかを強制しているかを機械検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[3] / ".wiki" / "schema"
CATALOG_SCHEMA = SCHEMA_DIR / "browser-extract-catalog-schema.json"
PARAMS_SCHEMA = SCHEMA_DIR / "browser-extract-params-schema.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestCatalogSchema:
    def test_is_valid_draft07_document(self) -> None:
        schema = load(CATALOG_SCHEMA)
        assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["schema_version", "tools"]

    def test_tool_entry_requires_browser_contract_fields(self) -> None:
        schema = load(CATALOG_SCHEMA)
        tool = schema["definitions"]["tool_browser"]
        required = set(tool["required"])
        # flow ref + pin / auth / origin allowlist / tier + 保証マトリクス /
        # 検証契約 / limits / 保持ポリシー / delivery を必須にする
        assert {
            "tool_id",
            "type",
            "flow",
            "auth",
            "origin_allowlist",
            "tier",
            "guarantees",
            "checks",
            "params_schema",
            "limits",
            "retention",
            "delivery",
            "account",
        } <= required
        assert tool["additionalProperties"] is False

    def test_flow_declares_ref_and_sha256_pin(self) -> None:
        schema = load(CATALOG_SCHEMA)
        flow = schema["definitions"]["tool_browser"]["properties"]["flow"]
        assert set(flow["required"]) == {"ref", "sha256"}
        assert flow["properties"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"

    def test_tier_is_closed_set(self) -> None:
        schema = load(CATALOG_SCHEMA)
        tier = schema["definitions"]["tool_browser"]["properties"]["tier"]
        assert set(tier["enum"]) == {"B1", "B2", "B3"}

    def test_auth_profile_is_closed_set(self) -> None:
        schema = load(CATALOG_SCHEMA)
        auth = schema["definitions"]["tool_browser"]["properties"]["auth"]
        profile = auth["properties"]["profile"]
        assert set(profile["enum"]) == {"none", "form", "form+totp", "human-assisted"}

    def test_origin_allowlist_has_method_path_resource_granularity(self) -> None:
        schema = load(CATALOG_SCHEMA)
        item = schema["definitions"]["tool_browser"]["properties"][
            "origin_allowlist"
        ]["items"]
        assert set(item["required"]) == {"method", "path_prefix", "resource_type"}

    def test_guarantee_matrix_is_machine_readable(self) -> None:
        schema = load(CATALOG_SCHEMA)
        g = schema["definitions"]["tool_browser"]["properties"]["guarantees"]
        assert {
            "integrity",
            "identity",
            "filter_correctness",
            "completeness",
            "human_verification",
        } <= set(g["required"])

    def test_check_vocabulary_is_closed(self) -> None:
        schema = load(CATALOG_SCHEMA)
        check = schema["definitions"]["check"]["properties"]["check"]
        assert set(check["enum"]) == {
            "filter_readback",
            "row_count_range",
            "selector_exists",
            "export_metadata_match",
            "ui_total_vs_file_rows",
            "tenant_id_match",
            "primary_key_unique",
            "artifact_hash",
            "screen_fingerprint",
        }

    def test_limits_include_browser_specific_bounds(self) -> None:
        schema = load(CATALOG_SCHEMA)
        limits = schema["definitions"]["tool_browser"]["properties"]["limits"]
        assert {
            "max_rows",
            "max_result_bytes",
            "max_cell_bytes",
            "max_artifact_bytes",
            "max_flow_seconds",
            "max_unapproved_bundles",
        } <= set(limits["required"])


class TestParamsMetaSchema:
    def test_is_valid_draft07_document(self) -> None:
        schema = load(PARAMS_SCHEMA)
        assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"

    def test_each_param_requires_a_constraint(self) -> None:
        """params_schema の各パラメータは enum / pattern / maxLength のいずれか必須。"""
        schema = load(PARAMS_SCHEMA)
        param_def = schema["properties"]["properties"]["additionalProperties"]
        required_one_of = {
            frozenset(variant["required"]) for variant in param_def["anyOf"]
        }
        assert frozenset({"enum"}) in required_one_of
        assert frozenset({"pattern"}) in required_one_of
        assert frozenset({"maxLength"}) in required_one_of

    def test_params_object_forbids_free_additional_properties(self) -> None:
        schema = load(PARAMS_SCHEMA)
        # tool 側 params_schema は additionalProperties:false を宣言させる
        assert schema["properties"]["additionalProperties"]["const"] is False
