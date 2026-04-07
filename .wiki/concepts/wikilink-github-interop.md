---
title: GitHub と wikilink 記法の相互運用性
type: wiki
source_refs:
  - "raw/articles/20260407-wikilink-github-interop.md"
created: 2026-04-07
updated: 2026-04-07
category: concepts
tags: [wikilink, github, gfm, commonmark, gollum]
related:
  - "concepts/wikilink-reader-comparison.md"
  - "concepts/wikilink-conversion-strategies.md"
  - "concepts/wikilink-link-parser-spec.md"
---

# GitHub と wikilink 記法の相互運用性

> `[[slug]]` 形式の wikilink は CommonMark/GFM 仕様には存在せず、GitHub では Wiki タブ（Gollum）でのみ解釈される。通常リポジトリの README や Issue、Gist では単なるリテラル文字列として表示される。

## CommonMark / GFM 仕様上の位置づけ

- **CommonMark** はリンクとして `[text](url)` と参照リンク `[text][ref]` のみを定義する。`[[…]]` は仕様外。
- **GitHub Flavored Markdown (GFM)** は CommonMark の strict superset として、テーブル・打ち消し線・autolink・タスクリストの 4 拡張のみを追加する。**wikilink は含まれない**。
- 公式実装の `cmark-gfm` も wikilink を解釈しない。

## GitHub の各サーフェスでの扱い

| サーフェス | `[[Page]]` の解釈 |
|---|---|
| Repository README / 通常 .md | 解釈されない（リテラル表示） |
| Issue / PR / コメント | 解釈されない |
| Gist | 解釈されない |
| Wiki タブ（`*.wiki` リポジトリ） | 解釈される（Gollum 経由） |

### Wiki タブだけが特殊な理由

GitHub Wiki は内部的に **Gollum**（Git-powered wiki エンジン、Ruby 製）でレンダリングされる。Gollum は MediaWiki 由来の `[[Page Name]]` をサポートし、`[[Display Text|Page Name]]` のエイリアスや画像埋め込みも扱える。ただし Gollum と GitHub Wiki の実装は時間とともに乖離している。

## 通常リポジトリでの追跡性問題

`.wiki/concepts/` を通常リポジトリ配下で運用する場合、`[[slug]]` リンクは GitHub 上で：

- クリック不能（プレーンテキスト表示）
- "Go to definition" / "Find references" の対象外
- 検索インデックスには載るがグラフ的構造は失われる

執筆体験（Obsidian / Foam / Dendron でのオートコンプリートとバックリンク）を取るか、GitHub 上での追跡性を取るかのトレードオフが発生する。本プロジェクトは前者を採り、補完策として lint と `## 関連` 併記方式を採用している。詳細は [[wikilink-conversion-strategies]] を参照。

## 関連

- [[wikilink-reader-comparison]] — どのリーダーが wikilink をどう解釈するか
- [[wikilink-conversion-strategies]] — `[[…]]` ↔ 標準リンクの変換戦略
- [[wikilink-link-parser-spec]] — 本プロジェクト lint の wikilink 抽出仕様

## 出典

- [GitHub Flavored Markdown Spec](https://github.github.com/gfm/)
- [A formal spec for GitHub Flavored Markdown — GitHub Blog](https://github.blog/engineering/user-experience/a-formal-spec-for-github-markdown/)
- [github/cmark-gfm](https://github.com/github/cmark-gfm)
- [gollum/gollum-lib](https://github.com/gollum/gollum-lib)
- [Gollum (software) — Wikipedia](https://en.wikipedia.org/wiki/Gollum_(software))
