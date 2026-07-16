"""承認ライフサイクルの共有 core — connector 非依存の prepare-publish /
approve-CAS / execute-consume の**fail-closed 順序保証**.

SQL 系（wiki-tool-query）と browser 系（wiki-browser-extract）が同一の
security 中核を共有し、二重実装と divergence を避けるための抽出。

この module が所有するのは**順序と lock 規律**であって policy ではない:

* publish — staging 排他生成 → durable write → **plan lock 下で最終名の
  不在確認 → rename** → 親 dir fsync（POSIX rename の空 dir 黙殺を塞ぐ）
* approve_cas — **plan lock を連続保持**して: validate（digest/TTL/state
  再検証）→ 状態遷移 → **audit-first**（承認イベントが書けなければ状態を
  変えない）→ durable state 書込
* consume_cas — **plan lock を連続保持**して: validate（matrix/precheck/
  deadline）→ **execute_attempted 監査**（書けなければ DB アクセス前に
  fail closed）→ 状態消費 → durable state 書込（single-use）

policy（bundle の中身・digest 計算・matrix 評価・監査 registry・状態 codec）は
すべて呼び出し側が callback で注入する。durable write helper は本 module が
真実源で、SQL runner は monkeypatch 互換のため re-export する。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from lib.domain.tool_query import RejectReason, build_plan_id
from lib.domain.types import Err, Ok, is_err
from lib.service.file_lock import FileLock, FileLockTimeout
from lib.service.tool_paths import resolve_no_symlink_path


STAGING_RETRY_LIMIT = 5


class ApprovalError(str, Enum):
    """共有 core が所有する順序・lock 失敗の reason code。

    値は SQL 側 RunnerReason と一致させ、呼び出し側は ``.value`` の文字列を
    そのまま透過できる（監査 registry にもこの文字列で載る）。
    """

    LOCK_TIMEOUT = "lock_timeout"
    PLAN_CONFLICT = "plan_conflict"
    AUDIT_WRITE_FAILED = "audit_write_failed"


# ---------------------------------------------------------------------------
# durable write helper（crash-safe 更新の真実源）
# ---------------------------------------------------------------------------


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_bytes_durable(path: Path, data: bytes) -> None:
    """temp 書き → fsync → os.replace → 親 dir fsync（crash-safe 更新）。"""

    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def write_json_durable(path: Path, data: dict) -> None:
    blob = (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    write_bytes_durable(path, blob)


def _reason_value(reason: object) -> str:
    return reason.value if isinstance(reason, Enum) else str(reason)


# ---------------------------------------------------------------------------
# 型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishResult:
    plan_id: str
    bundle_dir: Path


@dataclass(frozen=True)
class ConsumeOutcome:
    """consume_cas の成功結果。``context`` は validate が返した任意 payload。"""

    run_id: str
    state: object
    context: object


class ApprovalService:
    """proposal bundle の承認ライフサイクル（順序と lock 規律のみを所有）。

    durable state 書込は ``write_json`` 経由で行う（呼び出し側は monkeypatch
    可能な自前 helper を渡せる — SQL runner の crash-injection テスト互換）。
    """

    def __init__(
        self,
        *,
        plans_root: Path,
        lock: FileLock,
        lock_timeout: float,
        write_json: Callable[[Path, dict], None] = write_json_durable,
    ) -> None:
        self._plans_root = Path(plans_root)
        self._lock = lock
        self._lock_timeout = lock_timeout
        self._write_json = write_json

    @property
    def plans_root(self) -> Path:
        return self._plans_root

    # -- bundle 解決 --------------------------------------------------------

    def resolve_bundle(self, plan_id: str) -> Ok[Path] | Err:
        """plan_id を plans_root 配下の bundle dir に解決（全 segment symlink 拒否）。"""

        resolved = resolve_no_symlink_path(base=self._plans_root, relative=plan_id)
        if is_err(resolved):
            return Err(error=_reason_value(resolved.error), detail=resolved.detail)
        bundle = resolved.value
        if not bundle.is_dir():
            return Err(
                error=RejectReason.BUNDLE_MISSING.value,
                detail=f"bundle が存在しません: {plan_id}",
            )
        return Ok(value=bundle)

    # -- prepare-publish ----------------------------------------------------

    def publish_bundle(
        self,
        *,
        plan_id: str,
        rebuild_plan_id: Callable[[], str],
        build_files: Callable[[str], dict[str, bytes]],
    ) -> Ok[PublishResult] | Err:
        """``.staging-{plan_id}`` を排他生成し、``build_files`` の全ファイルを
        durable 書込してから plan lock 下で最終名へ no-clobber rename する。

        * staging 名衝突は ``rebuild_plan_id`` で plan_id を替えて再試行
        * ``build_files(final_plan_id)`` は「相対パス -> bytes」の平坦な map を
          返す（``proposal.json`` / ``state.json`` / サブディレクトリ含む）。
          plan_id を埋め込む payload は final_plan_id で再構築される
        """

        plans_root = self._plans_root
        plans_root.mkdir(parents=True, exist_ok=True)

        staging: Path | None = None
        for _ in range(STAGING_RETRY_LIMIT):
            candidate = plans_root / f".staging-{plan_id}"
            try:
                os.mkdir(candidate, mode=0o700)
                staging = candidate
                break
            except FileExistsError:
                plan_id = rebuild_plan_id()
        if staging is None:
            return Err(
                error=ApprovalError.PLAN_CONFLICT.value,
                detail="staging directory の生成に失敗（衝突が続く）",
            )

        try:
            files = build_files(plan_id)
            for rel, data in files.items():
                target = staging / rel
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_bytes_durable(target, data)
            fsync_dir(staging)

            with self._lock.acquire(
                str(plans_root / ".plans.lock"), timeout=self._lock_timeout
            ):
                final = plans_root / plan_id
                if os.path.lexists(final):
                    return Err(
                        error=ApprovalError.PLAN_CONFLICT.value,
                        detail=f"plan {plan_id!r} が既に存在します",
                    )
                os.rename(staging, final)
                staging = None
                fsync_dir(plans_root)
                return Ok(value=PublishResult(plan_id=plan_id, bundle_dir=final))
        except (OSError, FileLockTimeout) as exc:
            return Err(error=ApprovalError.PLAN_CONFLICT.value, detail=str(exc))
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    # -- approve-CAS --------------------------------------------------------

    def approve_cas(
        self,
        *,
        bundle: Path,
        validate: Callable[[Path], Ok | Err],
        do_approve: Callable[[object], Ok | Err],
        audit_approved: Callable[[], Ok | Err],
        write_state: Callable[[Path, object], None],
    ) -> Ok[object] | Err:
        """plan lock 下で draft → approved を durable CAS する。

        * ``validate(bundle)`` — digest 再照合 / TTL / state 読取（policy）。
          Ok の value が ``do_approve`` に渡る validated context
        * ``do_approve(ctx)`` — 状態遷移（attestation 記録を含む）。Ok[new_state]
        * ``audit_approved()`` — 承認イベント追記。**audit-first**: これが失敗
          したら state を書かない
        * ``write_state(bundle, new_state)`` — durable state 書込
        """

        try:
            with self._lock.acquire(str(bundle) + ".lock", timeout=self._lock_timeout):
                v = validate(bundle)
                if is_err(v):
                    return v
                new = do_approve(v.value)
                if is_err(new):
                    return new
                audit = audit_approved()
                if is_err(audit):
                    return Err(
                        error=ApprovalError.AUDIT_WRITE_FAILED.value,
                        detail=getattr(audit, "detail", ""),
                    )
                write_state(bundle, new.value)
                return Ok(value=new.value)
        except FileLockTimeout as exc:
            return Err(error=ApprovalError.LOCK_TIMEOUT.value, detail=str(exc))

    # -- execute-consume ----------------------------------------------------

    def consume_cas(
        self,
        *,
        bundle: Path,
        validate: Callable[[Path], Ok | Err],
        audit_attempted: Callable[[], Ok | Err],
        make_run_id: Callable[[], str],
        do_consume: Callable[[object, str], Ok | Err],
        write_state: Callable[[Path, object], None],
    ) -> Ok[ConsumeOutcome] | Err:
        """plan lock を連続保持して承認を single-use 消費する。

        * ``validate(bundle)`` — matrix / precheck / deadline（policy）。**拒否
          時の rejected 監査は validate 側が担う**（層により audit するもの・
          しないものが分かれるため）。Ok の value が ``do_consume`` に渡る
        * ``audit_attempted()`` — execute_attempted 追記。書けなければ DB
          アクセス前に fail closed（consume しない）
        * ``do_consume(ctx, run_id)`` — approved → consumed 遷移。Ok[new_state]
        * ``write_state`` — consumed の durable 書込。ここまでで single-use 成立

        戻り値の :class:`ConsumeOutcome` は run_id / consumed state / validate の
        context を持ち、呼び出し側が lock 解放後の本処理（DB 実行・delivery）に使う。
        """

        try:
            with self._lock.acquire(str(bundle) + ".lock", timeout=self._lock_timeout):
                v = validate(bundle)
                if is_err(v):
                    return v
                context = v.value
                attempted = audit_attempted()
                if is_err(attempted):
                    return Err(
                        error=ApprovalError.AUDIT_WRITE_FAILED.value,
                        detail=getattr(attempted, "detail", ""),
                    )
                run_id = make_run_id()
                consumed = do_consume(context, run_id)
                if is_err(consumed):
                    return consumed
                write_state(bundle, consumed.value)
                return Ok(
                    value=ConsumeOutcome(
                        run_id=run_id, state=consumed.value, context=context
                    )
                )
        except FileLockTimeout as exc:
            return Err(error=ApprovalError.LOCK_TIMEOUT.value, detail=str(exc))


# build_plan_id を re-export（publish の rebuild_plan_id で使う呼び出し側の便宜）
__all__ = [
    "ApprovalService",
    "ApprovalError",
    "PublishResult",
    "ConsumeOutcome",
    "fsync_dir",
    "write_bytes_durable",
    "write_json_durable",
    "build_plan_id",
]
