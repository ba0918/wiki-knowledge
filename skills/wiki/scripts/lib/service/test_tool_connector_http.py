"""Tests for tool_connector_http.py — HttpConnector（urllib、fake transport DI）.

SQL の代わりに request spec（JSON）を実行単位とする connector。契約:

* request spec は JSON Schema（tool-request-spec-schema.json）で検証、未知キー拒否
* URL canonicalization: decode 前の encoded separator 拒否（%2f・%5c・%2e%2e・
  NUL/control）→ percent-encoding 正規化（二重・不正 encoding は fail closed）
  → ``.`` / ``..`` 解決 → ``//``・backslash 拒否 → **一度だけ正規化した最終
  URL に対して** origin 完全一致 + segment 境界の prefix 照合 + メソッド照合
* リダイレクト拒否 / ``Accept-Encoding: identity`` 固定 + Content-Encoding 検査 /
  chunk 読みで max_response_bytes 超過時に全量確保前に遮断
* records_path / count_path の dot-path 解決。解決エラーは「どの segment まで
  解決できたか・期待した型・実際の型」を秘密値なしで構造化表示
* credential はヘッダ注入のみ — エラー・detail のどこにも出さない
"""

from __future__ import annotations

import json

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.tool_catalog import HttpConnectionConfig, HttpEndpointRule
from lib.service.tool_connector import ToolConnectorError
from lib.service.tool_connector_http import (
    FakeTransport,
    FakeTransportResponse,
    HttpConnector,
    HttpConnectorError,
    TransportError,
    build_request_url,
    parse_request_spec,
)


def make_config(**overrides) -> HttpConnectionConfig:
    args = dict(
        base_url="https://api.example.com",
        allowed_endpoints=(
            HttpEndpointRule(method="GET", path_prefix="/api/data"),
            HttpEndpointRule(method="POST", path_prefix="/api/queries"),
        ),
        auth_header_name="Authorization",
        auth_header_template="Key {credential}",
    )
    args.update(overrides)
    return HttpConnectionConfig(**args)


def main_spec(**overrides) -> str:
    spec = {
        "method": "GET",
        "path": "/api/data",
        "records_path": "result.rows",
        "columns": ["user_id", "email"],
    }
    spec.update(overrides)
    return json.dumps(spec)


def count_spec(**overrides) -> str:
    spec = {"method": "GET", "path": "/api/data", "count_path": "result.total"}
    spec.update(overrides)
    return json.dumps(spec)


def ok_body(rows=None, total=2) -> bytes:
    rows = rows if rows is not None else [
        {"user_id": 1, "email": "a@example.com"},
        {"user_id": 2, "email": "b@example.com"},
    ]
    return json.dumps({"result": {"rows": rows, "total": total}}).encode("utf-8")


def make_connector(
    transport: FakeTransport, *, config=None, max_response_bytes=1048576, **kwargs
) -> HttpConnector:
    return HttpConnector(
        config=config or make_config(),
        credential="hunter2",
        max_response_bytes=max_response_bytes,
        deadline_monotonic=kwargs.pop("deadline", 30.0),
        monotonic=kwargs.pop("monotonic", lambda: 0.0),
        transport=transport,
    )


# ---------------------------------------------------------------------------
# request spec の検証
# ---------------------------------------------------------------------------


class TestParseRequestSpec:
    def test_valid_main_spec_parses(self) -> None:
        result = parse_request_spec(main_spec())
        assert is_ok(result)
        spec = result.value
        assert spec.method == "GET"
        assert spec.records_path == ("result", "rows")
        assert spec.columns == ("user_id", "email")
        assert spec.count_path is None

    def test_valid_count_spec_parses(self) -> None:
        result = parse_request_spec(count_spec())
        assert is_ok(result)
        assert result.value.count_path == ("result", "total")

    def test_invalid_json_is_rejected(self) -> None:
        result = parse_request_spec("{ not json")
        assert is_err(result)
        assert result.error == HttpConnectorError.SPEC_INVALID

    def test_unknown_key_is_rejected(self) -> None:
        result = parse_request_spec(main_spec(surprise=1))
        assert is_err(result)
        assert result.error == HttpConnectorError.SPEC_INVALID

    @pytest.mark.parametrize("method", ["DELETE", "PUT", "get", ""])
    def test_method_outside_enum_is_rejected(self, method: str) -> None:
        assert is_err(parse_request_spec(main_spec(method=method)))

    def test_get_with_body_is_rejected(self) -> None:
        result = parse_request_spec(main_spec(body={"q": 1}))
        assert is_err(result)
        assert result.error == HttpConnectorError.SPEC_INVALID

    def test_records_path_requires_columns(self) -> None:
        spec = {"method": "GET", "path": "/api/data", "records_path": "rows"}
        assert is_err(parse_request_spec(json.dumps(spec)))

    def test_records_path_and_count_path_are_exclusive(self) -> None:
        result = parse_request_spec(main_spec(count_path="total"))
        assert is_err(result)

    def test_spec_without_projection_is_rejected(self) -> None:
        spec = {"method": "GET", "path": "/api/data"}
        assert is_err(parse_request_spec(json.dumps(spec)))

    @pytest.mark.parametrize(
        "path", [".a", "a..b", "a.", "a.*", "a[0].b", "", "a b"]
    )
    def test_bad_dot_path_is_rejected(self, path: str) -> None:
        assert is_err(parse_request_spec(main_spec(records_path=path))), path

    def test_duplicate_columns_are_rejected(self) -> None:
        result = parse_request_spec(main_spec(columns=["a", "a"]))
        assert is_err(result)

    def test_empty_columns_are_rejected(self) -> None:
        assert is_err(parse_request_spec(main_spec(columns=[])))
        assert is_err(parse_request_spec(main_spec(columns=[""])))

    def test_query_values_must_be_scalars(self) -> None:
        assert is_ok(parse_request_spec(main_spec(query={"page": 1, "q": "x"})))
        assert is_err(parse_request_spec(main_spec(query={"filter": {"a": 1}})))
        assert is_err(parse_request_spec(main_spec(query={"a": "x\ny"})))


# ---------------------------------------------------------------------------
# URL canonicalization と allowlist 照合
# ---------------------------------------------------------------------------


def url_of(path: str, *, method: str = "GET", query: dict | None = None):
    spec_result = parse_request_spec(
        json.dumps(
            {
                "method": method,
                "path": path,
                "records_path": "rows",
                "columns": ["a"],
                **({"query": query} if query else {}),
            }
        )
    )
    if is_err(spec_result):
        return spec_result
    return build_request_url(make_config(), spec_result.value)


class TestUrlCanonicalization:
    def test_exact_prefix_match_is_allowed(self) -> None:
        result = url_of("/api/data")
        assert is_ok(result)
        assert result.value == "https://api.example.com/api/data"

    def test_prefix_matches_at_segment_boundary(self) -> None:
        assert is_ok(url_of("/api/data/42"))

    def test_prefix_does_not_match_mid_segment(self) -> None:
        """/api/data は /api/database に一致しない（segment 境界照合）。"""
        result = url_of("/api/database")
        assert is_err(result)
        assert result.error == HttpConnectorError.ENDPOINT_NOT_ALLOWED

    def test_method_must_match_allowlist_entry(self) -> None:
        result = url_of("/api/data", method="POST")
        assert is_err(result)
        assert result.error == HttpConnectorError.ENDPOINT_NOT_ALLOWED

    def test_dot_segments_are_resolved_before_matching(self) -> None:
        result = url_of("/api/x/../data/42")
        assert is_ok(result)
        assert result.value == "https://api.example.com/api/data/42"

    def test_dot_segment_escape_above_root_is_rejected(self) -> None:
        assert is_err(url_of("/../etc/passwd"))

    def test_traversal_out_of_allowed_prefix_is_rejected(self) -> None:
        result = url_of("/api/data/../../admin")
        assert is_err(result)
        assert result.error == HttpConnectorError.ENDPOINT_NOT_ALLOWED

    @pytest.mark.parametrize(
        "path",
        [
            "/api/data/%ff",  # 不正 UTF-8（fail closed。U+FFFD 置換で通さない）
            "/api/data/%c0%af",  # overlong / 不正 UTF-8
            "/api//data",  # 空 segment
            "/api\\data",  # backslash
            "/api/data%2Fx",  # encoded slash（decode 前拒否）
            "/api/data%2fx",
            "/api/data%5Cx",  # encoded backslash
            "/api/%2e%2e/admin",  # encoded dot-dot
            "/api/%2E%2E/admin",
            "/api/data%00",  # encoded NUL
            "/api/data%0d%0a",  # encoded CRLF
            "/api/data%252e",  # 二重 encoding
            "/api/data%zz",  # 不正 encoding
            "/api/data x",  # raw space
            "//evil.example.com/api",  # scheme-relative
            "https://evil.example.com/api",  # 絶対 URL
            "/api/data#frag",  # fragment
            "/api/data?x=1",  # query は spec.query でのみ渡す
        ],
    )
    def test_dangerous_paths_are_rejected(self, path: str) -> None:
        assert is_err(url_of(path)), path

    def test_at_sign_in_segment_is_safe(self) -> None:
        """'@' は path 文字として合法 — origin は base_url 固定なので
        authority 混同は起きない（通過を固定）。"""
        result = url_of("/api/data/x@evil")
        assert is_ok(result)
        assert result.value == "https://api.example.com/api/data/x@evil"

    def test_query_is_encoded_from_spec_mapping_only(self) -> None:
        result = url_of("/api/data", query={"page": 2, "q": "a b"})
        assert is_ok(result)
        assert result.value == "https://api.example.com/api/data?page=2&q=a+b"

    def test_percent_in_query_value_is_encoded_not_interpreted(self) -> None:
        result = url_of("/api/data", query={"q": "50%"})
        assert is_ok(result)
        assert result.value.endswith("q=50%25")


# ---------------------------------------------------------------------------
# 実行（fake transport）
# ---------------------------------------------------------------------------


class TestExecuteStream:
    def test_rows_are_projected_in_column_order(self) -> None:
        transport = FakeTransport(FakeTransportResponse(body=ok_body()))
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_ok(result), getattr(result, "detail", None)
        with result.value as stream:
            assert stream.columns == ("user_id", "email")
            assert list(stream) == [
                (1, "a@example.com"),
                (2, "b@example.com"),
            ]

    def test_array_records_are_accepted(self) -> None:
        body = json.dumps({"result": {"rows": [[1, "a@x"], [2, "b@x"]]}}).encode()
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        with connector.execute_stream(main_spec()).value as stream:
            assert list(stream) == [(1, "a@x"), (2, "b@x")]

    def test_auth_and_identity_headers_are_sent(self) -> None:
        transport = FakeTransport(FakeTransportResponse(body=ok_body()))
        connector = make_connector(transport)
        connector.execute_stream(main_spec())
        request = transport.requests[0]
        headers = dict(request.headers)
        assert headers["Authorization"] == "Key hunter2"
        assert headers["Accept-Encoding"] == "identity"
        assert request.method == "GET"
        assert request.body is None

    def test_post_body_is_json_encoded(self) -> None:
        transport = FakeTransport(FakeTransportResponse(body=ok_body()))
        connector = make_connector(transport)
        spec = main_spec(method="POST", path="/api/queries/42", body={"max_age": 0})
        result = connector.execute_stream(spec)
        assert is_ok(result)
        request = transport.requests[0]
        assert request.method == "POST"
        assert json.loads(request.body.decode("utf-8")) == {"max_age": 0}
        assert dict(request.headers)["Content-Type"] == "application/json"

    def test_count_path_yields_single_int_row(self) -> None:
        transport = FakeTransport(FakeTransportResponse(body=ok_body(total=42)))
        connector = make_connector(transport)
        result = connector.execute_stream(count_spec())
        assert is_ok(result)
        with result.value as stream:
            assert stream.columns == ("count",)
            assert list(stream) == [(42,)]

    def test_count_path_non_integer_is_rejected(self) -> None:
        transport = FakeTransport(FakeTransportResponse(body=ok_body(total="many")))
        connector = make_connector(transport)
        result = connector.execute_stream(count_spec())
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_INVALID

    def test_disallowed_endpoint_never_reaches_transport(self) -> None:
        transport = FakeTransport(FakeTransportResponse(body=ok_body()))
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec(path="/admin/keys"))
        assert is_err(result)
        assert result.error == HttpConnectorError.ENDPOINT_NOT_ALLOWED
        assert transport.requests == []

    def test_expired_deadline_never_reaches_transport(self) -> None:
        transport = FakeTransport(FakeTransportResponse(body=ok_body()))
        connector = make_connector(transport, deadline=10.0, monotonic=lambda: 10.0)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == ToolConnectorError.DEADLINE_EXCEEDED
        assert transport.requests == []

    def test_credential_with_crlf_is_rejected_before_transport(self) -> None:
        """認証ヘッダ注入（header splitting）を送信前に止める。値は detail に出さない。"""
        transport = FakeTransport(FakeTransportResponse(body=ok_body()))
        connector = HttpConnector(
            config=make_config(),
            credential="secret\r\nX-Evil: 1",
            max_response_bytes=1048576,
            deadline_monotonic=30.0,
            monotonic=lambda: 0.0,
            transport=transport,
        )
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == ToolConnectorError.CONNECT_FAILED
        assert transport.requests == []
        assert "secret" not in result.detail

    def test_max_response_bytes_bounds_allocation_to_limit(self) -> None:
        """上限が小さいとき chunk 全量ではなく残容量+1 までしか確保しない。"""
        response = FakeTransportResponse(body=b"x" * 100_000)
        transport = FakeTransport(response)
        connector = make_connector(transport, max_response_bytes=1024)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_TOO_LARGE
        # READ_CHUNK_BYTES(64KiB) ではなく上限 +1 バイト程度しか読まない
        assert response.bytes_read <= 1024 + 1


class TestResponseGuards:
    def test_redirect_is_rejected(self) -> None:
        transport = FakeTransport(
            FakeTransportResponse(
                status=302, headers={"Location": "https://evil.example.com"}
            )
        )
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_INVALID
        assert "redirect" in result.detail

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, ToolConnectorError.NOT_AUTHORIZED),
            (403, ToolConnectorError.NOT_AUTHORIZED),
            (500, ToolConnectorError.EXECUTION_FAILED),
            (404, ToolConnectorError.EXECUTION_FAILED),
        ],
    )
    def test_error_statuses_are_classified(self, status, expected) -> None:
        transport = FakeTransport(FakeTransportResponse(status=status, body=b"{}"))
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == expected

    def test_compressed_response_is_rejected(self) -> None:
        """identity を要求してもサーバが圧縮を返したら Content-Encoding で拒否。"""
        transport = FakeTransport(
            FakeTransportResponse(
                body=b"\x1f\x8b...", headers={"Content-Encoding": "gzip"}
            )
        )
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_INVALID

    def test_non_json_content_type_is_rejected(self) -> None:
        transport = FakeTransport(
            FakeTransportResponse(
                body=ok_body(), headers={"Content-Type": "text/html"}
            )
        )
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_INVALID

    def test_non_utf8_charset_is_rejected(self) -> None:
        transport = FakeTransport(
            FakeTransportResponse(
                body=ok_body(),
                headers={"Content-Type": "application/json; charset=shift_jis"},
            )
        )
        connector = make_connector(transport)
        assert is_err(connector.execute_stream(main_spec()))

    def test_declared_content_length_over_limit_rejects_before_read(self) -> None:
        response = FakeTransportResponse(
            body=b"x" * 100, headers={"Content-Length": "99999999"}
        )
        transport = FakeTransport(response)
        connector = make_connector(transport, max_response_bytes=1024)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_TOO_LARGE
        assert response.bytes_read == 0

    def test_oversized_body_is_cut_off_during_chunked_read(self) -> None:
        """Content-Length なし（chunked 相当）でも読みながら上限で遮断する。"""
        response = FakeTransportResponse(body=b"x" * 100_000)
        transport = FakeTransport(response)
        connector = make_connector(transport, max_response_bytes=1024)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_TOO_LARGE
        assert response.bytes_read < 100_000  # 全量を読み切る前に遮断

    def test_content_length_mismatch_is_rejected(self) -> None:
        response = FakeTransportResponse(
            body=b'{"result": {"rows": []}}', headers={"Content-Length": "9999"}
        )
        transport = FakeTransport(response)
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_INVALID

    def test_transport_timeout_is_deadline_exceeded(self) -> None:
        transport = FakeTransport(error=TransportError("timeout", "read timed out"))
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == ToolConnectorError.DEADLINE_EXCEEDED

    def test_transport_connect_error_is_sanitized(self) -> None:
        transport = FakeTransport(
            error=TransportError("connect", "dns fail key=hunter2")
        )
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == ToolConnectorError.CONNECT_FAILED
        assert "hunter2" not in result.detail


class TestRecordsPathResolution:
    def test_missing_segment_reports_progress_and_types(self) -> None:
        body = json.dumps({"result": {"items": []}}).encode()
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_INVALID
        # 構造化エラー: 解決できた segment・期待型・実際型
        assert "result" in result.detail
        assert "rows" in result.detail
        assert "hunter2" not in result.detail

    def test_non_array_records_reports_actual_type(self) -> None:
        body = json.dumps({"result": {"rows": {"oops": 1}}}).encode()
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert "object" in result.detail or "dict" in result.detail

    def test_missing_column_key_is_rejected(self) -> None:
        body = ok_body(rows=[{"user_id": 1}])  # email 欠落
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert "email" in result.detail

    def test_nested_object_value_is_rejected_as_type_deviation(self) -> None:
        body = ok_body(rows=[{"user_id": 1, "email": {"nested": True}}])
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        result = connector.execute_stream(main_spec())
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_INVALID

    def test_bool_is_normalized_to_int(self) -> None:
        body = ok_body(rows=[{"user_id": True, "email": "x"}])
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        with connector.execute_stream(main_spec()).value as stream:
            assert list(stream) == [(1, "x")]

    def test_deeply_nested_records_path_resolves(self) -> None:
        deep: dict = {"leaf": []}
        path_parts = [f"level{i}" for i in range(40)]
        node: dict = deep
        for part in reversed(path_parts):
            node = {part: node}
        body = json.dumps(node).encode()
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        spec = main_spec(records_path=".".join(path_parts) + ".leaf")
        result = connector.execute_stream(spec)
        assert is_ok(result), getattr(result, "detail", None)
        with result.value as stream:
            assert list(stream) == []


# ---------------------------------------------------------------------------
# Redash / Kibana(ES) 代表レスポンス fixture
# ---------------------------------------------------------------------------


class TestRepresentativeFixtures:
    def test_redash_query_result_shape(self) -> None:
        body = json.dumps(
            {
                "query_result": {
                    "id": 99,
                    "data": {
                        "rows": [
                            {"user_id": 1, "email": "a@example.com"},
                            {"user_id": 3, "email": "c@example.com"},
                        ],
                        "columns": [{"name": "user_id"}, {"name": "email"}],
                    },
                }
            }
        ).encode()
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        spec = json.dumps(
            {
                "method": "POST",
                "path": "/api/queries/42",
                "body": {"max_age": 0},
                "records_path": "query_result.data.rows",
                "columns": ["user_id", "email"],
            }
        )
        result = connector.execute_stream(spec)
        assert is_ok(result), getattr(result, "detail", None)
        with result.value as stream:
            assert list(stream) == [(1, "a@example.com"), (3, "c@example.com")]

    def test_es_search_hits_project_scalar_fields(self) -> None:
        body = json.dumps(
            {
                "hits": {
                    "total": {"value": 2},
                    "hits": [
                        {"_id": "a1", "_score": 1.5, "_source": {"name": "x"}},
                        {"_id": "b2", "_score": 0.5, "_source": {"name": "y"}},
                    ],
                }
            }
        ).encode()
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        spec = main_spec(records_path="hits.hits", columns=["_id", "_score"])
        with connector.execute_stream(spec).value as stream:
            assert list(stream) == [("a1", 1.5), ("b2", 0.5)]

    def test_es_nested_source_projection_is_unsupported_with_clear_error(
        self,
    ) -> None:
        """_source（object）を column に指定した場合は型逸脱として明確に拒否
        — nested 投影は保証範囲外（guide 参照）。"""
        body = json.dumps(
            {"hits": {"hits": [{"_id": "a1", "_source": {"name": "x"}}]}}
        ).encode()
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        spec = main_spec(records_path="hits.hits", columns=["_id", "_source"])
        result = connector.execute_stream(spec)
        assert is_err(result)
        assert result.error == HttpConnectorError.RESPONSE_INVALID
        assert "_source" in result.detail

    def test_es_count_endpoint(self) -> None:
        body = json.dumps({"count": 123, "_shards": {"total": 1}}).encode()
        transport = FakeTransport(FakeTransportResponse(body=body))
        connector = make_connector(transport)
        spec = count_spec(count_path="count")
        with connector.execute_stream(spec).value as stream:
            assert list(stream) == [(123,)]


# ---------------------------------------------------------------------------
# schema-of-record（tool-request-spec-schema.json）との同期
# ---------------------------------------------------------------------------


class TestRequestSpecSchemaSync:
    @pytest.fixture()
    def schema(self) -> dict:
        from pathlib import Path

        schema_path = (
            Path(__file__).resolve().parents[5]
            / ".wiki"
            / "schema"
            / "tool-request-spec-schema.json"
        )
        return json.loads(schema_path.read_text(encoding="utf-8"))

    def test_full_schema_structure_matches_validator_constants(
        self, schema: dict
    ) -> None:
        from lib.service.tool_catalog import HTTP_METHODS
        from lib.service.tool_connector_http import (
            DOT_PATH_PATTERN,
            SPEC_OPTIONAL,
            SPEC_REQUIRED,
        )

        def strip_docs(obj):
            if isinstance(obj, dict):
                return {
                    k: strip_docs(v)
                    for k, v in obj.items()
                    if k not in ("description", "title", "$schema")
                }
            if isinstance(obj, list):
                return [strip_docs(v) for v in obj]
            return obj

        expected = {
            "type": "object",
            "additionalProperties": False,
            "required": list(SPEC_REQUIRED),
            "properties": {
                "method": {"enum": list(HTTP_METHODS)},
                "path": {"type": "string", "pattern": "^/"},
                "query": {
                    "type": "object",
                    "additionalProperties": {"type": ["string", "integer"]},
                },
                "body": {"type": "object"},
                "records_path": {"type": "string", "pattern": DOT_PATH_PATTERN},
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "count_path": {"type": "string", "pattern": DOT_PATH_PATTERN},
            },
            "oneOf": [
                {"required": ["records_path", "columns"]},
                {"required": ["count_path"]},
            ],
        }
        assert strip_docs(schema) == expected
        assert set(SPEC_REQUIRED) | set(SPEC_OPTIONAL) == set(
            schema["properties"]
        )
