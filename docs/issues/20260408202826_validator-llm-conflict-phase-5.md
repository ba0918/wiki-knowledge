---
title: Validator 第2段 - LLM ベース矛盾検出 (Phase 5+)
status: open
created: 2026-04-08 20:28:28
tags: source-agnostic-pipeline,phase-5,scope-cut,validator,llm,claim
source: docs/plans/20260408163658_source-agnostic-knowledge-pipeline.md
---

## 概要

Source-Agnostic Knowledge Pipeline の Phase 2 Validator は rule-based 3 種
（structural / link / frontmatter）のみ実装し、claim 間の semantic conflict を
LLM で検出する第2段は Phase 5+ に送った（Q12-2 A 案採用）。
claim_refs の蓄積量と rule-based では検出できない conflict のニーズが
見えたタイミングで着手する。

## 備考

### スコープアウト理由
- Q12-2 A 案（implementation 最小主義）に準拠
- claim 抽象（subject × attribute × period）による rule-based 検出で初期価値は出せる
- LLM コスト・精度不安定性を初期 MVP に持ち込まない
- 「自動で警報、手動で裁定」原則（LLM は候補抽出と説明生成のみ、最終判定器にしない）を Phase 5+ でも維持

### 着手判断基準
- Phase 2 完了後、以下が蓄積されたら着手検討:
  - claim_refs が一定量蓄積され、rule-based で検出できない conflict が運用で目立ち始めた
  - 同一 subject × attribute で period が交差する claim が頻出し、人力裁定が追いつかない
  - LLM ベースの候補抽出（最終判定器ではなく suggest）でユーザー体験が向上する見込みが立った

### 関連ファイル
- plan 本体: `docs/plans/20260408163658_source-agnostic-knowledge-pipeline.md`
- claim 抽象: plan の「claim 完全運用」セクション参照
- 関連記事: `.wiki/concepts/trust-score.md`

---

> **Note:** Do not include sensitive information (passwords, tokens, personal data, etc.) in this file.
