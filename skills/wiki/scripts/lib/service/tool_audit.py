"""wiki-tool-query の監査ログ追記（append-only JSONL、FileLock 排他、fail closed）.

イベントは状態遷移として記録する: ``prepare_attempted / prepared / approved /
execute_attempted / rejected / executed / published / failed``。

内容は値を含まないメタデータのみ — plan_id・tool_id・subcommand・sql_digest・
件数・時刻・delivery 先（catalog 相対）・reason。SQL 全文・条件値・結果行・
絶対パスは書かない。この invariant は **API 側の形式検証で強制する**:
自由文字列を受け付けるフィールドを持たない（reason は既知 reason code の
列挙値のみ、digest は SHA256 形式のみ、plan_id / tool_id / subcommand も
形式検証）。例外メッセージ等をそのまま流し込む経路を作らせない。

execute の安全性は「``execute_attempted`` が書けなければ DB アクセス前に
fail closed」に依存するため、追記は flush + fsync + **親 directory fsync**
（初回作成の directory entry も durable にする）まで行い、失敗は必ず Err で返す。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from lib.domain.tool_query import RejectReason, is_sha256_hex, parse_plan_id
from lib.domain.types import Err, Ok, is_err
from lib.service.clock import Clock
from lib.service.file_lock import FileLock, FileLockTimeout
from lib.service.path_validator import ID_PATTERN
from lib.service.tool_catalog import CatalogError, CredentialError
from lib.service.tool_connector import ToolConnectorError
from lib.service.tool_connector_http import HttpConnectorError
from lib.service.tool_connector_registry import RegistryError
from lib.service.tool_delivery import DeliveryError
from lib.service.tool_paths import ToolPathError
from lib.service.tool_sql_gate import SqlGateError


AUDIT_RELATIVE_PATH = "outputs/toolquery-audit.jsonl"

AUDIT_EVENTS = (
    "prepare_attempted",
    "prepared",
    "approved",
    "execute_attempted",
    "rejected",
    "executed",
    "published",
    "failed",
    # 診断イベント（plan 非依存）— doctor サブコマンドが記録する
    "doctor",
)

AUDIT_SUBCOMMANDS = ("prepare", "approve", "execute", "catalog-validate", "doctor")

# plan に紐付かない診断イベント。plan_id は None でなければならない
# （状態遷移イベントは逆に plan_id 必須）。
PLAN_INDEPENDENT_EVENTS = frozenset({"doctor"})

# runner（lib/service/tool_query_runner.py の RunnerReason）が所有する reason code。
# runner は audit に依存するため直接 import すると循環になる — ここに列挙し、
# test_tool_audit.py が RunnerReason との同期を機械検証する。
RUNNER_REASON_VALUES = (
    "row_limit_exceeded",
    "result_bytes_exceeded",
    "cell_bytes_exceeded",
    "key_column_missing",
    "duplicate_columns",
    "count_result_invalid",
    "delivery_not_allowed",
    "audit_write_failed",
    "sql_file_unreadable",
    "plan_conflict",
    "lock_timeout",
)

# reason は既知の reason code（各層の enum 値）のみ受理する — 自由文字列を
# 禁止することで、例外メッセージ経由の SQL 全文・値・絶対パス混入を構造的に防ぐ
ALLOWED_REASONS = frozenset(
    {e.value for e in RejectReason}
    | {e.value for e in ToolConnectorError}
    | {e.value for e in CatalogError}
    | {e.value for e in CredentialError}
    | {e.value for e in DeliveryError}
    | {e.value for e in ToolPathError}
    | {e.value for e in SqlGateError}
    | {e.value for e in RegistryError}
    | {e.value for e in HttpConnectorError}
    | set(RUNNER_REASON_VALUES)
)

_ID_RE = re.compile(ID_PATTERN)
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


class AuditError(str, Enum):
    INVALID_EVENT = "invalid_event"
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True)
class AuditEvent:
    """1 イベント分のメタデータ。値（結果行・条件値）を持つフィールドはない。"""

    event: str
    plan_id: str | None  # doctor（plan 非依存）のみ None
    tool_id: str
    subcommand: str
    sql_digest: str | None = None
    row_count: int | None = None
    delivery_dir: str | None = None  # catalog 相対表記のみ（絶対パス・traversal 拒否）
    reason: str | None = None  # ALLOWED_REASONS の列挙値のみ


def _is_relative_clean_dir(value: str) -> bool:
    """catalog 相対表記として妥当か: 絶対パス・Windows 絶対・バックスラッシュ・
    ``..``・空 / ``.`` 要素をすべて拒否する。"""

    if not isinstance(value, str) or not value:
        return False
    if os.path.isabs(value) or _WINDOWS_ABS_RE.match(value) or "\\" in value:
        return False
    parts = value.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _validate_event(event: AuditEvent) -> str | None:
    """禁止情報の混入を API 側で防ぐ形式検証。違反メッセージ（str）か None。"""

    if event.event not in AUDIT_EVENTS:
        return f"未知のイベント: {event.event!r}"
    if event.event in PLAN_INDEPENDENT_EVENTS:
        if event.plan_id is not None:
            return "診断イベント（doctor）に plan_id は載せない"
    elif event.plan_id is None or is_err(parse_plan_id(event.plan_id)):
        return "plan_id が生成形式ではない"
    if not isinstance(event.tool_id, str) or not _ID_RE.fullmatch(event.tool_id):
        return "tool_id が slug 形式ではない"
    if event.subcommand not in AUDIT_SUBCOMMANDS:
        return f"未知の subcommand: {event.subcommand!r}"
    if event.sql_digest is not None and not is_sha256_hex(event.sql_digest):
        return "sql_digest が SHA256 hex ではない"
    if event.row_count is not None and (
        type(event.row_count) is not int or event.row_count < 0
    ):
        return "row_count が非負整数ではない"
    if event.delivery_dir is not None and not _is_relative_clean_dir(
        event.delivery_dir
    ):
        return "delivery_dir は catalog 相対表記のみ（絶対パス・traversal 禁止）"
    if event.reason is not None and event.reason not in ALLOWED_REASONS:
        return f"reason は既知の reason code のみ: {event.reason!r}"
    return None


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class AuditLog:
    """``{wiki_root}/outputs/toolquery-audit.jsonl`` への排他追記。

    lock 取得順は全 subcommand で plan lock → audit lock に固定されている
    （呼び出し側の契約）。本クラスは audit lock のみを扱う。
    """

    def __init__(
        self,
        *,
        wiki_root: Path,
        lock: FileLock,
        clock: Clock,
        lock_timeout: float,
    ) -> None:
        self._wiki_root = Path(wiki_root)
        self._path = self._wiki_root / AUDIT_RELATIVE_PATH
        self._lock = lock
        self._clock = clock
        self._lock_timeout = lock_timeout

    def append(self, event: AuditEvent) -> Ok[None] | Err[AuditError]:
        violation = _validate_event(event)
        if violation is not None:
            return Err(error=AuditError.INVALID_EVENT, detail=violation)

        # 「書けなければ必ず Err」— entry 構築・encode・I/O・lock の失敗を
        # すべて WRITE_FAILED に変換する（KeyboardInterrupt は透過させる）
        try:
            entry: dict[str, object] = {
                "event": event.event,
                "at": self._clock.now(),
            }
            for field in (
                "plan_id",
                "tool_id",
                "subcommand",
                "sql_digest",
                "row_count",
                "delivery_dir",
                "reason",
            ):
                value = getattr(event, field)
                if value is not None:
                    entry[field] = value
            data = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")

            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock.acquire(
                str(self._path) + ".lock", timeout=self._lock_timeout
            ):
                with self._path.open("ab") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                # 初回作成直後のクラッシュでもファイルの存在が durable になるよう
                # directory entry も同期する（fail-closed 契約の前提）
                _fsync_dir(self._path.parent)
                _fsync_dir(self._path.parent.parent)
        except (OSError, FileLockTimeout, ValueError, TypeError, UnicodeError) as exc:
            return Err(error=AuditError.WRITE_FAILED, detail=str(exc))
        return Ok(value=None)
