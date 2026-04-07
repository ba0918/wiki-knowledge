---
title: wikilink ↔ 標準 Markdown link 変換戦略
type: wiki
source_refs:
  - "raw/articles/20260407-wikilink-conversion-strategies.md"
created: 2026-04-07
updated: 2026-04-07
category: practices
tags: [wikilink, conversion, pandoc, ci, pre-commit]
related:
  - "concepts/wikilink-github-interop.md"
  - "concepts/wikilink-reader-comparison.md"
  - "concepts/wikilink-link-parser-spec.md"
---

# wikilink ↔ 標準 Markdown link 変換戦略

> 執筆時の `[[slug]]` と GitHub 上で機能する `[Title](slug.md)` の乖離を埋めるための 4 つの戦略。本プロジェクトは「併記方式 + lint 検証」を採用している。

## 背景

[[wikilink-github-interop]] で示した通り、`[[slug]]` 形式は GitHub の通常レンダラでは解釈されない。一方、執筆時には [[wikilink-reader-comparison]] で挙げたエディタのオートコンプリートとバックリンクが手放せない。この乖離を埋める戦略を整理する。

## 4 つの基本戦略

### 1. Pre-commit 変換（書き換え型）

Git pre-commit フックで `[[slug]]` を `[Title](slug.md)` に書き換えてからコミットする。

- **利点**: GitHub 上で完全にクリック可能になる。
- **欠点**: ローカルファイルが書き換わり、エディタの wikilink autocomplete・graph view が壊れる。**通常は採用しない**。

### 2. CI 変換（rendered branch 型）

`main` には `[[…]]` のまま置き、CI が `gh-pages` や `rendered` ブランチへ変換版を push する。

- **利点**: 執筆体験を壊さず、公開ビューでもリンクが効く。
- **欠点**: 2 ブランチ運用になるため、ユーザがどちらを見るべきか混乱しやすい。GitHub Pages / Foam の典型構成。

### 3. 併記方式（dual-link）

本文中の `[[slug]]` に加え、`## 関連` セクションで明示的な相対パスリンクや wikilink を併記する。

- **利点**: ファイルは書き換えない。lint で wikilink ↔ related の整合をチェックしやすい。
- **欠点**: 冗長。書き手の規律が必要（lint で半自動化可能）。
- **本プロジェクトの採用方式**: [[wikilink-link-parser-spec]] が抽出した本文 wikilink を `frontmatter.related` と突き合わせる lint チェックがこれを支える。

### 4. ビルド時生成（site generator 型）

Hugo / 11ty / Quartz / Foam Publish などの静的サイトジェネレータが wikilink を解決する。出力先は標準リンク化される。

- **利点**: 高機能（バックリンク・グラフ・全文検索）。
- **欠点**: ビルドパイプライン全体を抱え込むコストが大きい。

## ツール別対応

### Pandoc

Pandoc 3.0 から `wikilinks_title_after_pipe` / `wikilinks_title_before_pipe` 拡張が追加され、`[[Name|Title]]` および `[[Title|Name]]` をネイティブに解釈できる。

```bash
pandoc --from=markdown+wikilinks_title_after_pipe input.md -o output.html
```

### Python-Markdown WikiLinks 拡張

`markdown` パッケージ標準同梱の `wikilinks` 拡張は `[[bracketed]]` を `<a class="wikilink" href="/bracketed/">bracketed</a>` 形式に変換する。Static site generator の前処理として軽量。

### sed / Lua filter（簡易）

`sed -E 's/\[\[([a-z0-9-]+)\]\]/[\1](\1.md)/g'` のような one-liner も使えるが、**コードブロック内の `[[…]]` を除外できない**。Pandoc Lua filter を書けばこの制約を回避可能。本プロジェクトの lint パーサもこの問題を意識し、フェンス除外を実装している（[[wikilink-link-parser-spec]] 参照）。

## 本プロジェクトの推奨パターン

1. ファイルには `[[slug]]` のみ書き、書き換えはしない（戦略 3 の軽量版）。
2. `## 関連` セクションで `[[slug]] — 説明` の形式で相互参照を明示する。
3. `lint-wiki.py` で `related_mismatch` を検出し、本文と `## 関連` の整合を保証する。
4. GitHub 上で読む場合は wikilink がプレーンテキストになることを許容する。

将来 GitHub 上での追跡性が必要になった場合は、戦略 2（CI rendered branch）が最も影響範囲の小さい移行先候補となる。

## 関連

- [[wikilink-github-interop]] — なぜ変換が必要か（GitHub 上での扱い）
- [[wikilink-reader-comparison]] — 変換先/元となるリーダー実装の差異
- [[wikilink-link-parser-spec]] — 本プロジェクト lint の抽出仕様

## 出典

- [pandoc PR #7705 — wikilinks support](https://github.com/jgm/pandoc/pull/7705)
- [WikiLinks — Python-Markdown documentation](https://python-markdown.github.io/extensions/wikilinks/)
- [Markdown — pandoc-discuss: how to convert [[WikiLinks]]](https://pandoc-discuss.narkive.com/u6Bv7wrd/markdown-how-to-convert-wikilinks-to-html)
- [pandoc Lua filter discussion](https://groups.google.com/g/pandoc-discuss/c/WUhyzFf5qLM)
