---
title: LLM Wiki ツーリング
type: wiki
source_refs:
  - "raw/articles/20260405-karpathy-llm-wiki-pattern.md"
created: 2026-04-05
updated: 2026-04-05
category: tools
tags: [obsidian, qmd, marp, dataview, search, tooling]
related:
  - "concepts/llm-wiki-knowledge-base.md"
  - "concepts/wiki-knowledge-architecture.md"
  - "concepts/llm-wiki-use-cases.md"
---

# LLM Wiki ツーリング

> LLM Wiki の運用を支えるツール群。閲覧・検索・出力形式の拡張に対応する。

## 閲覧：Obsidian

LLM Wiki との相性が最も良いエディタ。Karpathy 自身も LLM エージェントと Obsidian を並べて使うワークフローを採用している。

- **Graph View**: Wiki の形状を可視化 — ハブページ、孤立ページ、接続構造が一目でわかる
- **Web Clipper**: ブラウザ拡張機能で Web 記事を Markdown に変換。ソース取り込みに便利
- **画像のローカルダウンロード**: Settings → Files and links でアタッチメントフォルダを `raw/assets/` に設定。ホットキーで記事内の画像を一括ダウンロード。URL 切れを防ぎ、LLM が直接画像を参照可能にする

## 検索：qmd

Wiki のスケールが大きくなると `index.md` だけでは不十分になる。qmd はローカル Markdown ファイル向け検索エンジン。

- **ハイブリッド検索**: BM25 + ベクトル検索 + LLM リランキング
- **完全オンデバイス**: 外部サービス不要
- **CLI + MCP サーバー**: LLM からシェルアウトまたはネイティブツールとして利用可能

小規模（~100 ソース、数百ページ）では index.md で十分。それを超えたら qmd 等の導入を検討。

## 出力形式

### Marp

Markdown ベースのスライドデッキ形式。Obsidian プラグインあり。Wiki コンテンツから直接プレゼンテーションを生成できる。

### Dataview

Obsidian プラグイン。ページのフロントマター（YAML）に対してクエリを実行し、動的なテーブルやリストを生成。タグ・日付・ソース数などのメタデータを活用。

### その他

Query 操作の回答は Markdown ページ以外にも比較表、チャート（matplotlib）、キャンバスなど多様な形式で出力可能。

## バージョン管理

Wiki は Markdown ファイルの git リポジトリそのもの。バージョン履歴、ブランチ、コラボレーションが無料で手に入る。

## 出典

- [LLM Wiki — Karpathy's Original Pattern Document](../raw/articles/20260405-karpathy-llm-wiki-pattern.md)
