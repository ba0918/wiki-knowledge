"""browser_fixture_server の**ブラウザ非依存**な振る舞い検証（常時実行）.

fixture サーバーは smoke / E2E の土台。ここでは urllib でルートを直接叩き、
正常系（login → session cookie → reports の total/filter/tenant → CSV export）と
誤成功系変異ルート（セレクタずれ / 別 tenant / filter 未反映 / pagination 欠落）が
宣言どおりの content を配信することを検証する。TOTP は stdlib hmac の RFC 6238 実装。
実ブラウザによる抽出・検証契約の拒否は E2E（smoke ゲート下）で測る。
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request

import pytest

from lib.service.browser_fixture_server import (
    FIXTURE_PASSWORD,
    FIXTURE_TOTP_SECRET,
    FIXTURE_USERNAME,
    FixtureServer,
    totp_code,
)


# ---------------------------------------------------------------------------
# TOTP（RFC 6238、stdlib hmac）
# ---------------------------------------------------------------------------


class TestTotp:
    def test_rfc6238_sha1_known_vector(self) -> None:
        # RFC 6238 Appendix B: seed "12345678901234567890" を base32 化、
        # T=59 (step=30) の SHA-1 8桁は 94287082 → 6桁は末尾 287082
        import base64

        secret = base64.b32encode(b"12345678901234567890").decode()
        assert totp_code(secret, at_unix=59, digits=8) == "94287082"

    def test_code_changes_across_step_boundary(self) -> None:
        a = totp_code(FIXTURE_TOTP_SECRET, at_unix=0)
        b = totp_code(FIXTURE_TOTP_SECRET, at_unix=30)
        assert a != b


# ---------------------------------------------------------------------------
# HTTP 振る舞い（urllib、ブラウザ非依存）
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """303 の Set-Cookie を観測するためリダイレクトを追わない。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(url: str, *, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        resp = _OPENER.open(req)
        return resp.status, dict(resp.headers), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def _form(**fields) -> bytes:
    from urllib.parse import urlencode

    return urlencode(fields).encode("utf-8")


@pytest.fixture()
def server():
    srv = FixtureServer()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _login(server, *, totp: str | None = None) -> str:
    fields = {"username": FIXTURE_USERNAME, "password": FIXTURE_PASSWORD}
    if totp is not None:
        fields["totp"] = totp
    status, headers, _ = _request(
        server.base_url + "/login", data=_form(**fields), method="POST"
    )
    assert status in (200, 303), f"login 失敗: {status}"
    cookie = headers.get("Set-Cookie")
    assert cookie and "session=" in cookie, "session cookie が発行されない"
    return cookie.split(";", 1)[0]


class TestLogin:
    def test_login_page_served(self, server) -> None:
        status, _, body = _request(server.base_url + "/login")
        assert status == 200
        assert 'name="username"' in body and 'name="password"' in body

    def test_wrong_password_rejected_without_session(self, server) -> None:
        status, headers, _ = _request(
            server.base_url + "/login",
            data=_form(username=FIXTURE_USERNAME, password="wrong"),
            method="POST",
        )
        assert status == 401
        assert "Set-Cookie" not in headers

    def test_correct_credentials_issue_session(self, server) -> None:
        assert _login(server).startswith("session=")

    def test_totp_present_but_wrong_is_rejected(self, server) -> None:
        status, headers, _ = _request(
            server.base_url + "/login",
            data=_form(
                username=FIXTURE_USERNAME, password=FIXTURE_PASSWORD, totp="000000"
            ),
            method="POST",
        )
        assert status == 401
        assert "Set-Cookie" not in headers

    def test_totp_valid_is_accepted(self, server) -> None:
        import time

        code = totp_code(FIXTURE_TOTP_SECRET, at_unix=time.time())
        assert _login(server, totp=code).startswith("session=")


class TestProtectedRoutes:
    def test_reports_requires_session(self, server) -> None:
        status, _, _ = _request(server.base_url + "/reports?period=2026-07")
        assert status == 401

    def test_reports_shows_total_filter_and_tenant(self, server) -> None:
        cookie = _login(server)
        status, _, body = _request(
            server.base_url + "/reports?period=2026-07", headers={"Cookie": cookie}
        )
        assert status == 200
        # UI total（独立 oracle）・filter 表示（readback）・tenant（identity）
        assert 'role="status"' in body and 'aria-label="total"' in body
        assert ">3<" in body, "total は 3 行のはず"
        assert 'aria-label="Period"' in body and "2026-07" in body
        assert 'data-tenant="acme"' in body

    def test_export_returns_csv_matching_rows(self, server) -> None:
        cookie = _login(server)
        status, headers, body = _request(
            server.base_url + "/export?period=2026-07", headers={"Cookie": cookie}
        )
        assert status == 200
        assert "text/csv" in headers.get("Content-Type", "")
        assert "attachment" in headers.get("Content-Disposition", "")
        rows = list(csv.reader(io.StringIO(body)))
        assert rows[0] == ["user_id", "email"]
        assert len(rows) - 1 == 3, "ヘッダ除き 3 行"


class TestMutationRoutes:
    """誤成功系変異ルート（同一アプリのルート違い、guide §15）。"""

    def test_filter_ignored_shows_wrong_period(self, server) -> None:
        cookie = _login(server)
        _, _, body = _request(
            server.base_url + "/reports-filter-ignored?period=2026-07",
            headers={"Cookie": cookie},
        )
        # filter 表示が要求 period を反映しない（filter_readback が捕捉する変異）
        assert 'aria-label="Period"' in body
        assert "2026-07" not in body.split('aria-label="Period"')[1][:60]

    def test_wrong_tenant_shows_different_tenant(self, server) -> None:
        cookie = _login(server)
        _, _, body = _request(
            server.base_url + "/reports-wrong-tenant?period=2026-07",
            headers={"Cookie": cookie},
        )
        assert 'data-tenant="acme"' not in body
        assert "data-tenant=" in body

    def test_pagination_missing_total_exceeds_export_rows(self, server) -> None:
        cookie = _login(server)
        _, _, page = _request(
            server.base_url + "/reports-pagination-missing?period=2026-07",
            headers={"Cookie": cookie},
        )
        _, _, csv_body = _request(
            server.base_url + "/export-pagination-missing?period=2026-07",
            headers={"Cookie": cookie},
        )
        # UI total は 100、export は 3 行（ui_total_vs_file_rows が捕捉する変異）
        assert ">100<" in page
        rows = list(csv.reader(io.StringIO(csv_body)))
        assert len(rows) - 1 == 3

    def test_selector_drift_hides_export_button_name(self, server) -> None:
        cookie = _login(server)
        _, _, body = _request(
            server.base_url + "/reports-selector-drift?period=2026-07",
            headers={"Cookie": cookie},
        )
        # "Export CSV" という accessible name のボタンが存在しない（selector_not_found）
        assert "Export CSV" not in body
