---
title: trust_score/gap_detect の graph layer 未統合（検出ロジック二重実装）の解消
status: open
created: 2026-07-03 21:32:43
tags: refactor,graph-layer,trust-score,gap-detect
source:
---

## 概要

trust_score.py / gap_detect.py が graph layer（`.wiki/outputs/graph.json`）を消費せず、backlink 等を独自再計算している二重実装を解消する。

architecture.md は「dead_link / orphan / backlink の検出は graph layer に一元化」と謳うが、実際に graph.json を消費するのは lint-wiki.py のみ:

- `trust_score.py` の `count_backlinks`（trust_score.py:156 付近）は `concepts/*.md` を独自パースして backlink を再計算している
- `gap_detect.py` も独自トークン照合でカバレッジを判定している

graph_gen.py の出力（nodes / edges）を消費する形にリファクタし、検出ロジックの二重実装を排除する。動作自体は正しいため緊急性は低い。

関連して、lint-wiki.py のハイフン付きファイル名が原因で `trust_score.py:30` / `gap_detect.py:26` が importlib + `sys.modules` 手動登録で兄弟モジュールをロードしている脆い実装の解消も同時に検討する（共有ヘルパーを `lib/` に移して通常 import に置き換える案）。

## 備考

- 2026-07-03 のプロジェクト実用性分析セッションで検出
- 受け入れ基準の目安:
  - trust_score の backlink 計算と gap_detect のカバレッジ照合が graph.json（または lib/ 共通層）経由になる
  - graph 経由と従来計算の結果一致をテストで担保（lint の TestGraphConsumerMode と同じパターン）
  - `importlib.util.spec_from_file_location` による lint-wiki.py 直接ロードが解消される
  - 既存テスト全 pass（regression なし）

---

> **Note:** Do not include sensitive information (passwords, tokens, personal data, etc.) in this file.
