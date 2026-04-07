---
title: GitHub と wikilink 記法の相互運用性
scraped: 2026-04-07
tags: [wikilink, github, gfm, commonmark, gollum]
---

# GitHub と wikilink 記法の相互運用性

## 調査トリガー

`.wiki/concepts/*.md` で多用している `[[slug]]` 形式の wikilink が GitHub 上でどのように扱われるかを確認するため、一次情報を整理する。

## CommonMark / GFM 仕様上の位置づけ

- **CommonMark**: `[[…]]` 記法は仕様に存在しない。リンクは `[text](url)` または参照リンク `[text][ref]` のみが定義される。
- **GitHub Flavored Markdown (GFM)**: CommonMark の strict superset として定義され、テーブル、打ち消し線、autolink、タスクリストを追加する。**wikilink は GFM 拡張にも含まれない**。
- 実装としての `cmark-gfm` も wikilink をサポートしない。GFM スペックの公式リポジトリ（`github/cmark-gfm`）で確認できる。

## GitHub の各サーフェスでの扱い

GitHub には wikilink を解釈する場所と解釈しない場所がある：

| サーフェス | `[[Page]]` の解釈 | 備考 |
|---|---|---|
| Repository README / 通常 .md | 解釈されない | リテラル `[[Page]]` として表示される |
| Issue / PR / コメント | 解釈されない | 同上 |
| Gist | 解釈されない | 同上 |
| Wiki タブ（`*.wiki` リポジトリ） | 解釈される | Gollum エンジンで処理 |

### Wiki タブのみが特殊な理由

GitHub Wiki は内部的に **Gollum**（Git-powered wiki エンジン、Ruby 製）でレンダリングされる。Gollum は MediaWiki 由来の `[[Page Name]]` 構文をサポートし、`[[Display Text|Page Name]]` のエイリアスや `[[Image.png]]` の埋め込みも扱える。

ただし Gollum と GitHub Wiki の実装は時間とともに乖離しており、Gollum 側は「GitHub/GitLab wiki との互換性を維持する」と謳う立場にある。

## 含意：通常リポジトリでの追跡性問題

`.wiki/concepts/` を通常リポジトリの一部として運用する場合、`[[slug]]` リンクは GitHub 上では：

- クリック不能（プレーンテキスト表示）
- GitHub のコードナビゲーション（"Go to definition" / "Find references"）の対象外
- 検索インデックスには含まれるが、グラフ的構造は失われる

これが、別途 `wikilink-conversion-strategies` のような変換フローや併記方式が必要になる理由である。

## 出典

- [GitHub Flavored Markdown Spec](https://github.github.com/gfm/)
- [A formal spec for GitHub Flavored Markdown — GitHub Blog](https://github.blog/engineering/user-experience/a-formal-spec-for-github-markdown/)
- [github/cmark-gfm](https://github.com/github/cmark-gfm)
- [gollum/gollum-lib](https://github.com/gollum/gollum-lib)
- [Gollum (software) — Wikipedia](https://en.wikipedia.org/wiki/Gollum_(software))
