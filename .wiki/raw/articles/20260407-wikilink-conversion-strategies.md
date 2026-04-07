---
title: wikilink ↔ 標準 Markdown link 変換戦略
scraped: 2026-04-07
tags: [wikilink, conversion, pandoc, ci, pre-commit]
---

# wikilink ↔ 標準 Markdown link 変換戦略

## 背景

`[[slug]]` 形式の wikilink は GitHub 通常レンダラ（Repository README、Issue、PR）では解釈されない（詳細は `wikilink-github-interop` を参照）。一方、執筆時には `[[…]]` の方が短く、Obsidian / Foam / Dendron などのエディタでオートコンプリートとバックリンクが効くため手放せない。

この乖離を埋めるための変換アプローチを整理する。

## 4 つの基本戦略

### 1. Pre-commit 変換（書き換え型）

Git pre-commit フックで `[[slug]]` を `[Title](slug.md)` に書き換えてからコミットする。

- **利点**: GitHub 上で完全にクリック可能になる。追加 CI 不要。
- **欠点**: ローカルファイルが書き換わり、Obsidian 側の wikilink autocomplete・graph view が壊れる。**通常は採用しない**。

### 2. CI 変換（rendered branch 型）

`main` には `[[…]]` のまま置き、CI が `gh-pages` や `rendered` ブランチへ変換版を push する。

- **利点**: 執筆体験を壊さず、公開ビューでもリンクが効く。
- **欠点**: 2 ブランチ運用になるため、ユーザがどちらを見るべきか混乱することがある。GitHub Pages / Foam の典型構成。

### 3. 併記方式（dual-link）

`[[slug]]` の直後または見出しの「## 関連」セクションに、明示的な相対パスリンク `[Title](slug.md)` を併記する。

- **利点**: ファイルは書き換えない。GitHub と Obsidian の双方でリンクが機能する。lint で wikilink ↔ related の整合をチェックしやすい。
- **欠点**: 冗長。書き手の規律が必要（lint で半自動化可能）。
- **本プロジェクトの採用方式**: `lint-wiki.py` の `link_quality` チェックは `related` フロントマター項目と本文 wikilink の整合を検証する。これは併記方式の lint 実装の一形態。

### 4. ビルド時生成（site generator 型）

Hugo / 11ty / Quartz / Foam Publish などの静的サイトジェネレータが wikilink を解決する。出力先（HTML / GitHub Pages）は標準リンク化される。

- **利点**: 高機能（バックリンク・グラフ・全文検索）。
- **欠点**: ビルドパイプライン全体を抱え込むコストが大きい。

## ツール別対応

### Pandoc

Pandoc 3.0 から `wikilinks_title_after_pipe` / `wikilinks_title_before_pipe` 拡張が追加され、`[[Name|Title]]` および `[[Title|Name]]` をネイティブに解釈できるようになった。CommonMark / Markdown どちらの reader でも有効化可能。

```bash
pandoc --from=markdown+wikilinks_title_after_pipe input.md -o output.html
```

### Python-Markdown WikiLinks 拡張

`markdown` パッケージ標準同梱の `wikilinks` 拡張は `[[bracketed]]` を `<a class="wikilink" href="/bracketed/">bracketed</a>` 形式に変換する。Static site generator の前処理として軽量。

### sed / Lua filter（簡易）

`sed -E 's/\[\[([a-z0-9-]+)\]\]/[\1](\1.md)/g'` のような one-liner も使えるが、**コードブロック内の `[[…]]` を除外できない**ため正攻法ではない。Pandoc Lua filter を書けばこの制約を回避できる。

## 推奨パターン（本プロジェクト）

`wiki-knowladge` プロジェクトの現状方針：

1. ファイルには `[[slug]]` のみ書き、書き換えはしない（戦略 3 = 併記方式の軽量版）。
2. `## 関連` セクションで `[[slug]] — 説明` の形式で相互参照を明示する。
3. `lint-wiki.py` で `related_mismatch` を検出し、本文と `## 関連` の整合を保証する（詳細は `wikilink-link-parser-spec` 参照）。
4. GitHub 上で読む場合は wikilink がプレーンテキストになることを許容する。クリック追跡性は犠牲にして執筆体験を優先。

将来的に GitHub 上での追跡性が必要になった場合は **戦略 2（CI rendered branch）** が最も影響範囲が小さい移行先候補。

## 出典

- [Markdown — pandoc-discuss: how to convert [[WikiLinks]]](https://pandoc-discuss.narkive.com/u6Bv7wrd/markdown-how-to-convert-wikilinks-to-html)
- [pandoc PR #7705 — wikilinks support](https://github.com/jgm/pandoc/pull/7705)
- [WikiLinks — Python-Markdown documentation](https://python-markdown.github.io/extensions/wikilinks/)
- [pandoc Lua filter discussion](https://groups.google.com/g/pandoc-discuss/c/WUhyzFf5qLM)
