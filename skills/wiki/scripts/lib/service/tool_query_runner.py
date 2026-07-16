"""application service — prepare / approve / execute のユースケース組み立て.

責務: catalog・bundle・connector・delivery・audit の合成、**出力上限
enforcement**（max_rows / max_result_bytes / max_cell_bytes / 処理全体の
monotonic deadline）、クラッシュポイント表（計画「実行順序とクラッシュ
ポイント」節）どおりの順序制御。

single-use の CAS 契約: execute は plan lock を「state 読取 → 検証マトリクス
評価 → execute_attempted 監査追記 → consumed の durable 更新」まで連続保持
する。lock の取得順は全 subcommand で **plan lock → audit lock** に固定。

全 I/O 依存（connector / clock / lock / monotonic / nonce / audit）は DI 可能。
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

from lib.domain.tool_query import (
    ApprovalAttestation,
    FunnelStep,
    PlanState,
    Proposal,
    RejectReason,
    approve_transition,
    build_plan_id,
    check_rows_in_range,
    compute_expires_at,
    consume_transition,
    display_digest,
    evaluate_execute_matrix,
    is_expired,
    parse_plan_id,
    precheck_sql,
    proposal_from_json_dict,
    proposal_to_json_bytes,
    sha256_hex,
    state_from_json_dict,
    state_to_json_dict,
    validate_approved_by,
    validate_count_labels,
)
from lib.domain.types import Err, Ok, is_err
from lib.service.clock import Clock
from lib.service.file_lock import FileLock, FileLockTimeout
from lib.service.tool_audit import AuditEvent, AuditLog
from lib.service.tool_catalog import (
    Catalog,
    ToolEntry,
    load_catalog,
    load_credential,
    resolve_db_path,
    resolve_entry,
)
from lib.service.tool_connector import (
    ConnectorStreamError,
    RowStream,
    ToolConnectorError,
    open_sqlite_connector,
)
from lib.service.tool_delivery import (
    cell_size_bytes,
    cleanup_staging,
    create_staging_dir,
    encode_csv_row,
    publish_run_dir,
)
from lib.service.tool_paths import resolve_declared_dir, resolve_no_symlink_path


PLANS_RELATIVE_PATH = "outputs/toolquery-plans"
STAGING_RETRY_LIMIT = 5


class RunnerReason(str, Enum):
    """runner が所有する enforcement / 合成失敗の reason code。

    domain（RejectReason）・connector・delivery の reason は .value の文字列を
    そのまま透過する — 呼び出し側（CLI・監査）は文字列としてのみ扱う。
    """

    ROW_LIMIT_EXCEEDED = "row_limit_exceeded"
    RESULT_BYTES_EXCEEDED = "result_bytes_exceeded"
    CELL_BYTES_EXCEEDED = "cell_bytes_exceeded"
    KEY_COLUMN_MISSING = "key_column_missing"
    DUPLICATE_COLUMNS = "duplicate_columns"
    COUNT_RESULT_INVALID = "count_result_invalid"
    DELIVERY_NOT_ALLOWED = "delivery_not_allowed"
    AUDIT_WRITE_FAILED = "audit_write_failed"
    SQL_FILE_UNREADABLE = "sql_file_unreadable"
    PLAN_CONFLICT = "plan_conflict"
    LOCK_TIMEOUT = "lock_timeout"


# 政策拒否として `rejected` 監査に載せる reason（それ以外の実行時失敗は `failed`）
_POLICY_REASONS = frozenset(
    {
        RunnerReason.ROW_LIMIT_EXCEEDED.value,
        RunnerReason.RESULT_BYTES_EXCEEDED.value,
        RunnerReason.CELL_BYTES_EXCEEDED.value,
        RunnerReason.KEY_COLUMN_MISSING.value,
        RunnerReason.DUPLICATE_COLUMNS.value,
        RejectReason.ROWS_OUT_OF_RANGE.value,
        ToolConnectorError.NOT_AUTHORIZED.value,
        ToolConnectorError.DEADLINE_EXCEEDED.value,
    }
)


@dataclass(frozen=True)
class CountSql:
    """prepare に渡すファネル COUNT の 1 件（label は表示値、path は SQL ファイル）。"""

    label: str
    path: Path


@dataclass(frozen=True)
class PrepareOutcome:
    plan_id: str
    tool_id: str
    funnel: tuple[FunnelStep, ...]
    sql_digest: str
    sql_display_digest: str
    expected_rows: tuple[int, int]
    delivery_dir: str
    expires_at: str
    bundle_dir: Path


@dataclass(frozen=True)
class ApprovePreview:
    plan_id: str
    tool_id: str
    status: str
    sql_digest: str
    expected_rows: tuple[int, int]
    delivery_dir: str
    expires_at: str
    proposal_digest: str
    funnel: tuple[FunnelStep, ...]


@dataclass(frozen=True)
class ExecuteOutcome:
    run_id: str
    row_count: int
    duplicate_key_count: int
    null_counts: dict[str, int]
    csv_sha256: str
    sanitized_cell_count: int
    delivery_dir: str  # catalog 宣言の表記
    published_path: Path
    published_at: str
    data_as_of: str
    warnings: tuple[str, ...]


# ---------------------------------------------------------------------------
# durable write ヘルパー
# ---------------------------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes_durable(path: Path, data: bytes) -> None:
    """temp 書き → fsync → os.replace → 親 dir fsync（crash-safe 更新）。"""

    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _write_json_durable(path: Path, data: dict) -> None:
    blob = (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_bytes_durable(path, blob)


def _default_nonce() -> str:
    return secrets.token_hex(2)


def _reason_value(reason: object) -> str:
    return reason.value if isinstance(reason, Enum) else str(reason)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ToolQueryRunner:
    def __init__(
        self,
        *,
        wiki_root: Path,
        clock: Clock,
        lock: FileLock,
        monotonic: Callable[[], float] = time.monotonic,
        nonce: Callable[[], str] = _default_nonce,
        connector_factory: Callable[..., object] = open_sqlite_connector,
        audit: AuditLog | None = None,
        lock_timeout: float = 10.0,
    ) -> None:
        self._wiki_root = Path(wiki_root)
        self._clock = clock
        self._lock = lock
        self._monotonic = monotonic
        self._nonce = nonce
        self._connector_factory = connector_factory
        self._lock_timeout = lock_timeout
        self._audit = audit or AuditLog(
            wiki_root=self._wiki_root,
            lock=lock,
            clock=clock,
            lock_timeout=lock_timeout,
        )

    # -- 共通 ---------------------------------------------------------------

    @property
    def plans_root(self) -> Path:
        return self._wiki_root / PLANS_RELATIVE_PATH

    def _remaining(self, deadline: float) -> float:
        return deadline - self._monotonic()

    def _lock_budget(self, deadline: float | None) -> float:
        """FileLock の取得 timeout は全体 deadline の残時間以下にする。

        残時間が尽きている場合に floor で猶予を与えない — 呼び出し側が
        取得前に :meth:`_remaining` を確認して DEADLINE_EXCEEDED にする契約。
        """

        if deadline is None:
            return self._lock_timeout
        return min(self._lock_timeout, max(self._remaining(deadline), 0.0))

    def _audit_delivery(self, declared: str) -> str | None:
        # 監査ログには catalog 相対表記のみ載せる（絶対宣言は記録しない）
        return None if os.path.isabs(declared) else declared

    def _append_audit(
        self,
        event: str,
        *,
        plan_id: str,
        tool_id: str,
        subcommand: str,
        sql_digest: str | None = None,
        row_count: int | None = None,
        delivery_dir: str | None = None,
        reason: str | None = None,
    ) -> Ok[None] | Err:
        return self._audit.append(
            AuditEvent(
                event=event,
                plan_id=plan_id,
                tool_id=tool_id,
                subcommand=subcommand,
                sql_digest=sql_digest,
                row_count=row_count,
                delivery_dir=delivery_dir,
                reason=reason,
            )
        )

    def _resolve_bundle(self, plan_id: str) -> Ok[Path] | Err:
        resolved = resolve_no_symlink_path(base=self.plans_root, relative=plan_id)
        if is_err(resolved):
            return Err(error=_reason_value(resolved.error), detail=resolved.detail)
        bundle = resolved.value
        if not bundle.is_dir():
            return Err(
                error=RejectReason.BUNDLE_MISSING.value,
                detail=f"bundle が存在しません: {plan_id}",
            )
        return Ok(value=bundle)

    def _read_bundle_proposal(
        self, bundle: Path
    ) -> Ok[tuple[bytes, Proposal]] | Err:
        try:
            proposal_bytes = (bundle / "proposal.json").read_bytes()
        except OSError as exc:
            return Err(error=RejectReason.BUNDLE_MISSING.value, detail=str(exc))
        try:
            data = json.loads(proposal_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return Err(error=RejectReason.MALFORMED_PROPOSAL.value, detail=str(exc))
        parsed = proposal_from_json_dict(data)
        if is_err(parsed):
            return Err(error=_reason_value(parsed.error), detail=parsed.detail)
        # bundle directory 名と proposal の plan_id の一致を検証する —
        # 別 plan の proposal を差し込んで監査・receipt を偽装する経路を塞ぐ
        if parsed.value.plan_id != bundle.name:
            return Err(
                error=RejectReason.MALFORMED_PROPOSAL.value,
                detail="bundle directory 名と proposal.plan_id が一致しません",
            )
        return Ok(value=(proposal_bytes, parsed.value))

    def _read_state(self, bundle: Path) -> Ok[PlanState] | Err:
        """malformed / parse 不能な state.json は fail closed。"""

        try:
            raw = json.loads((bundle / "state.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return Err(error=RejectReason.MALFORMED_STATE.value, detail=str(exc))
        parsed = state_from_json_dict(raw)
        if is_err(parsed):
            return Err(error=_reason_value(parsed.error), detail=parsed.detail)
        return Ok(value=parsed.value)

    def _load_entry(self, tool_id: str) -> Ok[tuple[Catalog, ToolEntry]] | Err:
        catalog_result = load_catalog(wiki_root=self._wiki_root)
        if is_err(catalog_result):
            return Err(
                error=_reason_value(catalog_result.error),
                detail=catalog_result.detail,
            )
        catalog = catalog_result.value
        entry_result = resolve_entry(catalog, tool_id)
        if is_err(entry_result):
            return Err(
                error=_reason_value(entry_result.error), detail=entry_result.detail
            )
        return Ok(value=(catalog, entry_result.value))

    def _open_connector(self, entry: ToolEntry, deadline: float):
        db_path = resolve_db_path(entry=entry, wiki_root=self._wiki_root)
        if is_err(db_path):
            return Err(error=_reason_value(db_path.error), detail=db_path.detail)
        if entry.credential_ref is not None:
            # sqlite はファイル接続のため値は使わないが、enforcement
            # （0600 / regular file / symlink 拒否 / 構造検証）は必ず通す
            credential = load_credential(
                wiki_root=self._wiki_root, ref=entry.credential_ref
            )
            if is_err(credential):
                return Err(
                    error=_reason_value(credential.error), detail=credential.detail
                )
        result = self._connector_factory(
            db_path=db_path.value,
            allowed_tables=entry.allowed_tables,
            max_cell_bytes=entry.limits.max_cell_bytes,
            deadline_monotonic=deadline,
            monotonic=self._monotonic,
        )
        if is_err(result):
            return Err(error=_reason_value(result.error), detail=result.detail)
        return Ok(value=result.value)

    # -- prepare -------------------------------------------------------------

    def prepare(
        self,
        *,
        tool_id: str,
        sql_path: Path,
        count_sqls: Sequence[CountSql],
        key_columns: Sequence[str],
        expected_rows: tuple[int, int],
        deliver_to: str,
    ) -> Ok[PrepareOutcome] | Err:
        started = self._monotonic()
        loaded = self._load_entry(tool_id)
        if is_err(loaded):
            return loaded
        catalog, entry = loaded.value

        labels_ok = validate_count_labels([c.label for c in count_sqls])
        if is_err(labels_ok):
            return Err(error=_reason_value(labels_ok.error), detail=labels_ok.detail)
        if not key_columns or any(not isinstance(k, str) or not k for k in key_columns):
            return Err(
                error=RunnerReason.KEY_COLUMN_MISSING.value,
                detail="key_columns は非空の列名リストが必要です",
            )
        lo, hi = expected_rows
        if lo < 0 or lo > hi:
            return Err(
                error=RejectReason.INVALID_ROWS_RANGE.value,
                detail=f"min:max が不正: {expected_rows!r}",
            )
        if deliver_to not in entry.delivery_allowed_dirs:
            return Err(
                error=RunnerReason.DELIVERY_NOT_ALLOWED.value,
                detail=f"catalog の delivery.allowed_dirs にない: {deliver_to!r}",
            )

        # SQL は一度だけ read し、その同一 bytes を digest・bundle 保存・COUNT
        # 実行のすべてに使う（read しなおしによる TOCTOU を作らない）
        read = self._read_sql_bytes(sql_path)
        if is_err(read):
            return read
        sql_bytes, sql_text = read.value
        pre = precheck_sql(sql_text)
        if is_err(pre):
            return Err(error=_reason_value(pre.error), detail=pre.detail)

        count_payloads: list[tuple[CountSql, bytes, str]] = []
        for count in count_sqls:
            read = self._read_sql_bytes(count.path)
            if is_err(read):
                return read
            count_bytes, count_text = read.value
            pre = precheck_sql(count_text)
            if is_err(pre):
                return Err(
                    error=_reason_value(pre.error),
                    detail=f"count SQL {count.label!r}: {pre.detail}",
                )
            count_payloads.append((count, count_bytes, count_text))

        plan_id = build_plan_id(
            now_iso=self._clock.now(), nonce=self._nonce(), tool_id=tool_id
        )
        deadline = started + entry.limits.timeout_sec

        # dry-run COUNT も「制約・監査付きデータアクセス」— attempted が
        # 書けなければ DB アクセス前に fail closed（execute と同じ契約）
        attempted = self._append_audit(
            "prepare_attempted",
            plan_id=plan_id,
            tool_id=tool_id,
            subcommand="prepare",
            sql_digest=sha256_hex(sql_bytes),
        )
        if is_err(attempted):
            return Err(
                error=RunnerReason.AUDIT_WRITE_FAILED.value, detail=attempted.detail
            )

        # dry-run COUNT は本実行と同じ enforcement（connector 三重防御 +
        # deadline）と監査記録を通す
        funnel_result = self._run_funnel_counts(
            plan_id, entry, count_payloads, deadline
        )
        if is_err(funnel_result):
            return funnel_result
        funnel = funnel_result.value

        created_at = self._clock.now()
        proposal = Proposal(
            plan_id=plan_id,
            tool_id=tool_id,
            catalog_digest=catalog.digest,
            delivery_dir=deliver_to,
            key_columns=tuple(key_columns),
            expected_rows_min=lo,
            expected_rows_max=hi,
            funnel=funnel,
            sql_digest=sha256_hex(sql_bytes),
            sql_display_digest=display_digest(sql_text),
            created_at=created_at,
            expires_at=compute_expires_at(created_at=created_at),
        )
        publish = self._publish_bundle(proposal, sql_bytes, count_payloads)
        if is_err(publish):
            # COUNT 実行済み（attempted 記録済み）なので失敗も監査に残す
            self._append_audit(
                "failed",
                plan_id=plan_id,
                tool_id=tool_id,
                subcommand="prepare",
                reason=_reason_value(publish.error),
            )
            return publish
        plan_id, bundle_dir = publish.value

        audit = self._append_audit(
            "prepared",
            plan_id=plan_id,
            tool_id=tool_id,
            subcommand="prepare",
            sql_digest=proposal.sql_digest,
            delivery_dir=self._audit_delivery(deliver_to),
        )
        if is_err(audit):
            return Err(error=RunnerReason.AUDIT_WRITE_FAILED.value, detail=audit.detail)

        return Ok(
            value=PrepareOutcome(
                plan_id=plan_id,
                tool_id=tool_id,
                funnel=funnel,
                sql_digest=proposal.sql_digest,
                sql_display_digest=proposal.sql_display_digest,
                expected_rows=(lo, hi),
                delivery_dir=deliver_to,
                expires_at=proposal.expires_at,
                bundle_dir=bundle_dir,
            )
        )

    def _read_sql_bytes(self, path: Path) -> Ok[tuple[bytes, str]] | Err:
        try:
            raw = Path(path).read_bytes()
            return Ok(value=(raw, raw.decode("utf-8")))
        except (OSError, UnicodeDecodeError) as exc:
            return Err(error=RunnerReason.SQL_FILE_UNREADABLE.value, detail=str(exc))

    def _run_funnel_counts(
        self,
        plan_id: str,
        entry: ToolEntry,
        count_payloads: list[tuple[CountSql, bytes, str]],
        deadline: float,
    ) -> Ok[tuple[FunnelStep, ...]] | Err:
        def _fail(reason: str, detail: str) -> Err:
            self._append_audit(
                "failed",
                plan_id=plan_id,
                tool_id=entry.tool_id,
                subcommand="prepare",
                reason=reason,
            )
            return Err(error=reason, detail=detail)

        opened = self._open_connector(entry, deadline)
        if is_err(opened):
            return _fail(_reason_value(opened.error), opened.detail)
        connector = opened.value
        funnel: list[FunnelStep] = []
        try:
            for count, count_bytes, count_text in count_payloads:
                stream_result = connector.execute_stream(count_text)
                if is_err(stream_result):
                    return _fail(
                        _reason_value(stream_result.error), stream_result.detail
                    )
                try:
                    with stream_result.value as stream:
                        rows = []
                        for row in stream:
                            rows.append(row)
                            if len(rows) > 1:
                                break
                except ConnectorStreamError as exc:
                    return _fail(exc.reason.value, exc.detail)
                if (
                    len(rows) != 1
                    or len(rows[0]) != 1
                    or type(rows[0][0]) is not int
                    or rows[0][0] < 0
                ):
                    return _fail(
                        RunnerReason.COUNT_RESULT_INVALID.value,
                        f"count SQL {count.label!r} は 1 行 1 列の非負整数が必要です",
                    )
                funnel.append(
                    FunnelStep(
                        label=count.label,
                        count_sql_digest=sha256_hex(count_bytes),
                        row_count=rows[0][0],
                    )
                )
        finally:
            connector.close()
        return Ok(value=tuple(funnel))

    def _publish_bundle(
        self,
        proposal: Proposal,
        sql_bytes: bytes,
        count_payloads: list[tuple[CountSql, bytes, str]],
    ) -> Ok[tuple[str, Path]] | Err:
        plans_root = self.plans_root
        plans_root.mkdir(parents=True, exist_ok=True)

        # staging の排他生成のみ os.mkdir。衝突時は nonce を替えて再試行
        plan_id = proposal.plan_id
        staging: Path | None = None
        for _ in range(STAGING_RETRY_LIMIT):
            candidate = plans_root / f".staging-{plan_id}"
            try:
                os.mkdir(candidate, mode=0o700)
                staging = candidate
                break
            except FileExistsError:
                plan_id = build_plan_id(
                    now_iso=proposal.created_at,
                    nonce=self._nonce(),
                    tool_id=proposal.tool_id,
                )
        if staging is None:
            return Err(
                error=RunnerReason.PLAN_CONFLICT.value,
                detail="staging directory の生成に失敗（衝突が続く）",
            )
        if plan_id != proposal.plan_id:
            proposal = Proposal(
                **{
                    **proposal.__dict__,
                    "plan_id": plan_id,
                }
            )

        try:
            _write_bytes_durable(
                staging / "proposal.json", proposal_to_json_bytes(proposal)
            )
            _write_bytes_durable(staging / "query.sql", sql_bytes)
            counts_dir = staging / "counts"
            counts_dir.mkdir(mode=0o700)
            for i, (_, count_bytes, _) in enumerate(count_payloads):
                _write_bytes_durable(counts_dir / f"{i:02d}.sql", count_bytes)
            _write_json_durable(
                staging / "state.json",
                state_to_json_dict(PlanState(status="draft")),
            )
            _fsync_dir(staging)

            with self._lock.acquire(
                str(plans_root / ".plans.lock"), timeout=self._lock_timeout
            ):
                final = plans_root / plan_id
                # 既存の空 directory との衝突も拒否（rename の黙った置換を防ぐ）
                if os.path.lexists(final):
                    return Err(
                        error=RunnerReason.PLAN_CONFLICT.value,
                        detail=f"plan {plan_id!r} が既に存在します",
                    )
                os.rename(staging, final)
                staging = None  # publish 済み
                _fsync_dir(plans_root)
                return Ok(value=(plan_id, final))
        except (OSError, FileLockTimeout) as exc:
            return Err(error=RunnerReason.PLAN_CONFLICT.value, detail=str(exc))
        finally:
            if staging is not None:
                cleanup_staging(staging)

    # -- approve -------------------------------------------------------------

    def approve_preview(self, plan_id_text: str) -> Ok[ApprovePreview] | Err:
        parsed = parse_plan_id(plan_id_text)
        if is_err(parsed):
            return Err(error=_reason_value(parsed.error), detail=parsed.detail)
        bundle = self._resolve_bundle(parsed.value)
        if is_err(bundle):
            return bundle
        loaded = self._read_bundle_proposal(bundle.value)
        if is_err(loaded):
            return loaded
        proposal_bytes, proposal = loaded.value
        state = self._read_state(bundle.value)
        if is_err(state):
            return state
        return Ok(
            value=ApprovePreview(
                plan_id=proposal.plan_id,
                tool_id=proposal.tool_id,
                status=state.value.status,
                sql_digest=proposal.sql_digest,
                expected_rows=(
                    proposal.expected_rows_min,
                    proposal.expected_rows_max,
                ),
                delivery_dir=proposal.delivery_dir,
                expires_at=proposal.expires_at,
                proposal_digest=sha256_hex(proposal_bytes),
                funnel=proposal.funnel,
            )
        )

    def approve_commit(
        self,
        plan_id_text: str,
        *,
        approved_by: str,
        expected_proposal_digest: str,
    ) -> Ok[PlanState] | Err:
        """人間承認の durable CAS 更新。

        確認プロンプト表示中は lock を保持しない契約のため、preview で得た
        ``expected_proposal_digest`` を受け取り、**lock 内で state（draft）・
        proposal_digest・TTL を再検証**してから approved へ更新する。
        """

        name = validate_approved_by(approved_by)
        if is_err(name):
            return Err(error=_reason_value(name.error), detail=name.detail)
        parsed = parse_plan_id(plan_id_text)
        if is_err(parsed):
            return Err(error=_reason_value(parsed.error), detail=parsed.detail)
        bundle_result = self._resolve_bundle(parsed.value)
        if is_err(bundle_result):
            return bundle_result
        bundle = bundle_result.value

        try:
            with self._lock.acquire(
                str(bundle) + ".lock", timeout=self._lock_timeout
            ):
                loaded = self._read_bundle_proposal(bundle)
                if is_err(loaded):
                    return loaded
                proposal_bytes, proposal = loaded.value
                digest_now = sha256_hex(proposal_bytes)
                if digest_now != expected_proposal_digest:
                    return Err(
                        error=RejectReason.PROPOSAL_DIGEST_MISMATCH.value,
                        detail="表示時と proposal.json の bytes が一致しません",
                    )
                if is_expired(now=self._clock.now(), expires_at=proposal.expires_at):
                    return Err(
                        error=RejectReason.TTL_EXPIRED.value,
                        detail=proposal.expires_at,
                    )
                state = self._read_state(bundle)
                if is_err(state):
                    return state
                transition = approve_transition(
                    state.value,
                    ApprovalAttestation(
                        approved_by=name.value,
                        approved_at=self._clock.now(),
                        proposal_digest=digest_now,
                    ),
                )
                if is_err(transition):
                    return Err(
                        error=_reason_value(transition.error),
                        detail=transition.detail,
                    )
                new_state = transition.value
                # audit-first（execute の attempted と同じ順序）: 監査が書けなければ
                # 状態を変えない。監査成功後の書込みクラッシュは「監査に approved・
                # state は draft」となり、再 approve で回復できる
                audit = self._append_audit(
                    "approved",
                    plan_id=proposal.plan_id,
                    tool_id=proposal.tool_id,
                    subcommand="approve",
                    sql_digest=proposal.sql_digest,
                )
                if is_err(audit):
                    return Err(
                        error=RunnerReason.AUDIT_WRITE_FAILED.value,
                        detail=audit.detail,
                    )
                _write_json_durable(
                    bundle / "state.json", state_to_json_dict(new_state)
                )
                return Ok(value=new_state)
        except FileLockTimeout as exc:
            return Err(error=RunnerReason.LOCK_TIMEOUT.value, detail=str(exc))

    # -- execute -------------------------------------------------------------

    def execute(self, plan_id_text: str) -> Ok[ExecuteOutcome] | Err:
        started = self._monotonic()
        parsed = parse_plan_id(plan_id_text)
        if is_err(parsed):
            return Err(error=_reason_value(parsed.error), detail=parsed.detail)
        bundle_result = self._resolve_bundle(parsed.value)
        if is_err(bundle_result):
            return bundle_result
        bundle = bundle_result.value

        # --- CAS 区間: bundle・catalog の読み込みと検証も plan lock 内で行う。
        # lock 前に読むと、lock 内検証が古い bytes を対象にし、検証を通った
        # 古い SQL がそのまま DB 実行へ渡る TOCTOU が生まれる ---
        try:
            with self._lock.acquire(
                str(bundle) + ".lock", timeout=self._lock_timeout
            ):
                loaded = self._read_bundle_proposal(bundle)
                if is_err(loaded):
                    return loaded
                proposal_bytes, proposal = loaded.value
                try:
                    sql_bytes = (bundle / "query.sql").read_bytes()
                    counts_dir = bundle / "counts"
                    count_digests = tuple(
                        sha256_hex(path.read_bytes())
                        for path in sorted(counts_dir.glob("*.sql"))
                    )
                except OSError as exc:
                    return Err(
                        error=RejectReason.BUNDLE_MISSING.value, detail=str(exc)
                    )

                entry_loaded = self._load_entry(proposal.tool_id)
                if is_err(entry_loaded):
                    return entry_loaded
                catalog, entry = entry_loaded.value
                limits = entry.limits
                deadline = started + limits.timeout_sec

                def _audit_exec(event: str, *, reason: str | None = None, **kw):
                    return self._append_audit(
                        event,
                        plan_id=proposal.plan_id,
                        tool_id=proposal.tool_id,
                        subcommand="execute",
                        sql_digest=proposal.sql_digest,
                        reason=reason,
                        **kw,
                    )

                state_result = self._read_state(bundle)
                if is_err(state_result):
                    _audit_exec("rejected", reason=_reason_value(state_result.error))
                    return state_result
                state = state_result.value

                matrix = evaluate_execute_matrix(
                    state=state,
                    proposal=proposal,
                    proposal_digest_now=sha256_hex(proposal_bytes),
                    sql_digest_now=sha256_hex(sql_bytes),
                    count_sql_digests_now=count_digests,
                    catalog_digest_now=catalog.digest,
                    now=self._clock.now(),
                )
                if is_err(matrix):
                    _audit_exec("rejected", reason=_reason_value(matrix.error))
                    return Err(
                        error=_reason_value(matrix.error), detail=matrix.detail
                    )

                # 監査前の deadline 境界（期限切れ後に承認を消費しない）
                if self._remaining(deadline) <= 0:
                    reason = ToolConnectorError.DEADLINE_EXCEEDED.value
                    _audit_exec("rejected", reason=reason)
                    return Err(error=reason, detail="全体 deadline を超過")

                # 監査に attempted が書けなければ DB アクセス前に fail closed
                attempted = _audit_exec("execute_attempted")
                if is_err(attempted):
                    return Err(
                        error=RunnerReason.AUDIT_WRITE_FAILED.value,
                        detail=attempted.detail,
                    )

                run_id = build_plan_id(
                    now_iso=self._clock.now(),
                    nonce=self._nonce(),
                    tool_id=proposal.tool_id,
                )
                consumed = consume_transition(
                    state, consumed_at=self._clock.now(), run_id=run_id
                )
                if is_err(consumed):
                    return Err(
                        error=_reason_value(consumed.error), detail=consumed.detail
                    )
                # consumed へ durable 遷移してから DB 実行に入る（single-use）
                _write_json_durable(
                    bundle / "state.json", state_to_json_dict(consumed.value)
                )
        except FileLockTimeout as exc:
            return Err(error=RunnerReason.LOCK_TIMEOUT.value, detail=str(exc))

        # --- consumed 済み: ここからの失敗で承認は復活しない ---
        return self._run_and_publish(
            bundle, proposal, entry, sql_bytes, run_id, deadline, _audit_exec
        )

    def _run_and_publish(
        self,
        bundle: Path,
        proposal: Proposal,
        entry: ToolEntry,
        sql_bytes: bytes,
        run_id: str,
        deadline: float,
        _audit_exec,
    ) -> Ok[ExecuteOutcome] | Err:
        def _fail(reason: str, detail: str) -> Err:
            event = "rejected" if reason in _POLICY_REASONS else "failed"
            _audit_exec(event, reason=reason)
            return Err(error=reason, detail=detail)

        if proposal.delivery_dir not in entry.delivery_allowed_dirs:
            return _fail(
                RunnerReason.DELIVERY_NOT_ALLOWED.value,
                f"delivery 先が catalog で許可されていない: {proposal.delivery_dir!r}",
            )
        base_result = resolve_declared_dir(
            wiki_root=self._wiki_root, declared=proposal.delivery_dir
        )
        if is_err(base_result):
            return _fail(_reason_value(base_result.error), base_result.detail)
        delivery_base = base_result.value

        staging_result = create_staging_dir(
            delivery_dir=delivery_base, run_id=run_id
        )
        if is_err(staging_result):
            return _fail(
                _reason_value(staging_result.error), staging_result.detail
            )
        staging = staging_result.value
        published = False
        try:
            # 接続前の deadline 境界（期限切れ後に DB アクセスしない）
            if self._remaining(deadline) <= 0:
                return _fail(
                    ToolConnectorError.DEADLINE_EXCEEDED.value,
                    "全体 deadline を超過（接続前）",
                )
            opened = self._open_connector(entry, deadline)
            if is_err(opened):
                return _fail(_reason_value(opened.error), opened.detail)
            connector = opened.value
            data_as_of = self._clock.now()
            try:
                stream_result = connector.execute_stream(
                    sql_bytes.decode("utf-8")
                )
                if is_err(stream_result):
                    return _fail(
                        _reason_value(stream_result.error), stream_result.detail
                    )
                stats_result = self._stream_to_csv(
                    stream_result.value,
                    staging / "result.csv",
                    entry,
                    proposal.key_columns,
                    deadline,
                )
                if is_err(stats_result):
                    return _fail(
                        _reason_value(stats_result.error), stats_result.detail
                    )
                stats = stats_result.value
            finally:
                connector.close()

            # expected_rows_range は実行時制約 — publish 前に照合する
            in_range = check_rows_in_range(
                row_count=stats["row_count"],
                min_rows=proposal.expected_rows_min,
                max_rows=proposal.expected_rows_max,
            )
            if is_err(in_range):
                return _fail(_reason_value(in_range.error), in_range.detail)

            csv_sha = sha256_hex((staging / "result.csv").read_bytes())
            manifest = {
                "row_count": stats["row_count"],
                "duplicate_key_count": stats["duplicate_key_count"],
                "null_counts": stats["null_counts"],
                "csv_sha256": csv_sha,
                "sanitized_cell_count": stats["sanitized_cell_count"],
                "data_as_of": data_as_of,
                "key_columns": list(proposal.key_columns),
                "plan_id": proposal.plan_id,
                "run_id": run_id,
            }
            _write_json_durable(staging / "manifest.json", manifest)

            # 監査・publish 前の deadline 境界
            if self._remaining(deadline) <= 0:
                return _fail(
                    ToolConnectorError.DEADLINE_EXCEEDED.value,
                    "全体 deadline を超過（publish 前）",
                )

            # executed が書けなければ staging 破棄・非 publish で fail closed
            executed = _audit_exec("executed", row_count=stats["row_count"])
            if is_err(executed):
                return Err(
                    error=RunnerReason.AUDIT_WRITE_FAILED.value,
                    detail=executed.detail,
                )

            pub = publish_run_dir(
                staging_dir=staging,
                delivery_dir=delivery_base,
                run_id=run_id,
                lock=self._lock,
                lock_timeout=self._lock_budget(deadline),
            )
            if is_err(pub):
                return _fail(_reason_value(pub.error), pub.detail)
            published = True
            published_at = self._clock.now()

            warnings: list[str] = []
            audit_pub = _audit_exec(
                "published",
                row_count=stats["row_count"],
                delivery_dir=self._audit_delivery(proposal.delivery_dir),
            )
            if is_err(audit_pub):
                # publish 済みのため取り消せない — reconcile は Phase B
                warnings.append("published 監査イベントの記録に失敗しました")
            try:
                _write_json_durable(
                    bundle / "receipt.json",
                    {
                        "run_id": run_id,
                        "row_count": stats["row_count"],
                        "csv_sha256": csv_sha,
                        "published_at": published_at,
                        "delivery_dir": proposal.delivery_dir,
                    },
                )
            except OSError:
                warnings.append("receipt.json の書き込みに失敗しました")

            return Ok(
                value=ExecuteOutcome(
                    run_id=run_id,
                    row_count=stats["row_count"],
                    duplicate_key_count=stats["duplicate_key_count"],
                    null_counts=stats["null_counts"],
                    csv_sha256=csv_sha,
                    sanitized_cell_count=stats["sanitized_cell_count"],
                    delivery_dir=proposal.delivery_dir,
                    published_path=pub.value,
                    published_at=published_at,
                    data_as_of=data_as_of,
                    warnings=tuple(warnings),
                )
            )
        finally:
            if not published:
                cleanup_staging(staging)

    def _stream_to_csv(
        self,
        stream: RowStream,
        csv_path: Path,
        entry: ToolEntry,
        key_columns: tuple[str, ...],
        deadline: float,
    ) -> Ok[dict] | Err:
        """1-pass streaming: CSV 書き出し・NULL 集計・key 重複検出を同時に行う。

        出力上限は runner の所有: max_rows は超過行の到達時点で即中断、
        max_result_bytes は**無害化後 CSV encoded bytes** の累積、
        max_cell_bytes は型別規則（tool_delivery.cell_size_bytes）で計測する。
        """

        limits = entry.limits
        columns = stream.columns
        if len(set(columns)) != len(columns):
            return Err(
                error=RunnerReason.DUPLICATE_COLUMNS.value,
                detail=f"結果の列名が重複しています: {columns!r}",
            )
        missing = [k for k in key_columns if k not in columns]
        if missing:
            return Err(
                error=RunnerReason.KEY_COLUMN_MISSING.value,
                detail=f"key_columns が結果列にない: {missing!r}",
            )
        key_indexes = [columns.index(k) for k in key_columns]

        null_counts: dict[str, int] = {c: 0 for c in columns}
        seen_keys: set[tuple] = set()
        row_count = 0
        total_bytes = 0
        sanitized_total = 0

        try:
            with open(csv_path, "wb") as f:
                header, _ = encode_csv_row(columns)
                total_bytes += len(header)
                if total_bytes > limits.max_result_bytes:
                    return Err(
                        error=RunnerReason.RESULT_BYTES_EXCEEDED.value,
                        detail="ヘッダ行だけで max_result_bytes を超えています",
                    )
                f.write(header)

                for row in stream:
                    if self._monotonic() >= deadline:
                        return Err(
                            error=ToolConnectorError.DEADLINE_EXCEEDED.value,
                            detail="処理全体の deadline を超過しました",
                        )
                    row_count += 1
                    if row_count > limits.max_rows:
                        # max_rows + 1 件目で即中断
                        return Err(
                            error=RunnerReason.ROW_LIMIT_EXCEEDED.value,
                            detail=f"max_rows={limits.max_rows} を超過",
                        )
                    for value in row:
                        if cell_size_bytes(value) > limits.max_cell_bytes:
                            return Err(
                                error=RunnerReason.CELL_BYTES_EXCEEDED.value,
                                detail=f"max_cell_bytes={limits.max_cell_bytes} を超過",
                            )
                    encoded, sanitized = encode_csv_row(row)
                    sanitized_total += sanitized
                    total_bytes += len(encoded)
                    if total_bytes > limits.max_result_bytes:
                        return Err(
                            error=RunnerReason.RESULT_BYTES_EXCEEDED.value,
                            detail=f"max_result_bytes={limits.max_result_bytes} を超過",
                        )
                    f.write(encoded)
                    for column, value in zip(columns, row):
                        if value is None:
                            null_counts[column] += 1
                    seen_keys.add(tuple(row[i] for i in key_indexes))
                f.flush()
                os.fsync(f.fileno())
        except ConnectorStreamError as exc:
            return Err(error=exc.reason.value, detail=exc.detail)
        except OSError as exc:
            return Err(error=RunnerReason.SQL_FILE_UNREADABLE.value, detail=str(exc))
        finally:
            stream.close()

        return Ok(
            value={
                "row_count": row_count,
                "duplicate_key_count": row_count - len(seen_keys),
                "null_counts": null_counts,
                "sanitized_cell_count": sanitized_total,
            }
        )
