---
wiki_root: .wiki
created: 2026-04-05
---

# Wiki Knowledge Base

このプロジェクトは LLM Wiki Knowledge Base です。

## Scope

LLM向けの知識ベース構築の仕組みを実験するプロジェクト。Karpathy の LLM Wiki コンセプトを Claude Skill として実装し、既存プロジェクトに導入可能な形で提供する。

## Conventions

- Wiki記事は `.wiki/concepts/` にフラットに配置（`{slug}.md`）
- ソースドキュメントは `.wiki/raw/` に immutable に保存
- 相互参照は `[[wikilink]]` 記法を使用
- フロントマターは `.wiki/schema/page-template.json` に準拠
- カテゴリは `.wiki/schema/categories.json` で管理

## Articles

- [[llm-wiki-knowledge-base]] — LLM Wiki Knowledge Base（concepts）
- [[wiki-knowledge-architecture]] — Wiki ナレッジ構築アーキテクチャ（concepts）
- [[llm-wiki-use-cases]] — LLM Wiki ユースケース（concepts）
- [[llm-wiki-tooling]] — LLM Wiki ツーリング（tools）
- [[querylog]] — QueryLog メタデータログ基盤（concepts）
- [[trust-score]] — Trust Score 記事信頼度スコア（concepts）
- [[gap-detection]] — Gap Detection 知識ギャップ検出と Ingest 提案（concepts）
- [[graphify-knowledge-graph-concepts]] — graphify 知識グラフ構築パターンと適用判断（concepts）

## QueryLog

- wiki-query 実行時にクエリメタデータを `.wiki/outputs/querylog.jsonl` に蓄積する（JSONL、append-only）
- スキーマ: `.wiki/schema/querylog-schema.json`
- 集計: `python3 skills/wiki/scripts/querylog-stats.py --wiki-root .wiki`
- querylog.jsonl はデフォルト git 管理外（`.wiki/.gitignore`）

## Trust Score

- 記事ごとの信頼度スコア（0.0〜1.0）を4要素（ソース数・鮮度・引用頻度・backlink数）で算出
- 実行: `python3 skills/wiki/scripts/trust_score.py --wiki-root .wiki`
- 出力形式: `--format table`（デフォルト）/ `json` / `report`（Markdown レポート出力）
- レポート出力先: `.wiki/outputs/reports/{YYYYMMDD}-trust-score.md`
- QueryLog が空の場合は引用頻度を除外し、残り3要素で再配分
- Trust Score は derived value のためフロントマターには保存しない

## Gap Detection

- QueryLog の `gap_topics` を集計し、既存記事とのカバレッジを照合してナレッジギャップを検出
- 実行: `python3 skills/wiki/scripts/gap_detect.py --wiki-root .wiki`
- 出力形式: `--format table`（デフォルト）/ `json` / `report`（Markdown レポート出力）
- レポート出力先: `.wiki/outputs/reports/{YYYYMMDD}-gap-detect.md`
- `--threshold` でカバレッジ閾値を調整（デフォルト: 0.8）

## Lint

- Wiki 記事の品質・整合性を8項目で自動チェック
- `dead_link` / `orphan` は graph layer 経由で検出するため、**lint 実行前に `graph_gen.py` を実行する必要がある**
- `--use-graph` はデフォルト ON。`.wiki/outputs/graph.json` 不在時は **exit 2** で停止し、`graph_gen.py` の実行を案内する
- 単独実行を救済する opt-in フラグ: `--auto-graph`（graph 欠如時に lint 側が graph_gen を subprocess 呼び出し）
- legacy パス: `--no-graph`（inventory から直接 dead_link/orphan を再計算）
- 実行: `python3 skills/wiki/scripts/graph_gen.py --wiki-root .wiki && python3 skills/wiki/scripts/lint-wiki.py --wiki-root .wiki`
- 出力形式: `--format table`（デフォルト）/ `json` / `report`（Markdown レポート出力）
- レポート出力先: `.wiki/outputs/reports/{YYYYMMDD}-lint.md`
- チェック項目:
  - Dead link — `[[slug]]` の参照先が存在しない
  - Orphan — 被リンクなしの孤立記事
  - Missing source — `source_refs` のファイルが存在しない
  - Missing frontmatter — 必須フィールド欠損
  - Coverage gap — 2回以上参照されているが記事がない
  - Link quality — 一方向リンク、`related` と本文 wikilink の不一致
  - Article quality — 50 words 未満の短記事、推測ブロック 30% 超
  - Format violations — slug 命名規則、page-template.json 準拠、category/type/date/tags 検証

## Graph Layer

- `concepts/*.md` から派生する読み取り専用グラフ（nodes / edges / metadata.dangling_links）
- スクリプト: `skills/wiki/scripts/graph_gen.py`
- 出力先: `.wiki/outputs/graph.json`
- 役割: lint の `dead_link` / `orphan` 検出基盤。検出ロジックの二重実装を排除し、層越境を防ぐ
- 再生成: `python3 skills/wiki/scripts/graph_gen.py --wiki-root .wiki`
- cycle 実行時は `compile → graph_gen → lint` の順で orchestrator が明示的に呼び出す
- graph layer は派生物（derived）のため git 管理外運用も可能（必要に応じて `.wiki/.gitignore` で除外）

## Research Gaps

_未調査のトピックがあればここに記録_
