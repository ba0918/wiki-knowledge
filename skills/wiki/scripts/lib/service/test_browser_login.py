"""browser_login の chromium 非依存な核（常時実行）.

form_login 本体は実 chromium（E2E smoke）で測る。ここでは TOTP 生成の正しさと、
form / human-assisted が共有する finalize_capture（束縛・0600 保存・捕捉直後の
有効性検証）を検証する。
"""

from __future__ import annotations

import base64
import stat as _stat
from pathlib import Path

from lib.domain.browser_contract import parse_browser_catalog
from lib.domain.types import is_err, is_ok
from lib.service.browser_login import finalize_capture, login_rules, totp_code


def _form_entry():
    catalog = {
        "schema_version": 1,
        "tools": [
            {
                "tool_id": "events-web",
                "type": "browser",
                "flow": {"ref": "events_web.py", "sha256": "a" * 64},
                "auth": {
                    "profile": "form",
                    "credential_ref": "events-login",
                    "username": "svc-readonly",
                    "login_origins": ["https://idp.example.com"],
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
                    {"method": "GET", "path_prefix": "/reports", "resource_type": "document"}
                ],
                "tier": "B1",
                "guarantees": {
                    "integrity": "guaranteed",
                    "identity": "guaranteed",
                    "filter_correctness": "guaranteed",
                    "completeness": "guaranteed",
                    "human_verification": "required",
                },
                "checks": [{"check": "ui_total_vs_file_rows"}],
                "params_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"period": {"type": "string", "pattern": "^x$"}},
                },
                "limits": {
                    "max_rows": 10,
                    "max_result_bytes": 1024,
                    "max_cell_bytes": 128,
                    "max_artifact_bytes": 1024,
                    "max_flow_seconds": 30,
                    "max_unapproved_bundles": 3,
                },
                "retention": {"trace": "off", "screenshot": "off", "ttl_hours": 24},
                "delivery": {"allowed_dirs": ["deliveries"]},
                "account": {"id": "svc-readonly", "origin": "https://app.example.com"},
            }
        ],
    }
    result = parse_browser_catalog(catalog)
    assert is_ok(result), result
    return result.value[0]


class TestTotp:
    def test_rfc6238_sha1_known_vector(self) -> None:
        secret = base64.b32encode(b"12345678901234567890").decode()
        assert totp_code(secret, at_unix=59, digits=8) == "94287082"


class TestLoginRules:
    def test_login_rules_include_login_origins(self) -> None:
        rules = login_rules(_form_entry())
        origins = {r.origin for r in rules}
        assert "https://app.example.com" in origins  # 抽出 origin
        assert "https://idp.example.com" in origins  # login 中のみの追加 origin


class TestFinalizeCapture:
    def test_saves_binding_0600_and_reports_ttl(self, tmp_path: Path) -> None:
        entry = _form_entry()
        state = {"cookies": [], "origins": []}
        result = finalize_capture(
            wiki_root=tmp_path,
            entry=entry,
            storage_state=state,
            now_iso="2026-07-16T12:00:00Z",
            expires_at="2026-07-17T12:00:00Z",
        )
        assert is_ok(result), result
        info = result.value
        assert info["tool_id"] == "events-web"
        assert info["origin"] == "https://app.example.com"
        assert info["account"] == "svc-readonly"
        assert info["expires_at"] == "2026-07-17T12:00:00Z"
        # storage_state（秘密）は表示情報に含めない
        assert "storage_state" not in info and "cookies" not in info

        sess = tmp_path / ".local" / "browser-sessions" / "events-web.json"
        assert sess.exists()
        assert not (sess.stat().st_mode & 0o077), "0600 でなければならない"

    def test_expired_capture_fails_immediate_validation(self, tmp_path: Path) -> None:
        # 捕捉直後の有効性検証: expires_at が now 以下なら即失効として弾く
        entry = _form_entry()
        result = finalize_capture(
            wiki_root=tmp_path,
            entry=entry,
            storage_state={"cookies": [], "origins": []},
            now_iso="2026-07-16T12:00:00Z",
            expires_at="2026-07-16T12:00:00Z",
        )
        assert is_err(result)


# ---------------------------------------------------------------------------
# 実 chromium での form / form+totp 自動ログイン（smoke ゲート下）
# ---------------------------------------------------------------------------

import hashlib
import json
import os

import pytest

SMOKE = os.environ.get("BROWSER_EXTRACT_SMOKE")
smoke = pytest.mark.skipif(not SMOKE, reason="BROWSER_EXTRACT_SMOKE 未設定")

_MIN_FLOW = "def run(ctx, params):\n    return None\n"


def _fixture_catalog(base_url: str, auth: dict) -> dict:
    return {
        "schema_version": 1,
        "tools": [
            {
                "tool_id": "fx-web",
                "type": "browser",
                "flow": {
                    "ref": "fx_web.py",
                    "sha256": hashlib.sha256(_MIN_FLOW.encode()).hexdigest(),
                },
                "auth": auth,
                "origin_allowlist": [
                    {"method": m, "path_prefix": "/", "resource_type": t}
                    for m in ("GET", "POST")
                    for t in ("document", "other", "xhr", "fetch")
                ],
                "tier": "B1",
                "guarantees": {
                    "integrity": "guaranteed",
                    "identity": "guaranteed",
                    "filter_correctness": "guaranteed",
                    "completeness": "guaranteed",
                    "human_verification": "required",
                },
                "checks": [{"check": "ui_total_vs_file_rows"}],
                "params_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"period": {"type": "string", "pattern": "^[0-9-]+$"}},
                },
                "limits": {
                    "max_rows": 100,
                    "max_result_bytes": 1048576,
                    "max_cell_bytes": 4096,
                    "max_artifact_bytes": 1048576,
                    "max_flow_seconds": 30,
                    "max_unapproved_bundles": 3,
                },
                "retention": {"trace": "off", "screenshot": "off", "ttl_hours": 24},
                "delivery": {"allowed_dirs": ["deliveries"]},
                "account": {"id": "acme", "origin": base_url},
            }
        ],
    }


def _form_auth(*, totp: bool = False) -> dict:
    login = {
        "route": "login",
        "username_label": "Username",
        "password_label": "Password",
        "submit_role": "button",
        "submit_name": "Sign in",
        "success_url_contains": "/reports",
    }
    auth = {
        "profile": "form",
        "credential_ref": "fx-pw",
        "username": "alice",
        "login": login,
    }
    if totp:
        auth["profile"] = "form+totp"
        auth["totp_credential_ref"] = "fx-totp"
        login["totp_label"] = "One-time code"
    return auth


def _entry_for(base_url: str, auth: dict):
    result = parse_browser_catalog(_fixture_catalog(base_url, auth))
    assert is_ok(result), result
    return result.value[0]


@smoke
class TestFormLoginSmoke:
    def test_form_login_captures_session_cookie(self) -> None:
        from playwright.sync_api import sync_playwright
        from lib.service.browser_fixture_server import (
            FIXTURE_PASSWORD,
            FIXTURE_USERNAME,
            FixtureServer,
        )
        from lib.service.browser_login import form_login

        with FixtureServer() as srv:
            entry = _entry_for(srv.base_url, _form_auth())
            with sync_playwright() as pw:
                result = form_login(
                    pw,
                    entry=entry,
                    username=FIXTURE_USERNAME,
                    password=FIXTURE_PASSWORD,
                )
            assert is_ok(result), result
            cookies = result.value.get("cookies", [])
            assert any(c["name"] == "session" for c in cookies), "session cookie 未捕捉"

    def test_form_totp_login_succeeds_with_valid_code(self) -> None:
        import time

        from playwright.sync_api import sync_playwright
        from lib.service.browser_fixture_server import (
            FIXTURE_PASSWORD,
            FIXTURE_TOTP_SECRET,
            FIXTURE_USERNAME,
            FixtureServer,
        )
        from lib.service.browser_login import form_login

        with FixtureServer() as srv:
            entry = _entry_for(srv.base_url, _form_auth(totp=True))
            with sync_playwright() as pw:
                result = form_login(
                    pw,
                    entry=entry,
                    username=FIXTURE_USERNAME,
                    password=FIXTURE_PASSWORD,
                    totp_secret=FIXTURE_TOTP_SECRET,
                    at_unix=time.time(),
                )
            assert is_ok(result), result

    def test_wrong_password_maps_to_session_expired(self) -> None:
        from playwright.sync_api import sync_playwright
        from lib.service.browser_fixture_server import FIXTURE_USERNAME, FixtureServer
        from lib.service.browser_flow_runner import BrowserReason
        from lib.service.browser_login import form_login

        with FixtureServer() as srv:
            entry = _entry_for(srv.base_url, _form_auth())
            with sync_playwright() as pw:
                result = form_login(
                    pw,
                    entry=entry,
                    username=FIXTURE_USERNAME,
                    password="wrong-password",
                    default_timeout_ms=3000,
                )
            assert is_err(result)
            assert result.error == BrowserReason.SESSION_EXPIRED
