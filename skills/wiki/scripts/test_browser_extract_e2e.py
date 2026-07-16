"""E2E: fixture サーバー相手の chromium 実駆動一周（smoke ゲート下）.

form login → doctor → prepare（抽出 + 検証契約 enforce + 封印）→ approve（TTY 監査
アンカー照合、monkeypatch）→ execute（delivery 解放）を一周する。加えて誤成功系変異
ルート（filter 未反映 / 別 tenant / pagination 欠落 / セレクタずれ）が実ブラウザでも
検証契約により全て拒否されることを確認する（guide §15 の登録合格条件と同じ基準）。

test-only の承認迂回は作らない（TTY は monkeypatch のみ）。秘密（credential /
session state）は fixture 専用ダミーで、後始末は tmp_path が担う。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as _stat
from pathlib import Path

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.clock import SystemClock
from lib.service.file_lock import RealFileLock

import browser_extract_run as cli

SMOKE = os.environ.get("BROWSER_EXTRACT_SMOKE")
smoke = pytest.mark.skipif(not SMOKE, reason="BROWSER_EXTRACT_SMOKE 未設定")


_FLOW_TEMPLATE = '''\
def run(ctx, params):
    ctx.goto("{route}", period=params["period"])
    ctx.wait_stable("navigation_settled")
    ctx.read_filter("Period", "period")
    ctx.read_ui_total("status", "total")
    ctx.read_tenant()
    ctx.download_csv("button", "Export CSV")
'''


class _FakeStdin:
    def __init__(self, answer: str, *, tty: bool = True) -> None:
        self._answer, self._tty = answer, tty

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        return self._answer


def _build_e2e_wiki(tmp_path: Path, base_url: str, *, route: str) -> Path:
    from lib.service.browser_fixture_server import FIXTURE_PASSWORD

    wiki = tmp_path / "wiki"
    (wiki / "tools" / "flows").mkdir(parents=True)
    (wiki / "deliveries").mkdir()
    (wiki / "outputs").mkdir()
    (wiki / ".local").mkdir()

    flow_src = _FLOW_TEMPLATE.format(route=route)
    flow = wiki / "tools" / "flows" / "fx_web.py"
    flow.write_text(flow_src, encoding="utf-8")

    # credential（fixture 専用ダミー）を 0600 で置く
    cred = wiki / ".local" / "credentials.json"
    cred.write_text(json.dumps({"fx-pw": FIXTURE_PASSWORD}), encoding="utf-8")
    os.chmod(cred, 0o600)

    catalog = {
        "schema_version": 1,
        "tools": [
            {
                "tool_id": "fx-web",
                "type": "browser",
                "flow": {
                    "ref": "fx_web.py",
                    "sha256": hashlib.sha256(flow_src.encode()).hexdigest(),
                },
                "auth": {
                    "profile": "form",
                    "credential_ref": "fx-pw",
                    "username": "alice",
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
                "checks": [
                    {"check": "filter_readback", "param": "period"},
                    {"check": "row_count_range", "min": 1, "max": 100},
                    {"check": "ui_total_vs_file_rows"},
                    {"check": "primary_key_unique", "column": "user_id"},
                    {"check": "tenant_id_match", "expected_value": "acme"},
                ],
                "params_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "period": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}$"}
                    },
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
    (wiki / "tools" / "browser-catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return wiki


def _real_runner(wiki: Path) -> cli.BrowserRunner:
    return cli.BrowserRunner(
        wiki_root=wiki,
        clock=SystemClock(),
        lock=RealFileLock(),
        extractor=cli._real_extractor(wiki),
        nonce=lambda: os.urandom(2).hex(),
        login_fn=cli._real_login_fn(wiki),
    )


@smoke
class TestFullLap:
    def test_form_login_doctor_prepare_approve_execute(self, tmp_path, monkeypatch):
        from lib.service.browser_fixture_server import FixtureServer

        with FixtureServer() as srv:
            wiki = _build_e2e_wiki(tmp_path, srv.base_url, route="reports")
            runner = _real_runner(wiki)

            # doctor: login 疎通 + selector 実在（SMOKE 下で実 chromium）
            doc = runner.doctor("fx-web")
            assert is_ok(doc), doc
            status = {n: s for n, s, _ in doc.value}
            assert status["login_reachability"] == "OK"
            assert status["selector_exists"] == "OK"

            # prepare: form login → 抽出 → 検証契約 enforce → 封印
            prep = runner.prepare(
                tool_id="fx-web", params={"period": "2026-07"}, deliver_to="deliveries"
            )
            assert is_ok(prep), prep
            plan_id = prep.value.plan_id
            assert prep.value.row_count == 3

            # session state が 0600 で保存された
            sess = wiki / ".local" / "browser-sessions" / "fx-web.json"
            assert sess.exists()
            assert not (sess.stat().st_mode & 0o077)

            # approve: TTY 確認（monkeypatch）+ 監査アンカー照合
            monkeypatch.setattr(cli.sys, "stdin", _FakeStdin("yes\n"))
            rc = cli.main(
                ["--wiki-root", str(wiki), "approve", "--plan-id", plan_id,
                 "--approved-by", "e2e"]
            )
            assert rc == 0

            # execute: 封印済み成果物を delivery へ解放
            ex = runner.execute(plan_id)
            assert is_ok(ex), ex
            published = ex.value.published_path
            assert (published / "result.csv").exists()
            csv_text = (published / "result.csv").read_text(encoding="utf-8")
            assert csv_text.splitlines()[0] == "user_id,email"
            assert ex.value.row_count == 3

            # 監査に秘密が載っていないこと（session cookie 値・password が無い）
            audit = (wiki / "outputs" / "browser-audit.jsonl").read_text(encoding="utf-8")
            assert "s3cret-fixture" not in audit

    @pytest.mark.parametrize(
        "route,reason",
        [
            ("reports-filter-ignored", "readback_mismatch"),
            ("reports-wrong-tenant", "tenant_mismatch"),
            ("reports-pagination-missing", "ui_total_mismatch"),
            ("reports-selector-drift", "selector_not_found"),
        ],
    )
    def test_mutation_routes_are_rejected(self, tmp_path, route, reason):
        from lib.service.browser_fixture_server import FixtureServer

        with FixtureServer() as srv:
            wiki = _build_e2e_wiki(tmp_path, srv.base_url, route=route)
            runner = _real_runner(wiki)
            prep = runner.prepare(
                tool_id="fx-web", params={"period": "2026-07"}, deliver_to="deliveries"
            )
            assert is_err(prep), f"{route} は拒否されるべき"
            assert prep.error == reason, f"{route}: {prep.error} != {reason}"
            # 誤成功を封印しない（bundle は作られない）
            plans = wiki / "outputs" / "browser-plans"
            sealed = [p for p in plans.iterdir() if not p.name.startswith(".staging-")] if plans.exists() else []
            assert sealed == [], f"{route} で bundle が封印された"
