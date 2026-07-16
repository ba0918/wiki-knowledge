"""wiki-tool-query の純粋ドメインロジック — proposal bundle と承認状態機械.

* **frozen dataclasses** — 値で等価、イミュータブル
* **pure** — I/O なし、時刻は呼び出し側（service 層の Clock）から注入
* **stdlib-only**

設計判断
--------

* digest binding は **bytes digest**: ``sql_digest`` は query.sql ファイル
  bytes の SHA256。コメント除去等の意味的正規化は binding に使わない
  （文字列リテラル内 ``--`` を字句解析なしに安全に扱えないため）。
  ``sql_display_digest`` は表示・突合用の保守的正規化（trim + 改行統一のみ）
* 状態機械は ``draft → approved → consumed``（terminal）。**consumed の意味は
  「authorization spent（承認の消費）」であり実行成功ではない** — consumed 後に
  実行が失敗しても承認は復活しない（再実行には新 plan + 再承認）
* 事前チェック（:func:`precheck_sql`）は leading whitespace 除去 →
  SELECT / WITH 開始の確認 **のみ**。コメントで始まる SQL は事前チェックで
  拒否される。真実源はあくまで connector の authorizer（本チェックは UX 用の
  早期拒否であり、安全境界ではない）
* malformed な proposal / state は fail closed（固有 RejectReason の Err）
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal, Sequence

from lib.domain.types import Err, Ok, is_err


DEFAULT_TTL_HOURS = 24
MAX_LABEL_LEN = 64
MAX_APPROVED_BY_LEN = 128

# path_validator.ID_PATTERN と同じ slug 空間（domain は service を import
# できないため fragment をここに定義し、test_tool_query.py が同値性を固定する）
SLUG_FRAGMENT = r"[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?"
_NONCE_PATTERN = r"[a-z0-9]{4}"
# \d は Unicode 数字（アラビア数字等）を許すため ASCII の [0-9] を明示する
PLAN_ID_PATTERN = rf"^([0-9]{{14}})-({_NONCE_PATTERN})-({SLUG_FRAGMENT})$"
_PLAN_ID_RE = re.compile(PLAN_ID_PATTERN)
_NONCE_RE = re.compile(rf"^{_NONCE_PATTERN}$")
_ID_RE = re.compile(rf"^{SLUG_FRAGMENT}$")
_ISO_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]+)?Z$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SQL_PRECHECK_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

PlanStatus = Literal["draft", "approved", "consumed"]
_STATUSES: tuple[PlanStatus, ...] = ("draft", "approved", "consumed")


class RejectReason(str, Enum):
    """拒否・失敗の discriminator（監査ログの reason にもそのまま使う）。"""

    # 入力検証
    INVALID_PLAN_ID = "invalid_plan_id"
    INVALID_LABEL = "invalid_label"
    INVALID_APPROVED_BY = "invalid_approved_by"
    INVALID_ROWS_RANGE = "invalid_rows_range"
    SQL_PRECHECK_FAILED = "sql_precheck_failed"

    # bundle / 状態機械（execute 検証マトリクス）
    BUNDLE_MISSING = "bundle_missing"
    MALFORMED_STATE = "malformed_state"
    MALFORMED_PROPOSAL = "malformed_proposal"
    SQL_DIGEST_MISMATCH = "sql_digest_mismatch"
    COUNT_SQL_DIGEST_MISMATCH = "count_sql_digest_mismatch"
    PROPOSAL_DIGEST_MISMATCH = "proposal_digest_mismatch"
    CATALOG_DIGEST_MISMATCH = "catalog_digest_mismatch"
    TTL_EXPIRED = "ttl_expired"
    NOT_APPROVED = "not_approved"
    ALREADY_APPROVED = "already_approved"
    ALREADY_CONSUMED = "already_consumed"
    ROWS_OUT_OF_RANGE = "rows_out_of_range"


# ---------------------------------------------------------------------------
# ドメイン型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FunnelStep:
    """選定ファネルの1段。label は表示値、digest は counts/{nn}.sql の bytes SHA256。"""

    label: str
    count_sql_digest: str
    row_count: int


@dataclass(frozen=True)
class Proposal:
    """prepare が生成する immutable proposal（proposal.json の内容）。

    承認対象は SQL digest だけでなくこの **全 payload** — approve は
    proposal.json 全体の bytes SHA256 を attestation に保存し、execute が
    再計算照合する。
    """

    plan_id: str
    tool_id: str
    catalog_digest: str
    delivery_dir: str
    key_columns: tuple[str, ...]
    expected_rows_min: int
    expected_rows_max: int
    funnel: tuple[FunnelStep, ...]
    sql_digest: str
    sql_display_digest: str
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class ApprovalAttestation:
    """人間承認の記録。proposal_digest が承認時点の proposal.json bytes を束縛する。"""

    approved_by: str
    approved_at: str
    proposal_digest: str


@dataclass(frozen=True)
class PlanState:
    """state.json の内容（mutable durable record — 更新は service 層が crash-safe に行う）。"""

    status: PlanStatus
    approved_by: str | None = None
    approved_at: str | None = None
    proposal_digest: str | None = None
    consumed_at: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class ExecutionReceipt:
    """execute 完了時の記録（receipt.json）。"""

    run_id: str
    row_count: int
    csv_sha256: str
    published_at: str
    delivery_dir: str  # catalog 相対表記（絶対パスは書かない）


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def display_digest(sql: str) -> str:
    """表示・突合用の保守的正規化 digest（trim + CRLF/CR → LF のみ）。"""

    normalized = sql.replace("\r\n", "\n").replace("\r", "\n").strip()
    return sha256_hex(normalized.encode("utf-8"))


# ---------------------------------------------------------------------------
# SQL 事前チェック
# ---------------------------------------------------------------------------


def precheck_sql(sql: str) -> Ok[None] | Err[RejectReason]:
    """leading whitespace 除去 → SELECT / WITH 開始の確認のみ。

    コメントで始まる SQL もここで拒否される（parser なしのコメント除去は
    しない）。read-only の真実源は connector の authorizer。
    """

    if _SQL_PRECHECK_RE.match(sql):
        return Ok(value=None)
    return Err(
        error=RejectReason.SQL_PRECHECK_FAILED,
        detail="SQL は SELECT または WITH で始まる必要があります",
    )


# ---------------------------------------------------------------------------
# plan_id
# ---------------------------------------------------------------------------


def compact_timestamp(iso: str) -> str:
    """ISO 8601 UTC → ``YYYYMMDDHHMMSS``（directory 名に使える compact 形式）。"""

    m = _ISO_RE.match(iso)
    if not m:
        raise ValueError(f"ISO 8601 UTC (Z) 形式ではありません: {iso!r}")
    return "".join(m.groups())


def build_plan_id(*, now_iso: str, nonce: str, tool_id: str) -> str:
    """plan_id ``{YYYYMMDDHHMMSS}-{nonce4}-{tool_id}`` を組み立てる。

    引数はすべて内部生成値（Clock / secrets / catalog 検証済み tool_id）で
    あるべきで、不正はプログラミングエラーとして ValueError にする。
    """

    if not _NONCE_RE.fullmatch(nonce):
        raise ValueError(f"nonce は [a-z0-9]{{4}} が必要: {nonce!r}")
    if not _ID_RE.fullmatch(tool_id):
        raise ValueError(f"tool_id が slug 形式ではない: {tool_id!r}")
    return f"{compact_timestamp(now_iso)}-{nonce}-{tool_id}"


def parse_plan_id(text: str) -> Ok[str] | Err[RejectReason]:
    """外部入力 plan_id の単一 validator — 生成形式への完全一致のみ受理。

    path separator・絶対パス・``..``・制御文字は形式不一致として拒否される
    （パターンが ASCII 英数字と ``-``/``_`` しか許さないため）。timestamp 部は
    カレンダーとして実在する日時のみ受理する（月 13・時 99 などを弾く）。
    """

    if not isinstance(text, str):
        return Err(error=RejectReason.INVALID_PLAN_ID, detail=repr(text)[:80])
    m = _PLAN_ID_RE.fullmatch(text)
    if not m:
        return Err(error=RejectReason.INVALID_PLAN_ID, detail=repr(text)[:80])
    try:
        datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return Err(
            error=RejectReason.INVALID_PLAN_ID,
            detail=f"timestamp 部がカレンダー不正: {m.group(1)}",
        )
    return Ok(value=text)


def is_sha256_hex(value: object) -> bool:
    """lowercase 64 hex の SHA256 表現かどうか（digest フィールドの形式検証）。"""

    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def is_valid_timestamp(value: object) -> bool:
    """ISO 8601 UTC (Z) かつカレンダーとして実在する日時かどうか。"""

    if not isinstance(value, str) or not _ISO_RE.match(value):
        return False
    try:
        _parse_iso(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# 入力検証（label / approved_by / rows range）
# ---------------------------------------------------------------------------


def _has_control_char(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def validate_count_labels(labels: Sequence[str]) -> Ok[None] | Err[RejectReason]:
    """ファネル label の検証: 非空・長さ上限・制御文字禁止・重複禁止。"""

    seen: set[str] = set()
    for label in labels:
        if not isinstance(label, str) or not label:
            return Err(error=RejectReason.INVALID_LABEL, detail="空の label")
        if len(label) > MAX_LABEL_LEN:
            return Err(
                error=RejectReason.INVALID_LABEL,
                detail=f"label が {MAX_LABEL_LEN} 文字を超えています",
            )
        if _has_control_char(label):
            return Err(error=RejectReason.INVALID_LABEL, detail="label に制御文字")
        if label in seen:
            return Err(
                error=RejectReason.INVALID_LABEL, detail=f"label 重複: {label}"
            )
        seen.add(label)
    return Ok(value=None)


def validate_approved_by(name: str) -> Ok[str] | Err[RejectReason]:
    """approve の ``--approved-by``: trim 後非空・長さ上限・制御文字禁止。"""

    if not isinstance(name, str):
        return Err(error=RejectReason.INVALID_APPROVED_BY, detail="非文字列")
    trimmed = name.strip()
    if not trimmed:
        return Err(error=RejectReason.INVALID_APPROVED_BY, detail="trim 後に空")
    if len(trimmed) > MAX_APPROVED_BY_LEN:
        return Err(
            error=RejectReason.INVALID_APPROVED_BY,
            detail=f"{MAX_APPROVED_BY_LEN} 文字を超えています",
        )
    if _has_control_char(trimmed):
        return Err(error=RejectReason.INVALID_APPROVED_BY, detail="制御文字を含む")
    return Ok(value=trimmed)


_ROWS_RANGE_RE = re.compile(r"^(\d+):(\d+)$")


def parse_rows_range(text: str) -> Ok[tuple[int, int]] | Err[RejectReason]:
    """``min:max``（非負整数、min <= max）を解析する。"""

    m = _ROWS_RANGE_RE.fullmatch(text) if isinstance(text, str) else None
    if not m:
        return Err(
            error=RejectReason.INVALID_ROWS_RANGE,
            detail=f"min:max 形式が必要: {text!r}",
        )
    lo, hi = int(m.group(1)), int(m.group(2))
    if lo > hi:
        return Err(
            error=RejectReason.INVALID_ROWS_RANGE, detail=f"min > max: {text!r}"
        )
    return Ok(value=(lo, hi))


def check_rows_in_range(
    *, row_count: int, min_rows: int, max_rows: int
) -> Ok[None] | Err[RejectReason]:
    """実測 row_count の expected_rows_range 照合（境界値は範囲内）。"""

    if min_rows <= row_count <= max_rows:
        return Ok(value=None)
    return Err(
        error=RejectReason.ROWS_OUT_OF_RANGE,
        detail=f"実測 {row_count} 件は想定 {min_rows}..{max_rows} の範囲外",
    )


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


def _parse_iso(iso: str) -> datetime:
    if not _ISO_RE.match(iso):
        raise ValueError(f"ISO 8601 UTC (Z) 形式ではありません: {iso!r}")
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def compute_expires_at(*, created_at: str, ttl_hours: int = DEFAULT_TTL_HOURS) -> str:
    expires = _parse_iso(created_at) + timedelta(hours=ttl_hours)
    return expires.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_expired(*, now: str, expires_at: str) -> bool:
    """``now >= expires_at`` で期限切れ（expires_at ちょうどは期限切れ）。"""

    return _parse_iso(now) >= _parse_iso(expires_at)


# ---------------------------------------------------------------------------
# 状態遷移
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 遷移表駆動の汎用状態機械（connector 非依存）
# ---------------------------------------------------------------------------
#
# 既存の approve_transition / consume_transition は draft→approved→consumed に
# 特殊化された関数（attestation / run_id の記録という副作用を持つ）。汎用
# transition は「その status からその status へ遷移してよいか」だけを表駆動で
# 判定する純関数で、browser 側の seal-at-prepare 状態機械
# （prepared→approved→delivering→delivered/failed/expired）もこの上に載る。


class TransitionError(str, Enum):
    NOT_ALLOWED = "transition_not_allowed"


@dataclass(frozen=True)
class TransitionTable:
    """status 集合と許可遷移の宣言（frozen・値等価）。

    * ``initial`` — publish 直後の status
    * ``edges`` — ``{current: (許可される次 status, ...)}``
    * ``terminal`` — それ以上遷移できない終端 status 集合
    """

    initial: str
    edges: dict[str, tuple[str, ...]]
    terminal: frozenset[str]

    def statuses(self) -> frozenset[str]:
        acc: set[str] = {self.initial}
        acc |= set(self.edges.keys())
        acc |= set(self.terminal)
        for targets in self.edges.values():
            acc |= set(targets)
        return frozenset(acc)


def apply_transition(
    table: TransitionTable, *, current: str, target: str
) -> Ok[str] | Err[TransitionError]:
    """``current`` から ``target`` への遷移が表で許可されているか判定する。

    未知 status・終端からの遷移・宣言されていない edge はすべて
    :attr:`TransitionError.NOT_ALLOWED`（fail closed）。
    """

    if target not in table.edges.get(current, ()):
        return Err(
            error=TransitionError.NOT_ALLOWED,
            detail=f"{current!r} -> {target!r} は許可されていない",
        )
    return Ok(value=target)


# draft→approved→consumed の SQL 系遷移表。既存 approve/consume_transition が
# 実装している遷移の許可集合と一致する（test_transition.py が同値性を固定）。
SQL_TRANSITION_TABLE = TransitionTable(
    initial="draft",
    edges={"draft": ("approved",), "approved": ("consumed",)},
    terminal=frozenset({"consumed"}),
)


def _reject_for_status(status: PlanStatus) -> RejectReason:
    if status == "draft":
        return RejectReason.NOT_APPROVED
    if status == "approved":
        return RejectReason.ALREADY_APPROVED
    return RejectReason.ALREADY_CONSUMED


def approve_transition(
    state: PlanState, attestation: ApprovalAttestation
) -> Ok[PlanState] | Err[RejectReason]:
    """draft → approved。attestation（proposal_digest 含む）を記録する。"""

    if state.status != "draft":
        return Err(error=_reject_for_status(state.status), detail=state.status)
    return Ok(
        value=replace(
            state,
            status="approved",
            approved_by=attestation.approved_by,
            approved_at=attestation.approved_at,
            proposal_digest=attestation.proposal_digest,
        )
    )


def consume_transition(
    state: PlanState, *, consumed_at: str, run_id: str
) -> Ok[PlanState] | Err[RejectReason]:
    """approved → consumed（terminal）。承認の消費であり実行成功ではない。"""

    if state.status == "draft":
        return Err(error=RejectReason.NOT_APPROVED, detail="未承認")
    if state.status == "consumed":
        return Err(error=RejectReason.ALREADY_CONSUMED, detail="replay")
    return Ok(
        value=replace(
            state, status="consumed", consumed_at=consumed_at, run_id=run_id
        )
    )


def evaluate_execute_matrix(
    *,
    state: PlanState,
    proposal: Proposal,
    proposal_digest_now: str,
    sql_digest_now: str,
    count_sql_digests_now: tuple[str, ...],
    catalog_digest_now: str,
    now: str,
) -> Ok[None] | Err[RejectReason]:
    """execute の検証マトリクス。全て固有 reason code で拒否する。

    ``*_now`` 引数は呼び出し側（service）が bundle / catalog から**再計算**した
    現在値。digest 検査を TTL 検査より先に置く — expires_at 自体の改竄も
    digest 照合で落とすため。
    """

    if state.status == "draft":
        return Err(error=RejectReason.NOT_APPROVED, detail="未承認の plan")
    if state.status == "consumed":
        return Err(error=RejectReason.ALREADY_CONSUMED, detail="replay 拒否")

    if sql_digest_now != proposal.sql_digest:
        return Err(
            error=RejectReason.SQL_DIGEST_MISMATCH,
            detail="bundle 内 query.sql が proposal と一致しない",
        )

    expected_counts = tuple(step.count_sql_digest for step in proposal.funnel)
    if count_sql_digests_now != expected_counts:
        return Err(
            error=RejectReason.COUNT_SQL_DIGEST_MISMATCH,
            detail="bundle 内 counts/*.sql が proposal と一致しない",
        )

    if state.proposal_digest != proposal_digest_now:
        return Err(
            error=RejectReason.PROPOSAL_DIGEST_MISMATCH,
            detail="承認時の proposal.json と現在の bytes が一致しない",
        )

    if catalog_digest_now != proposal.catalog_digest:
        return Err(
            error=RejectReason.CATALOG_DIGEST_MISMATCH,
            detail="catalog が prepare 時から変更されている",
        )

    # deserialize を経ない Proposal（直接構築）に不正な expires_at が入っていても
    # 例外で抜けず fail closed にする
    try:
        expired = is_expired(now=now, expires_at=proposal.expires_at)
    except ValueError:
        return Err(
            error=RejectReason.MALFORMED_PROPOSAL,
            detail="expires_at が日時として不正",
        )
    if expired:
        return Err(error=RejectReason.TTL_EXPIRED, detail=proposal.expires_at)

    return Ok(value=None)


# ---------------------------------------------------------------------------
# 直列化（proposal.json / state.json）
# ---------------------------------------------------------------------------

_PROPOSAL_STR_FIELDS = (
    "plan_id",
    "tool_id",
    "catalog_digest",
    "delivery_dir",
    "sql_digest",
    "sql_display_digest",
    "created_at",
    "expires_at",
)
_PROPOSAL_FIELDS = _PROPOSAL_STR_FIELDS + (
    "key_columns",
    "expected_rows_min",
    "expected_rows_max",
    "funnel",
)
_STATE_FIELDS = (
    "status",
    "approved_by",
    "approved_at",
    "proposal_digest",
    "consumed_at",
    "run_id",
)


def proposal_to_json_dict(proposal: Proposal) -> dict:
    return {
        "plan_id": proposal.plan_id,
        "tool_id": proposal.tool_id,
        "catalog_digest": proposal.catalog_digest,
        "delivery_dir": proposal.delivery_dir,
        "key_columns": list(proposal.key_columns),
        "expected_rows_min": proposal.expected_rows_min,
        "expected_rows_max": proposal.expected_rows_max,
        "funnel": [
            {
                "label": step.label,
                "count_sql_digest": step.count_sql_digest,
                "row_count": step.row_count,
            }
            for step in proposal.funnel
        ],
        "sql_digest": proposal.sql_digest,
        "sql_display_digest": proposal.sql_display_digest,
        "created_at": proposal.created_at,
        "expires_at": proposal.expires_at,
    }


def proposal_to_json_bytes(proposal: Proposal) -> bytes:
    """proposal.json のファイル bytes（決定的直列化）。

    proposal_digest はこの bytes の SHA256 なので、書き込みと digest 計算は
    必ず同じ関数を通す。
    """

    return (
        json.dumps(
            proposal_to_json_dict(proposal),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _malformed_proposal(detail: str) -> Err[RejectReason]:
    return Err(error=RejectReason.MALFORMED_PROPOSAL, detail=detail)


def proposal_from_json_dict(data: object) -> Ok[Proposal] | Err[RejectReason]:
    """proposal.json の内容を厳格検証して :class:`Proposal` に変換する（fail closed）。"""

    if not isinstance(data, dict):
        return _malformed_proposal("オブジェクトではない")
    if set(data.keys()) != set(_PROPOSAL_FIELDS):
        return _malformed_proposal("フィールド集合が一致しない")
    for field in _PROPOSAL_STR_FIELDS:
        if not isinstance(data[field], str) or not data[field]:
            return _malformed_proposal(f"{field} が非空文字列ではない")

    # 表面的な型だけでなくドメイン制約まで再検証する — ここを通った Proposal は
    # 下流（検証マトリクス・監査・パス解決）で例外や不正値を発生させない
    if is_err(parse_plan_id(data["plan_id"])):
        return _malformed_proposal(f"plan_id が生成形式ではない: {data['plan_id']!r}")
    if not _ID_RE.fullmatch(data["tool_id"]):
        return _malformed_proposal(f"tool_id が slug 形式ではない: {data['tool_id']!r}")
    for field in ("catalog_digest", "sql_digest", "sql_display_digest"):
        if not is_sha256_hex(data[field]):
            return _malformed_proposal(f"{field} が SHA256 hex ではない")
    for field in ("created_at", "expires_at"):
        if not is_valid_timestamp(data[field]):
            return _malformed_proposal(f"{field} が ISO 8601 UTC 日時ではない")

    for field in ("expected_rows_min", "expected_rows_max"):
        if type(data[field]) is not int or data[field] < 0:
            return _malformed_proposal(f"{field} が非負整数ではない")
    if data["expected_rows_min"] > data["expected_rows_max"]:
        return _malformed_proposal("expected_rows_min > expected_rows_max")

    key_columns = data["key_columns"]
    if not isinstance(key_columns, list) or not all(
        isinstance(c, str) and c for c in key_columns
    ):
        return _malformed_proposal("key_columns が文字列配列ではない")

    funnel_raw = data["funnel"]
    if not isinstance(funnel_raw, list):
        return _malformed_proposal("funnel が配列ではない")
    funnel: list[FunnelStep] = []
    for i, step in enumerate(funnel_raw):
        if not isinstance(step, dict) or set(step.keys()) != {
            "label",
            "count_sql_digest",
            "row_count",
        }:
            return _malformed_proposal(f"funnel[{i}] のフィールド集合が不正")
        if not isinstance(step["label"], str) or not isinstance(
            step["count_sql_digest"], str
        ):
            return _malformed_proposal(f"funnel[{i}] の型が不正")
        if not is_sha256_hex(step["count_sql_digest"]):
            return _malformed_proposal(f"funnel[{i}].count_sql_digest が SHA256 hex ではない")
        if type(step["row_count"]) is not int or step["row_count"] < 0:
            return _malformed_proposal(f"funnel[{i}].row_count が非負整数ではない")
        funnel.append(
            FunnelStep(
                label=step["label"],
                count_sql_digest=step["count_sql_digest"],
                row_count=step["row_count"],
            )
        )
    if is_err(validate_count_labels([s.label for s in funnel])):
        return _malformed_proposal("funnel の label が不正（空・長さ・制御文字・重複）")
    return Ok(
        value=Proposal(
            plan_id=data["plan_id"],
            tool_id=data["tool_id"],
            catalog_digest=data["catalog_digest"],
            delivery_dir=data["delivery_dir"],
            key_columns=tuple(key_columns),
            expected_rows_min=data["expected_rows_min"],
            expected_rows_max=data["expected_rows_max"],
            funnel=tuple(funnel),
            sql_digest=data["sql_digest"],
            sql_display_digest=data["sql_display_digest"],
            created_at=data["created_at"],
            expires_at=data["expires_at"],
        )
    )


def state_to_json_dict(state: PlanState) -> dict:
    return {
        "status": state.status,
        "approved_by": state.approved_by,
        "approved_at": state.approved_at,
        "proposal_digest": state.proposal_digest,
        "consumed_at": state.consumed_at,
        "run_id": state.run_id,
    }


def _malformed_state(detail: str) -> Err[RejectReason]:
    return Err(error=RejectReason.MALFORMED_STATE, detail=detail)


def state_from_json_dict(data: object) -> Ok[PlanState] | Err[RejectReason]:
    """state.json の内容を厳格検証して :class:`PlanState` に変換する（fail closed）。

    status ごとの整合（draft は承認記録なし / approved は attestation 必須 /
    consumed は consumed_at・run_id 必須）まで検査する — parse 不能・不整合な
    state で execute へ進ませない。
    """

    if not isinstance(data, dict):
        return _malformed_state("オブジェクトではない")
    if set(data.keys()) != set(_STATE_FIELDS):
        return _malformed_state("フィールド集合が一致しない")
    status = data["status"]
    if status not in _STATUSES:
        return _malformed_state(f"未知の status: {status!r}")

    approval_fields = ("approved_by", "approved_at", "proposal_digest")
    consumed_fields = ("consumed_at", "run_id")

    def _all_none(fields: tuple[str, ...]) -> bool:
        return all(data[f] is None for f in fields)

    def _all_str(fields: tuple[str, ...]) -> bool:
        return all(isinstance(data[f], str) and data[f] for f in fields)

    def _attestation_valid() -> bool:
        # 表面的な非空だけでなくドメイン制約まで検証する（fail closed）
        return (
            not is_err(validate_approved_by(data["approved_by"]))
            and is_valid_timestamp(data["approved_at"])
            and is_sha256_hex(data["proposal_digest"])
        )

    if status == "draft":
        if not (_all_none(approval_fields) and _all_none(consumed_fields)):
            return _malformed_state("draft に承認・消費の記録が残っている")
    elif status == "approved":
        if not (_all_str(approval_fields) and _all_none(consumed_fields)):
            return _malformed_state("approved の attestation が不完全")
        if not _attestation_valid():
            return _malformed_state("approved の attestation の値が不正")
    else:  # consumed
        if not (_all_str(approval_fields) and _all_str(consumed_fields)):
            return _malformed_state("consumed の記録が不完全")
        if not _attestation_valid():
            return _malformed_state("consumed の attestation の値が不正")
        if not is_valid_timestamp(data["consumed_at"]) or is_err(
            parse_plan_id(data["run_id"])
        ):
            return _malformed_state("consumed の consumed_at / run_id が不正")

    return Ok(
        value=PlanState(
            status=status,
            approved_by=data["approved_by"],
            approved_at=data["approved_at"],
            proposal_digest=data["proposal_digest"],
            consumed_at=data["consumed_at"],
            run_id=data["run_id"],
        )
    )
