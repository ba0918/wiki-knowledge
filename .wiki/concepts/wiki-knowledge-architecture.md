---
title: Wiki ナレッジ構築アーキテクチャ
type: wiki
source_refs:
  - "raw/articles/20260405-wiki-knowledge-architecture.md"
  - "raw/articles/20260405-karpathy-llm-wiki-pattern.md"
created: 2026-04-05
updated: 2026-04-05
category: concepts
tags: [wiki, architecture, ingest, compile, query, lint, index, knowledge-base]
related:
  - "concepts/llm-wiki-knowledge-base.md"
  - "concepts/llm-wiki-tooling.md"
  - "concepts/llm-wiki-use-cases.md"
  - "concepts/querylog.md"
  - "concepts/trust-score.md"
---

# Wiki ナレッジ構築アーキテクチャ

> このプロジェクトの知識ベースは **Ingest → Compile → Index** の3層構造で構築される。人間がソースをキュレーションし、LLM が構造化を担う。

## 3層アーキテクチャ

### 1. Ingest（ソース取り込み）

生のドキュメント（URL、ファイル、記事）を `.wiki/raw/` に **immutable** で保存する層。

- フロントマターに `source_url` と `scraped` 日付を記録し、取り込み履歴を追跡
- 一度取り込んだソースは変更しない（immutable 原則）
- ここが「事実の原典」として機能する

### 2. Compile（記事生成）

取り込んだソースを LLM が読み込み、`.wiki/concepts/` に構造化 Wiki 記事を生成する層。

コンパイル時のルール：

- **スキーマ準拠**: `page-template.json` で定義されたフロントマター（title, type, source_refs, created, updated, category, tags, related）
- **出典追跡**: `source_refs` で記事とソースの対応を明示
- **相互参照**: `[[wikilink]]` 記法で記事間リンク
- **カテゴリ分類**: `categories.json` の4カテゴリ（concepts / tools / practices / references）
- **フラット配置**: `{slug}.md` でネストなし

### 3. Index（インデックス管理）

記事カタログと変更履歴を管理する層。

- `.wiki/index.md` — 全記事をカテゴリ別に一覧化
- `.wiki/log.md` — 変更履歴の追跡

## ディレクトリ構造

| パス | 役割 |
|------|------|
| `.wiki/raw/` | ソースドキュメント（immutable） |
| `.wiki/concepts/` | コンパイル済み Wiki 記事 |
| `.wiki/schema/` | スキーマ・カテゴリ定義 |
| `.wiki/index.md` | 全記事カタログ |
| `.wiki/log.md` | 変更履歴 |

## Karpathy オリジナルの Operations 定義

Karpathy のパターンドキュメントでは、3つのオペレーションが定義されている：

### Ingest

ソースを raw コレクションに追加し、LLM に処理させる。LLM はソースを読み、要約ページを作成し、インデックスを更新し、関連するエンティティ・コンセプトページを横断的に更新する。1つのソースが 10-15 ページに影響しうる。

### Query

Wiki に対して質問する。LLM は関連ページを検索・読み込み、引用付きの合成回答を生成する。重要な洞察：**良い回答は Wiki に新ページとして還元できる** — 探索も知識ベースに複利的に蓄積される。Query 実行時のメタデータは [[querylog]] に構造化ログとして蓄積され、知識ギャップの検出や [[trust-score]] の基盤となる。

### Lint

定期的なヘルスチェック。検出対象：
- ページ間の矛盾
- 新しいソースで上書きされた古い主張
- インバウンドリンクのない孤立ページ
- 言及されているが専用ページがないコンセプト
- 欠けている相互参照
- Web 検索で埋められるデータギャップ

## インデックスとログの役割

- **index.md**（コンテンツ指向）: 全ページカタログ。Query 時に LLM がまず index を読んで関連ページを特定し、詳細に入る。~100 ソース・数百ページ規模では embedding ベース RAG なしで十分機能する。
- **log.md**（時系列）: append-only の操作記録。`## [日付] 操作 | タイトル` の形式で unix ツールでパース可能。

## 設計思想

[[llm-wiki-knowledge-base]] の Karpathy コンセプトに基づき、**人間はキュレーションと質問に集中し、構造化は LLM に委譲する** というアプローチを取る。Claude Skill として実装されているため、任意のプロジェクトに導入可能。

## 出典

- [Wiki ナレッジ構築アーキテクチャ — 3層構造の解説](../raw/articles/20260405-wiki-knowledge-architecture.md)
- [LLM Wiki — Karpathy's Original Pattern Document](../raw/articles/20260405-karpathy-llm-wiki-pattern.md)
