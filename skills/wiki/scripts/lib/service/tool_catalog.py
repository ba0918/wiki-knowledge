"""tool catalog のロード・検証・entry 解決.

catalog（``{wiki_root}/tools/catalog.json``、git 管理）は wiki-tool-query の
**実行契約の真実源**: 接続先・relation allowlist・出力上限・delivery 先を
宣言する。Wiki 記事（Selection Recipe）は説明層であり、自然言語編集では
この安全境界を変更できない。

schema-of-record は ``{wiki_root}/schema/tool-catalog-schema.json``。実行時
検証は本モジュールの hand-rolled validator が担い（querylog_append.py と
同方式 — jsonschema 依存を追加しない）、schema JSON との全制約同期は
``test_tool_catalog.py`` が機械検証する。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from lib.domain.types import Err, Ok, is_err
from lib.service.path_validator import ID_PATTERN
from lib.service.tool_paths import ToolPathError, resolve_no_symlink_path


CATALOG_SCHEMA_VERSION = 1
CATALOG_RELATIVE_PATH = "tools/catalog.json"

# schema-of-record（tool-catalog-schema.json）と同期。
# test_tool_catalog.py::TestSchemaSync が機械的に同期を検証する。
CATALOG_REQUIRED_TOP = ("schema_version", "tools")
ENTRY_REQUIRED = (
    "tool_id",
    "type",
    "connection",
    "allowed_tables",
    "limits",
    "allowed_statements",
    "delivery",
)
ENTRY_OPTIONAL = ("credential_ref",)
LIMITS_REQUIRED = ("max_rows", "max_result_bytes", "max_cell_bytes", "timeout_sec")
CONNECTOR_TYPES = ("sqlite",)
ALLOWED_STATEMENTS = ("select",)
LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "max_rows": (1, 1_000_000),
    "max_result_bytes": (1, 268_435_456),
    "max_cell_bytes": (1, 1_048_576),
    "timeout_sec": (1, 600),
}
TABLE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"

_ID_RE = re.compile(ID_PATTERN)
_TABLE_RE = re.compile(TABLE_PATTERN)


class CatalogError(str, Enum):
    """Discriminator for catalog failures."""

    NOT_FOUND = "not_found"
    INVALID_JSON = "invalid_json"
    SCHEMA_VIOLATION = "schema_violation"
    UNKNOWN_TOOL = "unknown_tool"


@dataclass(frozen=True)
class ToolLimits:
    max_rows: int
    max_result_bytes: int
    max_cell_bytes: int
    timeout_sec: int


@dataclass(frozen=True)
class ToolEntry:
    tool_id: str
    type: str
    connection_path: str
    connection_base_dir: str | None
    credential_ref: str | None
    allowed_tables: tuple[str, ...]
    allowed_statements: tuple[str, ...]
    delivery_allowed_dirs: tuple[str, ...]
    limits: ToolLimits


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


def _validate_entry(index: int, entry: object) -> list[str]:
    where = f"tools[{index}]"
    if not isinstance(entry, dict):
        return [f"{where}: オブジェクトではない"]

    errors: list[str] = []
    for field in ENTRY_REQUIRED:
        if field not in entry:
            errors.append(f"{where}: 必須フィールド欠損: {field}")
    known = set(ENTRY_REQUIRED) | set(ENTRY_OPTIONAL)
    for key in entry:
        if key not in known:
            errors.append(f"{where}: 未知のキー: {key}")
    if errors:
        return errors

    tool_id = entry["tool_id"]
    if not isinstance(tool_id, str) or not _ID_RE.fullmatch(tool_id):
        errors.append(f"{where}: tool_id が slug 形式ではない: {tool_id!r}")

    if entry["type"] not in CONNECTOR_TYPES:
        errors.append(f"{where}: type が未対応: {entry['type']!r}")

    conn = entry["connection"]
    if not isinstance(conn, dict):
        errors.append(f"{where}: connection がオブジェクトではない")
    else:
        for key in conn:
            if key not in ("path", "base_dir"):
                errors.append(f"{where}: connection の未知のキー: {key}")
        path = conn.get("path")
        if not isinstance(path, str) or not path:
            errors.append(f"{where}: connection.path が非空文字列ではない")
        base_dir = conn.get("base_dir")
        if base_dir is not None and (not isinstance(base_dir, str) or not base_dir):
            errors.append(f"{where}: connection.base_dir が非空文字列ではない")

    cred = entry.get("credential_ref")
    if cred is not None and (not isinstance(cred, str) or not _ID_RE.fullmatch(cred)):
        errors.append(f"{where}: credential_ref が slug 形式ではない: {cred!r}")

    tables = entry["allowed_tables"]
    if not isinstance(tables, list) or not tables:
        errors.append(f"{where}: allowed_tables が非空配列ではない")
    else:
        for t in tables:
            if not isinstance(t, str) or not _TABLE_RE.fullmatch(t):
                errors.append(f"{where}: allowed_tables に不正な識別子: {t!r}")

    limits = entry["limits"]
    if not isinstance(limits, dict):
        errors.append(f"{where}: limits がオブジェクトではない")
    else:
        for key in limits:
            if key not in LIMITS_REQUIRED:
                errors.append(f"{where}: limits の未知のキー: {key}")
        for field in LIMITS_REQUIRED:
            if field not in limits:
                errors.append(f"{where}: limits.{field} 欠損")
                continue
            value = limits[field]
            lo, hi = LIMIT_BOUNDS[field]
            if not _is_positive_int(value) or not (lo <= value <= hi):
                errors.append(
                    f"{where}: limits.{field} は {lo}..{hi} の整数が必要: {value!r}"
                )

    if entry["allowed_statements"] != list(ALLOWED_STATEMENTS):
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


def _to_entry(raw: dict) -> ToolEntry:
    limits = raw["limits"]
    return ToolEntry(
        tool_id=raw["tool_id"],
        type=raw["type"],
        connection_path=raw["connection"]["path"],
        connection_base_dir=raw["connection"].get("base_dir"),
        credential_ref=raw.get("credential_ref"),
        allowed_tables=tuple(raw["allowed_tables"]),
        allowed_statements=tuple(raw["allowed_statements"]),
        delivery_allowed_dirs=tuple(raw["delivery"]["allowed_dirs"]),
        limits=ToolLimits(
            max_rows=limits["max_rows"],
            max_result_bytes=limits["max_result_bytes"],
            max_cell_bytes=limits["max_cell_bytes"],
            timeout_sec=limits["timeout_sec"],
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
