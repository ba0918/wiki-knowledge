"""Step 7 — B1 プロトタイプ実測: 誤成功系 corpus に対する検証契約の拒否率.

登録壁打ちの中心質問は「このツールで『正しいデータを取れた』ことを何で確認できるか」。
正常系1件では語彙 v1 の過不足を測れないため、誤成功系 corpus（誤セレクタ→別 tenant /
filter 未反映 / pagination 欠落 / 部分 download / HTML エラーページ 200 / truncation /
空結果 / 主キー重複）を用意し、**各変異が全て拒否**され**正常系が誤拒否されない**ことを
機械検証する（= 反証 fixture による登録合格条件、guide §15）。

実 chromium なしで測るため、フロー実行の成果（ExtractionResult）を fake で注入する。
検証契約 enforce は browser 非依存の pure ロジック（browser_contract）なので、この
実測は決定的判定規則そのものを測る。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.browser_flow_runner import ExtractionResult
from lib.service.clock import FixedClock
from lib.service.file_lock import RealFileLock

import browser_extract_run as cli

NOW = "2026-07-16T12:00:00Z"


class FakeExtractor:
    def __init__(self, result: ExtractionResult) -> None:
        self._result = result

    def extract(self, *, entry, params, session_state, deadline_monotonic):
        from lib.domain.types import Ok

        return Ok(value=self._result)


import tempfile


def make_wiki(tmp_path: Path) -> Path:
    wiki_root = Path(tempfile.mkdtemp(dir=tmp_path, prefix="wiki-"))
    (wiki_root / "tools" / "flows").mkdir(parents=True)
    (wiki_root / "deliveries").mkdir()
    (wiki_root / "outputs").mkdir()
    flow = wiki_root / "tools" / "flows" / "reports.py"
    flow.write_text("def run(ctx, params):\n    return None\n", encoding="utf-8")
    flow_sha = hashlib.sha256(flow.read_bytes()).hexdigest()
    catalog = {
        "schema_version": 1,
        "tools": [
            {
                "tool_id": "events-web",
                "type": "browser",
                "flow": {"ref": "reports.py", "sha256": flow_sha},
                "auth": {"profile": "none"},
                "origin_allowlist": [
                    {"method": "GET", "path_prefix": "/reports", "resource_type": "document"}
                ],
                "tier": "B1",
                "guarantees": {
                    "integrity": "guaranteed",
                    "identity": "guaranteed",
                    "filter_correctness": "guaranteed",
                    "completeness": "guaranteed",
                    "human_verification": "required",
                },
                # 正しさ（誤成功検出）+ 独立 anchor を複数組み合わせる
                "checks": [
                    {"check": "filter_readback", "param": "period"},
                    {"check": "row_count_range", "min": 1, "max": 100},
                    {"check": "ui_total_vs_file_rows"},
                    {"check": "primary_key_unique", "column": "user_id"},
                    {"check": "tenant_id_match", "expected_value": "svc-readonly"},
                ],
                "params_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "period": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}$"}
                    },
                },
                "limits": {
                    "max_rows": 10000,
                    "max_result_bytes": 10485760,
                    "max_cell_bytes": 4096,
                    "max_artifact_bytes": 10485760,
                    "max_flow_seconds": 120,
                    "max_unapproved_bundles": 20,
                },
                "retention": {"trace": "off", "screenshot": "off", "ttl_hours": 24},
                "delivery": {"allowed_dirs": ["deliveries"]},
                "account": {"id": "svc-readonly", "origin": "https://app.example.com"},
            }
        ],
    }
    (wiki_root / "tools" / "browser-catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return wiki_root


def result(**overrides) -> ExtractionResult:
    args = dict(
        columns=("user_id", "email"),
        rows=((1, "a@example.com"), (2, "b@example.com")),
        artifact_bytes=b"user_id,email\r\n1,a@example.com\r\n2,b@example.com\r\n",
        readbacks={"period": "2026-07"},
        ui_total=2,
        account_id="svc-readonly",
        screen_fingerprint="fp-main",
        extracted_at=NOW,
    )
    args.update(overrides)
    return ExtractionResult(**args)


def run_prepare(tmp_path: Path, extraction: ExtractionResult):
    wiki_root = make_wiki(tmp_path)
    runner = cli.BrowserRunner(
        wiki_root=wiki_root,
        clock=FixedClock(now=NOW),
        lock=RealFileLock(),
        extractor=FakeExtractor(extraction),
        nonce=lambda: "aa00",
    )
    return runner.prepare(
        tool_id="events-web", params={"period": "2026-07"}, deliver_to="deliveries"
    )


# 誤成功系 corpus: (名前, ExtractionResult 変異, 期待拒否 reason)
CORPUS = [
    (
        "filter_not_applied",  # フィルタ未反映（UI に別期間が出ている）
        result(readbacks={"period": "2020-01"}),
        "readback_mismatch",
    ),
    (
        "wrong_tenant",  # 誤セレクタ等で別 tenant の同型画面を取った
        result(account_id="other-tenant"),
        "tenant_mismatch",
    ),
    (
        "pagination_missing",  # UI は 2 件だがファイルは 1 件（ページ欠落）
        result(rows=((1, "a@example.com"),), ui_total=2),
        "ui_total_mismatch",
    ),
    (
        "partial_download",  # 部分 download（UI 総数とファイル行数の乖離）
        result(rows=((1, "a@example.com"),), ui_total=2),
        "ui_total_mismatch",
    ),
    (
        "truncation",  # 途中で切れた（同上の乖離）
        result(rows=((1, "a@example.com"),), ui_total=2),
        "ui_total_mismatch",
    ),
    (
        "html_error_page_200",  # 200 だが中身はエラーページ → データ行なし
        result(rows=(), ui_total=0),
        "row_count_out_of_range",
    ),
    (
        "empty_result",  # 空結果
        result(rows=(), ui_total=0),
        "row_count_out_of_range",
    ),
    (
        "duplicate_rows",  # 二重取得（主キー重複）
        result(
            rows=((1, "a@example.com"), (1, "b@example.com")),
            ui_total=2,
        ),
        "duplicate_primary_key",
    ),
]


class TestFalseSuccessCorpus:
    def test_normal_case_is_not_false_rejected(self, tmp_path: Path) -> None:
        assert is_ok(run_prepare(tmp_path, result()))

    @pytest.mark.parametrize("name,mutation,expected", CORPUS, ids=[c[0] for c in CORPUS])
    def test_each_false_success_mutation_is_rejected(
        self, tmp_path: Path, name: str, mutation: ExtractionResult, expected: str
    ) -> None:
        outcome = run_prepare(tmp_path, mutation)
        assert is_err(outcome), f"{name} が拒否されなかった（誤成功を見逃した）"
        assert outcome.error == expected, f"{name}: 期待 {expected} != {outcome.error}"

    def test_corpus_rejection_rate_is_total(self, tmp_path: Path) -> None:
        """全変異の拒否率 = 100%、正常系の誤拒否 = 0 を定量確認（語彙 v1 合格条件）。"""
        rejected = sum(
            1 for _, mut, _ in CORPUS if is_err(run_prepare(tmp_path, mut))
        )
        assert rejected == len(CORPUS)
        assert is_ok(run_prepare(tmp_path, result()))
