---
title: graphify から学ぶ知識グラフ構築の概念
type: wiki
source_refs:
  - "raw/articles/20260407-graphify-knowledge-graph-concepts.md"
created: 2026-04-07
updated: 2026-04-07
category: concepts
tags: [knowledge-graph, graphify, design-patterns, confidence, clustering]
related:
  - "concepts/wiki-knowledge-architecture.md"
  - "concepts/trust-score.md"
  - "concepts/gap-detection.md"
  - "concepts/querylog.md"
  - "concepts/llm-wiki-knowledge-base.md"
---

# graphify から学ぶ知識グラフ構築の概念

[graphify](https://github.com/safishamsi/graphify) は、コード・ドキュメント・論文・画像などのマルチモーダル入力を**知識グラフに変換**し、LLM ナビゲーション用に構造化する Claude Skill である。Karpathy の `/raw` フォルダワークフロー（[[llm-wiki-knowledge-base]] ([↗](llm-wiki-knowledge-base.md)) と同系統の発想）に着想を得ているが、graphify は**グラフ構造を陽に持つ**点が特徴で、wikilink ベースの Wiki に適用可能な設計パターンをいくつか提供する。

## 7 段階パイプライン

graphify のコアは以下のパイプラインで構成される:

```
detect → extract → build_graph → cluster → analyze → report → export
```

[[wiki-knowledge-architecture]] ([↗](wiki-knowledge-architecture.md)) の Ingest → Compile → Index の 4 相パイプラインと比較すると、`extract` と `compile` がほぼ対応する一方で、graphify には `cluster` と `analyze` という独立したグラフ分析フェーズが存在する。これがフラットな記事カタログ + wikilink 構造の Wiki に欠けている要素である。

## 主要設計パターン

### 1. エッジ単位の信頼度 3 段階ラベル

graphify はエッジ単位で `confidence` 属性を持つ:

- **EXTRACTED**: ソースに明示的に書かれている関係
- **INFERRED**: LLM 推論による関係（数値の `confidence_score` 付き）
- **AMBIGUOUS**: 要レビュー

これは [[trust-score]] ([↗](trust-score.md)) が記事単位で算出する 4 要素スコアとは**異なる粒度**で、どの主張・どの関係が推論かを構造的に追跡できる。Knowledge Graph 専門家の評価では、エッジ単位は「記事単位（粗すぎる）と claim 単位（運用負荷大）の折衷案」として実装容易性と精度のバランスが良い。

> [推測] wiki-knowladge に取り込む場合、graph.json の edge 属性として保持するのが二重管理を避ける筋となる。

### 2. immutable extraction schema

graphify のスキーマは「いつ抽出されたか」「何が根拠か」を frozen に保ち、後から書き換えない。これにより derived value（信頼度伝播・clustering 結果）が再現可能になる。

[[trust-score]] ([↗](trust-score.md)) が「derived value のためフロントマターに保存しない」としている設計と同じ思想で、**source of truth と派生値の明確な分離**が重要であることを示している。

### 3. Leiden ベースの自動クラスタリング

embeddings を使わず、グラフ密度のみで community を検出する。設定値:

- `_MAX_COMMUNITY_FRACTION = 0.25`: 全体の 25% を超えるクラスタは再帰的に分割
- `_MIN_SPLIT_SIZE = 10`: 最小分割サイズ

[[gap-detection]] ([↗](gap-detection.md)) の現状実装は QueryLog の `gap_topics` 集計ベースだが、Leiden を組み合わせると「**同じクラスタ内で参照されるべきだが wikilink がない記事ペア**」を自動検出できる。

> [推測] ただし wiki-knowladge の規模（記事数十）では Leiden の統計的有意性が落ちるため、`graspologic` 等の依存追加に見合うかは判断が分かれる。Knowledge Graph 専門家の評価では「記事数 < 50 では centrality 指標の変動が大きく、ランキングよりカテゴリ分けの方が安定」とされる。

### 4. God nodes と surprise detection

graphify の `analyze.py` は以下を検出する:

- **God nodes**: ファイルノードを除外した最高次数ノード（過度に仲介している概念）
- **Surprising connections**: cross-file の non-obvious な関係

ただし「God node = 悪」ではない。健全な knowledge graph は**スケールフリー特性**を持つ。検出する場合の正しい指標は「**hub fail で何記事が孤立するか**」（isolation risk）。

### 5. Hyperedge（3+ ノードのグループ関係）

「複数クラスが共通インターフェース実装」「複数ステップの認証フロー」のような **3 個以上のノードを 1 つの関係でグループ化**する構造を、通常の 2 項エッジとは別に持つ。フラットな wikilink では表現しづらい関係を扱える。

### 6. SHA256 キャッシュ + インクリメンタル

ファイル単位の SHA256 を記録し、変更があったファイルのみ再 extract する。100+ ファイル規模でインクリメンタル更新を可能にする運用パターン。

> [推測] wiki-knowladge に適用する場合、LLM 出力の non-determinism のため cache invalidation 戦略が複雑化する。determinism guarantee（temperature=0 + 固定 seed、または LLM params を cache key に含める）を確保してから導入するのが安全。

## wiki-knowladge への適用候補と判断

graphify の概念を取り込む候補として 7 案を抽出し、team-brainstorm で評価した結果、以下の方針が合意された。

| 候補 | 判定 | 理由 |
|------|------|------|
| エッジ単位の信頼度ラベル | 拡張余地として予約 | graph.json の edge 属性として `_custom`/`claim_id` を予約地に置く |
| SHA256 キャッシュ | **defer** | LLM non-determinism のため determinism test 完了後に導入 |
| Leiden クラスタリング | **defer** | 記事数規模で価値が出るのを待つ |
| クロスファイル surprise 検出 | **defer** | querylog データ不足 + 偽陽性検証必要（3 段階フィルタ要） |
| God nodes lint | **defer** | isolation risk として再定義してから |
| Audit trail レポート | **defer** | 既存 lint レポートで部分的に代替 |
| Hyperedge | **defer** | 将来のスキーマ拡張で対応 |

代わりに**最初の一歩**として合意された MVP は、graphify から「グラフを陽に持つ」というアイデアのみを取り込み、以下の 3 層構造で実装する:

```
.wiki/concepts/*.md  (source of truth)
        ↓ inventory.py (pure)
.wiki/outputs/inventory.json  (derived, deterministic)
        ↓ graph_gen.py (pure)
.wiki/outputs/graph.json  (read-only view)
        ↓ consumer
lint-wiki.py（[[gap-detection]] / [[trust-score]] は将来）
```

設計上の重要な合意事項:

- **graph.json は read-only VIEW** に限定。source of truth は frontmatter + wikilink のまま
- **拡張余地スキーマ**: `version: "1.0"` 必須、edge に `weight / co_citation_count / co_citation_frequency / confidence / sources[] / claim_id / _custom` を予約地として最初から含める
- **inventory.json 中間層**で二重管理を回避
- **graph_gen.py が graph.json を所有**（compile は LLM 呼び出しに専念、lint は consumer に徹する）

実装計画は `docs/plans/20260407183028_wiki-graph-layer-mvp.md` を参照。

## 設計上の学び

### 粒度問題

信頼度を「記事単位 / エッジ単位 / claim 単位」のどこに持たせるかは設計の核心。[[trust-score]] ([↗](trust-score.md)) が記事単位で動いている上に claim 単位を重ねると二重管理になるため、エッジ単位（graph.json の edge 属性）が最も clean な拡張点となる。

### 小規模グラフの統計脆弱性

記事数 < 50 では centrality 指標（PageRank、betweenness 等）の変動が大きい。「上位 5 記事」のようなランキングよりも「カテゴリ分け」（グループ）の方が安定する。これは Leiden や God nodes の MVP 採用を見送った主因。

### missing edge ≠ related

「security」query で oauth / SAML / JWT が共起しても、それは「分類バラバラ」のサインで本来 related ではない可能性がある。共引用検出（[[querylog]] ([↗](querylog.md)) の `sources_cited` を起点とする）の偽陽性は事前検証必須で、最低でも以下の 3 段階フィルタが必要:

1. `co_citation_frequency ≥ 0.08`
2. 既存パス長 > 2-hop（既に近接していない）
3. domain coherence（同カテゴリ内）

### scale-free は健全

健全な knowledge graph は自然にスケールフリー特性を持つ。graphify の god nodes 検出をそのまま導入すると false positive が多くなるため、「hub fail で何記事が孤立するか」という isolation risk として再定義する必要がある。

## 出典

- [graphify リポジトリ](https://github.com/safishamsi/graphify)（clone: `examples/graphify/`）
- [graphify 調査ソース記事](../raw/articles/20260407-graphify-knowledge-graph-concepts.md)
- 主要参照: `examples/graphify/{ARCHITECTURE.md, validate.py, cluster.py, analyze.py, cache.py, report.py}`
- 関連: [[wiki-knowledge-architecture]] ([↗](wiki-knowledge-architecture.md)), [[trust-score]] ([↗](trust-score.md)), [[gap-detection]] ([↗](gap-detection.md)), [[querylog]] ([↗](querylog.md)), [[llm-wiki-knowledge-base]] ([↗](llm-wiki-knowledge-base.md))
