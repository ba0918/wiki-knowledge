---
title: graphify から学ぶ知識グラフ構築の概念とパターン
scraped: 2026-04-07
tags: [knowledge-graph, graphify, design-patterns, brainstorm-summary]
---

# graphify から学ぶ知識グラフ構築の概念とパターン

本ドキュメントは、`safishamsi/graphify` リポジトリ（`examples/graphify/` に clone 済み）の調査と、その概念を wiki-knowledge プロジェクトに取り込むための team-brainstorm セッション (Round 1-3) の成果をまとめたもの。出典は graphify のソースコード、ARCHITECTURE.md、および 5 ロール (Challenger / Explorer / Connector / Grounded / Knowledge Graph Expert) の議論。

## graphify とは

graphify は **マルチモーダル入力 (コード / ドキュメント / 論文 / 画像) を知識グラフに変換し、LLM ナビゲーション用に構造化する Claude Skill**。Andrej Karpathy の `/raw` フォルダワークフローに着想を得ている。コアは 7 段階パイプライン:

```
detect → extract → build_graph → cluster → analyze → report → export
```

主な特徴:

- **2 パス抽出**: AST による決定論的コード解析と Claude による semantic extraction を並列実行 (`extract.py:107`)
- **ハイブリッド信頼度ラベル**: `EXTRACTED` (明示的) / `INFERRED` (推論、`confidence_score` 付き) / `AMBIGUOUS` (要レビュー) の 3 段階 (`validate.py:1-63`)
- **トポロジーベース clustering**: Leiden community detection を embeddings 不要で実行。グラフ密度のみで community を特定 (`cluster.py:1-80`)
- **God nodes / surprise detection**: ファイルノードを除外した最高次数ノード、cross-file の non-obvious edge を検出 (`analyze.py:39-90`)
- **SHA256 キャッシュ**: 変更ファイルのみ再処理、git hook 統合で自動更新 (`cache.py:77-116`)
- **Audit trail markdown レポート**: communities / surprises / hyperedges / token cost を人間可読形式で出力 (`report.py:7-100`)
- **71.5x トークン削減**: 初回は extraction コスト、以降の query は compact JSON を読むため複利的に削減

## graphify の主要設計パターン

### 1. 信頼度の 3 段階ラベル制 (エッジ単位)

```python
REQUIRED_EDGE_FIELDS = {"source", "target", "relation", "confidence", "source_file"}
VALID_CONFIDENCES = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}
```

エッジ単位で confidence を持たせることで、「どの主張が LLM 推論か」を構造的に追跡できる。`INFERRED` には数値の `confidence_score` が付き、レポートで平均化される (`report.py:20-29`)。

### 2. immutable extraction schema

graphify のスキーマは「いつ抽出されたか」「何が根拠か」を frozen に保つ。後から書き換えない。これにより derived value (信頼度伝播・clustering) が再現可能になる。

### 3. Leiden ベースの自動クラスタリング

```python
_MAX_COMMUNITY_FRACTION = 0.25  # 25% より大きいコミュニティは分割
_MIN_SPLIT_SIZE = 10
```

embeddings に依存せず、グラフ密度だけでコミュニティを検出する。過度に大きなクラスタは再帰的に分割する。

### 4. Hyperedge (3+ ノードのグループ関係)

「複数クラスが共通インターフェース実装」「複数ステップの認証フロー」のような **3 個以上のノードを 1 つの関係でグループ化**する構造を edge とは別に持つ (`cache.py:116`, `export.py:249`)。

### 5. SHA256 キャッシュ + インクリメンタル

ファイル単位の SHA256 を記録し、変更があったファイルのみ再 extract。これにより 100+ ファイル規模でもインクリメンタルに graph を更新できる。

## wiki-knowledge への適用候補 (7 案)

調査の結果、graphify から取り込む候補として以下を抽出した:

| 候補 | 概要 | 適用先 |
|------|------|--------|
| P1-A 信頼度 3 段階ラベル | claim 単位で `EXTRACTED/INFERRED/AMBIGUOUS` を付与 | compile 時に記事 frontmatter / lint / trust-score に統合 |
| P1-B SHA256 キャッシュ | compile を incremental 化 | wiki-cycle 高速化 |
| P2-A Leiden クラスタリング | 同クラスタ内 wikilink 欠落を検出 | gap-detect 強化 |
| P2-B クロスファイル surprise 検出 | querylog で共引用されるが wikilink 無いペアを検出 | gap-detect / lint レポート |
| P3-A God nodes lint | ハブ概念を lint レポートに掲示 | lint-wiki.py |
| P3-B Audit trail 拡張 | communities / surprises を含む lint レポート | lint レポート |
| P4 Hyperedge | 3+ ノードのグループ関係 | 将来のスキーマ拡張 |

## team-brainstorm の合意 (Round 1-3)

5 ロール議論の結果、上記候補を以下のように整理した。

### Accepted (全員一致)

1. **graph.json は read-only VIEW に限定**: source of truth は frontmatter + wikilink のまま、graph.json は derived product。手編集禁止、`.gitignore` 対象、compile 直後に都度再生成。
2. **拡張余地スキーマ**: `version: "1.0"` 必須。edge には `weight / co_citation_count / co_citation_frequency / confidence / sources[] / claim_id / _custom` を最初から予約地として含める。後の破壊的変更を回避。
3. **inventory.json 中間層** (Explorer 提案): 記事テキスト → `inventory.json` (cacheable, deterministic) → `graph.json` の 3 層に分離。二重管理を避ける。
4. **graph_gen.py 新規スクリプトが graph.json を所有** (満場一致): compile は LLM 呼び出しに専念、graph_gen は markdown→graph 専門、lint は consumer に徹する。
5. **MVP からは SHA256 cache を抜く**: LLM の non-determinism のため determinism guarantee が困難。`--incremental` フラグで妥協し、cache は Phase 2 で determinism test 完了後に導入。
6. **Layer 2 = lint 高速化**: P2-B coref-detect は querylog データ不足のため defer。代わりに lint-wiki.py を graph.json consumer として書き換え、Dead Link / Orphan / backlink 検出を graph 経由に統一する。
7. **coref-detect (P2-B) は 3 段階フィルタ必須**: `co_citation_frequency ≥ 0.08` → 既存パス長 > 2-hop → domain coherence。実装前に querylog 100 件サンプルで偽陽性率を手で estimate する。

### Defer (Layer 3 以降)

- claim provenance chain (Explorer 案 2)
- God node isolation risk (Challenger 修正案: 「単に hub である」ではなく「hub fail で孤立する記事数」を計測)
- Leiden clustering (記事数が増えてから)
- hyperedge / graph-constraints DSL / graph_check.py

### 設計上の重要な学び

- **粒度問題**: 信頼度を「記事単位 / エッジ単位 / claim 単位」のどこに置くかは設計の核心。KG 専門家は「エッジ単位が折衷案として最適」と推奨。Trust Score (記事単位) との二重管理を避けるには、graph.json の edge attribute として持たせるのが clean。
- **小規模グラフの統計脆弱性**: 記事数 < 50 では centrality 指標の変動が大きい。「PageRank 上位 5 記事」のようなランキングよりも「カテゴリ分け」(グループ) の方が安定する。
- **missing edge ≠ related** (Challenger 警告): 「security」query で oauth / SAML / JWT が共起しても、それは「分類バラバラ」のサインで本来 related ではない可能性がある。共引用検出の偽陽性は事前検証必須。
- **scale-free は健全**: God node = 悪ではない。健全な knowledge graph はスケールフリー特性を持つ。検出する場合は「hub fail で何記事が孤立するか」(isolation risk) を指標にする。

## MVP 実装プラン

合意に基づく MVP は 3 層構造:

```
Layer 1a: scripts/lib/inventory.py + .wiki/outputs/inventory.json
          - 記事テキストから決定論的にメタデータを派生
          - stdlib only

Layer 1b: scripts/graph_gen.py → .wiki/outputs/graph.json
          - inventory + 任意 querylog → read-only graph view
          - schema version 1.0、_custom / claim_id 予約

Layer 2:  lint-wiki.py の graph 利用化
          - Dead Link / Orphan / backlink を graph query 経由に統一
          - 既存テスト全 pass を保証
```

実装計画は `docs/plans/20260407183028_wiki-graph-layer-mvp.md` を参照。

## 出典

- リポジトリ: https://github.com/safishamsi/graphify (clone: `examples/graphify/`)
- 主要参照ファイル: `examples/graphify/{ARCHITECTURE.md, validate.py:1-63, cluster.py:1-80, analyze.py:39-90, cache.py:77-116, report.py:7-100, extract.py:107, export.py:249}`
- 議論セッション: team-brainstorm Round 1-3 (5 ロール: Challenger / Explorer / Connector / Grounded / Knowledge Graph Expert)
- 関連既存記事: trust-score / gap-detection / querylog / wiki-knowledge-architecture
