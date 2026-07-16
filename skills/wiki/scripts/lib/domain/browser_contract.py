"""検証語彙 v1 + browser tool 宣言のパース/検証/enforce（pure・Playwright 非依存）.

* **frozen dataclasses** — 値等価・イミュータブル
* **pure** — I/O なし。実行時証拠（抽出行・UI readback・成果物ハッシュ）は
  service 層（flow runner）が集めて :class:`CheckEvidence` として渡す
* **stdlib-only**

責務:

1. browser catalog エントリの hand-rolled 検証（schema-of-record
   ``browser-extract-catalog-schema.json`` と構造制約同期。相関制約 = B1 は
   独立 anchor 最低1つ / params meta-schema 準拠 は validator が真実源）
2. 閉じた検証語彙 v1 の check 評価。**未知語彙は fail-closed**、証拠不足も
   fail-closed（passed=False）— 「検証できなかった」を「合格」にしない

役割分担（guide §4）: filter_readback / row_count_range + 独立 anchor = 正しさ
（誤成功検出）、artifact_hash = 完全性（改ざん検出）、screen_fingerprint =
identity（別画面検出）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from lib.domain.types import Err, Ok


# ---------------------------------------------------------------------------
# 閉集合（schema-of-record と同期。test_browser_contract.py が機械検証）
# ---------------------------------------------------------------------------

CHECK_NAMES = frozenset(
    {
        "filter_readback",
        "row_count_range",
        "selector_exists",
        "export_metadata_match",
        "ui_total_vs_file_rows",
        "tenant_id_match",
        "primary_key_unique",
        "artifact_hash",
        "screen_fingerprint",
    }
)

# セレクタと同一の DOM 解釈に依存しない独立 oracle。B1 契約は最低1つ含む
INDEPENDENT_ANCHORS = frozenset(
    {
        "export_metadata_match",
        "ui_total_vs_file_rows",
        "tenant_id_match",
        "primary_key_unique",
    }
)

TIERS = ("B1", "B2", "B3")
AUTH_PROFILES = ("none", "form", "form+totp", "human-assisted")
ORIGIN_METHODS = ("GET", "POST", "PUT", "DELETE", "HEAD")
RESOURCE_TYPES = (
    "document",
    "xhr",
    "fetch",
    "script",
    "stylesheet",
    "image",
    "font",
    "media",
    "other",
)
GUARANTEE_LEVELS = ("guaranteed", "partial", "none")
HUMAN_VERIFICATION_LEVELS = ("required", "none")
RETENTION_TOGGLE = ("off", "on")

BROWSER_CATALOG_SCHEMA_VERSION = 1

# schema JSON と同期する制約群
_TOOL_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$")
_FLOW_REF_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\.py$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HTTPS_ORIGIN_RE = re.compile(r"^https://")
_HTTP_ORIGIN_RE = re.compile(r"^https?://")
_PATH_PREFIX_RE = re.compile(r"^/")

ENTRY_REQUIRED = (
    "tool_id",
    "type",
    "flow",
    "auth",
    "origin_allowlist",
    "tier",
    "guarantees",
    "checks",
    "params_schema",
    "limits",
    "retention",
    "delivery",
    "account",
)
LIMITS_REQUIRED = (
    "max_rows",
    "max_result_bytes",
    "max_cell_bytes",
    "max_artifact_bytes",
    "max_flow_seconds",
    "max_unapproved_bundles",
)
LIMIT_BOUNDS = {
    "max_rows": (1, 1_000_000),
    "max_result_bytes": (1, 268_435_456),
    "max_cell_bytes": (1, 1_048_576),
    "max_artifact_bytes": (1, 268_435_456),
    "max_flow_seconds": (1, 3_600),
    "max_unapproved_bundles": (1, 1_000),
}
GUARANTEE_FIELDS = (
    "integrity",
    "identity",
    "filter_correctness",
    "completeness",
    "human_verification",
)


class BrowserCatalogError(str, Enum):
    NOT_FOUND = "browser_catalog_not_found"
    INVALID_JSON = "browser_catalog_invalid_json"
    SCHEMA_VIOLATION = "browser_catalog_schema_violation"
    UNKNOWN_TOOL = "browser_unknown_tool"
    UNKNOWN_CHECK = "browser_unknown_check"
    MISSING_ANCHOR = "browser_missing_independent_anchor"
    PARAMS_SCHEMA_INVALID = "browser_params_schema_invalid"


# ---------------------------------------------------------------------------
# 型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowRef:
    ref: str
    sha256: str


@dataclass(frozen=True)
class AuthConfig:
    profile: str
    credential_ref: str | None = None
    session_ttl_hours: int | None = None
    login_origins: tuple[str, ...] = ()


@dataclass(frozen=True)
class OriginRule:
    method: str
    path_prefix: str
    resource_type: str
    state_changing: bool = False


@dataclass(frozen=True)
class Guarantees:
    integrity: str
    identity: str
    filter_correctness: str
    completeness: str
    human_verification: str


@dataclass(frozen=True)
class BrowserLimits:
    max_rows: int
    max_result_bytes: int
    max_cell_bytes: int
    max_artifact_bytes: int
    max_flow_seconds: int
    max_unapproved_bundles: int


@dataclass(frozen=True)
class Retention:
    trace: str
    screenshot: str
    ttl_hours: int


@dataclass(frozen=True)
class Account:
    id: str
    origin: str


@dataclass(frozen=True)
class Check:
    check: str
    param: str | None = None
    column: str | None = None
    selector_role: str | None = None
    selector_name: str | None = None
    min: int | None = None
    max: int | None = None
    expected_value: str | None = None


@dataclass(frozen=True)
class BrowserToolEntry:
    tool_id: str
    flow: FlowRef
    auth: AuthConfig
    origin_allowlist: tuple[OriginRule, ...]
    tier: str
    guarantees: Guarantees
    checks: tuple[Check, ...]
    params_schema: dict
    limits: BrowserLimits
    retention: Retention
    delivery_allowed_dirs: tuple[str, ...]
    account: Account


# ---------------------------------------------------------------------------
# params meta-schema 検証
# ---------------------------------------------------------------------------


def validate_params_schema(schema: object) -> list[str]:
    """tool の params_schema が meta-schema に準拠するか検証する。

    各パラメータは enum / pattern / maxLength のいずれかで値空間を有界にし、
    params オブジェクトは ``additionalProperties: false`` を宣言する
    （自由文字列・未知キーの注入を塞ぐ）。
    """

    errors: list[str] = []
    if not isinstance(schema, dict):
        return ["params_schema がオブジェクトではない"]
    if schema.get("type") != "object":
        errors.append("params_schema.type は 'object' 固定")
    if schema.get("additionalProperties") is not False:
        errors.append("params_schema.additionalProperties は false が必要")
    props = schema.get("properties")
    if not isinstance(props, dict):
        errors.append("params_schema.properties がオブジェクトではない")
        return errors
    for name, spec in props.items():
        if not isinstance(spec, dict):
            errors.append(f"params_schema.properties.{name} がオブジェクトではない")
            continue
        if not any(k in spec for k in ("enum", "pattern", "maxLength")):
            errors.append(
                f"params_schema.properties.{name} は enum/pattern/maxLength の"
                "いずれかが必要（自由文字列を許さない）"
            )
    return errors


# ---------------------------------------------------------------------------
# catalog 検証（hand-rolled、schema-of-record と同期）
# ---------------------------------------------------------------------------


def _is_pos_int_in(value: object, lo: int, hi: int) -> bool:
    return type(value) is int and lo <= value <= hi


def _validate_check(where: str, raw: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return [f"{where}: check がオブジェクトではない"]
    name = raw.get("check")
    if name not in CHECK_NAMES:
        errors.append(f"{where}: 未知の check: {name!r}")
    known = {
        "check",
        "param",
        "column",
        "selector_role",
        "selector_name",
        "min",
        "max",
        "expected_value",
    }
    for key in raw:
        if key not in known:
            errors.append(f"{where}: check の未知のキー: {key}")
    return errors


def _validate_entry(index: int, entry: object) -> list[str]:
    where = f"tools[{index}]"
    if not isinstance(entry, dict):
        return [f"{where}: オブジェクトではない"]

    errors: list[str] = []
    for field_name in ENTRY_REQUIRED:
        if field_name not in entry:
            errors.append(f"{where}: 必須フィールド欠損: {field_name}")
    for key in entry:
        if key not in ENTRY_REQUIRED:
            errors.append(f"{where}: 未知のキー: {key}")
    if errors:
        return errors

    if entry["type"] != "browser":
        errors.append(f"{where}: type は 'browser' 固定: {entry['type']!r}")
    if not (isinstance(entry["tool_id"], str) and _TOOL_ID_RE.fullmatch(entry["tool_id"])):
        errors.append(f"{where}: tool_id が slug 形式ではない: {entry['tool_id']!r}")

    flow = entry["flow"]
    if not isinstance(flow, dict) or set(flow.keys()) != {"ref", "sha256"}:
        errors.append(f"{where}: flow は ref / sha256 のみを持つオブジェクト")
    else:
        if not (isinstance(flow["ref"], str) and _FLOW_REF_RE.fullmatch(flow["ref"])):
            errors.append(f"{where}: flow.ref が *.py の相対名ではない")
        if not (
            isinstance(flow["sha256"], str) and _SHA256_RE.fullmatch(flow["sha256"])
        ):
            errors.append(f"{where}: flow.sha256 が SHA256 hex ではない")

    auth = entry["auth"]
    if not isinstance(auth, dict):
        errors.append(f"{where}: auth がオブジェクトではない")
    else:
        if auth.get("profile") not in AUTH_PROFILES:
            errors.append(f"{where}: auth.profile が未知: {auth.get('profile')!r}")
        for key in auth:
            if key not in (
                "profile",
                "credential_ref",
                "session_ttl_hours",
                "login_origins",
            ):
                errors.append(f"{where}: auth の未知のキー: {key}")
        origins = auth.get("login_origins", [])
        if not isinstance(origins, list) or not all(
            isinstance(o, str) and _HTTPS_ORIGIN_RE.match(o) for o in origins
        ):
            errors.append(f"{where}: auth.login_origins は https URL の配列")

    allowlist = entry["origin_allowlist"]
    if not isinstance(allowlist, list) or not allowlist:
        errors.append(f"{where}: origin_allowlist が非空配列ではない")
    else:
        for i, rule in enumerate(allowlist):
            rw = f"{where}: origin_allowlist[{i}]"
            if not isinstance(rule, dict):
                errors.append(f"{rw} がオブジェクトではない")
                continue
            for key in rule:
                if key not in ("method", "path_prefix", "resource_type", "state_changing"):
                    errors.append(f"{rw} の未知のキー: {key}")
            if rule.get("method") not in ORIGIN_METHODS:
                errors.append(f"{rw}.method が未知: {rule.get('method')!r}")
            if not (
                isinstance(rule.get("path_prefix"), str)
                and _PATH_PREFIX_RE.match(rule["path_prefix"])
            ):
                errors.append(f"{rw}.path_prefix が / 始まりではない")
            if rule.get("resource_type") not in RESOURCE_TYPES:
                errors.append(f"{rw}.resource_type が未知: {rule.get('resource_type')!r}")
            if "state_changing" in rule and type(rule["state_changing"]) is not bool:
                errors.append(f"{rw}.state_changing は boolean")

    if entry["tier"] not in TIERS:
        errors.append(f"{where}: tier が未知: {entry['tier']!r}")

    guarantees = entry["guarantees"]
    if not isinstance(guarantees, dict) or set(guarantees.keys()) != set(
        GUARANTEE_FIELDS
    ):
        errors.append(f"{where}: guarantees のフィールド集合が不正")
    else:
        for f in ("integrity", "identity", "filter_correctness", "completeness"):
            if guarantees[f] not in GUARANTEE_LEVELS:
                errors.append(f"{where}: guarantees.{f} が未知: {guarantees[f]!r}")
        if guarantees["human_verification"] not in HUMAN_VERIFICATION_LEVELS:
            errors.append(f"{where}: guarantees.human_verification が未知")

    checks = entry["checks"]
    if not isinstance(checks, list) or not checks:
        errors.append(f"{where}: checks が非空配列ではない")
    else:
        for i, c in enumerate(checks):
            errors.extend(_validate_check(f"{where}: checks[{i}]", c))
        # B1 は独立 anchor を最低1つ含む（相関制約は validator が真実源）
        names = {c.get("check") for c in checks if isinstance(c, dict)}
        if entry["tier"] == "B1" and not (names & INDEPENDENT_ANCHORS):
            errors.append(
                f"{where}: tier B1 は独立 anchor（{sorted(INDEPENDENT_ANCHORS)}）を"
                "最低1つ含む必要がある"
            )

    params_errors = validate_params_schema(entry["params_schema"])
    errors.extend(f"{where}: params_schema — {e}" for e in params_errors)

    limits = entry["limits"]
    if not isinstance(limits, dict) or set(limits.keys()) != set(LIMITS_REQUIRED):
        errors.append(f"{where}: limits のフィールド集合が不正")
    else:
        for f in LIMITS_REQUIRED:
            lo, hi = LIMIT_BOUNDS[f]
            if not _is_pos_int_in(limits[f], lo, hi):
                errors.append(f"{where}: limits.{f} は {lo}..{hi} の整数")

    retention = entry["retention"]
    if not isinstance(retention, dict) or set(retention.keys()) != {
        "trace",
        "screenshot",
        "ttl_hours",
    }:
        errors.append(f"{where}: retention のフィールド集合が不正")
    else:
        if retention["trace"] not in RETENTION_TOGGLE:
            errors.append(f"{where}: retention.trace は off/on")
        if retention["screenshot"] not in RETENTION_TOGGLE:
            errors.append(f"{where}: retention.screenshot は off/on")
        if not _is_pos_int_in(retention["ttl_hours"], 1, 720):
            errors.append(f"{where}: retention.ttl_hours は 1..720 の整数")

    delivery = entry["delivery"]
    if not isinstance(delivery, dict) or set(delivery.keys()) != {"allowed_dirs"}:
        errors.append(f"{where}: delivery は allowed_dirs のみを持つ")
    else:
        dirs = delivery["allowed_dirs"]
        if not isinstance(dirs, list) or not dirs or not all(
            isinstance(d, str) and d for d in dirs
        ):
            errors.append(f"{where}: delivery.allowed_dirs が非空文字列の非空配列ではない")

    account = entry["account"]
    if not isinstance(account, dict) or set(account.keys()) != {"id", "origin"}:
        errors.append(f"{where}: account は id / origin のみを持つ")
    else:
        if not (isinstance(account["id"], str) and account["id"]):
            errors.append(f"{where}: account.id が非空文字列ではない")
        if not (
            isinstance(account["origin"], str)
            and _HTTP_ORIGIN_RE.match(account["origin"])
        ):
            errors.append(f"{where}: account.origin が http(s) URL ではない")

    return errors


def validate_browser_catalog(data: object) -> list[str]:
    """browser catalog データを schema-of-record 準拠で検証しエラー一覧を返す。"""

    if not isinstance(data, dict):
        return ["catalog がオブジェクトではない"]
    errors: list[str] = []
    for field_name in ("schema_version", "tools"):
        if field_name not in data:
            errors.append(f"必須フィールド欠損: {field_name}")
    for key in data:
        if key not in ("schema_version", "tools"):
            errors.append(f"未知のキー: {key}")
    if errors:
        return errors

    if type(data["schema_version"]) is not int or data["schema_version"] != (
        BROWSER_CATALOG_SCHEMA_VERSION
    ):
        errors.append(
            f"schema_version は整数 {BROWSER_CATALOG_SCHEMA_VERSION} 固定"
        )

    tools = data["tools"]
    if not isinstance(tools, list):
        return errors + ["tools が配列ではない"]

    seen: set[str] = set()
    for i, entry in enumerate(tools):
        errors.extend(_validate_entry(i, entry))
        if isinstance(entry, dict) and isinstance(entry.get("tool_id"), str):
            tid = entry["tool_id"]
            if tid in seen:
                errors.append(f"tool_id 重複: {tid}")
            seen.add(tid)
    return errors


def _to_check(raw: dict) -> Check:
    return Check(
        check=raw["check"],
        param=raw.get("param"),
        column=raw.get("column"),
        selector_role=raw.get("selector_role"),
        selector_name=raw.get("selector_name"),
        min=raw.get("min"),
        max=raw.get("max"),
        expected_value=raw.get("expected_value"),
    )


def _to_entry(raw: dict) -> BrowserToolEntry:
    auth = raw["auth"]
    return BrowserToolEntry(
        tool_id=raw["tool_id"],
        flow=FlowRef(ref=raw["flow"]["ref"], sha256=raw["flow"]["sha256"]),
        auth=AuthConfig(
            profile=auth["profile"],
            credential_ref=auth.get("credential_ref"),
            session_ttl_hours=auth.get("session_ttl_hours"),
            login_origins=tuple(auth.get("login_origins", ())),
        ),
        origin_allowlist=tuple(
            OriginRule(
                method=r["method"],
                path_prefix=r["path_prefix"],
                resource_type=r["resource_type"],
                state_changing=r.get("state_changing", False),
            )
            for r in raw["origin_allowlist"]
        ),
        tier=raw["tier"],
        guarantees=Guarantees(**{f: raw["guarantees"][f] for f in GUARANTEE_FIELDS}),
        checks=tuple(_to_check(c) for c in raw["checks"]),
        params_schema=raw["params_schema"],
        limits=BrowserLimits(**{f: raw["limits"][f] for f in LIMITS_REQUIRED}),
        retention=Retention(
            trace=raw["retention"]["trace"],
            screenshot=raw["retention"]["screenshot"],
            ttl_hours=raw["retention"]["ttl_hours"],
        ),
        delivery_allowed_dirs=tuple(raw["delivery"]["allowed_dirs"]),
        account=Account(id=raw["account"]["id"], origin=raw["account"]["origin"]),
    )


def parse_browser_catalog(
    data: object,
) -> Ok[tuple[BrowserToolEntry, ...]] | Err[BrowserCatalogError]:
    """検証済み catalog を型付き entry 列に変換する（fail-closed）。

    未知 check / B1 anchor 欠落 / params_schema 不正は個別の discriminator で
    返し、それ以外の構造違反は SCHEMA_VIOLATION にまとめる。
    """

    errors = validate_browser_catalog(data)
    if errors:
        joined = "; ".join(errors)
        # 相関制約系の失敗は固有 discriminator を優先して返す（テスト・監査用）
        if any("未知の check" in e for e in errors):
            return Err(error=BrowserCatalogError.UNKNOWN_CHECK, detail=joined)
        if any("独立 anchor" in e for e in errors):
            return Err(error=BrowserCatalogError.MISSING_ANCHOR, detail=joined)
        if any("params_schema" in e for e in errors):
            return Err(error=BrowserCatalogError.PARAMS_SCHEMA_INVALID, detail=joined)
        return Err(error=BrowserCatalogError.SCHEMA_VIOLATION, detail=joined)
    return Ok(value=tuple(_to_entry(e) for e in data["tools"]))


def resolve_browser_entry(
    entries: tuple[BrowserToolEntry, ...], tool_id: str
) -> Ok[BrowserToolEntry] | Err[BrowserCatalogError]:
    for entry in entries:
        if entry.tool_id == tool_id:
            return Ok(value=entry)
    return Err(error=BrowserCatalogError.UNKNOWN_TOOL, detail=tool_id)


# ---------------------------------------------------------------------------
# 検証語彙 v1 の enforce（pure）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckEvidence:
    """flow runner が集めた実行時証拠。評価に必要な項目が欠ければ fail-closed。"""

    rows: tuple[tuple, ...] | None = None
    columns: tuple[str, ...] | None = None
    params: dict[str, str] = field(default_factory=dict)
    readbacks: dict[str, str] = field(default_factory=dict)
    ui_total: int | None = None
    file_row_count: int | None = None
    account_id: str | None = None
    artifact_sha256: str | None = None
    expected_artifact_sha256: str | None = None
    screen_fingerprint: str | None = None
    expected_fingerprint: str | None = None
    selectors_found: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckOutcome:
    check: str
    passed: bool
    reason: str | None = None


def _fail(check: str, reason: str) -> CheckOutcome:
    return CheckOutcome(check=check, passed=False, reason=reason)


def _pass(check: str) -> CheckOutcome:
    return CheckOutcome(check=check, passed=True)


def _readback_match(check: Check, e: CheckEvidence, reason: str) -> CheckOutcome:
    key = check.param
    if key is None or key not in e.readbacks or key not in e.params:
        return _fail(check.check, "missing_evidence")
    if e.readbacks[key] != e.params[key]:
        return _fail(check.check, reason)
    return _pass(check.check)


def evaluate_check(check: Check, evidence: CheckEvidence) -> CheckOutcome:
    """1 つの check を証拠に対して評価する。未知語彙・証拠不足は fail-closed。"""

    name = check.check
    if name not in CHECK_NAMES:
        return _fail(name, "unknown_check")

    if name == "filter_readback":
        return _readback_match(check, evidence, "readback_mismatch")
    if name == "export_metadata_match":
        return _readback_match(check, evidence, "readback_mismatch")

    if name == "row_count_range":
        if (
            evidence.file_row_count is None
            or check.min is None
            or check.max is None
        ):
            return _fail(name, "missing_evidence")
        if check.min <= evidence.file_row_count <= check.max:
            return _pass(name)
        return _fail(name, "row_count_out_of_range")

    if name == "ui_total_vs_file_rows":
        if evidence.ui_total is None or evidence.file_row_count is None:
            return _fail(name, "missing_evidence")
        if evidence.ui_total == evidence.file_row_count:
            return _pass(name)
        return _fail(name, "ui_total_mismatch")

    if name == "primary_key_unique":
        if (
            evidence.rows is None
            or evidence.columns is None
            or check.column is None
            or check.column not in evidence.columns
        ):
            return _fail(name, "missing_evidence")
        idx = evidence.columns.index(check.column)
        keys = [row[idx] for row in evidence.rows]
        if len(keys) == len(set(keys)):
            return _pass(name)
        return _fail(name, "duplicate_primary_key")

    if name == "tenant_id_match":
        if evidence.account_id is None or check.expected_value is None:
            return _fail(name, "missing_evidence")
        if evidence.account_id == check.expected_value:
            return _pass(name)
        return _fail(name, "tenant_mismatch")

    if name == "artifact_hash":
        if (
            evidence.artifact_sha256 is None
            or evidence.expected_artifact_sha256 is None
        ):
            return _fail(name, "missing_evidence")
        if evidence.artifact_sha256 == evidence.expected_artifact_sha256:
            return _pass(name)
        return _fail(name, "artifact_tampered")

    if name == "screen_fingerprint":
        if (
            evidence.screen_fingerprint is None
            or evidence.expected_fingerprint is None
        ):
            return _fail(name, "missing_evidence")
        if evidence.screen_fingerprint == evidence.expected_fingerprint:
            return _pass(name)
        return _fail(name, "screen_mismatch")

    if name == "selector_exists":
        key = f"{check.selector_role}:{check.selector_name}"
        if key not in evidence.selectors_found:
            return _fail(name, "missing_evidence")
        if evidence.selectors_found[key]:
            return _pass(name)
        return _fail(name, "selector_not_found")

    return _fail(name, "unknown_check")  # 到達しない（closed set）


def enforce_checks(
    checks: tuple[Check, ...], evidence: CheckEvidence
) -> tuple[tuple[CheckOutcome, ...], bool]:
    """全 check を評価し、``(outcomes, all_passed)`` を返す。"""

    outcomes = tuple(evaluate_check(c, evidence) for c in checks)
    return outcomes, all(o.passed for o in outcomes)
