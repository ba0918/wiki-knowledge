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

## Research Gaps

_未調査のトピックがあればここに記録_
