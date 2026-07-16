"""Contract tests for tool_approval.ApprovalService — 承認ライフサイクルの
順序保証（single-use / TTL / audit-first）を connector 非依存の stub adapter で
機械検証する.

ここで固定する契約は SQL runner（test_tool_query_runner.py）と browser adapter
（Step 6）の両方が満たす。stub adapter は最小の file-based bundle（state.json +
proposal.json）で core の順序だけを検証し、SQL の PlanState 形は焼き込まない。
"""

from __future__ import annotations

import json
from pathlib import Path

from lib.domain.tool_query import (
    RejectReason,
    TransitionError,
    TransitionTable,
    apply_transition,
    compute_expires_at,
    is_expired,
    sha256_hex,
)
from lib.domain.types import Err, Ok, is_err, is_ok
from lib.service.file_lock import RealFileLock
from lib.service.tool_approval import (
    ApprovalError,
    ApprovalService,
    write_json_durable,
)


NOW = "2026-07-16T12:00:00Z"
LATER = "2026-07-18T00:00:00Z"

STUB_TABLE = TransitionTable(
    initial="draft",
    edges={"draft": ("approved",), "approved": ("consumed",)},
    terminal=frozenset({"consumed"}),
)


class AuditSpy:
    """順序検証用の監査スパイ。特定イベントだけ失敗させられる。"""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.events: list[str] = []
        self._fail = fail or set()

    def record(self, event: str):
        if event in self._fail:
            return Err(error="write_failed", detail="fake audit failure")
        self.events.append(event)
        return Ok(value=None)


def make_service(plans_root: Path, **overrides) -> ApprovalService:
    args = dict(plans_root=plans_root, lock=RealFileLock(), lock_timeout=5.0)
    args.update(overrides)
    return ApprovalService(**args)


def read_status(bundle: Path) -> str:
    return json.loads((bundle / "state.json").read_text(encoding="utf-8"))["status"]


def prepare_stub_bundle(
    service: ApprovalService, plan_id: str, *, expires_at: str
) -> Path:
    proposal = {"plan_id": plan_id, "expires_at": expires_at}
    proposal_bytes = json.dumps(proposal, sort_keys=True).encode("utf-8")

    def build_files(final_plan_id: str) -> dict[str, bytes]:
        return {
            "proposal.json": proposal_bytes,
            "state.json": (
                json.dumps({"status": "draft"}, sort_keys=True) + "\n"
            ).encode("utf-8"),
        }

    result = service.publish_bundle(
        plan_id=plan_id,
        rebuild_plan_id=lambda: plan_id,
        build_files=build_files,
    )
    assert is_ok(result), result
    return result.value.bundle_dir


def approve_callbacks(bundle: Path, spy: AuditSpy, *, now: str, expected_digest: str):
    def validate(b: Path):
        proposal = json.loads((b / "proposal.json").read_text(encoding="utf-8"))
        digest_now = sha256_hex((b / "proposal.json").read_bytes())
        if digest_now != expected_digest:
            return Err(error=RejectReason.PROPOSAL_DIGEST_MISMATCH.value)
        if is_expired(now=now, expires_at=proposal["expires_at"]):
            return Err(error=RejectReason.TTL_EXPIRED.value)
        state = json.loads((b / "state.json").read_text(encoding="utf-8"))
        return Ok(value=state)

    def do_approve(state: dict):
        t = apply_transition(
            STUB_TABLE, current=state["status"], target="approved"
        )
        if is_err(t):
            return Err(error=RejectReason.ALREADY_APPROVED.value)
        return Ok(value={**state, "status": "approved"})

    def write_state(b: Path, new: dict) -> None:
        write_json_durable(b / "state.json", new)

    return validate, do_approve, (lambda: spy.record("approved")), write_state


def consume_callbacks(bundle: Path, spy: AuditSpy):
    def validate(b: Path):
        state = json.loads((b / "state.json").read_text(encoding="utf-8"))
        t = apply_transition(STUB_TABLE, current=state["status"], target="consumed")
        if is_err(t):
            reason = (
                RejectReason.ALREADY_CONSUMED.value
                if state["status"] == "consumed"
                else RejectReason.NOT_APPROVED.value
            )
            spy.record("rejected")
            return Err(error=reason)
        return Ok(value=state)

    def do_consume(state: dict, run_id: str):
        return Ok(value={**state, "status": "consumed", "run_id": run_id})

    def write_state(b: Path, new: dict) -> None:
        write_json_durable(b / "state.json", new)

    return validate, (lambda: spy.record("execute_attempted")), do_consume, write_state


def digest_of(bundle: Path) -> str:
    return sha256_hex((bundle / "proposal.json").read_bytes())


class TestPublish:
    def test_publish_creates_bundle_with_initial_state(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        bundle = prepare_stub_bundle(
            service, "20260716120000-aa00-stub", expires_at=compute_expires_at(created_at=NOW)
        )
        assert bundle.is_dir()
        assert read_status(bundle) == "draft"

    def test_final_name_collision_fails_without_leftover_staging(
        self, tmp_path: Path
    ) -> None:
        service = make_service(tmp_path)
        (tmp_path / "20260716120000-aa00-stub").mkdir()
        result = service.publish_bundle(
            plan_id="20260716120000-aa00-stub",
            rebuild_plan_id=lambda: "20260716120000-aa00-stub",
            build_files=lambda pid: {"state.json": b"{}"},
        )
        assert is_err(result)
        assert result.error == ApprovalError.PLAN_CONFLICT.value
        assert not any(p.name.startswith(".staging-") for p in tmp_path.iterdir())


class TestApproveCas:
    def test_approve_transitions_draft_to_approved(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        bundle = prepare_stub_bundle(
            service, "20260716120000-aa00-stub", expires_at=compute_expires_at(created_at=NOW)
        )
        spy = AuditSpy()
        v, da, aa, ws = approve_callbacks(
            bundle, spy, now=NOW, expected_digest=digest_of(bundle)
        )
        result = service.approve_cas(
            bundle=bundle, validate=v, do_approve=da, audit_approved=aa, write_state=ws
        )
        assert is_ok(result), result
        assert read_status(bundle) == "approved"
        assert spy.events == ["approved"]

    def test_stale_digest_is_rejected(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        bundle = prepare_stub_bundle(
            service, "20260716120000-aa00-stub", expires_at=compute_expires_at(created_at=NOW)
        )
        spy = AuditSpy()
        v, da, aa, ws = approve_callbacks(
            bundle, spy, now=NOW, expected_digest="0" * 64
        )
        result = service.approve_cas(
            bundle=bundle, validate=v, do_approve=da, audit_approved=aa, write_state=ws
        )
        assert is_err(result)
        assert result.error == RejectReason.PROPOSAL_DIGEST_MISMATCH.value
        assert read_status(bundle) == "draft"

    def test_expired_plan_cannot_be_approved(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        bundle = prepare_stub_bundle(
            service, "20260716120000-aa00-stub", expires_at=compute_expires_at(created_at=NOW)
        )
        spy = AuditSpy()
        v, da, aa, ws = approve_callbacks(
            bundle, spy, now=LATER, expected_digest=digest_of(bundle)
        )
        result = service.approve_cas(
            bundle=bundle, validate=v, do_approve=da, audit_approved=aa, write_state=ws
        )
        assert is_err(result)
        assert result.error == RejectReason.TTL_EXPIRED.value
        assert read_status(bundle) == "draft"

    def test_audit_first_keeps_state_draft_on_audit_failure(
        self, tmp_path: Path
    ) -> None:
        """approved 監査が書けなければ state を書かない（audit-first 順序）。"""
        service = make_service(tmp_path)
        bundle = prepare_stub_bundle(
            service, "20260716120000-aa00-stub", expires_at=compute_expires_at(created_at=NOW)
        )
        spy = AuditSpy(fail={"approved"})
        v, da, aa, ws = approve_callbacks(
            bundle, spy, now=NOW, expected_digest=digest_of(bundle)
        )
        result = service.approve_cas(
            bundle=bundle, validate=v, do_approve=da, audit_approved=aa, write_state=ws
        )
        assert is_err(result)
        assert result.error == ApprovalError.AUDIT_WRITE_FAILED.value
        assert read_status(bundle) == "draft"


class TestConsumeCas:
    def _approve(self, service, bundle) -> None:
        spy = AuditSpy()
        v, da, aa, ws = approve_callbacks(
            bundle, spy, now=NOW, expected_digest=digest_of(bundle)
        )
        assert is_ok(
            service.approve_cas(
                bundle=bundle,
                validate=v,
                do_approve=da,
                audit_approved=aa,
                write_state=ws,
            )
        )

    def test_consume_spends_authorization_single_use(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        bundle = prepare_stub_bundle(
            service, "20260716120000-aa00-stub", expires_at=compute_expires_at(created_at=NOW)
        )
        self._approve(service, bundle)
        spy = AuditSpy()
        v, aa, dc, ws = consume_callbacks(bundle, spy)
        first = service.consume_cas(
            bundle=bundle,
            validate=v,
            audit_attempted=aa,
            make_run_id=lambda: "20260716120000-rr00-stub",
            do_consume=dc,
            write_state=ws,
        )
        assert is_ok(first), first
        assert read_status(bundle) == "consumed"
        assert spy.events == ["execute_attempted"]

        # 二度目は replay として拒否（single-use）
        second = service.consume_cas(
            bundle=bundle,
            validate=v,
            audit_attempted=aa,
            make_run_id=lambda: "20260716120000-rr01-stub",
            do_consume=dc,
            write_state=ws,
        )
        assert is_err(second)
        assert second.error == RejectReason.ALREADY_CONSUMED.value
        assert spy.events[-1] == "rejected"

    def test_consume_before_approve_is_rejected(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        bundle = prepare_stub_bundle(
            service, "20260716120000-aa00-stub", expires_at=compute_expires_at(created_at=NOW)
        )
        spy = AuditSpy()
        v, aa, dc, ws = consume_callbacks(bundle, spy)
        result = service.consume_cas(
            bundle=bundle,
            validate=v,
            audit_attempted=aa,
            make_run_id=lambda: "20260716120000-rr00-stub",
            do_consume=dc,
            write_state=ws,
        )
        assert is_err(result)
        assert result.error == RejectReason.NOT_APPROVED.value
        assert read_status(bundle) == "draft"
        assert spy.events == ["rejected"]

    def test_attempted_audit_failure_stops_before_consume(
        self, tmp_path: Path
    ) -> None:
        """execute_attempted が書けなければ consume しない（fail closed）。"""
        service = make_service(tmp_path)
        bundle = prepare_stub_bundle(
            service, "20260716120000-aa00-stub", expires_at=compute_expires_at(created_at=NOW)
        )
        self._approve(service, bundle)
        spy = AuditSpy(fail={"execute_attempted"})
        v, aa, dc, ws = consume_callbacks(bundle, spy)
        result = service.consume_cas(
            bundle=bundle,
            validate=v,
            audit_attempted=aa,
            make_run_id=lambda: "20260716120000-rr00-stub",
            do_consume=dc,
            write_state=ws,
        )
        assert is_err(result)
        assert result.error == ApprovalError.AUDIT_WRITE_FAILED.value
        assert read_status(bundle) == "approved"  # 承認は消費されていない
