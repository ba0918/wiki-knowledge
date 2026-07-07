---
title: Trust Score — Wiki 記事の信頼度スコア
type: wiki
source_refs:
  - "raw/articles/20260406-trust-score-feature.md"
  - "raw/articles/20260707-trust-score-v2-query-retrieve.md"
created: 2026-04-06
updated: 2026-07-07
category: concepts
tags: [trust-score, quality, metrics, scoring, derived-value]
related:
  - "concepts/querylog.md"
  - "concepts/wiki-knowledge-architecture.md"
  - "concepts/llm-wiki-knowledge-base.md"
  - "concepts/gap-detection.md"
  - "concepts/graphify-knowledge-graph-concepts.md"
---

# Trust Score

Trust Score は、Wiki 記事ごとに「どの程度信頼できるか」を 0.0〜1.0 の範囲で定量評価するスコアリング機能である。4つの要素の加重合計として算出される。

## 背景と目的

[[llm-wiki-knowledge-base]] ([↗](llm-wiki-knowledge-base.md)) では、記事が compile や query promote で漸進的に増えていく。しかし全記事が同等に信頼できるわけではない。ソースが1つしかない記事、更新されずに放置された記事、他記事から参照されていない孤立した記事は、信頼性が低い可能性がある。

Trust Score はこれらの品質シグナルを統合し、記事の信頼度を数値化する。用途は3つ:

- **lint 時**: スコア 0.3 未満の記事を警告として検出
- **query 時**: retrieval pre-pass（`query_retrieve.py`）が各候補記事に trust を注釈し、0.30 未満の記事の引用には「（信頼度低）」が付される
- **運用時**: Wiki 全体の健全性を定期モニタリング

## 4つの計算要素（v2 — 絶対スケール）

| 要素 | 重み | v2 算式 | 根拠 |
|------|------|---------|------|
| ソース数 | 0.30 | `n / (n + 1)` | 1件=0.50、2件=0.67、3件=0.75。複数ソースで裏付けされた記事は信頼性が高い（逓減リターン） |
| 鮮度 | 0.20 | `0.5 ** (elapsed_days / 365)` | 半減期365日。1年=0.50、2年=0.25。**0にならない** — スナップショット方針では経過時間は「上流との乖離リスクの漸増」であって無効化ではない。`updated` 無しのみ 0.0 |
| 引用頻度 | 0.30 | `c / (c + 2)`（c = [[querylog]] ([↗](querylog.md)) の `sources_cited` 出現回数） | 実際に引用される記事は有用性が高い |
| backlink数 | 0.20 | `b / (b + 2)`（b = 他記事からの被参照数、deduplicated） | ハブ記事は構造的に重要 |

### 絶対スケール（v1 の min-max 正規化は廃止）

v1 では各要素の raw value を全記事にわたって min-max 正規化していたが、これはスコアを「wiki 内相対評価」にしてしまい、どんな完璧な wiki でも各要素の最下位が 0.00 に張り付く一方、警告閾値 0.30 は絶対値として扱われるという意味論の破綻があった（実測: 12記事中10記事が 0.30 未満）。v2 では全要素が記事単体で決まる絶対的な飽和カーブとなり、0.30 閾値が安定した意味を持つ。

### フォールバック（QueryLog 空）

QueryLog エントリが 0 件の場合、引用頻度を除外して残り3要素で再配分する:

| 要素 | 通常 | フォールバック |
|------|------|---------------|
| ソース数 | 0.30 | 0.40 |
| 鮮度 | 0.20 | 0.30 |
| 引用頻度 | 0.30 | 0.00 |
| backlink数 | 0.20 | 0.30 |

## Backlink の重複排除

同一記事から `related` フロントマターと本文 `[[wikilink]]` の両方で参照されていても **1回** としてカウントする。slug 正規化（`concepts/foo.md` → `foo`、`[[foo]]` → `foo`）の後に set で deduplicate する。自己参照はカウントしない。

## Derived Value としての設計

Trust Score はフロントマターに保存しない。これは [[wiki-knowledge-architecture]] ([↗](wiki-knowledge-architecture.md)) の3層分離原則に基づく重要な設計判断である。

- Trust Score は QueryLog の蓄積、他記事の追加・削除、時間経過で常に変動する **derived value**（派生値）
- フロントマターに書くと、状態変化のたびに即座に stale になる
- Knowledge 層の記事に Output 層のメタデータを混入させることになる

そのため、Trust Score は常にオンデマンドで計算し、結果は Output 層（`outputs/reports/`）にレポートとして出力する。

## 実装構造

計算ロジックは全て純粋関数で実装されている。`compute_trust_scores(articles, querylog_entries, today)` がメインの計算関数で、副作用を持たない。`today` は DI で注入可能であり、テスト容易性を確保している。

既存の `lint-wiki.py` の `parse_frontmatter()` / `find_wikilinks()` と、`querylog_stats.py` の `load_querylog()` をインポートして再利用する。

### CLI

```
python3 trust_score.py --wiki-root .wiki [--format table|json|report]
```

`report` 形式では `{wiki_root}/outputs/reports/{YYYYMMDD}-trust-score.md` に Markdown レポートを出力する。

### lint 統合

`lint-wiki.py` 自体は変更せず（構造チェック特化を維持）、SKILL.md の lint ワークフロー内でオーケストレーションとして統合する。単一責任原則を維持する設計である。同じパターンで [[gap-detection]] ([↗](gap-detection.md)) も lint ワークフローに統合されている。

## テスト

30件のテストケースで飽和カーブの境界値、半減期減衰、絶対スケール性（記事単体でスコアが決まること）、backlink の deduplicate、フォールバック重み配分、重み定数の合計検証などをカバーしている。

## 関連

エッジ単位の信頼度ラベルを graph 構造に統合する将来案については [[graphify-knowledge-graph-concepts]] ([↗](graphify-knowledge-graph-concepts.md)) を参照。

## 出典

- [Trust Score 実装解説](../raw/articles/20260406-trust-score-feature.md)
- [Trust Score v2 と Query Retrieval Pre-pass の設計](../raw/articles/20260707-trust-score-v2-query-retrieve.md)
