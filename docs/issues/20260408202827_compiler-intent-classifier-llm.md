---
title: Compiler intent_classifier の LLM 化検討
status: open
created: 2026-04-08 20:28:28
tags: source-agnostic-pipeline,phase-2,scope-cut,compiler,llm,intent-classifier
source: docs/plans/20260408163658_source-agnostic-knowledge-pipeline.md
---

## 概要

Source-Agnostic Knowledge Pipeline の Phase 2 Compiler の intent_classifier は
当初 rule-based で実装する（キーワード/パターンマッチング）（Q12-2 A 案採用）。
Phase 2 完了後に実運用での精度を評価し、rule-based で不足する場合は
LLM ベースへ移行するか判断する。

## 備考

### スコープアウト理由
- Q12-2 A 案（implementation 最小主義）に準拠
- rule-based は deterministic でテストしやすく、Phase 2 の MVP 価値検証に十分
- LLM コスト・呼び出し遅延を初期から持ち込まない

### 着手判断基準
- Phase 2 実装時に rule-based 誤分類率の計測方法を定義する
- 判断基準（例）:
  - 誤分類率が X%（閾値は Phase 2 で決定）を超過
  - rule パターンの追加が急増し、メンテナンスコストが LLM 呼び出しコストを上回る
  - 4 記事型（decision / runbook / reference / concept）以外の分類が増え、rule 網羅が困難になった

### 関連ファイル
- plan 本体: `docs/plans/20260408163658_source-agnostic-knowledge-pipeline.md`
- 記事型定義: plan の「記事型」セクション参照

### 設計メモ
- LLM 化しても「LLM は候補抽出と説明生成のみ、最終判定器にしない」原則は維持
- 決定論的 mock（deterministic mock）でテスト可能な形で DI する

---

> **Note:** Do not include sensitive information (passwords, tokens, personal data, etc.) in this file.
