"""HttpConnector × stdlib local HTTP server の統合テスト（通常テストに含める）.

fake transport では urllib 実装差（redirect 処理・timeout・chunked 配信・
ヘッダ正規化）を検証できないため、127.0.0.1 に実サーバーを立てて
UrllibTransport ごと通す。外部ネットワークには一切出ない。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.tool_catalog import HttpConnectionConfig, HttpEndpointRule
from lib.service.tool_connector import ToolConnectorError
from lib.service.tool_connector_http import (
    HttpConnector,
    HttpConnectorError,
    UrllibTransport,
)


class _Handler(BaseHTTPRequestHandler):
    seen_headers: list[dict] = []

    def log_message(self, *args) -> None:  # テスト出力を汚さない
        pass

    def _send_json(self, payload: dict, *, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).seen_headers.append(dict(self.headers))
        if self.path.startswith("/api/data"):
            self._send_json(
                {"result": {"rows": [{"id": 1, "name": "alice"}], "total": 1}}
            )
        elif self.path.startswith("/api/redirect"):
            self.send_response(302)
            self.send_header("Location", "/api/data")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path.startswith("/api/gzip"):
            body = b"\x1f\x8b\x08\x00fakegzip"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/huge"):
            # Content-Length 宣言なしの chunked 配信で上限超過を再現する
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"x" * 8192
            for _ in range(64):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
        elif self.path.startswith("/api/forbidden"):
            self._send_json({"error": "forbidden"}, status=403)
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        type(self).seen_headers.append(dict(self.headers))
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self._send_json({"echo": payload, "count": 7})


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        # クライアント側切断（サイズ遮断テスト）による BrokenPipe を握りつぶす
        pass


@pytest.fixture(scope="module")
def server():
    httpd = _QuietServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    thread.join(timeout=5)


def make_connector(server, *, max_response_bytes: int = 1048576) -> HttpConnector:
    port = server.server_address[1]
    config = HttpConnectionConfig(
        base_url=f"http://127.0.0.1:{port}",
        allowed_endpoints=(
            HttpEndpointRule(method="GET", path_prefix="/api/data"),
            HttpEndpointRule(method="GET", path_prefix="/api/redirect"),
            HttpEndpointRule(method="GET", path_prefix="/api/gzip"),
            HttpEndpointRule(method="GET", path_prefix="/api/huge"),
            HttpEndpointRule(method="GET", path_prefix="/api/forbidden"),
            HttpEndpointRule(method="POST", path_prefix="/api/echo"),
        ),
        auth_header_name="Authorization",
        auth_header_template="Key {credential}",
        allow_insecure=True,
    )
    return HttpConnector(
        config=config,
        credential="hunter2",
        max_response_bytes=max_response_bytes,
        deadline_monotonic=__import__("time").monotonic() + 30.0,
        transport=UrllibTransport(),
    )


def spec(path: str, *, method: str = "GET", body: dict | None = None, **kw) -> str:
    data = {"method": method, "path": path}
    if body is not None:
        data["body"] = body
    if "count_path" in kw:
        data["count_path"] = kw["count_path"]
    else:
        data["records_path"] = kw.get("records_path", "result.rows")
        data["columns"] = kw.get("columns", ["id", "name"])
    return json.dumps(data)


class TestLocalServerIntegration:
    def test_get_roundtrip_projects_rows(self, server) -> None:
        connector = make_connector(server)
        result = connector.execute_stream(spec("/api/data"))
        assert is_ok(result), getattr(result, "detail", None)
        with result.value as stream:
            assert stream.columns == ("id", "name")
            assert list(stream) == [(1, "alice")]

    def test_auth_and_identity_headers_reach_the_server(self, server) -> None:
        _Handler.seen_headers.clear()
        connector = make_connector(server)
        assert is_ok(connector.execute_stream(spec("/api/data")))
        headers = _Handler.seen_headers[-1]
        assert headers.get("Authorization") == "Key hunter2"
        assert headers.get("Accept-Encoding") == "identity"

    def test_post_body_roundtrip_and_count(self, server) -> None:
        connector = make_connector(server)
        result = connector.execute_stream(
            spec("/api/echo", method="POST", body={"q": 1}, count_path="count")
        )
        assert is_ok(result), getattr(result, "detail", None)
        with result.value as stream:
            assert list(stream) == [(7,)]

    def test_redirect_is_rejected_not_followed(self, server) -> None:
        """urllib の既定は redirect 追跡 — transport が追跡しないことを実サーバーで固定。"""
        connector = make_connector(server)
        result = connector.execute_stream(spec("/api/redirect"))
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_INVALID
        assert "redirect" in result.detail

    def test_gzip_response_is_rejected(self, server) -> None:
        connector = make_connector(server)
        result = connector.execute_stream(spec("/api/gzip"))
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_INVALID

    def test_chunked_oversize_is_cut_off(self, server) -> None:
        connector = make_connector(server, max_response_bytes=16384)
        result = connector.execute_stream(spec("/api/huge"))
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_TOO_LARGE

    def test_http_error_status_is_classified(self, server) -> None:
        connector = make_connector(server)
        result = connector.execute_stream(spec("/api/forbidden"))
        assert is_err(result)
        assert result.error == ToolConnectorError.NOT_AUTHORIZED

    def test_connection_refused_is_connect_failed(self) -> None:
        config = HttpConnectionConfig(
            base_url="http://127.0.0.1:9",  # discard port — 接続拒否想定
            allowed_endpoints=(
                HttpEndpointRule(method="GET", path_prefix="/api/data"),
            ),
            auth_header_name="Authorization",
            auth_header_template="Key {credential}",
            allow_insecure=True,
        )
        connector = HttpConnector(
            config=config,
            credential="hunter2",
            max_response_bytes=1024,
            deadline_monotonic=__import__("time").monotonic() + 5.0,
            transport=UrllibTransport(),
        )
        result = connector.execute_stream(spec("/api/data"))
        assert is_err(result)
        assert result.error in (
            ToolConnectorError.CONNECT_FAILED,
            ToolConnectorError.DEADLINE_EXCEEDED,
        )
