---
title: Trust Score — Wiki 記事の信頼度スコア機能
scraped: 2026-04-06
tags: [trust-score, wiki, quality, metrics, phase3a]
---

# Trust Score — Wiki 記事の信頼度スコア機能

## 概要

Trust Score は、Wiki 記事ごとに「どの程度信頼できるか」を定量評価するスコアリング機能である。スコアは 0.0〜1.0 の範囲で、4つの要素の加重合計として算出される。Phase 3a として実装された。

## 設計思想

### なぜ Trust Score が必要か

LLM Wiki では、記事が自動生成（compile）や query promote で増えていく。しかし、全ての記事が同等に信頼できるわけではない。ソースが1つしかない記事、更新されずに放置された記事、他の記事から参照されていない孤立した記事は、情報の信頼性が低い可能性がある。

Trust Score はこれらの品質シグナルを統合し、記事の信頼度を数値化する。これにより：

- **lint 時**: 低スコア記事を警告として検出できる
- **query 時**: 引用元の信頼度を把握できる（Phase 3b で統合予定）
- **運用時**: Wiki 全体の健全性を定期的にモニタリングできる

### Derived Value としての設計

Trust Score はフロントマターに保存しない。これは重要な設計判断である。

理由：
1. Trust Score は QueryLog の蓄積、他記事の追加・削除、時間経過で常に変動する **derived value**（派生値）
2. フロントマターに書くと、QueryLog が追記されるたびに即座に stale になる
3. Source 層 / Knowledge 層の分離原則に反する — Knowledge 層の記事に Output 層のメタデータを混入させることになる

そのため、Trust Score は常にオンデマンドで計算し、結果は Output 層（`outputs/reports/`）にレポートとして出力する。

## 4つの計算要素

### 1. ソース数（重み: 0.30）

```
raw value = len(source_refs)
```

フロントマターの `source_refs` 配列の長さ。複数ソースで裏付けされた記事は信頼性が高い。例えば、Karpathy の原典と独自の解説記事の両方を source_refs に持つ記事は、単一ソースの記事よりも信頼度が高い。

### 2. 鮮度（重み: 0.20）

```
raw value = max(0.0, 1.0 - elapsed_days / 365)
```

`updated` フロントマターからの経過日数に基づく線形減衰。365日で 0.0 に到達し、それ以降も 0.0 のまま。更新日が今日なら 1.0、半年前なら約 0.5、1年以上前なら 0.0。

古い記事は陳腐化リスクがあるため、鮮度を信頼度の一要素として組み込む。ただし重みは 0.20 と控えめで、古くても他の要素が高ければ十分なスコアが出る。

### 3. 引用頻度（重み: 0.30）

```
raw value = count(sources_cited に含まれる回数)
```

QueryLog の `sources_cited` フィールドに出現する回数。wiki-query で回答を生成する際に、実際に引用された記事ほど有用性が高いと判断する。

これは QueryLog 蓄積機能（Phase 2a）に依存する。QueryLog が空の場合はこの要素を除外し、残りの3要素で再配分する（フォールバック動作）。

### 4. Backlink 数（重み: 0.20）

```
raw value = 他記事からの被参照数（deduplicated）
```

他の記事から `related` フロントマターや本文 `[[wikilink]]` で参照されている回数。ハブ記事（多くの記事から参照される概念的中心の記事）は構造的に重要である。

**重複排除ルール**: 同一記事から `related` と `[[wikilink]]` の両方で参照されていても 1回としてカウントする。slug 正規化は `concepts/foo.md` → `foo`、`[[foo]]` → `foo` で統一した後、set で deduplicate する。自己参照はカウントしない。

## 正規化と重み配分

### min-max 正規化

各要素の raw value を全記事にわたって min-max 正規化し、0.0〜1.0 の範囲に変換する。

```
normalized = (value - min) / (max - min)
```

### エッジケース対応

- **記事数 < 3**: min-max が不安定なため、全要素の正規化値を **0.5 固定**（中央値）
- **min == max（全記事が同じ raw value）**: 全要素 0.5 固定
- **記事数 0**: 空リストを返す

### フォールバック重み配分（QueryLog 空）

QueryLog エントリが 0 件の場合、引用頻度を除外して残りの3要素で再配分：

| 要素 | 通常重み | フォールバック重み |
|------|----------|-------------------|
| ソース数 | 0.30 | 0.40 |
| 鮮度 | 0.20 | 0.30 |
| 引用頻度 | 0.30 | 0.00（除外） |
| backlink数 | 0.20 | 0.30 |

重みの合計はどちらの場合も 1.0 になるため、スコアの範囲は変わらない。

## 実装構造

### 純粋関数ベース

Design Principles に従い、計算ロジックは全て純粋関数で実装している。

- `compute_trust_scores(articles, querylog_entries, today) -> list[ArticleScore]`: メインの計算関数。副作用なし。`today` は DI で注入可能（テスト容易性）
- `parse_article_metadata(concept_dir) -> list[ArticleMeta]`: フロントマター + wikilink 抽出
- `count_backlinks(articles) -> dict[str, int]`: 被参照数（deduplicated）
- `count_citations(entries, articles) -> dict[str, int]`: QueryLog からの引用回数
- `normalize_scores(raw_values) -> list[float]`: min-max 正規化

### 既存コードの再利用

- `lint-wiki.py` の `parse_frontmatter()` と `find_wikilinks()` をインポートして再利用
- `querylog_stats.py` の `load_querylog()` をインポートして再利用

新規に一から実装するのではなく、既存の well-tested な関数を活用することで、コードの重複を避けつつ信頼性を確保している。

### CLI インターフェース

```
python3 trust_score.py --wiki-root .wiki [--format table|json|report]
```

- `table`（デフォルト）: ターミナルにテーブル表示
- `json`: 構造化 JSON 出力（プログラム連携用）
- `report`: `{wiki_root}/outputs/reports/{YYYYMMDD}-trust-score.md` に Markdown レポート出力

### lint ワークフロー統合

`lint-wiki.py` 自体は変更せず、SKILL.md の lint ワークフロー内で「lint-wiki.py 実行後に trust_score.py も実行」というオーケストレーションで統合。単一責任原則を維持する設計。

スコアが 0.3 未満の記事は lint レポートの 🟡 Warning として記載される。

## テスト

28件のテストケースで以下をカバー：

- 各純粋関数の基本動作
- 正規化の境界値（min == max、記事数 < 3、空リスト）
- backlink の deduplicate（同一記事からの重複参照、自己参照除外）
- フォールバック重み配分（QueryLog 空時）
- 鮮度の線形減衰（0日=1.0、365日=0.0、730日=0.0）
- 重み定数の合計が 1.0 であること

## Phase 3b への展望

Phase 3a（本実装）のスコープ外として以下を Phase 3b に分離した：

- **query 時の低スコア注釈**: 低スコア記事を引用する場合に「この情報は裏付けが限定的です」と注釈を付ける機能。現在5記事で QueryLog も初期状態のため、運用データが溜まってから統合する。
- **スコアの時系列トラッキング**: Trust Score の変遷を記録し、記事の品質改善/劣化を追跡する機能。
