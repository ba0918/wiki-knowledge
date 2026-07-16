"""Tests for the transition-table-driven generic state machine.

汎用 transition 関数は status 集合と遷移表を引数に取り、既存の
consume_transition / approve_transition はその特殊化として整合する。
browser 側の seal-at-prepare 状態機械もこの汎用関数の上に載る。
"""

from __future__ import annotations

from lib.domain.tool_query import (
    SQL_TRANSITION_TABLE,
    TransitionError,
    TransitionTable,
    apply_transition,
)
from lib.domain.types import is_err, is_ok


class TestApplyTransition:
    def test_allowed_edge_returns_target_status(self) -> None:
        table = TransitionTable(
            initial="draft",
            edges={"draft": ("approved",), "approved": ("consumed",)},
            terminal=frozenset({"consumed"}),
        )
        result = apply_transition(table, current="draft", target="approved")
        assert is_ok(result)
        assert result.value == "approved"

    def test_disallowed_edge_is_rejected(self) -> None:
        table = TransitionTable(
            initial="draft",
            edges={"draft": ("approved",), "approved": ("consumed",)},
            terminal=frozenset({"consumed"}),
        )
        # draft から consumed への直行は宣言されていない
        result = apply_transition(table, current="draft", target="consumed")
        assert is_err(result)
        assert result.error == TransitionError.NOT_ALLOWED

    def test_transition_from_terminal_is_rejected(self) -> None:
        table = TransitionTable(
            initial="draft",
            edges={"draft": ("approved",), "approved": ("consumed",)},
            terminal=frozenset({"consumed"}),
        )
        result = apply_transition(table, current="consumed", target="approved")
        assert is_err(result)
        assert result.error == TransitionError.NOT_ALLOWED

    def test_unknown_current_status_is_rejected(self) -> None:
        table = TransitionTable(
            initial="draft",
            edges={"draft": ("approved",)},
            terminal=frozenset(),
        )
        result = apply_transition(table, current="bogus", target="approved")
        assert is_err(result)
        assert result.error == TransitionError.NOT_ALLOWED

    def test_branching_edges_are_supported(self) -> None:
        """browser の delivering → delivered / failed の分岐を表現できる。"""
        table = TransitionTable(
            initial="prepared",
            edges={
                "prepared": ("approved",),
                "approved": ("delivering",),
                "delivering": ("delivered", "failed"),
            },
            terminal=frozenset({"delivered", "failed", "expired"}),
        )
        assert is_ok(apply_transition(table, current="delivering", target="delivered"))
        assert is_ok(apply_transition(table, current="delivering", target="failed"))
        assert is_err(apply_transition(table, current="delivering", target="approved"))


class TestSqlTableConsistency:
    def test_sql_table_statuses_match_domain(self) -> None:
        assert SQL_TRANSITION_TABLE.statuses() == frozenset(
            {"draft", "approved", "consumed"}
        )

    def test_sql_table_specializes_existing_transitions(self) -> None:
        """SQL 遷移表は既存 consume/approve_transition と同じ許可/拒否を持つ。"""
        # draft → approved は許可、それ以外の approve 元は不許可
        assert is_ok(
            apply_transition(SQL_TRANSITION_TABLE, current="draft", target="approved")
        )
        assert is_err(
            apply_transition(
                SQL_TRANSITION_TABLE, current="approved", target="approved"
            )
        )
        # approved → consumed は許可、draft → consumed は不許可（未承認）
        assert is_ok(
            apply_transition(
                SQL_TRANSITION_TABLE, current="approved", target="consumed"
            )
        )
        assert is_err(
            apply_transition(
                SQL_TRANSITION_TABLE, current="draft", target="consumed"
            )
        )
