"""Playwright 実行層 — 固定フローコードを capability API 越しに実行し、宣言外
通信を封じ込める.

**設計の分割**（テストの実行ゲート）:

* **ブラウザ非依存の決定的ロジック**（本 module 上部）— AST ゲート・flow pin・
  URL 正規化 + allowlist 照合・janitor・例外 sanitize。playwright を import せず
  常時実行 unit テストで検証する
* **Playwright orchestration**（下部の :class:`BrowserFlowRunner` / :class:`FlowContext`）
  — 実 chromium を要する interception / teardown / E2E。playwright は**遅延 import**
  し、テストは ``BROWSER_EXTRACT_SMOKE`` ゲート下で実行する

honest scoping: in-process Python への構造的封じ込めは達成不能（guide §2）。
担保は (1) catalog の SHA-256 pin（flow_pin_mismatch = 実行拒否）、(2) ロード時
AST 静的ゲート（import / exec / eval / dunder 拒否）、(3) PR レビュー の3層による
**事故防止とレビュー支援**であり、悪意フローへの構造的境界は主張しない。
"""

from __future__ import annotations

import ast
import hashlib
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from lib.domain.types import Err, Ok, is_err
from lib.service.tool_connector_http import _canonicalize_segments


class BrowserReason(str, Enum):
    """browser 系の閉じた reason enum（監査 / CLI / stdout に載る唯一の分類）。

    Playwright 例外の生テキスト（URL / セレクタ / DOM / call log）は runner 境界で
    ここに写像し、一切通さない（http connector の from-None 剥がしと同じ規律）。
    """

    SELECTOR_NOT_FOUND = "selector_not_found"
    UI_DRIFT = "ui_drift"
    SESSION_EXPIRED = "session_expired"
    SESSION_BINDING_MISMATCH = "session_binding_mismatch"
    ORIGIN_BLOCKED = "origin_blocked"
    READBACK_MISMATCH = "readback_mismatch"
    SEAL_MISMATCH = "seal_mismatch"
    FLOW_TIMEOUT = "flow_timeout"
    BUNDLE_CAP_EXCEEDED = "bundle_cap_exceeded"
    FLOW_PIN_MISMATCH = "flow_pin_mismatch"
    FLOW_AST_VIOLATION = "flow_ast_violation"
    INTERNAL_ERROR = "internal_error"


# ===========================================================================
# ブラウザ非依存の決定的ロジック（常時実行）
# ===========================================================================


# ---------------------------------------------------------------------------
# ロード時 AST 静的ゲート
# ---------------------------------------------------------------------------
#
# 許可ノード集合（guide §3 の許可リストと同期。除外は negative test と対）。
# whitelist 方式 — ここに無いノードが 1 つでもあれば flow_ast_violation。

_ALLOWED_AST_NODES = frozenset(
    {
        # モジュール構造
        "Module",
        "FunctionDef",
        "arguments",
        "arg",
        "Load",
        "Store",
        "Del",
        # 文
        "Assign",
        "AnnAssign",
        "AugAssign",
        "Expr",
        "Return",
        "Pass",
        "If",
        "For",
        "While",
        "Break",
        "Continue",
        "With",
        "withitem",
        # 式
        "Call",
        "Attribute",
        "Name",
        "Constant",
        "Compare",
        "BoolOp",
        "UnaryOp",
        "BinOp",
        "Subscript",
        "Slice",
        "IfExp",
        "List",
        "Tuple",
        "Dict",
        "Set",
        "keyword",
        "Starred",
        "comprehension",
        "ListComp",
        "SetComp",
        "DictComp",
        "GeneratorExp",
        "JoinedStr",
        "FormattedValue",
        # 演算子・比較（leaf ノード）
        "And",
        "Or",
        "Not",
        "Add",
        "Sub",
        "Mult",
        "Div",
        "Mod",
        "Pow",
        "LShift",
        "RShift",
        "BitOr",
        "BitXor",
        "BitAnd",
        "FloorDiv",
        "MatMult",
        "USub",
        "UAdd",
        "Invert",
        "Eq",
        "NotEq",
        "Lt",
        "LtE",
        "Gt",
        "GtE",
        "Is",
        "IsNot",
        "In",
        "NotIn",
    }
)

# 名前ベースで callee を拒否する呼び出し（言語機構への脱出経路）
_FORBIDDEN_CALL_NAMES = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "__import__",
        "open",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "input",
        "breakpoint",
        "memoryview",
    }
)


def _ast_violation(detail: str) -> Err[BrowserReason]:
    return Err(error=BrowserReason.FLOW_AST_VIOLATION, detail=detail)


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def check_flow_ast(source: str) -> Ok[None] | Err[BrowserReason]:
    """フローソースをロード前に AST 許可リストで検査する（fail-closed）。

    許可外ノード・import / exec / eval / dunder 属性アクセス・名前ベース禁止呼び出し・
    単一 ``run(ctx, params)`` 以外の関数定義を拒否する。
    """

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return _ast_violation(f"parse できない: {exc.msg}")

    func_defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    if len(func_defs) != 1:
        return _ast_violation("フローは単一の run 関数のみを定義できる")
    run = func_defs[0]
    if run not in tree.body:
        return _ast_violation("run はモジュール直下の関数でなければならない")
    if run.name != "run":
        return _ast_violation(f"関数名は run 固定: {run.name!r}")
    arg_names = [a.arg for a in run.args.args]
    if arg_names != ["ctx", "params"]:
        return _ast_violation("run の引数は (ctx, params) 固定")

    for node in ast.walk(tree):
        type_name = type(node).__name__
        if type_name not in _ALLOWED_AST_NODES:
            return _ast_violation(f"禁止ノード: {type_name}")
        if isinstance(node, ast.Attribute) and _is_dunder(node.attr):
            return _ast_violation(f"dunder 属性アクセス: {node.attr}")
        if isinstance(node, ast.Name) and _is_dunder(node.id):
            return _ast_violation(f"dunder 名: {node.id}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_CALL_NAMES:
                return _ast_violation(f"禁止呼び出し: {node.func.id}")
    return Ok(value=None)


# ---------------------------------------------------------------------------
# flow pin（catalog の SHA-256 と照合）
# ---------------------------------------------------------------------------


def verify_flow_pin(
    source_bytes: bytes, expected_sha256: str
) -> Ok[None] | Err[BrowserReason]:
    """フローファイル bytes の SHA-256 が catalog 宣言と一致するか（実行時 git 非照会）。"""

    actual = hashlib.sha256(source_bytes).hexdigest()
    if actual != expected_sha256:
        return Err(
            error=BrowserReason.FLOW_PIN_MISMATCH,
            detail="flow の SHA-256 が catalog 宣言と一致しない",
        )
    return Ok(value=None)


# ---------------------------------------------------------------------------
# URL 正規化 + allowlist 照合
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonUrl:
    origin: str  # scheme://host[:port]（default port は省略）
    segments: tuple[str, ...]


@dataclass(frozen=True)
class OriginRuleLite:
    """catalog OriginRule + account origin を平坦化した照合単位。"""

    origin: str
    method: str
    path_prefix: str
    resource_type: str


_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize_request_url(url: str) -> Ok[CanonUrl] | Err[BrowserReason]:
    """実行時 URL を照合可能な (origin, segments) に正規化する（一度だけ・fail-closed）。

    * scheme は http(s) のみ（``data:`` / ``blob:`` は interception が発火しない
      ため拒否）
    * userinfo 拒否・IDN/punycode 正規化・末尾ドット除去・port 明示化
    * path は http connector の segment 正規化を流用（encoded separator 拒否・
      二重 encoding fail-closed）
    """

    from urllib.parse import urlsplit

    try:
        split = urlsplit(url)
    except ValueError:
        return Err(error=BrowserReason.ORIGIN_BLOCKED, detail="URL を解析できない")
    if split.scheme not in ("http", "https"):
        return Err(
            error=BrowserReason.ORIGIN_BLOCKED,
            detail=f"許可されない scheme: {split.scheme!r}",
        )
    if split.username is not None or split.password is not None:
        return Err(error=BrowserReason.ORIGIN_BLOCKED, detail="userinfo は不可")
    host = split.hostname
    if not host:
        return Err(error=BrowserReason.ORIGIN_BLOCKED, detail="host が解決できない")
    host = host.rstrip(".").lower()
    try:
        host = unicodedata.normalize("NFC", host).encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return Err(error=BrowserReason.ORIGIN_BLOCKED, detail="host を正規化できない")

    try:
        port = split.port
    except ValueError:
        return Err(error=BrowserReason.ORIGIN_BLOCKED, detail="port が不正")
    origin = f"{split.scheme}://{host}"
    if port is not None and port != _DEFAULT_PORTS.get(split.scheme):
        origin = f"{origin}:{port}"

    segments_result = _canonicalize_segments(split.path or "/")
    if is_err(segments_result):
        return Err(
            error=BrowserReason.ORIGIN_BLOCKED,
            detail="path を正規化できない（encoded separator 等）",
        )
    return Ok(value=CanonUrl(origin=origin, segments=segments_result.value))


def _prefix_segments(path_prefix: str) -> tuple[str, ...]:
    return tuple(s for s in path_prefix.split("/") if s)


def match_allowlist(
    *,
    method: str,
    url: str,
    resource_type: str,
    rules: tuple[OriginRuleLite, ...],
) -> Ok[None] | Err[BrowserReason]:
    """リクエストを allowlist と照合する（origin 完全一致 + method + segment 境界の
    path prefix + resource type）。宣言外は origin_blocked。"""

    canon = canonicalize_request_url(url)
    if is_err(canon):
        return canon
    for rule in rules:
        if rule.method != method or rule.resource_type != resource_type:
            continue
        if rule.origin != canon.value.origin:
            continue
        prefix = _prefix_segments(rule.path_prefix)
        if canon.value.segments[: len(prefix)] == prefix:
            return Ok(value=None)
    return Err(
        error=BrowserReason.ORIGIN_BLOCKED,
        detail=f"allowlist 外: {method} {canon.value.origin}/"
        f"{'/'.join(canon.value.segments)} [{resource_type}]",
    )


# ---------------------------------------------------------------------------
# janitor（保持ポリシー / 異常終了後の回収）
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sweep_bundles(
    plans_root: Path, *, now: str
) -> tuple[list[str], list[str]]:
    """期限切れ / incomplete bundle を回収する。``(removed, failed)`` を返す。

    * ``.staging-*`` や manifest 欠落 bundle は incomplete として回収（SIGKILL・
      再起動・disk full で manifest を書く前に落ちたもの）
    * manifest.json の ``expires_at`` が ``now`` 以下の bundle も回収
    * 削除失敗は failed に積む（呼び出し側が監査して次回再試行）
    """

    removed: list[str] = []
    failed: list[str] = []
    if not plans_root.is_dir():
        return removed, failed

    now_dt = _parse_iso(now)
    for child in sorted(plans_root.iterdir()):
        if not child.is_dir():
            continue
        reap = False
        if child.name.startswith(".staging-"):
            reap = True
        else:
            manifest = child / "manifest.json"
            if not manifest.is_file():
                reap = True
            else:
                try:
                    import json

                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    expires_at = data.get("expires_at")
                    reap = isinstance(expires_at, str) and now_dt >= _parse_iso(
                        expires_at
                    )
                except (OSError, ValueError):
                    reap = True
        if not reap:
            continue
        try:
            shutil.rmtree(child)
            removed.append(child.name)
        except OSError:
            failed.append(child.name)
    return removed, failed


# ---------------------------------------------------------------------------
# 例外 sanitize（runner 境界で全例外を閉じた reason へ写像）
# ---------------------------------------------------------------------------


def sanitize_exception(exc: BaseException) -> BrowserReason:
    """runner 境界を越える全例外を閉じた reason enum に写像する。

    生の例外テキスト（URL / セレクタ / DOM / call log / パラメータ値）は監査・
    stdout・CLI 出力に**一切**通さない — ここで型・名前だけを見て分類する。
    """

    name = type(exc).__name__.lower()
    if "timeout" in name:
        return BrowserReason.FLOW_TIMEOUT
    return BrowserReason.INTERNAL_ERROR


# ===========================================================================
# Playwright orchestration（smoke ゲート下でのみ実行 — playwright は遅延 import）
# ===========================================================================

# chromium 起動引数: WebRTC を無効化（ICE/STUN/data channel は request
# interception を通らない exfil 経路）。remote debugging port は開かない。
CHROMIUM_LAUNCH_ARGS = (
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--force-webrtc-ip-handling-policy",
    "--disable-features=WebRtcHideLocalIpsWithMdns",
)

FIXED_LOCALE = "en-US"
FIXED_TIMEZONE = "UTC"
FIXED_VIEWPORT = {"width": 1280, "height": 900}


@dataclass(frozen=True)
class LaunchProfile:
    """実行ごとの隔離 launch profile（ephemeral user-data-dir・fresh context）。"""

    headless: bool
    user_data_dir: Path
    storage_state: dict | None


@dataclass(frozen=True)
class ExtractionResult:
    """フロー実行の成果（seal-at-prepare の封印対象）。

    CLI の prepare はこれを消費して検証契約を enforce し、成果物 + manifest を
    封印する。CLI は :class:`FlowExtractor` Protocol に依存するため、テストは
    実 chromium なしで fake extractor を注入できる（SQL runner の connector 注入と同型）。
    """

    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    artifact_bytes: bytes  # export ファイル / DOM 抽出の生成物（封印対象）
    readbacks: dict[str, str]
    ui_total: int | None
    account_id: str | None
    screen_fingerprint: str | None
    extracted_at: str


class FlowExtractor:
    """フロー実行のインターフェース（Protocol 相当）。

    実装は :class:`BrowserFlowRunner`（Playwright、smoke）。CLI・テストはこの型で
    受け取り、fake 実装を注入して browser 非依存に prepare を検証する。
    """

    def extract(
        self, *, entry, params: dict, session_state: dict | None, deadline_monotonic: float
    ) -> "Ok[ExtractionResult] | Err[BrowserReason]":  # pragma: no cover - interface
        raise NotImplementedError


class BrowserFlowRunner(FlowExtractor):
    """Playwright 実装（smoke ゲート下でのみ実行）。playwright は extract 内で遅延 import。

    封じ込め（guide §7）: context スコープ ``route('**/*')`` で全リクエストを
    allowlist 照合し宣言外を abort、``route_web_socket`` で全 WS 拒否、
    ``service_workers='block'``、redirect hop 再検証、``data:``/``blob:`` navigation
    拒否、ephemeral user-data-dir + fresh context + WebRTC 無効化。teardown は
    ``finally`` で確実に close、hard timeout / SIGINT は force-kill する。
    """

    def __init__(self, *, wiki_root: Path, monotonic) -> None:
        self._wiki_root = Path(wiki_root)
        self._monotonic = monotonic

    def extract(
        self, *, entry, params: dict, session_state: dict | None, deadline_monotonic: float
    ) -> "Ok[ExtractionResult] | Err[BrowserReason]":
        # 実 chromium 実装は smoke ゲート下で検証する。ここでは封じ込め契約を
        # 組み立てる骨格を示し、生成する成果物は ExtractionResult に封じる。
        try:
            from playwright.sync_api import sync_playwright  # 遅延 import
        except ImportError:
            return Err(
                error=BrowserReason.INTERNAL_ERROR,
                detail="playwright 未インストール（requirements-browser.txt）",
            )

        rules = _rules_from_entry(entry)
        flow = load_flow(
            path=self._wiki_root / "tools" / "flows" / entry.flow.ref,
            expected_sha256=entry.flow.sha256,
        )
        if is_err(flow):
            return flow

        import tempfile

        user_data_dir = Path(tempfile.mkdtemp(prefix="be-udd-"))
        try:
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    headless=True,
                    args=list(CHROMIUM_LAUNCH_ARGS),
                    service_workers="block",
                    locale=FIXED_LOCALE,
                    timezone_id=FIXED_TIMEZONE,
                    viewport=dict(FIXED_VIEWPORT),
                    storage_state=session_state,
                )
                try:
                    _install_interception(context, rules)
                    page = context.new_page()
                    ctx = FlowContext(page=page, entry=entry, params=params)
                    flow.value(ctx, params)
                    return Ok(value=ctx.build_result(self._clock_now()))
                finally:
                    context.close()
        except BaseException as exc:  # noqa: BLE001 - 境界で全例外を sanitize
            return Err(error=sanitize_exception(exc), detail="")
        finally:
            shutil.rmtree(user_data_dir, ignore_errors=True)

    def _clock_now(self) -> str:
        from datetime import timezone

        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rules_from_entry(entry) -> tuple[OriginRuleLite, ...]:
    """catalog entry の origin_allowlist を account.origin で平坦化する。"""

    origin = entry.account.origin.rstrip("/")
    return tuple(
        OriginRuleLite(
            origin=origin,
            method=r.method,
            path_prefix=r.path_prefix,
            resource_type=r.resource_type,
        )
        for r in entry.origin_allowlist
    )


def _install_interception(context, rules: tuple[OriginRuleLite, ...]) -> None:  # pragma: no cover - smoke
    """context スコープの request interception + WebSocket 拒否を設置する。"""

    def _route(route, request) -> None:
        decision = match_allowlist(
            method=request.method,
            url=request.url,
            resource_type=request.resource_type,
            rules=rules,
        )
        if is_err(decision):
            route.abort()
        else:
            route.continue_()

    context.route("**/*", _route)
    # 全 WebSocket を deny-by-default
    try:
        context.route_web_socket("**/*", lambda ws: ws.close())
    except AttributeError:
        pass  # playwright < 1.48（requirements で下限を固定）


class FlowContext:  # pragma: no cover - smoke（capability API は実 page を包む）
    """フローに渡す型付き capability API。raw Playwright を渡さない（guide §3）。

    navigation の origin は常に catalog（named route）から構成し、param→origin 直結を
    構造的に禁止する。セレクタはパラメータの値バインディングでのみ使い、文字列補間しない。
    """

    def __init__(self, *, page, entry, params: dict) -> None:
        self._page = page
        self._entry = entry
        self._params = params
        self._readbacks: dict[str, str] = {}
        self._artifact_bytes = b""
        self._columns: tuple[str, ...] = ()
        self._rows: tuple[tuple, ...] = ()
        self._ui_total: int | None = None

    def goto(self, route_id: str, **path_params) -> None:
        origin = self._entry.account.origin.rstrip("/")
        # origin は常に catalog、path は canonicalize 済み segment のみ
        path = "/" + route_id.strip("/")
        for key, value in path_params.items():
            path = path + "/" + str(value)
        self._page.goto(origin + path, wait_until="networkidle")

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        return self._page.get_by_role(role, name=name, exact=exact)

    def get_by_label(self, text: str):
        return self._page.get_by_label(text)

    def get_by_text(self, text: str):
        return self._page.get_by_text(text)

    def fill(self, locator, value: str) -> None:
        locator.fill(value)

    def click(self, locator, *, role: str, name: str) -> None:
        # role + accessible name の複合条件で確認してから click（破壊的ボタン誤爆防止）
        self._page.get_by_role(role, name=name).wait_for(state="visible")
        locator.click()

    def wait_stable(self, predicate: str) -> None:
        if predicate == "navigation_settled":
            self._page.wait_for_load_state("networkidle")
        elif predicate == "loading_indicator_gone":
            self._page.wait_for_load_state("networkidle")
        # readback_stable / row_count_settled は実装で readback を繰り返し比較する

    def read_text(self, locator) -> str:
        return locator.inner_text()

    def record_readback(self, param: str, value: str) -> None:
        self._readbacks[param] = value

    def download(self, trigger_locator, *, role: str, name: str) -> None:
        self._page.get_by_role(role, name=name).wait_for(state="visible")
        with self._page.expect_download() as dl:
            trigger_locator.click()
        download = dl.value
        # サーバー指定 filename は使わず runner が読み取り、bytes を封じる
        path = download.path()
        self._artifact_bytes = Path(path).read_bytes()

    def build_result(self, extracted_at: str) -> ExtractionResult:
        return ExtractionResult(
            columns=self._columns,
            rows=self._rows,
            artifact_bytes=self._artifact_bytes,
            readbacks=dict(self._readbacks),
            ui_total=self._ui_total,
            account_id=self._entry.account.id,
            screen_fingerprint=None,
            extracted_at=extracted_at,
        )


def load_flow(*, path: Path, expected_sha256: str):
    """フローファイルを pin 照合 + AST ゲート後にロードし ``run`` callable を返す。

    exec は honest-scoping の限界（in-process Python）だが、AST ゲートが import /
    exec / eval / dunder を事前に塞いだソースのみをロードする。最小 builtins の
    名前空間で評価する。
    """

    try:
        source_bytes = path.read_bytes()
    except OSError:
        return Err(
            error=BrowserReason.FLOW_PIN_MISMATCH, detail="flow ファイルを読めない"
        )
    pin = verify_flow_pin(source_bytes, expected_sha256)
    if is_err(pin):
        return pin
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _ast_violation("flow が UTF-8 ではない")
    gate = check_flow_ast(source)
    if is_err(gate):
        return gate

    safe_builtins = {
        name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(
            __builtins__, name
        )
        for name in (
            "range",
            "len",
            "str",
            "int",
            "float",
            "bool",
            "list",
            "dict",
            "tuple",
            "set",
            "enumerate",
            "zip",
            "sorted",
            "min",
            "max",
            "sum",
            "abs",
            "any",
            "all",
            "reversed",
        )
    }
    namespace: dict = {"__builtins__": safe_builtins}
    try:
        exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
    except Exception:
        return Err(error=BrowserReason.INTERNAL_ERROR, detail="flow のロードに失敗")
    run = namespace.get("run")
    if not callable(run):
        return _ast_violation("run 関数が定義されていない")
    return Ok(value=run)
