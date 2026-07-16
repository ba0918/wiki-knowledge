"""tool catalog のロード・検証・entry 解決.

catalog（``{wiki_root}/tools/catalog.json``、git 管理）は wiki-tool-query の
**実行契約の真実源**: 接続先・relation allowlist・出力上限・delivery 先を
宣言する。Wiki 記事（Selection Recipe）は説明層であり、自然言語編集では
この安全境界を変更できない。

schema-of-record は ``{wiki_root}/schema/tool-catalog-schema.json``。実行時
検証は本モジュールの hand-rolled validator が担い（querylog_append.py と
同方式 — jsonschema 依存を追加しない）、schema JSON との**構造制約同期**
（required / enum / type / additionalProperties / minItems / minLength /
pattern / const / bounds）は ``test_tool_catalog.py`` が機械検証する。

**相関制約（cross-field）は validator のみが所有する**: ``allow_insecure_tls``
の localhost 限定 / ``tls_ca_file`` との相互排他 / http の ``allow_insecure``
が https で禁止 / ``base_url`` が origin のみ — これらは draft-07 の if/then
で表現しきれない（表現しても本プロジェクトは schema を実行時 validation に
使わないため二重管理になる）。構造 schema はこれらを意図的に持たず、
真実源は validator。相関制約の enforcement は個別テスト（localhost 限定・
相互排他等）が担保する。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat as _stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from lib.domain.types import Err, Ok, is_err
from lib.service.path_validator import ID_PATTERN
from lib.service.tool_paths import ToolPathError, resolve_no_symlink_path


CATALOG_SCHEMA_VERSION = 1
CATALOG_RELATIVE_PATH = "tools/catalog.json"

# schema-of-record（tool-catalog-schema.json）と同期。
# test_tool_catalog.py::TestSchemaSync が機械的に同期を検証する。
CATALOG_REQUIRED_TOP = ("schema_version", "tools")
CONNECTOR_TYPES = ("sqlite", "postgres", "mysql", "http")
ALLOWED_STATEMENTS = ("select",)
HTTP_METHODS = ("GET", "POST")
LOCALHOST_HOSTS = ("localhost", "127.0.0.1", "::1")
DEFAULT_PG_SCHEMA = "public"

# type 別の必須 / 任意フィールド（schema JSON の oneOf variant と同期）。
# credential_ref は sqlite のみ任意（ファイル接続で値を使わない）、リモート
# 接続の pg / mysql / http では必須 — 匿名接続の宣言を catalog 段階で拒否する。
ENTRY_REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {
    "sqlite": (
        "tool_id",
        "type",
        "connection",
        "allowed_tables",
        "limits",
        "allowed_statements",
        "delivery",
    ),
    "postgres": (
        "tool_id",
        "type",
        "connection",
        "credential_ref",
        "allowed_tables",
        "limits",
        "allowed_statements",
        "delivery",
    ),
    "mysql": (
        "tool_id",
        "type",
        "connection",
        "credential_ref",
        "allowed_tables",
        "limits",
        "allowed_statements",
        "delivery",
    ),
    # http に allowed_tables / allowed_statements は存在しない（endpoint
    # allowlist が対応物）— 宣言されたら未知キーとして拒否する
    "http": ("tool_id", "type", "connection", "credential_ref", "limits", "delivery"),
}
ENTRY_OPTIONAL_BY_TYPE: dict[str, tuple[str, ...]] = {
    "sqlite": ("credential_ref",),
    "postgres": (),
    "mysql": (),
    "http": (),
}
CONNECTION_REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {
    "sqlite": ("path",),
    "postgres": ("host", "port", "dbname", "user"),
    "mysql": ("host", "port", "dbname", "user"),
    "http": ("base_url", "allowed_endpoints", "auth_header_name", "auth_header_template"),
}
CONNECTION_OPTIONAL_BY_TYPE: dict[str, tuple[str, ...]] = {
    "sqlite": ("base_dir",),
    "postgres": (
        "default_schema",
        "tls_ca_file",
        "allow_insecure_tls",
        "canary_relation",
    ),
    "mysql": ("tls_ca_file", "allow_insecure_tls", "canary_relation"),
    "http": ("allow_insecure",),
}
LIMITS_REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {
    "sqlite": ("max_rows", "max_result_bytes", "max_cell_bytes", "timeout_sec"),
    "postgres": ("max_rows", "max_result_bytes", "max_cell_bytes", "timeout_sec"),
    "mysql": ("max_rows", "max_result_bytes", "max_cell_bytes", "timeout_sec"),
    "http": (
        "max_rows",
        "max_result_bytes",
        "max_cell_bytes",
        "timeout_sec",
        "max_response_bytes",
    ),
}
LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "max_rows": (1, 1_000_000),
    "max_result_bytes": (1, 268_435_456),
    "max_cell_bytes": (1, 1_048_576),
    "timeout_sec": (1, 600),
    "max_response_bytes": (1, 268_435_456),
}
PORT_BOUNDS = (1, 65535)

TABLE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"
QUALIFIED_TABLE_PATTERN = (
    r"^[A-Za-z_][A-Za-z0-9_]{0,127}(?:\.[A-Za-z_][A-Za-z0-9_]{0,127})?$"
)
HOST_PATTERN = r"^[A-Za-z0-9._:-]+$"
DBNAME_PATTERN = r"^[A-Za-z0-9_$-]{1,64}$"
USER_PATTERN = r"^[A-Za-z0-9_.$-]{1,128}$"
PG_SCHEMA_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
HEADER_NAME_PATTERN = r"^[A-Za-z0-9-]{1,64}$"
HEADER_TEMPLATE_PATTERN = r"\{credential\}"
# base_url / path_prefix は正規表現で表しきれない制約（origin のみ・
# canonical path）を validator が持つ。schema 側 pattern は前提部分のみ
BASE_URL_PATTERN = r"^https?://"
PATH_PREFIX_PATTERN = r"^/"

_ID_RE = re.compile(ID_PATTERN)
_TABLE_RE = re.compile(TABLE_PATTERN)
_QUALIFIED_TABLE_RE = re.compile(QUALIFIED_TABLE_PATTERN)
_HOST_RE = re.compile(HOST_PATTERN)
_DBNAME_RE = re.compile(DBNAME_PATTERN)
_USER_RE = re.compile(USER_PATTERN)
_PG_SCHEMA_RE = re.compile(PG_SCHEMA_PATTERN)
_HEADER_NAME_RE = re.compile(HEADER_NAME_PATTERN)


class CatalogError(str, Enum):
    """Discriminator for catalog failures."""

    NOT_FOUND = "not_found"
    INVALID_JSON = "invalid_json"
    SCHEMA_VIOLATION = "schema_violation"
    UNKNOWN_TOOL = "unknown_tool"


class CredentialError(str, Enum):
    """Discriminator for credential 解決の失敗。detail に秘密値は載せない。"""

    NOT_FOUND = "credential_not_found"
    NOT_REGULAR_FILE = "credential_not_regular_file"
    BAD_PERMISSIONS = "credential_bad_permissions"
    MALFORMED = "credential_malformed"
    UNKNOWN_REF = "credential_unknown_ref"


@dataclass(frozen=True)
class ToolLimits:
    max_rows: int
    max_result_bytes: int
    max_cell_bytes: int
    timeout_sec: int
    max_response_bytes: int | None = None  # http のみ（SQL 系は None）


# --- type 別 tagged connection config -------------------------------------
# catalog parse 時に type ごとの設定を確定し、以降の層は型で分岐しない
# （runner は registry へ委譲するだけで connection の中身を見ない）。


@dataclass(frozen=True)
class SqliteConnectionConfig:
    path: str
    base_dir: str | None = None


@dataclass(frozen=True)
class PostgresConnectionConfig:
    host: str
    port: int
    dbname: str
    user: str
    default_schema: str = DEFAULT_PG_SCHEMA
    tls_ca_file: str | None = None
    allow_insecure_tls: bool = False
    canary_relation: str | None = None


@dataclass(frozen=True)
class MySqlConnectionConfig:
    host: str
    port: int
    dbname: str
    user: str
    tls_ca_file: str | None = None
    allow_insecure_tls: bool = False
    canary_relation: str | None = None


@dataclass(frozen=True)
class HttpEndpointRule:
    method: str
    path_prefix: str


@dataclass(frozen=True)
class HttpConnectionConfig:
    base_url: str
    allowed_endpoints: tuple[HttpEndpointRule, ...]
    auth_header_name: str
    auth_header_template: str  # "{credential}" を秘密値で置換して注入
    allow_insecure: bool = False


ConnectionConfig = (
    SqliteConnectionConfig
    | PostgresConnectionConfig
    | MySqlConnectionConfig
    | HttpConnectionConfig
)


@dataclass(frozen=True)
class ToolEntry:
    tool_id: str
    type: str
    connection: ConnectionConfig
    credential_ref: str | None
    allowed_tables: tuple[str, ...]
    allowed_statements: tuple[str, ...]
    delivery_allowed_dirs: tuple[str, ...]
    limits: ToolLimits

    # Phase A 互換の sqlite 用アクセサ。他 type で触れたら設計違反として
    # 即座に落とす（registry を経ずに DB path を引く経路を作らせない）
    @property
    def connection_path(self) -> str:
        if not isinstance(self.connection, SqliteConnectionConfig):
            raise TypeError("connection_path は sqlite entry のみ参照できます")
        return self.connection.path

    @property
    def connection_base_dir(self) -> str | None:
        if not isinstance(self.connection, SqliteConnectionConfig):
            raise TypeError("connection_base_dir は sqlite entry のみ参照できます")
        return self.connection.base_dir


@dataclass(frozen=True)
class Catalog:
    schema_version: int
    entries: tuple[ToolEntry, ...]
    digest: str  # catalog.json bytes の SHA256 hex（proposal binding に使う）


# ---------------------------------------------------------------------------
# 検証（純粋）
# ---------------------------------------------------------------------------


def _is_positive_int(value: object) -> bool:
    # bool は int のサブクラスだが limit 値としては不正
    return type(value) is int and value > 0


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _has_control_char(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_canonical_path_prefix(value: object) -> bool:
    """endpoint allowlist の path_prefix は最初から canonical 形式を要求する。

    実行時 URL は canonicalize してから照合するが、allowlist 側に encoding・
    traversal・空 segment の余地があると照合の意味論が曖昧になるため、
    宣言段階で拒否する。
    """

    if not isinstance(value, str) or not value.startswith("/"):
        return False
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    if any(ch in value for ch in ("%", "\\", "?", "#")):
        return False
    parts = value.split("/")
    if any(part in (".", "..") for part in parts):
        return False
    # 先頭の空要素（leading /）以外の空 segment（"//"・末尾 /）を拒否
    return "" not in parts[1:]


def _validate_connection_sqlite(where: str, conn: dict) -> list[str]:
    errors: list[str] = []
    if not _is_nonempty_str(conn.get("path")):
        errors.append(f"{where}: connection.path が非空文字列ではない")
    base_dir = conn.get("base_dir")
    if base_dir is not None and not _is_nonempty_str(base_dir):
        errors.append(f"{where}: connection.base_dir が非空文字列ではない")
    return errors


def _validate_connection_db(where: str, conn: dict, *, ctype: str) -> list[str]:
    errors: list[str] = []
    if not _matches(_HOST_RE, conn.get("host")):
        errors.append(f"{where}: connection.host が host 形式ではない")
    port = conn.get("port")
    lo, hi = PORT_BOUNDS
    if not _is_positive_int(port) or not (lo <= port <= hi):
        errors.append(f"{where}: connection.port は {lo}..{hi} の整数が必要")
    if not _matches(_DBNAME_RE, conn.get("dbname")):
        errors.append(f"{where}: connection.dbname が識別子形式ではない")
    if not _matches(_USER_RE, conn.get("user")):
        errors.append(f"{where}: connection.user が識別子形式ではない")

    if ctype == "postgres" and "default_schema" in conn:
        if not _matches(_PG_SCHEMA_RE, conn["default_schema"]):
            errors.append(f"{where}: connection.default_schema が識別子形式ではない")

    tls_ca_file = conn.get("tls_ca_file")
    if tls_ca_file is not None and not _is_nonempty_str(tls_ca_file):
        errors.append(f"{where}: connection.tls_ca_file が非空文字列ではない")

    allow_insecure = conn.get("allow_insecure_tls", False)
    if type(allow_insecure) is not bool:
        errors.append(f"{where}: connection.allow_insecure_tls は boolean が必要")
    elif allow_insecure:
        if conn.get("host") not in LOCALHOST_HOSTS:
            errors.append(
                f"{where}: allow_insecure_tls は host が localhost の場合のみ許可"
            )
        if tls_ca_file is not None:
            errors.append(
                f"{where}: allow_insecure_tls と tls_ca_file は同時に宣言できない"
            )

    canary = conn.get("canary_relation")
    if canary is not None and not _matches(_QUALIFIED_TABLE_RE, canary):
        errors.append(f"{where}: connection.canary_relation が relation 形式ではない")
    return errors


def _validate_connection_http(where: str, conn: dict) -> list[str]:
    errors: list[str] = []
    allow_insecure = conn.get("allow_insecure", False)
    if type(allow_insecure) is not bool:
        errors.append(f"{where}: connection.allow_insecure は boolean が必要")
        allow_insecure = False

    base_url = conn.get("base_url")
    if not isinstance(base_url, str) or not re.match(BASE_URL_PATTERN, base_url):
        errors.append(f"{where}: connection.base_url は http(s) URL が必要")
    else:
        try:
            split = urlsplit(base_url)
            hostname = split.hostname
        except ValueError:
            split = None
            hostname = None
        if split is None or not hostname:
            errors.append(f"{where}: connection.base_url の host が解決できない")
        else:
            if split.username is not None or split.password is not None:
                errors.append(f"{where}: connection.base_url に userinfo は不可")
            if split.path not in ("", "/") or split.query or split.fragment:
                errors.append(
                    f"{where}: connection.base_url は origin のみ"
                    "（path・query・fragment 不可）"
                )
            if split.scheme == "http":
                if not allow_insecure:
                    errors.append(
                        f"{where}: http の base_url は allow_insecure: true の"
                        "明示 opt-in が必要"
                    )
                elif hostname not in LOCALHOST_HOSTS:
                    errors.append(
                        f"{where}: allow_insecure は localhost 限定"
                    )
            elif allow_insecure:
                errors.append(
                    f"{where}: allow_insecure は http の base_url 専用"
                    "（https では宣言できない）"
                )

    endpoints = conn.get("allowed_endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        errors.append(f"{where}: connection.allowed_endpoints が非空配列ではない")
    else:
        for i, ep in enumerate(endpoints):
            ep_where = f"{where}: allowed_endpoints[{i}]"
            if not isinstance(ep, dict):
                errors.append(f"{ep_where} がオブジェクトではない")
                continue
            for key in ep:
                if key not in ("method", "path_prefix"):
                    errors.append(f"{ep_where} の未知のキー: {key}")
            if ep.get("method") not in HTTP_METHODS:
                errors.append(
                    f"{ep_where}.method は {list(HTTP_METHODS)} のいずれかが必要"
                )
            if not _is_canonical_path_prefix(ep.get("path_prefix")):
                errors.append(
                    f"{ep_where}.path_prefix は canonical な / 始まりパスが必要"
                )

    if not _matches(_HEADER_NAME_RE, conn.get("auth_header_name")):
        errors.append(f"{where}: connection.auth_header_name が header 名ではない")
    template = conn.get("auth_header_template")
    if (
        not _is_nonempty_str(template)
        or "{credential}" not in template
        or _has_control_char(template)
    ):
        errors.append(
            f"{where}: connection.auth_header_template は制御文字なしで"
            " {credential} を含む必要がある"
        )
    return errors


def _validate_entry(index: int, entry: object) -> list[str]:
    where = f"tools[{index}]"
    if not isinstance(entry, dict):
        return [f"{where}: オブジェクトではない"]

    ctype = entry.get("type")
    if "type" not in entry:
        return [f"{where}: 必須フィールド欠損: type"]
    if ctype not in CONNECTOR_TYPES:
        return [f"{where}: type が未対応: {ctype!r}"]

    errors: list[str] = []
    required = ENTRY_REQUIRED_BY_TYPE[ctype]
    known = set(required) | set(ENTRY_OPTIONAL_BY_TYPE[ctype])
    for field in required:
        if field not in entry:
            errors.append(f"{where}: 必須フィールド欠損: {field}")
    for key in entry:
        if key not in known:
            errors.append(f"{where}: 未知のキー: {key}")
    if errors:
        return errors

    tool_id = entry["tool_id"]
    if not isinstance(tool_id, str) or not _ID_RE.fullmatch(tool_id):
        errors.append(f"{where}: tool_id が slug 形式ではない: {tool_id!r}")

    # credential_ref は required（remote connector）と optional（sqlite）で
    # 扱いが分岐する。required の型で null / 非文字列を通すと、schema の
    # "type": "string" と乖離したまま接続段階まで進む（存在チェックだけでは
    # 値 null を弾けない）ため、required の場合は非空 slug 文字列を必須にする
    cred = entry.get("credential_ref")
    cred_required = "credential_ref" in required
    if cred_required:
        if not isinstance(cred, str) or not _ID_RE.fullmatch(cred):
            errors.append(
                f"{where}: credential_ref は非空の slug 文字列が必須: {cred!r}"
            )
    elif cred is not None and (
        not isinstance(cred, str) or not _ID_RE.fullmatch(cred)
    ):
        errors.append(f"{where}: credential_ref が slug 形式ではない: {cred!r}")

    conn = entry["connection"]
    if not isinstance(conn, dict):
        errors.append(f"{where}: connection がオブジェクトではない")
    else:
        conn_known = set(CONNECTION_REQUIRED_BY_TYPE[ctype]) | set(
            CONNECTION_OPTIONAL_BY_TYPE[ctype]
        )
        conn_errors: list[str] = []
        for field in CONNECTION_REQUIRED_BY_TYPE[ctype]:
            if field not in conn:
                conn_errors.append(f"{where}: connection.{field} 欠損")
        for key in conn:
            if key not in conn_known:
                conn_errors.append(f"{where}: connection の未知のキー: {key}")
        if conn_errors:
            errors.extend(conn_errors)
        elif ctype == "sqlite":
            errors.extend(_validate_connection_sqlite(where, conn))
        elif ctype in ("postgres", "mysql"):
            errors.extend(_validate_connection_db(where, conn, ctype=ctype))
        else:
            errors.extend(_validate_connection_http(where, conn))

    if "allowed_tables" in known:
        tables = entry["allowed_tables"]
        table_re = _TABLE_RE if ctype == "sqlite" else _QUALIFIED_TABLE_RE
        if not isinstance(tables, list) or not tables:
            errors.append(f"{where}: allowed_tables が非空配列ではない")
        else:
            for t in tables:
                if not isinstance(t, str) or not table_re.fullmatch(t):
                    errors.append(f"{where}: allowed_tables に不正な識別子: {t!r}")

    limits = entry["limits"]
    limits_required = LIMITS_REQUIRED_BY_TYPE[ctype]
    if not isinstance(limits, dict):
        errors.append(f"{where}: limits がオブジェクトではない")
    else:
        for key in limits:
            if key not in limits_required:
                errors.append(f"{where}: limits の未知のキー: {key}")
        for field in limits_required:
            if field not in limits:
                errors.append(f"{where}: limits.{field} 欠損")
                continue
            value = limits[field]
            lo, hi = LIMIT_BOUNDS[field]
            if not _is_positive_int(value) or not (lo <= value <= hi):
                errors.append(
                    f"{where}: limits.{field} は {lo}..{hi} の整数が必要: {value!r}"
                )

    if "allowed_statements" in known and entry["allowed_statements"] != list(
        ALLOWED_STATEMENTS
    ):
        errors.append(
            f"{where}: allowed_statements は {list(ALLOWED_STATEMENTS)!r} 固定: "
            f"{entry['allowed_statements']!r}"
        )

    delivery = entry["delivery"]
    if not isinstance(delivery, dict):
        errors.append(f"{where}: delivery がオブジェクトではない")
    else:
        for key in delivery:
            if key != "allowed_dirs":
                errors.append(f"{where}: delivery の未知のキー: {key}")
        dirs = delivery.get("allowed_dirs")
        if not isinstance(dirs, list) or not dirs:
            errors.append(f"{where}: delivery.allowed_dirs が非空配列ではない")
        else:
            for d in dirs:
                if not isinstance(d, str) or not d:
                    errors.append(
                        f"{where}: delivery.allowed_dirs に非空文字列以外: {d!r}"
                    )

    return errors


def validate_catalog(data: object) -> list[str]:
    """catalog データを schema-of-record 準拠で検証し、エラーの一覧を返す。"""

    if not isinstance(data, dict):
        return ["catalog がオブジェクトではない"]

    errors: list[str] = []
    for field in CATALOG_REQUIRED_TOP:
        if field not in data:
            errors.append(f"必須フィールド欠損: {field}")
    for key in data:
        if key not in CATALOG_REQUIRED_TOP:
            errors.append(f"未知のキー: {key}")
    if errors:
        return errors

    version = data["schema_version"]
    # bool は int のサブクラス（True == 1）なので type 一致まで要求する
    if type(version) is not int or version != CATALOG_SCHEMA_VERSION:
        errors.append(
            f"schema_version は整数 {CATALOG_SCHEMA_VERSION} 固定: {version!r}"
        )

    tools = data["tools"]
    if not isinstance(tools, list):
        return errors + ["tools が配列ではない"]

    seen_ids: set[str] = set()
    for i, entry in enumerate(tools):
        errors.extend(_validate_entry(i, entry))
        if isinstance(entry, dict):
            tool_id = entry.get("tool_id")
            if isinstance(tool_id, str):
                if tool_id in seen_ids:
                    errors.append(f"tool_id 重複: {tool_id}")
                seen_ids.add(tool_id)

    return errors


# ---------------------------------------------------------------------------
# ロード・解決（I/O）
# ---------------------------------------------------------------------------


def _to_connection(ctype: str, conn: dict) -> ConnectionConfig:
    if ctype == "sqlite":
        return SqliteConnectionConfig(
            path=conn["path"], base_dir=conn.get("base_dir")
        )
    if ctype == "postgres":
        return PostgresConnectionConfig(
            host=conn["host"],
            port=conn["port"],
            dbname=conn["dbname"],
            user=conn["user"],
            default_schema=conn.get("default_schema", DEFAULT_PG_SCHEMA),
            tls_ca_file=conn.get("tls_ca_file"),
            allow_insecure_tls=conn.get("allow_insecure_tls", False),
            canary_relation=conn.get("canary_relation"),
        )
    if ctype == "mysql":
        return MySqlConnectionConfig(
            host=conn["host"],
            port=conn["port"],
            dbname=conn["dbname"],
            user=conn["user"],
            tls_ca_file=conn.get("tls_ca_file"),
            allow_insecure_tls=conn.get("allow_insecure_tls", False),
            canary_relation=conn.get("canary_relation"),
        )
    return HttpConnectionConfig(
        base_url=conn["base_url"],
        allowed_endpoints=tuple(
            HttpEndpointRule(method=ep["method"], path_prefix=ep["path_prefix"])
            for ep in conn["allowed_endpoints"]
        ),
        auth_header_name=conn["auth_header_name"],
        auth_header_template=conn["auth_header_template"],
        allow_insecure=conn.get("allow_insecure", False),
    )


def _to_entry(raw: dict) -> ToolEntry:
    limits = raw["limits"]
    ctype = raw["type"]
    return ToolEntry(
        tool_id=raw["tool_id"],
        type=ctype,
        connection=_to_connection(ctype, raw["connection"]),
        credential_ref=raw.get("credential_ref"),
        allowed_tables=tuple(raw.get("allowed_tables", ())),
        allowed_statements=tuple(raw.get("allowed_statements", ())),
        delivery_allowed_dirs=tuple(raw["delivery"]["allowed_dirs"]),
        limits=ToolLimits(
            max_rows=limits["max_rows"],
            max_result_bytes=limits["max_result_bytes"],
            max_cell_bytes=limits["max_cell_bytes"],
            timeout_sec=limits["timeout_sec"],
            max_response_bytes=limits.get("max_response_bytes"),
        ),
    )


def load_catalog(*, wiki_root: Path) -> Ok[Catalog] | Err[CatalogError]:
    """catalog.json を読み、検証済み :class:`Catalog`（bytes digest 付き）を返す。"""

    path = wiki_root / CATALOG_RELATIVE_PATH
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return Err(error=CatalogError.NOT_FOUND, detail=str(CATALOG_RELATIVE_PATH))
    except OSError as e:
        return Err(error=CatalogError.NOT_FOUND, detail=str(e))

    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return Err(error=CatalogError.INVALID_JSON, detail=str(e))

    errors = validate_catalog(data)
    if errors:
        return Err(error=CatalogError.SCHEMA_VIOLATION, detail="; ".join(errors))

    return Ok(
        value=Catalog(
            schema_version=data["schema_version"],
            entries=tuple(_to_entry(raw) for raw in data["tools"]),
            digest=hashlib.sha256(raw_bytes).hexdigest(),
        )
    )


def resolve_entry(
    catalog: Catalog, tool_id: str
) -> Ok[ToolEntry] | Err[CatalogError]:
    for entry in catalog.entries:
        if entry.tool_id == tool_id:
            return Ok(value=entry)
    return Err(error=CatalogError.UNKNOWN_TOOL, detail=tool_id)


CREDENTIALS_RELATIVE_PATH = ".local/credentials.json"


def load_credential(
    *, wiki_root: Path, ref: str
) -> Ok[str] | Err[CredentialError]:
    """``{wiki_root}/.local/credentials.json`` から credential_ref で秘密値を引く。

    enforcement: wiki_root への containment + **全 segment symlink 拒否**
    （``.local`` 自体が symlink のケースを含む）/ ``O_NOFOLLOW`` で open して
    **同一 fd** を fstat 検証と読み取りに使う（lookup を 1 回にして検査と
    読み取りの間の差し替えを防ぐ）/ regular file / permission 0600 以下 /
    構造検証。返り値の秘密値は接続にのみ使用し、呼び出し側はログ・stdout・
    例外メッセージのいずれにも載せてはならない。本関数の detail も ref 名のみ。
    """

    resolved = resolve_no_symlink_path(
        base=wiki_root, relative=CREDENTIALS_RELATIVE_PATH
    )
    if is_err(resolved):
        if resolved.error in (
            ToolPathError.SYMLINK_COMPONENT,
            ToolPathError.SYMLINK_ESCAPE,
        ):
            return Err(
                error=CredentialError.NOT_REGULAR_FILE,
                detail="credentials.json への経路に symlink があります",
            )
        return Err(error=CredentialError.NOT_FOUND, detail=CREDENTIALS_RELATIVE_PATH)

    try:
        fd = os.open(resolved.value, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return Err(error=CredentialError.NOT_FOUND, detail=CREDENTIALS_RELATIVE_PATH)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return Err(
                error=CredentialError.NOT_REGULAR_FILE,
                detail="credentials.json は symlink 不可",
            )
        return Err(error=CredentialError.NOT_FOUND, detail=CREDENTIALS_RELATIVE_PATH)

    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            return Err(
                error=CredentialError.NOT_REGULAR_FILE,
                detail="credentials.json は regular file が必要",
            )
        if st.st_mode & 0o077:
            return Err(
                error=CredentialError.BAD_PERMISSIONS,
                detail="credentials.json は 0600 が必要",
            )
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                fd = -1  # fdopen が所有権を持つ（二重 close 防止）
                data = json.load(f)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return Err(error=CredentialError.MALFORMED, detail="JSON として読めません")
    finally:
        if fd >= 0:
            os.close(fd)

    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        return Err(
            error=CredentialError.MALFORMED,
            detail="{ref: 値(文字列)} のオブジェクトが必要",
        )
    if ref not in data:
        return Err(error=CredentialError.UNKNOWN_REF, detail=ref)
    return Ok(value=data[ref])


def resolve_db_path(
    *, entry: ToolEntry, wiki_root: Path
) -> Ok[Path] | Err[ToolPathError]:
    """DB path を base（宣言 base_dir または wiki_root）配下に封じ込めて解決する。"""

    if entry.connection_base_dir is None:
        base = wiki_root
    elif Path(entry.connection_base_dir).is_absolute():
        # 絶対 base_dir は場所の宣言としては信頼するが、symlink でないことは
        # resolve_no_symlink_path が base 自身の lexical path も含めて検査する
        base = Path(entry.connection_base_dir)
    else:
        base_result = resolve_no_symlink_path(
            base=wiki_root, relative=entry.connection_base_dir
        )
        if is_err(base_result):
            return base_result
        base = base_result.value

    return resolve_no_symlink_path(base=base, relative=entry.connection_path)
