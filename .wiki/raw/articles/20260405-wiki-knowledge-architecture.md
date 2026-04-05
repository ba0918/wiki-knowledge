---
title: Wiki ナレッジ構築アーキテクチャ — 3層構造の解説
source_url: null
scraped: 2026-04-05
tags: [wiki, architecture, ingest, compile, knowledge-base]
---

# Wiki ナレッジ構築アーキテクチャ

このプロジェクトの知識ベース構築の仕組みは、3つのレイヤーで構成される。

## 1. ソース取り込み（Ingest）

生のドキュメント（URL、ファイル、記事）を `.wiki/raw/` に immutable（変更不可）で保存する。ここが「事実の原典」となる。

- フロントマターに `source_url` と `scraped` 日付を記録し、いつ何を取り込んだかを追跡可能にする
- ソースは一度取り込んだら変更しない（immutable）

## 2. コンパイル（Compile）

取り込んだソースを LLM が読み込み、構造化された Wiki 記事として `.wiki/concepts/` に生成する。

### コンパイル時のルール

- **フロントマター**: `page-template.json` スキーマに厳密に準拠（title, type, source_refs, created, updated, category, tags, related）
- **出典追跡**: `source_refs` フィールドで各記事がどのソースから生成されたか明示
- **相互参照**: `[[wikilink]]` 記法で記事間のリンクを実現
- **カテゴリ分類**: `categories.json` で管理される4カテゴリ（concepts / tools / practices / references）に振り分け
- **フラット配置**: ディレクトリのネストなし、`{slug}.md` でシンプルに配置

## 3. インデックス管理

- `.wiki/index.md` が全記事をカテゴリ別に一覧化するカタログとして機能する
- `.wiki/log.md` で変更履歴を追跡する

## 根底にある思想

Karpathy の LLM Wiki コンセプトに基づく設計思想：

> 人間はソースのキュレーションと質問に集中し、構造化は LLM に委譲する

人間の役割は「何を知識にするか」「何を聞きたいか」を決めること。記事の生成・相互リンク・整合性チェックは LLM が担当する。Claude Skill として実装されているため、他のプロジェクトにも導入可能。

## 各ディレクトリの役割

| パス | 役割 |
|------|------|
| `.wiki/raw/` | ソースドキュメント（immutable） |
| `.wiki/concepts/` | コンパイル済み Wiki 記事 |
| `.wiki/schema/` | フロントマタースキーマ・カテゴリ定義 |
| `.wiki/index.md` | 全記事カタログ |
| `.wiki/log.md` | 変更履歴 |
