"""Tests for browser_flow_runner の**ブラウザ非依存**な決定的ロジック（常時実行）.

実 chromium を要する interception / teardown / E2E は BROWSER_EXTRACT_SMOKE 環境変数
ゲート下（未設定時 skip）。ここで検証するのは AST ゲート・flow pin・URL 正規化 +
allowlist 照合・janitor・例外 sanitize — いずれも「決定的な判定規則」でありブラウザを
起動しない。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.browser_flow_runner import (
    BrowserReason,
    OriginRuleLite,
    canonicalize_request_url,
    check_flow_ast,
    match_allowlist,
    sanitize_exception,
    sweep_bundles,
    verify_flow_pin,
)


# ---------------------------------------------------------------------------
# AST ゲート（常時実行）
# ---------------------------------------------------------------------------

GOOD_FLOW = """
def run(ctx, params):
    ctx.goto("reports", period=params["period"])
    ctx.wait_stable("navigation_settled")
    total = ctx.read_text(ctx.get_by_role("status", name="total"))
    rows = []
    for i in range(3):
        rows.append(ctx.read_text(ctx.get_by_role("cell")))
    return ctx.download(ctx.get_by_role("button", name="Export"), role="button", name="Export")
"""


class TestAstGate:
    def test_valid_flow_passes(self) -> None:
        assert is_ok(check_flow_ast(GOOD_FLOW))

    @pytest.mark.parametrize(
        "src",
        [
            "import os\ndef run(ctx, params):\n    return 1",
            "from os import system\ndef run(ctx, params):\n    return 1",
            "def run(ctx, params):\n    return __import__('os')",
            "def run(ctx, params):\n    return ().__class__.__bases__",
            "def run(ctx, params):\n    exec('x=1')",
            "def run(ctx, params):\n    return eval('1')",
            "def run(ctx, params):\n    return open('/etc/passwd')",
            "def run(ctx, params):\n    return getattr(ctx, '__globals__')",
            "class Evil:\n    pass\ndef run(ctx, params):\n    return 1",
            "def run(ctx, params):\n    f = lambda: 1\n    return f()",
            "def run(ctx, params):\n    global x\n    return 1",
        ],
    )
    def test_forbidden_constructs_are_rejected(self, src: str) -> None:
        result = check_flow_ast(src)
        assert is_err(result), src
        assert result.error == BrowserReason.FLOW_AST_VIOLATION

    def test_missing_run_function_is_rejected(self) -> None:
        result = check_flow_ast("x = 1\n")
        assert is_err(result)
        assert result.error == BrowserReason.FLOW_AST_VIOLATION

    def test_dunder_attribute_access_is_rejected(self) -> None:
        result = check_flow_ast(
            "def run(ctx, params):\n    return params.__class__"
        )
        assert is_err(result)
        assert result.error == BrowserReason.FLOW_AST_VIOLATION


# ---------------------------------------------------------------------------
# flow pin（常時実行）
# ---------------------------------------------------------------------------


class TestFlowPin:
    def test_matching_pin_passes(self) -> None:
        src = b"def run(ctx, params):\n    return 1\n"
        digest = hashlib.sha256(src).hexdigest()
        assert is_ok(verify_flow_pin(src, digest))

    def test_mismatched_pin_is_rejected(self) -> None:
        result = verify_flow_pin(b"tampered", "a" * 64)
        assert is_err(result)
        assert result.error == BrowserReason.FLOW_PIN_MISMATCH


# ---------------------------------------------------------------------------
# URL 正規化 + allowlist 照合（常時実行）
# ---------------------------------------------------------------------------

RULES = (
    OriginRuleLite(
        origin="https://app.example.com",
        method="GET",
        path_prefix="/reports",
        resource_type="document",
    ),
    OriginRuleLite(
        origin="https://app.example.com",
        method="POST",
        path_prefix="/api/export",
        resource_type="xhr",
    ),
)


class TestUrlCanonicalization:
    def test_plain_url_canonicalizes(self) -> None:
        result = canonicalize_request_url("https://app.example.com/reports/2026")
        assert is_ok(result), result
        assert result.value.origin == "https://app.example.com"
        assert result.value.segments == ("reports", "2026")

    def test_userinfo_is_rejected(self) -> None:
        result = canonicalize_request_url("https://user:pw@app.example.com/reports")
        assert is_err(result)

    def test_trailing_dot_host_is_normalized(self) -> None:
        result = canonicalize_request_url("https://app.example.com./reports")
        assert is_ok(result)
        assert result.value.origin == "https://app.example.com"

    def test_default_port_is_omitted_and_explicit_kept(self) -> None:
        a = canonicalize_request_url("https://app.example.com:443/reports")
        assert is_ok(a)
        assert a.value.origin == "https://app.example.com"
        b = canonicalize_request_url("https://app.example.com:8443/reports")
        assert is_ok(b)
        assert b.value.origin == "https://app.example.com:8443"

    def test_encoded_separator_is_rejected(self) -> None:
        result = canonicalize_request_url("https://app.example.com/reports%2f..%2fadmin")
        assert is_err(result)

    def test_data_and_blob_urls_are_rejected(self) -> None:
        assert is_err(canonicalize_request_url("data:text/html,hi"))
        assert is_err(canonicalize_request_url("blob:https://app.example.com/x"))


class TestAllowlistMatching:
    def test_allowed_request_passes(self) -> None:
        assert is_ok(
            match_allowlist(
                method="GET",
                url="https://app.example.com/reports/2026",
                resource_type="document",
                rules=RULES,
            )
        )

    def test_wrong_method_is_blocked(self) -> None:
        result = match_allowlist(
            method="DELETE",
            url="https://app.example.com/reports/2026",
            resource_type="document",
            rules=RULES,
        )
        assert is_err(result)
        assert result.error == BrowserReason.ORIGIN_BLOCKED

    def test_wrong_origin_is_blocked(self) -> None:
        result = match_allowlist(
            method="GET",
            url="https://evil.example.com/reports/2026",
            resource_type="document",
            rules=RULES,
        )
        assert is_err(result)
        assert result.error == BrowserReason.ORIGIN_BLOCKED

    def test_path_prefix_must_respect_segment_boundary(self) -> None:
        # /reports-secret は /reports の prefix 文字列だが segment 境界で不一致
        result = match_allowlist(
            method="GET",
            url="https://app.example.com/reports-secret/x",
            resource_type="document",
            rules=RULES,
        )
        assert is_err(result)
        assert result.error == BrowserReason.ORIGIN_BLOCKED

    def test_wrong_resource_type_is_blocked(self) -> None:
        result = match_allowlist(
            method="GET",
            url="https://app.example.com/reports/2026",
            resource_type="script",
            rules=RULES,
        )
        assert is_err(result)
        assert result.error == BrowserReason.ORIGIN_BLOCKED


# ---------------------------------------------------------------------------
# janitor（常時実行・ファイル操作）
# ---------------------------------------------------------------------------


class TestJanitor:
    def _make_bundle(self, plans_root: Path, plan_id: str, expires_at: str) -> None:
        bundle = plans_root / plan_id
        bundle.mkdir(parents=True)
        (bundle / "manifest.json").write_text(
            f'{{"expires_at": "{expires_at}"}}', encoding="utf-8"
        )

    def test_expired_bundles_are_removed(self, tmp_path: Path) -> None:
        plans = tmp_path / "browser-plans"
        self._make_bundle(plans, "20260716120000-aa00-web", "2026-07-17T00:00:00Z")
        self._make_bundle(plans, "20260716120000-aa01-web", "2026-07-30T00:00:00Z")
        removed, failed = sweep_bundles(plans, now="2026-07-18T00:00:00Z")
        assert failed == []
        assert removed == ["20260716120000-aa00-web"]
        assert not (plans / "20260716120000-aa00-web").exists()
        assert (plans / "20260716120000-aa01-web").exists()

    def test_incomplete_bundle_without_manifest_is_reaped(self, tmp_path: Path) -> None:
        """SIGKILL 等で manifest を書く前に落ちた bundle も回収対象。"""
        plans = tmp_path / "browser-plans"
        (plans / ".staging-x").mkdir(parents=True)
        removed, failed = sweep_bundles(plans, now="2026-07-18T00:00:00Z")
        assert ".staging-x" in removed

    def test_missing_plans_root_is_noop(self, tmp_path: Path) -> None:
        removed, failed = sweep_bundles(
            tmp_path / "absent", now="2026-07-18T00:00:00Z"
        )
        assert removed == []
        assert failed == []


# ---------------------------------------------------------------------------
# 例外 sanitize（常時実行）
# ---------------------------------------------------------------------------


class TestExceptionSanitize:
    def test_maps_to_closed_reason_without_text(self) -> None:
        reason = sanitize_exception(ValueError("token=SECRET https://x/y?t=abc"))
        assert isinstance(reason, BrowserReason)

    def test_timeout_like_exception_maps_to_flow_timeout(self) -> None:
        class TimeoutError_(Exception):
            pass

        reason = sanitize_exception(TimeoutError_("Timeout 30000ms exceeded"))
        # 未知例外は internal_error に写像される（生テキストを通さない）
        assert reason in (BrowserReason.INTERNAL_ERROR, BrowserReason.FLOW_TIMEOUT)


# ---------------------------------------------------------------------------
# 実 chromium を要する E2E（smoke ゲート）
# ---------------------------------------------------------------------------

SMOKE = os.environ.get("BROWSER_EXTRACT_SMOKE")


@pytest.mark.skipif(not SMOKE, reason="BROWSER_EXTRACT_SMOKE 未設定")
class TestBrowserSmoke:
    def test_playwright_is_importable(self) -> None:
        import playwright  # noqa: F401
