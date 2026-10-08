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

    def test_ipv4_literal_host_canonicalizes(self) -> None:
        # IP リテラルは IDNA エンコードできない（ローカル fixture の origin）。
        # 数値ホストは IDNA を通さず素通しし、port つき origin を保つ
        result = canonicalize_request_url("http://127.0.0.1:44667/reports")
        assert is_ok(result), result
        assert result.value.origin == "http://127.0.0.1:44667"
        assert result.value.segments == ("reports",)

    def test_ipv6_literal_host_canonicalizes(self) -> None:
        result = canonicalize_request_url("http://[::1]:8080/reports")
        assert is_ok(result), result
        assert result.value.origin == "http://[::1]:8080"

    def test_root_path_canonicalizes_to_empty_segments(self) -> None:
        # ブラウザは origin root "/" を正常に発行する（http connector の segment
        # 正規化は末尾空 segment を拒否するため browser 側で吸収する）
        result = canonicalize_request_url("http://127.0.0.1:8080/")
        assert is_ok(result), result
        assert result.value.segments == ()

    def test_single_trailing_slash_is_tolerated(self) -> None:
        result = canonicalize_request_url("https://app.example.com/reports/")
        assert is_ok(result), result
        assert result.value.segments == ("reports",)

    def test_double_slash_is_still_rejected(self) -> None:
        # 多重スラッシュ（path 混同攻撃面）は依然として拒否する
        assert is_err(canonicalize_request_url("https://app.example.com/a//b"))


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
# interception 設置の mechanism（常時実行・chromium 非依存）
#
# 実 chromium での「宣言内継続 / 宣言外 abort / context スコープ」は smoke
# （TestInterceptionSmoke）が測る。ここでは fake context/route で「context スコープ
# ルート + 全 WS deny の設置」と「宣言外 abort + on_block 通知 / 宣言内 continue」を
# 決定的に検証する（live WebSocket は sync dispatcher と deadlock し得るため測らない）。
# ---------------------------------------------------------------------------


class _FakeRoute:
    def __init__(self) -> None:
        self.action: str | None = None

    def abort(self) -> None:
        self.action = "abort"

    def continue_(self) -> None:
        self.action = "continue"


class _FakeRequest:
    def __init__(self, *, method: str, url: str, resource_type: str) -> None:
        self.method = method
        self.url = url
        self.resource_type = resource_type


class _FakeContext:
    def __init__(self) -> None:
        self.http_pattern: str | None = None
        self.http_handler = None
        self.ws_pattern: str | None = None

    def route(self, pattern, handler) -> None:
        self.http_pattern = pattern
        self.http_handler = handler

    def route_web_socket(self, pattern, handler) -> None:
        self.ws_pattern = pattern


class TestInterceptionMechanism:
    def test_installs_context_scope_http_route_and_ws_deny(self) -> None:
        from lib.service.browser_flow_runner import install_interception

        ctx = _FakeContext()
        install_interception(ctx, RULES)
        # context スコープ（page ではなく '**/*'）で全リクエスト・全 WS を捕捉する
        assert ctx.http_pattern == "**/*"
        assert ctx.ws_pattern == "**/*"

    def test_foreign_origin_is_aborted_and_notified(self) -> None:
        from lib.service.browser_flow_runner import install_interception

        ctx = _FakeContext()
        blocked: list[int] = []
        install_interception(ctx, RULES, on_block=lambda: blocked.append(1))
        route = _FakeRoute()
        ctx.http_handler(
            route,
            _FakeRequest(
                method="GET",
                url="https://evil.example.com/reports/2026",
                resource_type="document",
            ),
        )
        assert route.action == "abort"
        assert blocked == [1], "宣言外 abort は on_block に通知される"

    def test_allowed_request_continues_without_notify(self) -> None:
        from lib.service.browser_flow_runner import install_interception

        ctx = _FakeContext()
        blocked: list[int] = []
        install_interception(ctx, RULES, on_block=lambda: blocked.append(1))
        route = _FakeRoute()
        ctx.http_handler(
            route,
            _FakeRequest(
                method="GET",
                url="https://app.example.com/reports/2026",
                resource_type="document",
            ),
        )
        assert route.action == "continue"
        assert blocked == []


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

    def test_selector_error_maps_to_selector_not_found(self) -> None:
        from lib.service.browser_flow_runner import FlowSelectorError

        reason = sanitize_exception(FlowSelectorError("button[name='X'] 不在"))
        assert reason == BrowserReason.SELECTOR_NOT_FOUND


# ---------------------------------------------------------------------------
# 実 chromium を要する E2E（smoke ゲート）
# ---------------------------------------------------------------------------

SMOKE = os.environ.get("BROWSER_EXTRACT_SMOKE")

pytestmark_smoke = pytest.mark.skipif(not SMOKE, reason="BROWSER_EXTRACT_SMOKE 未設定")


@pytestmark_smoke
class TestBrowserSmoke:
    def test_playwright_is_importable(self) -> None:
        import playwright  # noqa: F401


# ---------------------------------------------------------------------------
# 実 chromium での封じ込め・teardown・download（smoke ゲート下）
#
# 「決定的な判定規則」ではなく Playwright API との実接合部を測る。fixture サーバー
# （browser_fixture_server）を相手に、interception の全リクエスト捕捉・context スコープ・
# teardown の確実性・hard timeout・download の atomic 配置を実挙動で検証する。
# ---------------------------------------------------------------------------

if SMOKE:
    from lib.service.browser_flow_runner import (
        BrowserFlowRunner,
        FlowContext,
        contained_context,
        rules_from_entry,
        save_download_atomic,
    )
    from lib.service.browser_fixture_server import FixtureServer
    from playwright.sync_api import sync_playwright


def _fixture_rules(base_url: str) -> tuple[OriginRuleLite, ...]:
    """fixture origin への GET/POST を document/other で許可する allowlist。"""

    from lib.service.browser_flow_runner import canonicalize_request_url

    origin = canonicalize_request_url(base_url + "/").value.origin
    rules = []
    for method in ("GET", "POST"):
        for rtype in ("document", "other", "xhr", "fetch"):
            rules.append(
                OriginRuleLite(
                    origin=origin, method=method, path_prefix="/", resource_type=rtype
                )
            )
    return tuple(rules)


@pytestmark_smoke
class TestInterceptionSmoke:
    def test_allowed_continues_and_foreign_origin_blocked_context_scoped(self) -> None:
        with FixtureServer() as srv:
            rules = _fixture_rules(srv.base_url)
            blocked: list[int] = []
            with sync_playwright() as pw:
                with contained_context(
                    pw, rules=rules, on_block=lambda: blocked.append(1)
                ) as context:
                    page = context.new_page()
                    # 宣言内 origin は継続する
                    resp = page.goto(srv.base_url + "/login")
                    assert resp is not None and resp.status == 200
                    assert not blocked, "許可 origin で abort してはならない"

                    # 新規タブ（context スコープ）から宣言外 origin へ → abort
                    page2 = context.new_page()
                    with pytest.raises(Exception):
                        page2.goto("http://127.0.0.1:1/forbidden", timeout=3000)
                    assert blocked, "宣言外 origin の abort が on_block に記録される"

    def test_service_workers_do_not_register(self) -> None:
        with FixtureServer() as srv:
            rules = _fixture_rules(srv.base_url)
            with sync_playwright() as pw:
                with contained_context(pw, rules=rules) as context:
                    page = context.new_page()
                    page.goto(srv.base_url + "/login")
                    # service_workers='block' 下では worker が生成されない
                    assert context.service_workers == []


@pytestmark_smoke
class TestTeardownSmoke:
    def _run_and_capture_udd(self, *, raise_in_block: bool) -> Path:
        holder: dict[str, Path] = {}
        rules: tuple[OriginRuleLite, ...] = ()
        try:
            with sync_playwright() as pw:
                with contained_context(
                    pw,
                    rules=rules,
                    on_setup=lambda ctx, udd: holder.__setitem__("udd", udd),
                ) as context:
                    context.new_page()
                    if raise_in_block:
                        raise RuntimeError("フロー内例外")
        except RuntimeError:
            pass
        return holder["udd"]

    def test_normal_exit_closes_context_and_purges_udd(self) -> None:
        udd = self._run_and_capture_udd(raise_in_block=False)
        assert not udd.exists(), "正常終了後に ephemeral user-data-dir が残っている"

    def test_exception_path_also_purges_udd(self) -> None:
        udd = self._run_and_capture_udd(raise_in_block=True)
        assert not udd.exists(), "例外経路でも udd を purge しなければならない"


@pytestmark_smoke
class TestHardTimeoutSmoke:
    def test_navigation_timeout_maps_to_flow_timeout_and_cleans_up(self) -> None:
        # /hang は応答を返さない。page 既定 timeout 超過 → TimeoutError → flow_timeout。
        with FixtureServer() as srv:
            rules = _fixture_rules(srv.base_url)
            udd_holder: dict[str, Path] = {}
            with sync_playwright() as pw:
                try:
                    with contained_context(
                        pw,
                        rules=rules,
                        default_timeout_ms=1000,
                        on_setup=lambda c, udd: udd_holder.__setitem__("udd", udd),
                    ) as context:
                        page = context.new_page()
                        page.goto(srv.base_url + "/hang")
                    reason = None
                except BaseException as exc:  # noqa: BLE001
                    reason = sanitize_exception(exc)
            assert reason == BrowserReason.FLOW_TIMEOUT
            assert not udd_holder["udd"].exists(), "timeout 後も udd を purge する"


@pytestmark_smoke
class TestDownloadSmoke:
    def test_download_saved_atomically_with_runner_name(self, tmp_path) -> None:
        with FixtureServer() as srv:
            rules = _fixture_rules(srv.base_url)
            # 事前に session を発行して cookie を storage_state 化する
            token = srv.issue_session()
            state = {
                "cookies": [
                    {
                        "name": "session",
                        "value": token,
                        "domain": "127.0.0.1",
                        "path": "/",
                        "httpOnly": True,
                        "secure": False,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            }
            spool = tmp_path / "spool"
            with sync_playwright() as pw:
                with contained_context(
                    pw, rules=rules, storage_state=state
                ) as context:
                    page = context.new_page()
                    page.goto(srv.base_url + "/reports?period=2026-07")
                    with page.expect_download() as dl:
                        page.get_by_role("button", name="Export CSV").click()
                    final = save_download_atomic(
                        dl.value, spool_dir=spool, nonce="abcd"
                    )
            assert final.exists()
            # サーバー指定名 report.csv ではなく runner 生成名で置かれる
            assert final.name == "download-abcd.bin"
            assert not (spool / "report.csv").exists()
            assert not (spool / ".dl-abcd.part").exists(), "一時ファイルは残らない"
            body = final.read_text()
            assert body.splitlines()[0] == "user_id,email"
            assert len([l for l in body.splitlines() if l]) == 4  # header + 3 行


class TestBrowserVersionGate:
    @pytest.mark.parametrize("version", ["155.0.8059.39", "155.0.8059.40", "156.0.8078.4"])
    def test_fixed_versions(self, version):
        from lib.service.browser_flow_runner import require_safe_browser_version
        require_safe_browser_version(version)

    @pytest.mark.parametrize("version", [None, "", "unknown", "155", "155.0.8059.39-dev", "155.0.8059.38", "153.0.8010.12", "139.0.7258.5"])
    def test_old_or_unverified_versions(self, version):
        from lib.service.browser_flow_runner import BrowserVersionError, require_safe_browser_version
        with pytest.raises(BrowserVersionError) as error:
            require_safe_browser_version(version)
        assert sanitize_exception(error.value) == BrowserReason.BROWSER_VERSION_UNSAFE

    @pytest.mark.parametrize("headless", [True, False])
    @pytest.mark.parametrize("version", ["153.0.8010.12", None])
    def test_rejected_context_is_closed_before_session_or_callback(self, monkeypatch, headless, version):
        monkeypatch.setattr("lib.service.browser_flow_runner.require_safe_bundled_browser", lambda **kwargs: None)
        from types import SimpleNamespace
        from unittest.mock import Mock
        from lib.service.browser_flow_runner import BrowserVersionError, contained_context
        context = Mock()
        context.browser = SimpleNamespace(version=version)
        pw = Mock()
        pw.chromium.launch_persistent_context.return_value = context
        on_setup = Mock()
        with pytest.raises(BrowserVersionError):
            with contained_context(pw, rules=(), storage_state={"cookies": [{"name": "secret"}]}, on_setup=on_setup, headless=headless):
                pytest.fail("Unsafe browser must not be yielded")
        context.close.assert_called_once()
        context.add_cookies.assert_not_called()
        context.add_init_script.assert_not_called()
        context.new_page.assert_not_called()
        on_setup.assert_not_called()
        kwargs = pw.chromium.launch_persistent_context.call_args.kwargs
        assert kwargs["headless"] is headless
        assert not Path(kwargs["user_data_dir"]).exists()

    def test_fixed_context_preserves_containment_and_teardown(self, monkeypatch):
        monkeypatch.setattr("lib.service.browser_flow_runner.require_safe_bundled_browser", lambda **kwargs: None)
        from types import SimpleNamespace
        from unittest.mock import Mock
        from lib.service.browser_flow_runner import contained_context
        context = Mock()
        context.browser = SimpleNamespace(version="155.0.8059.39")
        pw = Mock()
        pw.chromium.launch_persistent_context.return_value = context
        with contained_context(pw, rules=()) as active:
            assert active is context
            context.route.assert_called_once()
            context.route_web_socket.assert_called_once()
        context.close.assert_called_once()
        assert not Path(pw.chromium.launch_persistent_context.call_args.kwargs["user_data_dir"]).exists()


    @pytest.mark.parametrize("headless", [True, False])
    @pytest.mark.parametrize("version", ["153.0.8010.12", None])
    def test_unsafe_bundle_never_launches(self, tmp_path, monkeypatch, headless, version):
        import json
        from unittest.mock import Mock
        from lib.service.browser_flow_runner import BrowserVersionError, contained_context
        manifest = tmp_path / "driver" / "package" / "browsers.json"
        manifest.parent.mkdir(parents=True)
        name = "chromium-headless-shell" if headless else "chromium"
        manifest.write_text(json.dumps({"browsers": [{"name": name, "browserVersion": version}]}))
        monkeypatch.setattr("importlib.resources.files", lambda package: tmp_path)
        pw = Mock()
        with pytest.raises(BrowserVersionError):
            with contained_context(pw, rules=(), headless=headless):
                pytest.fail("Unsafe bundled browser must not launch")
        pw.chromium.launch_persistent_context.assert_not_called()

    @pytest.mark.parametrize("contents", [None, "not JSON", '{"browsers": []}'])
    def test_unverified_manifest_fails_closed(self, tmp_path, monkeypatch, contents):
        from lib.service.browser_flow_runner import BrowserVersionError, require_safe_bundled_browser
        manifest = tmp_path / "driver" / "package" / "browsers.json"
        manifest.parent.mkdir(parents=True)
        if contents is not None:
            manifest.write_text(contents)
        monkeypatch.setattr("importlib.resources.files", lambda package: tmp_path)
        with pytest.raises(BrowserVersionError):
            require_safe_bundled_browser(headless=True)

    @pytest.mark.parametrize("headless", [True, False])
    def test_verified_bundle_manifest_passes(self, tmp_path, monkeypatch, headless):
        import json
        from lib.service.browser_flow_runner import require_safe_bundled_browser
        manifest = tmp_path / "driver" / "package" / "browsers.json"
        manifest.parent.mkdir(parents=True)
        name = "chromium-headless-shell" if headless else "chromium"
        manifest.write_text(json.dumps({"browsers": [{"name": name, "browserVersion": "155.0.8059.39"}]}))
        monkeypatch.setattr("importlib.resources.files", lambda package: tmp_path)
        require_safe_bundled_browser(headless=headless)
