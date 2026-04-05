---
title: Trust Score — Wiki 記事の信頼度スコア
type: wiki
source_refs:
  - "raw/articles/20260406-trust-score-feature.md"
created: 2026-04-06
updated: 2026-04-06
category: concepts
tags: [trust-score, quality, metrics, scoring, derived-value]
related:
  - "concepts/querylog.md"
  - "concepts/wiki-knowledge-architecture.md"
  - "concepts/llm-wiki-knowledge-base.md"
---

# Trust Score

Trust Score は、Wiki 記事ごとに「どの程度信頼できるか」を 0.0〜1.0 の範囲で定量評価するスコアリング機能である。4つの要素の加重合計として算出される。

## 背景と目的

[[llm-wiki-knowledge-base]] では、記事が compile や query promote で漸進的に増えていく。しかし全記事が同等に信頼できるわけではない。ソースが1つしかない記事、更新されずに放置された記事、他記事から参照されていない孤立した記事は、信頼性が低い可能性がある。

Trust Score はこれらの品質シグナルを統合し、記事の信頼度を数値化する。用途は3つ:

- **lint 時**: スコア 0.3 未満の記事を警告として検出
- **query 時**: 引用元の信頼度を把握（Phase 3b で統合予定）
- **運用時**: Wiki 全体の健全性を定期モニタリング

## 4つの計算要素

| 要素 | 重み | raw value | 根拠 |
|------|------|-----------|------|
| ソース数 | 0.30 | `len(source_refs)` | 複数ソースで裏付けされた記事は信頼性が高い |
| 鮮度 | 0.20 | `max(0.0, 1.0 - elapsed_days / 365)` | 線形減衰。365日で 0.0 |
| 引用頻度 | 0.30 | [[querylog]] の `sources_cited` 出現回数 | 実際に引用される記事は有用性が高い |
| backlink数 | 0.20 | 他記事からの被参照数（deduplicated） | ハブ記事は構造的に重要 |

### 正規化

各要素の raw value を全記事にわたって min-max 正規化し、0.0〜1.0 に変換する。エッジケース対応として、記事数が 3 未満の場合は全要素を 0.5 固定とする。min と max が等しい場合も同様。

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

Trust Score はフロントマターに保存しない。これは [[wiki-knowledge-architecture]] の3層分離原則に基づく重要な設計判断である。

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

`lint-wiki.py` 自体は変更せず（構造チェック特化を維持）、SKILL.md の lint ワークフロー内でオーケストレーションとして統合する。単一責任原則を維持する設計である。

## テスト

28件のテストケースで正規化の境界値、backlink の deduplicate、フォールバック重み配分、鮮度の線形減衰、重み定数の合計検証などをカバーしている。

## 出典

- [Trust Score 実装解説](../raw/articles/20260406-trust-score-feature.md)
