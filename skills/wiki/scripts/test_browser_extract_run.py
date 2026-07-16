"""Tests for browser_extract_run — seal-at-prepare CLI（browser 非依存・常時実行）.

FlowExtractor を fake で注入し、実 chromium なしで prepare（封印）→ approve（監査
アンカー照合）→ execute（delivery 解放のみ）の seal-at-prepare 契約を検証する。
最重要は approve の「封印 artifact + manifest からの再導出ハッシュ vs prepared 監査
イベントの封印ハッシュの fail-closed 照合」— artifact 改変・manifest 改変とも拒否のみ合格。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lib.domain.types import Err, Ok, is_err, is_ok
from lib.service.browser_flow_runner import BrowserReason, ExtractionResult
from lib.service.clock import FixedClock
from lib.service.file_lock import RealFileLock

import browser_extract_run as cli


NOW = "2026-07-16T12:00:00Z"


class FakeExtractor:
    """フロー実行を模す — 固定の抽出結果（or エラー）を返す。"""

    def __init__(self, result=None, error: BrowserReason | None = None) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    def extract(self, *, entry, params, session_state, deadline_monotonic):
        self.calls += 1
        if self._error is not None:
            return Err(error=self._error, detail="")
        return Ok(value=self._result)


def sample_result(**overrides) -> ExtractionResult:
    csv = b"user_id,email\r\n1,a@example.com\r\n2,b@example.com\r\n"
    args = dict(
        columns=("user_id", "email"),
        rows=((1, "a@example.com"), (2, "b@example.com")),
        artifact_bytes=csv,
        readbacks={"period": "2026-07"},
        ui_total=2,
        account_id="svc-readonly",
        screen_fingerprint="fp-main",
        extracted_at=NOW,
    )
    args.update(overrides)
    return ExtractionResult(**args)


def make_wiki(tmp_path: Path) -> Path:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "tools" / "flows").mkdir(parents=True)
    (wiki_root / "deliveries").mkdir()
    (wiki_root / "outputs").mkdir()
    flow = wiki_root / "tools" / "flows" / "events_web.py"
    flow.write_text(
        "def run(ctx, params):\n    return None\n", encoding="utf-8"
    )
    flow_sha = hashlib.sha256(flow.read_bytes()).hexdigest()
    catalog = {
        "schema_version": 1,
        "tools": [
            {
                "tool_id": "events-web",
                "type": "browser",
                "flow": {"ref": "events_web.py", "sha256": flow_sha},
                "auth": {"profile": "none"},
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
                    {"check": "row_count_range", "min": 1, "max": 100},
                    {"check": "ui_total_vs_file_rows"},
                    {"check": "primary_key_unique", "column": "user_id"},
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
                    "max_unapproved_bundles": 3,
                },
                "retention": {"trace": "off", "screenshot": "off", "ttl_hours": 24},
                "delivery": {"allowed_dirs": ["deliveries"]},
                "account": {"id": "svc-readonly", "origin": "https://app.example.com"},
            }
        ],
    }
    (wiki_root / "tools" / "browser-catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return wiki_root


def make_runner(wiki_root: Path, extractor, **overrides) -> cli.BrowserRunner:
    args = dict(
        wiki_root=wiki_root,
        clock=FixedClock(now=NOW),
        lock=RealFileLock(),
        extractor=extractor,
        nonce=lambda: "aa00",
        lock_timeout=5.0,
    )
    args.update(overrides)
    return cli.BrowserRunner(**args)


def do_prepare(runner, **overrides):
    args = dict(
        tool_id="events-web",
        params={"period": "2026-07"},
        deliver_to="deliveries",
    )
    args.update(overrides)
    return runner.prepare(**args)


def bundle_dir(wiki_root: Path, plan_id: str) -> Path:
    return wiki_root / "outputs" / "browser-plans" / plan_id


def audit_events(wiki_root: Path) -> list[dict]:
    path = wiki_root / "outputs" / "browser-audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


class TestPrepareSeal:
    def test_prepare_seals_artifact_and_records_prepared_audit(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        extractor = FakeExtractor(result=sample_result())
        runner = make_runner(wiki_root, extractor)
        result = do_prepare(runner)
        assert is_ok(result), result
        outcome = result.value
        b = bundle_dir(wiki_root, outcome.plan_id)
        assert (b / "artifact.bin").exists()
        assert (b / "manifest.json").exists()
        state = json.loads((b / "state.json").read_text(encoding="utf-8"))
        assert state["status"] == "prepared"
        art_sha = hashlib.sha256((b / "artifact.bin").read_bytes()).hexdigest()
        man_sha = hashlib.sha256((b / "manifest.json").read_bytes()).hexdigest()
        assert state["sealed_artifact_digest"] == art_sha
        assert state["sealed_manifest_digest"] == man_sha
        events = audit_events(wiki_root)
        prepared = [e for e in events if e["event"] == "prepared"][0]
        assert prepared["row_count"] == 2
        assert prepared["artifact_digest"] == art_sha
        assert prepared["manifest_digest"] == man_sha

    def test_prepare_rejects_when_checks_fail(self, tmp_path):
        """誤成功系: filter が未反映（readback 不一致）だと prepare が拒否される。"""
        wiki_root = make_wiki(tmp_path)
        extractor = FakeExtractor(
            result=sample_result(readbacks={"period": "2020-01"})
        )
        runner = make_runner(wiki_root, extractor)
        result = do_prepare(runner)
        assert is_err(result)
        assert result.error == "readback_mismatch"

    def test_prepare_rejects_partial_export(self, tmp_path):
        """UI total=2 だがファイル 1 行 → ui_total_vs_file_rows で拒否。"""
        wiki_root = make_wiki(tmp_path)
        csv = b"user_id,email\r\n1,a@example.com\r\n"
        extractor = FakeExtractor(
            result=sample_result(
                artifact_bytes=csv, rows=((1, "a@example.com"),), ui_total=2
            )
        )
        runner = make_runner(wiki_root, extractor)
        result = do_prepare(runner)
        assert is_err(result)
        assert result.error == "ui_total_mismatch"

    def test_prepare_rejects_duplicate_primary_key(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        csv = b"user_id,email\r\n1,a@example.com\r\n1,b@example.com\r\n"
        extractor = FakeExtractor(
            result=sample_result(
                artifact_bytes=csv,
                rows=((1, "a@example.com"), (1, "b@example.com")),
                ui_total=2,
            )
        )
        runner = make_runner(wiki_root, extractor)
        result = do_prepare(runner)
        assert is_err(result)
        assert result.error == "duplicate_primary_key"

    def test_extractor_error_is_sanitized_reason(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        extractor = FakeExtractor(error=BrowserReason.ORIGIN_BLOCKED)
        runner = make_runner(wiki_root, extractor)
        result = do_prepare(runner)
        assert is_err(result)
        assert result.error == "origin_blocked"

    def test_unapproved_bundle_cap_is_enforced(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        extractor = FakeExtractor(result=sample_result())
        nonces = iter(["aa00", "aa01", "aa02", "aa03", "aa04"])
        runner = make_runner(wiki_root, extractor, nonce=lambda: next(nonces))
        for _ in range(3):
            assert is_ok(do_prepare(runner))
        # 4 本目は上限（max_unapproved_bundles=3）超過で拒否
        result = do_prepare(runner)
        assert is_err(result)
        assert result.error == "bundle_cap_exceeded"


class TestApproveAuditAnchor:
    def _prepare(self, wiki_root):
        extractor = FakeExtractor(result=sample_result())
        runner = make_runner(wiki_root, extractor)
        return runner, do_prepare(runner).value.plan_id

    def test_approve_succeeds_on_untampered_bundle(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        runner, plan_id = self._prepare(wiki_root)
        result = runner.approve(plan_id, approved_by="mizumi")
        assert is_ok(result), result
        state = json.loads(
            (bundle_dir(wiki_root, plan_id) / "state.json").read_text(encoding="utf-8")
        )
        assert state["status"] == "approved"

    def test_approve_rejects_tampered_artifact(self, tmp_path):
        """prepare 後に artifact を書き換えると監査アンカーと不一致で拒否。"""
        wiki_root = make_wiki(tmp_path)
        runner, plan_id = self._prepare(wiki_root)
        art = bundle_dir(wiki_root, plan_id) / "artifact.bin"
        art.write_bytes(b"user_id,email\r\n9,evil@example.com\r\n")
        result = runner.approve(plan_id, approved_by="mizumi")
        assert is_err(result)
        assert result.error == "seal_mismatch"

    def test_approve_rejects_tampered_manifest(self, tmp_path):
        """manifest 改変も『拒否』のみ合格（偽造 anchor を反映しない）。"""
        wiki_root = make_wiki(tmp_path)
        runner, plan_id = self._prepare(wiki_root)
        man = bundle_dir(wiki_root, plan_id) / "manifest.json"
        data = json.loads(man.read_text(encoding="utf-8"))
        data["row_count"] = 999999
        man.write_text(json.dumps(data), encoding="utf-8")
        result = runner.approve(plan_id, approved_by="mizumi")
        assert is_err(result)
        assert result.error == "seal_mismatch"

    def test_approved_event_records_pinned_hashes(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        runner, plan_id = self._prepare(wiki_root)
        assert is_ok(runner.approve(plan_id, approved_by="mizumi"))
        approved = [e for e in audit_events(wiki_root) if e["event"] == "approved"][0]
        assert "artifact_digest" in approved
        assert "manifest_digest" in approved


class TestExecuteDelivery:
    def _prepare_approve(self, wiki_root):
        extractor = FakeExtractor(result=sample_result())
        runner = make_runner(wiki_root, extractor)
        plan_id = do_prepare(runner).value.plan_id
        assert is_ok(runner.approve(plan_id, approved_by="mizumi"))
        return runner, plan_id

    def test_execute_releases_sealed_artifact_to_delivery(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        runner, plan_id = self._prepare_approve(wiki_root)
        result = runner.execute(plan_id)
        assert is_ok(result), result
        published = result.value.published_path
        assert (published / "result.csv").read_bytes() == sample_result().artifact_bytes
        assert (published / "manifest.json").exists()

    def test_second_execute_is_single_use_rejected(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        runner, plan_id = self._prepare_approve(wiki_root)
        assert is_ok(runner.execute(plan_id))
        result = runner.execute(plan_id)
        assert is_err(result)

    def test_execute_does_not_re_run_the_browser(self, tmp_path):
        """execute は delivery 解放のみ — extractor を呼ばない。"""
        wiki_root = make_wiki(tmp_path)
        extractor = FakeExtractor(result=sample_result())
        runner = make_runner(wiki_root, extractor)
        plan_id = do_prepare(runner).value.plan_id
        assert is_ok(runner.approve(plan_id, approved_by="mizumi"))
        calls_before = extractor.calls
        assert is_ok(runner.execute(plan_id))
        assert extractor.calls == calls_before  # ブラウザ再実行なし


class TestPlanTypeGuard:
    def test_execute_rejects_non_browser_plan_id_namespace(self, tmp_path):
        """SQL plan 置き場を指しても browser CLI は消費できない（取り違え遮断）。"""
        wiki_root = make_wiki(tmp_path)
        extractor = FakeExtractor(result=sample_result())
        runner = make_runner(wiki_root, extractor)
        # browser-plans に存在しない plan_id
        result = runner.execute("20260716120000-zz99-events-web")
        assert is_err(result)
        assert result.error == "bundle_missing"


class TestPreviewRendering:
    def test_escapes_terminal_escape_and_control_bytes(self):
        raw = "normal\x1b[31mRED\x07\ttab"
        rendered = cli.render_preview_cell(raw, width=40)
        assert "\x1b" not in rendered
        assert "\x07" not in rendered

    def test_clips_wide_cjk_with_marker(self):
        rendered = cli.render_preview_cell("あ" * 50, width=10)
        # East Asian width を考慮して 10 幅以内 + truncation マーカー
        assert rendered.endswith("…")


class TestReasonHints:
    def test_every_browser_reason_has_hint(self):
        for reason in BrowserReason:
            hint = cli.reason_hint(reason.value)
            assert "what" in hint and "why" in hint and "next" in hint


class TestDoctor:
    def test_reports_flow_pin_and_ast_ok(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root, FakeExtractor(result=sample_result()))
        result = runner.doctor("events-web")
        assert is_ok(result)
        status = {name: st for name, st, _ in result.value}
        assert status["catalog_resolve"] == "OK"
        assert status["flow_pin"] == "OK"
        assert status["flow_ast"] == "OK"
        assert status["params_schema"] == "OK"
        # 実 chromium 検査はデータ非接触の honest scoping で SKIP 明示
        assert status["login_reachability"] == "SKIP"

    def test_detects_flow_pin_mismatch(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        flow = wiki_root / "tools" / "flows" / "events_web.py"
        flow.write_text("def run(ctx, params):\n    return 1\n", encoding="utf-8")
        runner = make_runner(wiki_root, FakeExtractor(result=sample_result()))
        result = runner.doctor("events-web")
        assert is_ok(result)
        status = {name: st for name, st, _ in result.value}
        assert status["flow_pin"] == "NG"

    def test_doctor_records_plan_independent_audit(self, tmp_path):
        wiki_root = make_wiki(tmp_path)
        runner = make_runner(wiki_root, FakeExtractor(result=sample_result()))
        runner.doctor("events-web")
        doctor_events = [e for e in audit_events(wiki_root) if e["event"] == "doctor"]
        assert len(doctor_events) == 1
        assert "plan_id" not in doctor_events[0]


# ---------------------------------------------------------------------------
# session 解決（form / form+totp の自動ログイン接合。chromium 非依存）
#
# form 系の prepare は session store を解決し、無ければ login_fn（実体は headless
# form login、テストは fake 注入）で捕捉して 0600 で保存し再利用する。実 chromium は
# smoke（E2E）が測る。ここでは「解決 → 保存 → 再利用 → 失敗の伝播」を決定的に検証する。
# ---------------------------------------------------------------------------


class RecordingExtractor:
    """extract 時に受け取った session_state を記録する fake。"""

    def __init__(self, result) -> None:
        self._result = result
        self.session_states: list = []

    def extract(self, *, entry, params, session_state, deadline_monotonic):
        self.session_states.append(session_state)
        return Ok(value=self._result)


def make_wiki_form(tmp_path: Path) -> Path:
    """profile=form の catalog を持つ wiki を作る（login block つき）。"""

    wiki_root = make_wiki(tmp_path)
    catalog_path = wiki_root / "tools" / "browser-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["tools"][0]["auth"] = {
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
    }
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return wiki_root


_STATE = {"cookies": [{"name": "session", "value": "tok", "domain": "app.example.com",
                       "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax"}],
          "origins": []}


class TestSessionResolution:
    def test_form_profile_auto_logs_in_and_reuses_saved_session(self, tmp_path):
        wiki_root = make_wiki_form(tmp_path)
        extractor = RecordingExtractor(sample_result())
        calls = []

        def login_fn(entry):
            calls.append(entry.tool_id)
            return Ok(value=_STATE)

        nonces = iter(["aa01", "aa02"])
        runner = make_runner(
            wiki_root, extractor, login_fn=login_fn, nonce=lambda: next(nonces)
        )
        r1 = do_prepare(runner)
        assert is_ok(r1), r1
        # 初回は login_fn が呼ばれ、captured storage_state が extractor に渡る
        assert calls == ["events-web"]
        assert extractor.session_states[-1] == _STATE
        # session は 0600 で保存される
        sess = wiki_root / ".local" / "browser-sessions" / "events-web.json"
        assert sess.exists()
        import stat as _stat
        assert not (sess.stat().st_mode & 0o077)

        # 2 回目は保存済み session を再利用し login_fn を呼ばない
        r2 = do_prepare(runner)
        assert is_ok(r2), r2
        assert calls == ["events-web"], "保存済み session があれば再ログインしない"
        assert extractor.session_states[-1] == _STATE

    def test_login_failure_propagates_as_session_expired(self, tmp_path):
        wiki_root = make_wiki_form(tmp_path)
        extractor = RecordingExtractor(sample_result())

        def login_fn(entry):
            return Err(error=BrowserReason.SESSION_EXPIRED, detail="")

        runner = make_runner(wiki_root, extractor, login_fn=login_fn)
        result = do_prepare(runner)
        assert is_err(result)
        assert result.error == "session_expired"
        assert extractor.session_states == [], "ログイン失敗時は抽出しない"

    def test_none_profile_passes_no_session(self, tmp_path):
        wiki_root = make_wiki(tmp_path)  # profile=none
        extractor = RecordingExtractor(sample_result())
        runner = make_runner(wiki_root, extractor, login_fn=lambda e: Ok(value=_STATE))
        assert is_ok(do_prepare(runner))
        assert extractor.session_states == [None]


# ---------------------------------------------------------------------------
# doctor の実 chromium 疎通 + selector 実在確認（smoke ゲート下）
# ---------------------------------------------------------------------------

import os

import pytest

SMOKE = os.environ.get("BROWSER_EXTRACT_SMOKE")
smoke = pytest.mark.skipif(not SMOKE, reason="BROWSER_EXTRACT_SMOKE 未設定")


def _write_fixture_wiki(tmp_path: Path, base_url: str, *, submit_name="Sign in", route="login") -> Path:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "tools" / "flows").mkdir(parents=True)
    (wiki_root / "outputs").mkdir()
    flow_src = "def run(ctx, params):\n    return None\n"
    flow = wiki_root / "tools" / "flows" / "fx_web.py"
    flow.write_text(flow_src, encoding="utf-8")
    catalog = {
        "schema_version": 1,
        "tools": [
            {
                "tool_id": "fx-web",
                "type": "browser",
                "flow": {"ref": "fx_web.py", "sha256": hashlib.sha256(flow_src.encode()).hexdigest()},
                "auth": {
                    "profile": "form",
                    "credential_ref": "fx-pw",
                    "username": "alice",
                    "login": {
                        "route": route,
                        "username_label": "Username",
                        "password_label": "Password",
                        "submit_role": "button",
                        "submit_name": submit_name,
                        "success_url_contains": "/reports",
                    },
                },
                "origin_allowlist": [
                    {"method": m, "path_prefix": "/", "resource_type": t}
                    for m in ("GET", "POST") for t in ("document", "other", "xhr", "fetch")
                ],
                "tier": "B1",
                "guarantees": {"integrity": "guaranteed", "identity": "guaranteed",
                               "filter_correctness": "guaranteed", "completeness": "guaranteed",
                               "human_verification": "required"},
                "checks": [{"check": "ui_total_vs_file_rows"}],
                "params_schema": {"type": "object", "additionalProperties": False,
                                  "properties": {"period": {"type": "string", "pattern": "^[0-9-]+$"}}},
                "limits": {"max_rows": 100, "max_result_bytes": 1048576, "max_cell_bytes": 4096,
                           "max_artifact_bytes": 1048576, "max_flow_seconds": 30, "max_unapproved_bundles": 3},
                "retention": {"trace": "off", "screenshot": "off", "ttl_hours": 24},
                "delivery": {"allowed_dirs": ["deliveries"]},
                "account": {"id": "acme", "origin": base_url},
            }
        ],
    }
    (wiki_root / "tools" / "browser-catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return wiki_root


@smoke
class TestDoctorChromiumSmoke:
    def _doctor(self, wiki_root):
        runner = make_runner(wiki_root, FakeExtractor(result=sample_result()))
        result = runner.doctor("fx-web")
        assert is_ok(result), result
        return {name: (st, detail) for name, st, detail in result.value}

    def test_reachable_login_and_selectors_report_ok(self, tmp_path):
        from lib.service.browser_fixture_server import FixtureServer
        with FixtureServer() as srv:
            status = self._doctor(_write_fixture_wiki(tmp_path, srv.base_url))
        assert status["login_reachability"][0] == "OK"
        assert status["selector_exists"][0] == "OK"

    def test_selector_drift_reports_ng(self, tmp_path):
        from lib.service.browser_fixture_server import FixtureServer
        with FixtureServer() as srv:
            status = self._doctor(
                _write_fixture_wiki(tmp_path, srv.base_url, submit_name="No Such Button")
            )
        # ページには到達するが submit セレクタが実在しない
        assert status["login_reachability"][0] == "OK"
        assert status["selector_exists"][0] == "NG"

    def test_unreachable_route_reports_ng(self, tmp_path):
        from lib.service.browser_fixture_server import FixtureServer
        with FixtureServer() as srv:
            status = self._doctor(_write_fixture_wiki(tmp_path, srv.base_url, route="nonexistent"))
        assert status["login_reachability"][0] == "NG"
