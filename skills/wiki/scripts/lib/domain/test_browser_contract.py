"""Tests for lib/domain/browser_contract.py — 検証語彙 v1 + 宣言的契約の
パース/検証/enforce（pure、Playwright 非依存）.

契約の中心は「正しいデータを取れたか」の機械検証。未知語彙は fail-closed、
B1 契約は独立 anchor を最低1つ含まないと catalog-validate で拒否される。
"""

from __future__ import annotations

import json
from pathlib import Path

from lib.domain.browser_contract import (
    CHECK_NAMES,
    INDEPENDENT_ANCHORS,
    BrowserCatalogError,
    Check,
    CheckEvidence,
    enforce_checks,
    evaluate_check,
    parse_browser_catalog,
    validate_browser_catalog,
    validate_params_schema,
)
from lib.domain.types import is_err, is_ok

SCHEMA_DIR = Path(__file__).resolve().parents[5] / ".wiki" / "schema"


def valid_entry(**overrides) -> dict:
    entry = {
        "tool_id": "events-web",
        "type": "browser",
        "flow": {"ref": "events_web.py", "sha256": "a" * 64},
        "auth": {
            "profile": "form",
            "credential_ref": "events-login",
            "username": "svc-readonly",
            "login": {
                "route": "login",
                "username_label": "Username",
                "password_label": "Password",
                "submit_role": "button",
                "submit_name": "Sign in",
                "success_url_contains": "/reports",
            },
        },
        "origin_allowlist": [
            {
                "method": "GET",
                "path_prefix": "/reports",
                "resource_type": "document",
            }
        ],
        "tier": "B1",
        "guarantees": {
            "integrity": "guaranteed",
            "identity": "guaranteed",
            "filter_correctness": "guaranteed",
            "completeness": "guaranteed",
            "human_verification": "required",
        },
        "checks": [
            {"check": "filter_readback", "param": "period"},
            {"check": "row_count_range", "min": 1, "max": 1000},
            {"check": "ui_total_vs_file_rows"},
        ],
        "params_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "period": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}$"}
            },
        },
        "limits": {
            "max_rows": 10000,
            "max_result_bytes": 10485760,
            "max_cell_bytes": 4096,
            "max_artifact_bytes": 10485760,
            "max_flow_seconds": 120,
            "max_unapproved_bundles": 5,
        },
        "retention": {"trace": "off", "screenshot": "off", "ttl_hours": 24},
        "delivery": {"allowed_dirs": ["deliveries"]},
        "account": {"id": "svc-readonly", "origin": "https://app.example.com"},
    }
    entry.update(overrides)
    return entry


def catalog(*entries: dict) -> dict:
    return {"schema_version": 1, "tools": list(entries)}


class TestCatalogValidation:
    def test_valid_b1_entry_passes(self) -> None:
        assert validate_browser_catalog(catalog(valid_entry())) == []

    def test_parse_returns_typed_entries(self) -> None:
        result = parse_browser_catalog(catalog(valid_entry()))
        assert is_ok(result), result
        entries = result.value
        assert entries[0].tool_id == "events-web"
        assert entries[0].tier == "B1"
        assert entries[0].flow.sha256 == "a" * 64
        assert entries[0].checks[0].check == "filter_readback"

    def test_unknown_check_name_is_rejected(self) -> None:
        entry = valid_entry()
        entry["checks"] = [{"check": "vibes_ok"}]
        errors = validate_browser_catalog(catalog(entry))
        assert any("vibes_ok" in e for e in errors)
        result = parse_browser_catalog(catalog(entry))
        assert is_err(result)
        assert result.error == BrowserCatalogError.UNKNOWN_CHECK

    def test_b1_without_independent_anchor_is_rejected(self) -> None:
        entry = valid_entry()
        # 独立 anchor（ui_total_vs_file_rows）を外し正しさ check のみ残す
        entry["checks"] = [
            {"check": "filter_readback", "param": "period"},
            {"check": "row_count_range", "min": 1, "max": 1000},
        ]
        result = parse_browser_catalog(catalog(entry))
        assert is_err(result)
        assert result.error == BrowserCatalogError.MISSING_ANCHOR

    def test_b2_without_independent_anchor_is_allowed(self) -> None:
        entry = valid_entry(tier="B2")
        entry["guarantees"]["completeness"] = "none"
        entry["guarantees"]["integrity"] = "partial"
        entry["checks"] = [{"check": "filter_readback", "param": "period"}]
        assert validate_browser_catalog(catalog(entry)) == []

    def test_missing_required_field_is_rejected(self) -> None:
        entry = valid_entry()
        del entry["flow"]
        errors = validate_browser_catalog(catalog(entry))
        assert any("flow" in e for e in errors)

    def test_flow_sha256_must_be_hex64(self) -> None:
        entry = valid_entry()
        entry["flow"]["sha256"] = "not-a-hash"
        errors = validate_browser_catalog(catalog(entry))
        assert any("sha256" in e for e in errors)

    def test_bad_tier_is_rejected(self) -> None:
        entry = valid_entry(tier="B9")
        errors = validate_browser_catalog(catalog(entry))
        assert any("tier" in e for e in errors)

    def test_bad_auth_profile_is_rejected(self) -> None:
        entry = valid_entry()
        entry["auth"]["profile"] = "magic-link"
        errors = validate_browser_catalog(catalog(entry))
        assert any("profile" in e for e in errors)

    def test_unknown_top_level_key_is_rejected(self) -> None:
        entry = valid_entry()
        entry["danger"] = True
        errors = validate_browser_catalog(catalog(entry))
        assert any("danger" in e for e in errors)

    def test_form_profile_requires_username(self) -> None:
        entry = valid_entry()
        del entry["auth"]["username"]
        errors = validate_browser_catalog(catalog(entry))
        assert any("username" in e for e in errors)

    def test_form_profile_requires_login_block(self) -> None:
        entry = valid_entry()
        del entry["auth"]["login"]
        errors = validate_browser_catalog(catalog(entry))
        assert any("login" in e for e in errors)

    def test_form_profile_requires_credential_ref(self) -> None:
        entry = valid_entry()
        del entry["auth"]["credential_ref"]
        errors = validate_browser_catalog(catalog(entry))
        assert any("credential_ref" in e for e in errors)

    def test_form_totp_requires_totp_credential_ref_and_label(self) -> None:
        entry = valid_entry()
        entry["auth"]["profile"] = "form+totp"
        # totp_credential_ref も login.totp_label も無い → 両方が指摘される
        errors = validate_browser_catalog(catalog(entry))
        assert any("totp_credential_ref" in e for e in errors)
        assert any("totp_label" in e for e in errors)

    def test_form_totp_with_totp_fields_passes(self) -> None:
        entry = valid_entry()
        entry["auth"]["profile"] = "form+totp"
        entry["auth"]["totp_credential_ref"] = "events-totp"
        entry["auth"]["login"]["totp_label"] = "One-time code"
        assert validate_browser_catalog(catalog(entry)) == []

    def test_none_profile_needs_no_login(self) -> None:
        entry = valid_entry()
        entry["auth"] = {"profile": "none"}
        assert validate_browser_catalog(catalog(entry)) == []

    def test_parse_exposes_login_config(self) -> None:
        result = parse_browser_catalog(catalog(valid_entry()))
        assert is_ok(result), result
        auth = result.value[0].auth
        assert auth.username == "svc-readonly"
        assert auth.login is not None
        assert auth.login.route == "login"
        assert auth.login.submit_name == "Sign in"
        assert auth.login.success_url_contains == "/reports"

    def test_duplicate_tool_id_is_rejected(self) -> None:
        errors = validate_browser_catalog(catalog(valid_entry(), valid_entry()))
        assert any("重複" in e or "duplicate" in e.lower() for e in errors)


class TestParamsSchemaValidation:
    def test_param_with_pattern_is_valid(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"period": {"type": "string", "pattern": "^x$"}},
        }
        assert validate_params_schema(schema) == []

    def test_param_without_any_constraint_is_rejected(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"free": {"type": "string"}},
        }
        errors = validate_params_schema(schema)
        assert any("free" in e for e in errors)

    def test_free_additional_properties_is_rejected(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": True,
            "properties": {},
        }
        errors = validate_params_schema(schema)
        assert errors != []

    def test_entry_with_invalid_params_schema_is_rejected(self) -> None:
        entry = valid_entry()
        entry["params_schema"]["properties"]["loose"] = {"type": "string"}
        result = parse_browser_catalog(catalog(entry))
        assert is_err(result)
        assert result.error == BrowserCatalogError.PARAMS_SCHEMA_INVALID


class TestCheckEvaluation:
    def test_filter_readback_pass_and_fail(self) -> None:
        check = Check(check="filter_readback", param="period")
        ok = evaluate_check(
            check,
            CheckEvidence(params={"period": "2026-07"}, readbacks={"period": "2026-07"}),
        )
        assert ok.passed
        ng = evaluate_check(
            check,
            CheckEvidence(params={"period": "2026-07"}, readbacks={"period": "2026-06"}),
        )
        assert not ng.passed
        assert ng.reason == "readback_mismatch"

    def test_row_count_range_pass_and_fail(self) -> None:
        check = Check(check="row_count_range", min=1, max=10)
        assert evaluate_check(check, CheckEvidence(file_row_count=5)).passed
        out = evaluate_check(check, CheckEvidence(file_row_count=0))
        assert not out.passed

    def test_ui_total_vs_file_rows_detects_partial_export(self) -> None:
        check = Check(check="ui_total_vs_file_rows")
        assert evaluate_check(
            check, CheckEvidence(ui_total=42, file_row_count=42)
        ).passed
        # 部分 export: UI は 42 件だがファイルは 30 行
        out = evaluate_check(check, CheckEvidence(ui_total=42, file_row_count=30))
        assert not out.passed

    def test_primary_key_unique_detects_duplicates(self) -> None:
        check = Check(check="primary_key_unique", column="user_id")
        rows_ok = CheckEvidence(columns=("user_id",), rows=((1,), (2,), (3,)))
        assert evaluate_check(check, rows_ok).passed
        rows_dup = CheckEvidence(columns=("user_id",), rows=((1,), (1,), (3,)))
        assert not evaluate_check(check, rows_dup).passed

    def test_tenant_id_match_detects_wrong_tenant(self) -> None:
        check = Check(check="tenant_id_match", expected_value="svc-readonly")
        assert evaluate_check(
            check, CheckEvidence(account_id="svc-readonly")
        ).passed
        assert not evaluate_check(
            check, CheckEvidence(account_id="other-tenant")
        ).passed

    def test_artifact_hash_detects_tampering(self) -> None:
        check = Check(check="artifact_hash")
        assert evaluate_check(
            check,
            CheckEvidence(artifact_sha256="a" * 64, expected_artifact_sha256="a" * 64),
        ).passed
        assert not evaluate_check(
            check,
            CheckEvidence(artifact_sha256="b" * 64, expected_artifact_sha256="a" * 64),
        ).passed

    def test_screen_fingerprint_detects_wrong_screen(self) -> None:
        check = Check(check="screen_fingerprint")
        assert evaluate_check(
            check,
            CheckEvidence(screen_fingerprint="fp1", expected_fingerprint="fp1"),
        ).passed
        assert not evaluate_check(
            check,
            CheckEvidence(screen_fingerprint="fp2", expected_fingerprint="fp1"),
        ).passed

    def test_missing_evidence_fails_closed(self) -> None:
        """評価に必要な証拠が欠けている check は fail-closed（passed=False）。"""
        check = Check(check="row_count_range", min=1, max=10)
        out = evaluate_check(check, CheckEvidence())
        assert not out.passed

    def test_enforce_checks_all_pass(self) -> None:
        checks = (
            Check(check="row_count_range", min=1, max=10),
            Check(check="primary_key_unique", column="id"),
        )
        evidence = CheckEvidence(
            file_row_count=3, columns=("id",), rows=((1,), (2,), (3,))
        )
        outcomes, all_ok = enforce_checks(checks, evidence)
        assert all_ok
        assert len(outcomes) == 2

    def test_enforce_checks_reports_first_failure(self) -> None:
        checks = (
            Check(check="row_count_range", min=5, max=10),
            Check(check="primary_key_unique", column="id"),
        )
        evidence = CheckEvidence(
            file_row_count=3, columns=("id",), rows=((1,), (1,))
        )
        outcomes, all_ok = enforce_checks(checks, evidence)
        assert not all_ok
        assert not outcomes[0].passed


class TestSchemaSync:
    def test_check_vocabulary_matches_schema(self) -> None:
        schema = json.loads(
            (SCHEMA_DIR / "browser-extract-catalog-schema.json").read_text(
                encoding="utf-8"
            )
        )
        schema_checks = set(
            schema["definitions"]["check"]["properties"]["check"]["enum"]
        )
        assert CHECK_NAMES == schema_checks

    def test_independent_anchors_are_subset_of_vocabulary(self) -> None:
        assert INDEPENDENT_ANCHORS <= CHECK_NAMES
