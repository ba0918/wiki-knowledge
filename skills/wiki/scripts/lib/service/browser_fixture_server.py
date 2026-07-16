"""smoke / E2E 用のローカル fixture web アプリ（stdlib http.server・外部依存なし）.

browser-extract の smoke / E2E は実 chromium を要するが、実サービスに触れずに
封じ込め・抽出・検証契約を測るための土台がこの fixture サーバー。tool-query の
http harness（``test_tool_connector_http_server.py``）は JSON API 用でブラウザ向けの
HTML 配信に転用しづらいため新規に置く。

提供するもの:

* **login form**（username / password、任意で TOTP）。TOTP は stdlib hmac の
  RFC 6238 実装で検証（外部依存を増やさない）
* session cookie による保護ルート
* **UI total 付きテーブル**（``role="status"`` の total = 独立 oracle）+ filter 表示
  （readback）+ tenant 表示（identity）
* **CSV export download**（``Content-Disposition: attachment``）
* **誤成功系変異ルート**（guide §15、同一アプリのルート違い）:
  セレクタずれ / 別 tenant 同型画面 / filter 未反映 / pagination 欠落

秘密（password / TOTP secret）は fixture 専用のダミー値であり、実サービスの credential は
使わない。サーバーは値をログに残さない。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# TOTP 生成の真実源は browser_login（production util）。fixture は re-export する。
from lib.service.browser_login import totp_code


# fixture 専用のダミー credential（実サービスのものは使わない）
FIXTURE_USERNAME = "alice"
FIXTURE_PASSWORD = "s3cret-fixture"
FIXTURE_TOTP_SECRET = base64.b32encode(b"browser-extract-fx").decode("ascii")

# 決定的な抽出対象データ
_TENANT = "acme"
_COLUMNS = ("user_id", "email")
_ROWS = ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com"))


# ---------------------------------------------------------------------------
# TOTP（RFC 6238、stdlib のみ）
# ---------------------------------------------------------------------------


def _totp_valid(secret_b32: str, code: str, *, now: float) -> bool:
    """現在および前後 1 ステップの窓で TOTP を照合する（clock skew 許容）。"""

    for drift in (-1, 0, 1):
        if hmac.compare_digest(totp_code(secret_b32, at_unix=now + drift * 30), code):
            return True
    return False


# ---------------------------------------------------------------------------
# HTML レンダリング
# ---------------------------------------------------------------------------


def _table_rows_html(rows) -> str:
    cells = []
    for uid, email in rows:
        cells.append(
            f'<tr><td role="cell">{uid}</td>'
            f'<td role="cell">{html.escape(email)}</td></tr>'
        )
    return "".join(cells)


def _reports_html(
    *, period_shown: str, total: int, tenant: str, export_name: str, export_href: str
) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Reports</title></head>
<body data-tenant="{html.escape(tenant)}">
  <label for="period">Period</label>
  <span id="period" aria-label="Period">{html.escape(period_shown)}</span>
  <div role="status" aria-label="total">{total}</div>
  <table><thead><tr><th>user_id</th><th>email</th></tr></thead>
  <tbody>{_table_rows_html(_ROWS)}</tbody></table>
  <a href="{html.escape(export_href)}" role="button" download>{html.escape(export_name)}</a>
</body></html>"""


def _login_html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Login</title></head>
<body>
  <form method="POST" action="/login">
    <label for="username">Username</label>
    <input id="username" name="username" type="text">
    <label for="password">Password</label>
    <input id="password" name="password" type="password">
    <label for="totp">One-time code</label>
    <input id="totp" name="totp" type="text">
    <button type="submit">Sign in</button>
  </form>
</body></html>"""


def _csv_bytes(rows) -> bytes:
    lines = [",".join(_COLUMNS)]
    lines += [f"{uid},{email}" for uid, email in rows]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP ハンドラ
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "BrowserFixture/1"

    # -- helpers -----------------------------------------------------------

    @property
    def _srv(self) -> "FixtureServer":
        return self.server.fixture  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:  # 値をログに残さない（沈黙）
        return

    def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, status: int, markup: str, extra: dict | None = None) -> None:
        self._send(
            status,
            markup.encode("utf-8"),
            {"Content-Type": "text/html; charset=utf-8", **(extra or {})},
        )

    def _has_session(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        token = ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "session":
                token = v
        return token != "" and token in self._srv.sessions

    def _period(self) -> str:
        qs = parse_qs(urlparse(self.path).query)
        return qs.get("period", [""])[0]

    # -- POST /login -------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/login":
            self._html(404, "<h1>not found</h1>")
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        fields = {k: v[0] for k, v in parse_qs(raw).items()}
        ok = (
            fields.get("username") == FIXTURE_USERNAME
            and fields.get("password") == FIXTURE_PASSWORD
        )
        if ok and "totp" in fields:
            ok = _totp_valid(
                FIXTURE_TOTP_SECRET, fields["totp"], now=self._srv.time_func()
            )
        if not ok:
            self._html(401, "<h1>unauthorized</h1>")
            return
        token = self._srv.issue_session()
        self._send(
            303,
            b"",
            {
                "Location": "/reports",
                "Set-Cookie": f"session={token}; Path=/; HttpOnly",
            },
        )

    # -- GET ---------------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path in ("/", "/login"):
            self._html(200, _login_html())
            return

        # 応答を返さず navigation を hang させる（hard timeout smoke 用）。
        # session 不要 — page 既定 timeout の発火を測るため認証前に置く。
        if path == "/hang":
            time.sleep(30)
            self._html(200, "<h1>late</h1>")
            return

        # 以降は保護ルート
        if not self._has_session():
            self._html(401, "<h1>login required</h1>")
            return

        period = self._period()

        if path == "/reports":
            self._html(
                200,
                _reports_html(
                    period_shown=period,
                    total=len(_ROWS),
                    tenant=_TENANT,
                    export_name="Export CSV",
                    export_href=f"/export?period={period}",
                ),
            )
            return
        if path == "/export":
            self._csv(_ROWS)
            return

        # -- 誤成功系変異ルート（guide §15）--------------------------------
        if path == "/reports-filter-ignored":
            # 要求 period を反映せず固定の別 period を表示する
            self._html(
                200,
                _reports_html(
                    period_shown="1970-01",
                    total=len(_ROWS),
                    tenant=_TENANT,
                    export_name="Export CSV",
                    export_href=f"/export?period={period}",
                ),
            )
            return
        if path == "/reports-wrong-tenant":
            self._html(
                200,
                _reports_html(
                    period_shown=period,
                    total=len(_ROWS),
                    tenant="globex",
                    export_name="Export CSV",
                    export_href=f"/export?period={period}",
                ),
            )
            return
        if path == "/reports-pagination-missing":
            # UI total は 100 と主張するが export は 3 行のみ
            self._html(
                200,
                _reports_html(
                    period_shown=period,
                    total=100,
                    tenant=_TENANT,
                    export_name="Export CSV",
                    export_href=f"/export-pagination-missing?period={period}",
                ),
            )
            return
        if path == "/export-pagination-missing":
            self._csv(_ROWS)
            return
        if path == "/reports-selector-drift":
            # Export ボタンの accessible name が変わっている（selector_not_found）
            self._html(
                200,
                _reports_html(
                    period_shown=period,
                    total=len(_ROWS),
                    tenant=_TENANT,
                    export_name="Download",
                    export_href=f"/export?period={period}",
                ),
            )
            return

        self._html(404, "<h1>not found</h1>")

    def _csv(self, rows) -> None:
        self._send(
            200,
            _csv_bytes(rows),
            {
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": 'attachment; filename="report.csv"',
            },
        )


# ---------------------------------------------------------------------------
# サーバー
# ---------------------------------------------------------------------------


class FixtureServer:
    """ephemeral port で起動する fixture web アプリ（テスト用）。

    ``time_func`` を注入すると TOTP 検証の時刻を制御できる（決定的テスト）。
    """

    def __init__(self, *, time_func=time.time) -> None:
        self.time_func = time_func
        self.sessions: set[str] = set()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._counter = 0

    def issue_session(self) -> str:
        self._counter += 1
        token = hashlib.sha256(
            f"{self._counter}:{self.time_func()}".encode()
        ).hexdigest()[:32]
        self.sessions.add(token)
        return token

    @property
    def base_url(self) -> str:
        assert self._httpd is not None, "start() していない"
        host, port = self._httpd.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def start(self) -> None:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.fixture = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FixtureServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
