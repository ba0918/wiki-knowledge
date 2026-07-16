"""HttpConnector — 汎用 HTTP API connector（urllib、one-shot JSON API 専用）.

SQL ファイルの代わりに **request spec**（JSON）を実行単位とする。digest
binding・bundle 保存・一度読み bytes 一貫は SQL と同じ経路で効く（runner は
テキストの中身に依存しない）。

enforcement の設計（計画「HttpConnector の実行モデル」節）:

* **request spec は schema 検証**（未知キー拒否。schema-of-record は
  ``{wiki_root}/schema/tool-request-spec-schema.json``、同期は
  test_tool_request_spec_schema.py が機械検証）
* **URL canonicalization は一度だけ** — decode 前に encoded separator
  （``%2f`` / ``%5c`` / ``%2e%2e`` / NUL・control の encoding）を拒否し、
  percent-encoding を正規化（二重・不正 encoding は fail closed）、``.``/``..``
  segment を解決、``//`` と backslash を拒否。照合は最終 URL に対して
  origin 完全一致 + **segment 境界**の path prefix + メソッド一致
* リダイレクトは拒否（allowlist 迂回防止）
* ``Accept-Encoding: identity`` 固定送信 + Content-Encoding 検査 — wire
  バイト数 = 実体サイズとなり、``max_response_bytes`` の streaming 遮断が
  実効になる。読み込みは chunk 単位で検査し、超過時点で切断
* **メモリモデル**: JSON parse 後は document 全体 + 正規化後 rows が同時に
  メモリへ載る（設計上の保証範囲 — max_response_bytes を予算に合わせ小さく
  設定する。既定推奨 8 MiB、根拠は guide）
* credential はヘッダ注入のみ。エラー detail・ログのどこにも出さない

保証範囲外（Non-Goals）: 非同期 job / polling、streaming JSON parse、
レスポンス DSL（Redash / ES クエリ内容）の静的検証。
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator, Protocol

from lib.domain.types import Err, Ok, is_err
from lib.service.tool_catalog import HttpConnectionConfig
from lib.service.tool_connector import Row, ToolConnectorError


# schema-of-record（tool-request-spec-schema.json）と同期。
SPEC_REQUIRED = ("method", "path")
SPEC_OPTIONAL = ("query", "body", "records_path", "columns", "count_path")
DOT_PATH_PATTERN = r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$"

_DOT_PATH_RE = re.compile(DOT_PATH_PATTERN)
# %00-%1f / %7f の encoding（decode すると制御文字になるもの）
_ENCODED_CONTROL_RE = re.compile(r"%(?:[01][0-9A-Fa-f]|7[Ff])")
_INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")

READ_CHUNK_BYTES = 65536

# RFC 3986 pchar のうち quote が既定でエスケープする文字を safe に残す
_SEGMENT_SAFE = "@:!$&'()*+,;="


class HttpConnectorError(str, Enum):
    """Discriminator for HTTP connector failures（監査 reason にもそのまま使う）。"""

    SPEC_INVALID = "http_spec_invalid"
    ENDPOINT_NOT_ALLOWED = "http_endpoint_not_allowed"
    RESPONSE_TOO_LARGE = "http_response_too_large"
    RESPONSE_INVALID = "http_response_invalid"


@dataclass(frozen=True)
class RequestSpec:
    method: str
    path: str
    query: tuple[tuple[str, object], ...]
    body: dict | None
    records_path: tuple[str, ...] | None
    columns: tuple[str, ...] | None
    count_path: tuple[str, ...] | None


def _spec_error(detail: str) -> Err[HttpConnectorError]:
    return Err(error=HttpConnectorError.SPEC_INVALID, detail=detail)


def _has_control_char(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def parse_request_spec(text: str) -> Ok[RequestSpec] | Err[HttpConnectorError]:
    """request spec JSON を厳格検証する（未知キー拒否・fail closed）。"""

    from lib.service.tool_catalog import HTTP_METHODS

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _spec_error(f"JSON として読めません: {exc}")
    if not isinstance(data, dict):
        return _spec_error("request spec はオブジェクトが必要")

    for field in SPEC_REQUIRED:
        if field not in data:
            return _spec_error(f"必須フィールド欠損: {field}")
    known = set(SPEC_REQUIRED) | set(SPEC_OPTIONAL)
    for key in data:
        if key not in known:
            return _spec_error(f"未知のキー: {key}")

    method = data["method"]
    if method not in HTTP_METHODS:
        return _spec_error(f"method は {list(HTTP_METHODS)} のいずれか: {method!r}")

    path = data["path"]
    if not isinstance(path, str) or not path:
        return _spec_error("path が非空文字列ではない")

    query_raw = data.get("query", {})
    if not isinstance(query_raw, dict):
        return _spec_error("query はオブジェクトが必要")
    query: list[tuple[str, object]] = []
    for key, value in query_raw.items():
        if not isinstance(key, str) or not key or _has_control_char(key):
            return _spec_error(f"query のキーが不正: {key!r}")
        if type(value) is str:
            if _has_control_char(value):
                return _spec_error(f"query 値に制御文字: key={key!r}")
        elif type(value) is not int:  # bool は type 不一致で除外される
            return _spec_error(f"query 値は文字列か整数が必要: key={key!r}")
        query.append((key, value))

    body = data.get("body")
    if body is not None:
        if not isinstance(body, dict):
            return _spec_error("body はオブジェクトが必要")
        if method != "POST":
            return _spec_error("body は POST でのみ使用できる")

    has_records = "records_path" in data or "columns" in data
    has_count = "count_path" in data
    if has_records and has_count:
        return _spec_error("records_path/columns と count_path は排他")
    if not has_records and not has_count:
        return _spec_error("records_path + columns または count_path が必要")

    records_path: tuple[str, ...] | None = None
    columns: tuple[str, ...] | None = None
    count_path: tuple[str, ...] | None = None

    if has_records:
        if "records_path" not in data or "columns" not in data:
            return _spec_error("records_path と columns は対で必要")
        rp = data["records_path"]
        if not isinstance(rp, str) or not _DOT_PATH_RE.fullmatch(rp):
            return _spec_error(f"records_path が dot-path 形式ではない: {rp!r}")
        records_path = tuple(rp.split("."))
        cols = data["columns"]
        if (
            not isinstance(cols, list)
            or not cols
            or not all(
                isinstance(c, str) and c and not _has_control_char(c) for c in cols
            )
        ):
            return _spec_error("columns は非空文字列の非空配列が必要")
        if len(set(cols)) != len(cols):
            return _spec_error("columns に重複がある")
        columns = tuple(cols)
    else:
        cp = data["count_path"]
        if not isinstance(cp, str) or not _DOT_PATH_RE.fullmatch(cp):
            return _spec_error(f"count_path が dot-path 形式ではない: {cp!r}")
        count_path = tuple(cp.split("."))

    return Ok(
        value=RequestSpec(
            method=method,
            path=path,
            query=tuple(query),
            body=body,
            records_path=records_path,
            columns=columns,
            count_path=count_path,
        )
    )


# ---------------------------------------------------------------------------
# URL canonicalization + allowlist 照合
# ---------------------------------------------------------------------------


def _canonicalize_segments(path: str) -> Ok[tuple[str, ...]] | Err[HttpConnectorError]:
    """path を decode 済み segment 列に正規化する（一度だけ・fail closed）。"""

    if not path.startswith("/"):
        return _spec_error("path は / 始まりの相対パスのみ（絶対 URL 不可）")
    if "?" in path or "#" in path:
        return _spec_error("path に query / fragment は書けない（query は spec.query）")
    if "\\" in path:
        return _spec_error("path に backslash は不可")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in path):
        return _spec_error("path に制御文字・空白は不可")

    lower = path.lower()
    # decode 前の文字単位 encoded separator 規則（計画で固定された順序）
    if "%2f" in lower or "%5c" in lower:
        return _spec_error("encoded separator（%2f / %5c）は decode せず拒否")
    if "%2e%2e" in lower:
        return _spec_error("encoded dot-dot（%2e%2e）は decode せず拒否")
    if _ENCODED_CONTROL_RE.search(path):
        return _spec_error("NUL / 制御文字の encoding は decode せず拒否")
    if _INVALID_PERCENT_RE.search(path):
        return _spec_error("不正な percent-encoding（fail closed）")

    # unquote の既定 errors="replace" は不正 UTF-8 を U+FFFD へ置換し別 URL
    # として通してしまう。bytes へ decode してから厳格 UTF-8 変換し、曖昧・
    # 不正なバイト列は fail closed で拒否する
    decoded_bytes = urllib.parse.unquote_to_bytes(path)
    try:
        decoded = decoded_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _spec_error("不正な UTF-8 percent-encoding（fail closed）")
    if "\x00" in decoded:
        return _spec_error("NUL を含む path（fail closed）")
    if "%" in decoded:
        return _spec_error("二重 percent-encoding（fail closed）")
    if "\\" in decoded or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in decoded):
        return _spec_error("decode 後に separator / 制御文字が出現")

    stack: list[str] = []
    for segment in decoded.split("/")[1:]:
        if segment == "":
            return _spec_error("空 segment（// または末尾 /）は不可")
        if segment == ".":
            continue
        if segment == "..":
            if not stack:
                return _spec_error("`..` が root を越える")
            stack.pop()
            continue
        stack.append(segment)
    return Ok(value=tuple(stack))


def build_request_url(
    config: HttpConnectionConfig, spec: RequestSpec
) -> Ok[str] | Err[HttpConnectorError]:
    """canonicalize 済み最終 URL を構築し、allowlist と照合する。

    origin は常に catalog の base_url（spec からは path しか採らない）。
    """

    segments_result = _canonicalize_segments(spec.path)
    if is_err(segments_result):
        return segments_result
    segments = segments_result.value

    matched = False
    for rule in config.allowed_endpoints:
        if rule.method != spec.method:
            continue
        prefix_segments = tuple(rule.path_prefix.split("/")[1:])
        if segments[: len(prefix_segments)] == prefix_segments:
            matched = True
            break
    if not matched:
        return Err(
            error=HttpConnectorError.ENDPOINT_NOT_ALLOWED,
            detail=f"allowlist 外の endpoint: {spec.method} /{'/'.join(segments)}",
        )

    canonical_path = "/" + "/".join(
        urllib.parse.quote(seg, safe=_SEGMENT_SAFE) for seg in segments
    )
    url = config.base_url.rstrip("/") + canonical_path
    if spec.query:
        url += "?" + urllib.parse.urlencode(list(spec.query))
    return Ok(value=url)


# ---------------------------------------------------------------------------
# transport 抽象（urllib 実装 + fake）
# ---------------------------------------------------------------------------


class TransportError(Exception):
    """ネットワーク層の失敗。kind: ``timeout`` / ``connect`` / ``protocol``。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind


@dataclass(frozen=True)
class HttpRequest:
    url: str
    method: str
    headers: tuple[tuple[str, str], ...]
    body: bytes | None


class TransportResponse(Protocol):
    status: int

    def header(self, name: str) -> str | None: ...  # pragma: no cover - protocol

    def read_chunk(self, n: int) -> bytes: ...  # pragma: no cover - protocol

    def close(self) -> None: ...  # pragma: no cover - protocol


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 追跡しない — 3xx は HTTPError として上がり、呼び出し側が拒否する
        return None


class _UrllibResponse:
    def __init__(
        self,
        raw,
        *,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._raw = raw
        self.status = getattr(raw, "status", None) or raw.code
        self._deadline = deadline_monotonic
        self._monotonic = monotonic

    def header(self, name: str) -> str | None:
        return self._raw.headers.get(name)

    def _refresh_read_timeout(self) -> None:
        """各 read 前に socket timeout を残時間へ再設定する。

        socket timeout は本来 I/O の無通信時間の上限であり、slow-drip
        （timeout 未満の間隔で少量ずつ送り続ける）に対しては総時間の上限に
        ならない。残時間を毎回 timeout に入れることで全体 deadline を実効的な
        総時間上限にする。socket に到達できない場合は初期 timeout に委ねる。
        """

        if self._deadline is None:
            return
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise TransportError("timeout", "deadline exceeded")
        sock = getattr(getattr(self._raw, "fp", None), "raw", None)
        sock = getattr(sock, "_sock", None)
        if sock is not None:
            try:
                sock.settimeout(remaining)
            except OSError:
                pass

    def read_chunk(self, n: int) -> bytes:
        try:
            self._refresh_read_timeout()
            return self._raw.read(n)
        except (TimeoutError, socket.timeout) as exc:
            raise TransportError("timeout", "read timed out") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError("protocol", type(exc).__name__) from exc

    def close(self) -> None:
        try:
            self._raw.close()
        except OSError:
            pass


class UrllibTransport:
    """redirect 非追跡の urllib transport（外部依存なし）。"""

    def send(
        self,
        request: HttpRequest,
        *,
        timeout: float,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> _UrllibResponse:
        try:
            req = urllib.request.Request(
                request.url, data=request.body, method=request.method
            )
            for name, value in request.headers:
                req.add_header(name, value)
        except ValueError as exc:
            # http.client は CR/LF を含むヘッダ値に ValueError を送出し、その
            # メッセージにヘッダ値全体（= credential）を含める。秘密を含まない
            # TransportError へ変換して traceback への露出を断つ（from None）
            raise TransportError("protocol", "不正なヘッダ値") from None
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            raw = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raw = exc  # 非 2xx / 3xx はレスポンスとして扱う（status で分類）
        except ValueError as exc:
            # 送信段でのヘッダ値検証エラー等（credential を含み得る）を遮断
            raise TransportError("protocol", "不正な要求") from None
        except (TimeoutError, socket.timeout) as exc:
            raise TransportError("timeout", "request timed out") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TransportError("timeout", "request timed out") from exc
            raise TransportError("connect", type(reason).__name__) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError("connect", type(exc).__name__) from exc
        return _UrllibResponse(
            raw, deadline_monotonic=deadline_monotonic, monotonic=monotonic
        )


# ---------------------------------------------------------------------------
# HttpConnector
# ---------------------------------------------------------------------------


def _json_type_name(value: object) -> str:
    if value is None:
        return "null"
    return {
        bool: "boolean",
        int: "number",
        float: "number",
        str: "string",
        list: "array",
        dict: "object",
    }.get(type(value), type(value).__name__)


def _resolve_dot_path(
    document: object, path: tuple[str, ...], *, label: str
) -> Ok[object] | Err[HttpConnectorError]:
    """dot-path を解決する。エラーは「どこまで解決できたか・期待型・実際型」を
    構造化して返す（レスポンス値そのものは detail に載せない）。"""

    node = document
    resolved: list[str] = []
    for segment in path:
        where = ".".join(resolved) or "(root)"
        if not isinstance(node, dict):
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail=(
                    f"{label} 解決エラー: {where} まで解決 / "
                    f"segment {segment!r} には object が必要 / "
                    f"実際: {_json_type_name(node)}"
                ),
            )
        if segment not in node:
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail=(
                    f"{label} 解決エラー: {where} まで解決 / "
                    f"segment {segment!r} が存在しない"
                ),
            )
        node = node[segment]
        resolved.append(segment)
    return Ok(value=node)


def _normalize_json_value(value: object) -> Ok[object] | Err[HttpConnectorError]:
    if value is None or type(value) in (int, float, str):
        return Ok(value=value)
    if isinstance(value, bool):
        return Ok(value=int(value))
    if isinstance(value, int):
        return Ok(value=int(value))
    return Err(
        error=HttpConnectorError.RESPONSE_INVALID,
        detail=f"値型逸脱: {_json_type_name(value)} は Connector 契約外",
    )


class _HttpRowStream:
    """materialized rows の RowStream（HTTP は one-shot 取得のため遅延しない）。"""

    def __init__(self, columns: tuple[str, ...], rows: list[Row]) -> None:
        self._columns = columns
        self._rows = rows
        self._closed = False

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    @property
    def closed(self) -> bool:
        return self._closed

    def __iter__(self) -> Iterator[Row]:
        yield from self._rows

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "_HttpRowStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class HttpConnector:
    """request spec を実行する Connector 実装（Connector protocol 準拠）。"""

    def __init__(
        self,
        *,
        config: HttpConnectionConfig,
        credential: str,
        max_response_bytes: int,
        deadline_monotonic: float,
        monotonic: Callable[[], float] = time.monotonic,
        transport=None,
    ) -> None:
        self._config = config
        self._credential = credential
        self._max_response_bytes = max_response_bytes
        self._deadline = deadline_monotonic
        self._monotonic = monotonic
        self._transport = transport if transport is not None else UrllibTransport()

    def execute_stream(self, text: str) -> Ok[_HttpRowStream] | Err:
        spec_result = parse_request_spec(text)
        if is_err(spec_result):
            return spec_result
        spec = spec_result.value

        # credential に CR/LF・制御文字が混ざると認証ヘッダ注入（header
        # splitting）になり、urllib/http.client が値ごと ValueError に載せて
        # 露出する。ヘッダ組み立て前に接続を止める（値は detail に出さない）
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in self._credential):
            return Err(
                error=ToolConnectorError.CONNECT_FAILED,
                detail="credential に制御文字が含まれています（ヘッダ注入防止）",
            )

        url_result = build_request_url(self._config, spec)
        if is_err(url_result):
            return url_result
        url = url_result.value

        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            return Err(
                error=ToolConnectorError.DEADLINE_EXCEEDED,
                detail="deadline を超過しているため送信しません",
            )

        headers: list[tuple[str, str]] = [
            (
                self._config.auth_header_name,
                self._config.auth_header_template.replace(
                    "{credential}", self._credential
                ),
            ),
            ("Accept", "application/json"),
            ("Accept-Encoding", "identity"),
        ]
        body: bytes | None = None
        if spec.body is not None:
            body = json.dumps(spec.body, ensure_ascii=False).encode("utf-8")
            headers.append(("Content-Type", "application/json"))

        request = HttpRequest(
            url=url, method=spec.method, headers=tuple(headers), body=body
        )
        try:
            response = self._transport.send(
                request,
                timeout=remaining,
                deadline_monotonic=self._deadline,
                monotonic=self._monotonic,
            )
        except TransportError as exc:
            return self._transport_error(exc)

        try:
            payload_result = self._read_json(response)
        finally:
            response.close()
        if is_err(payload_result):
            return payload_result
        document = payload_result.value

        if spec.count_path is not None:
            return self._project_count(document, spec)
        return self._project_records(document, spec)

    def close(self) -> None:
        # 接続プールを持たない（one-shot request のみ）
        pass

    # -- 内部 ---------------------------------------------------------------

    def _transport_error(self, exc: TransportError) -> Err:
        if exc.kind == "timeout":
            return Err(
                error=ToolConnectorError.DEADLINE_EXCEEDED,
                detail="HTTP 要求がタイムアウトしました",
            )
        if exc.kind == "connect":
            return Err(
                error=ToolConnectorError.CONNECT_FAILED,
                detail="HTTP 接続に失敗しました（詳細はサーバー側を確認）",
            )
        return Err(
            error=ToolConnectorError.EXECUTION_FAILED,
            detail="HTTP 転送が中断されました",
        )

    def _read_json(self, response) -> Ok[object] | Err:
        status = response.status
        if 300 <= status < 400:
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail=f"redirect（{status}）は許可されていません（allowlist 迂回防止）",
            )
        if status in (401, 403):
            return Err(
                error=ToolConnectorError.NOT_AUTHORIZED,
                detail=f"HTTP {status}: 資格情報のスコープを確認してください",
            )
        if not (200 <= status < 300):
            return Err(
                error=ToolConnectorError.EXECUTION_FAILED,
                detail=f"HTTP {status}",
            )

        encoding = (response.header("Content-Encoding") or "").strip().lower()
        if encoding not in ("", "identity"):
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail=(
                    f"Content-Encoding {encoding!r} は拒否"
                    "（identity を要求している — サイズ遮断の実効性のため）"
                ),
            )

        content_type = response.header("Content-Type")
        if content_type is None:
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail="Content-Type がない応答は拒否（JSON API のみ対応）",
            )
        mime, _, params = content_type.partition(";")
        mime = mime.strip().lower()
        if mime != "application/json" and not mime.endswith("+json"):
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail=f"Content-Type {mime!r} は拒否（JSON API のみ対応）",
            )
        charset_match = re.search(r"charset=([^;\s]+)", params, re.IGNORECASE)
        if charset_match and charset_match.group(1).strip("\"'").lower() not in (
            "utf-8",
            "utf8",
        ):
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail=f"charset {charset_match.group(1)!r} は拒否（UTF-8 のみ）",
            )

        declared_length: int | None = None
        content_length = response.header("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                return Err(
                    error=HttpConnectorError.RESPONSE_INVALID,
                    detail="Content-Length が整数ではない",
                )
            if declared_length > self._max_response_bytes:
                return Err(
                    error=HttpConnectorError.RESPONSE_TOO_LARGE,
                    detail=(
                        f"Content-Length {declared_length} が"
                        f" max_response_bytes を超過（読み込み前に遮断）"
                    ),
                )

        buffer = bytearray()
        while True:
            if self._monotonic() >= self._deadline:
                return Err(
                    error=ToolConnectorError.DEADLINE_EXCEEDED,
                    detail="レスポンス読み込み中に deadline を超過",
                )
            # 残容量 +1 バイトだけ読む。max_response_bytes を厳密なメモリ予算
            # として守る（chunk 全量を buffer に載せてから検査すると、上限が
            # 小さくても READ_CHUNK_BYTES ぶん先に確保してしまう）
            want = min(READ_CHUNK_BYTES, self._max_response_bytes - len(buffer) + 1)
            try:
                chunk = response.read_chunk(want)
            except TransportError as exc:
                return self._transport_error(exc)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > self._max_response_bytes:
                return Err(
                    error=HttpConnectorError.RESPONSE_TOO_LARGE,
                    detail=(
                        f"レスポンスが max_response_bytes="
                        f"{self._max_response_bytes} を超過（全量確保前に切断）"
                    ),
                )

        if declared_length is not None and len(buffer) != declared_length:
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail=(
                    f"Content-Length 不一致: 宣言 {declared_length} /"
                    f" 実際 {len(buffer)}"
                ),
            )

        try:
            return Ok(value=json.loads(bytes(buffer).decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail="レスポンスを JSON として読めません",
            )

    def _project_count(self, document: object, spec: RequestSpec) -> Ok | Err:
        resolved = _resolve_dot_path(document, spec.count_path, label="count_path")
        if is_err(resolved):
            return resolved
        value = resolved.value
        if type(value) is not int or value < 0:
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail=(
                    "count_path は非負整数が必要 / "
                    f"実際: {_json_type_name(value)}"
                ),
            )
        return Ok(value=_HttpRowStream(("count",), [(value,)]))

    def _project_records(self, document: object, spec: RequestSpec) -> Ok | Err:
        resolved = _resolve_dot_path(
            document, spec.records_path, label="records_path"
        )
        if is_err(resolved):
            return resolved
        records = resolved.value
        if not isinstance(records, list):
            return Err(
                error=HttpConnectorError.RESPONSE_INVALID,
                detail=(
                    "records_path は配列を指す必要がある / "
                    f"実際: {_json_type_name(records)}"
                ),
            )
        columns = spec.columns
        rows: list[Row] = []
        for i, record in enumerate(records):
            if isinstance(record, dict):
                cells = []
                for column in columns:
                    if column not in record:
                        return Err(
                            error=HttpConnectorError.RESPONSE_INVALID,
                            detail=f"行 {i}: column {column!r} が record にない",
                        )
                    cells.append(record[column])
            elif isinstance(record, list):
                if len(record) != len(columns):
                    return Err(
                        error=HttpConnectorError.RESPONSE_INVALID,
                        detail=(
                            f"行 {i}: 配列長 {len(record)} が"
                            f" columns 数 {len(columns)} と不一致"
                        ),
                    )
                cells = list(record)
            else:
                return Err(
                    error=HttpConnectorError.RESPONSE_INVALID,
                    detail=(
                        f"行 {i}: record は object か配列が必要 / "
                        f"実際: {_json_type_name(record)}"
                    ),
                )
            normalized = []
            for column, cell in zip(columns, cells):
                value_result = _normalize_json_value(cell)
                if is_err(value_result):
                    return Err(
                        error=HttpConnectorError.RESPONSE_INVALID,
                        detail=f"行 {i} column {column!r}: {value_result.detail}",
                    )
                normalized.append(value_result.value)
            rows.append(tuple(normalized))
        return Ok(value=_HttpRowStream(columns, rows))


# ---------------------------------------------------------------------------
# FakeTransport — service テスト用の決定的 double
# ---------------------------------------------------------------------------


class FakeTransportResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status = status
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        self._headers = {k.lower(): v for k, v in merged.items()}
        self._body = body
        self._pos = 0
        self.bytes_read = 0
        self.closed = False

    def header(self, name: str) -> str | None:
        return self._headers.get(name.lower())

    def read_chunk(self, n: int) -> bytes:
        chunk = self._body[self._pos : self._pos + n]
        self._pos += len(chunk)
        self.bytes_read += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(
        self,
        *responses: FakeTransportResponse,
        error: TransportError | None = None,
    ) -> None:
        self._responses = list(responses)
        self._error = error
        self.requests: list[HttpRequest] = []
        self.timeouts: list[float] = []

    def send(
        self,
        request: HttpRequest,
        *,
        timeout: float,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> FakeTransportResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise AssertionError("FakeTransport: 用意された応答がありません")
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)
