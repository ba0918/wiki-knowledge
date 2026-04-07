---
title: wikilink リーダー実装比較
type: wiki
source_refs:
  - "raw/articles/20260407-wikilink-reader-comparison.md"
created: 2026-04-07
updated: 2026-04-07
category: tools
tags: [wikilink, obsidian, foam, dendron, vscode, comparison]
related:
  - "concepts/wikilink-github-interop.md"
  - "concepts/wikilink-conversion-strategies.md"
  - "concepts/wikilink-link-parser-spec.md"
---

# wikilink リーダー実装比較

> Obsidian / Foam / Dendron / VS Code Markdown Notes は同じ `[[…]]` 構文を採用しつつ、解決ルール・エイリアス順序・transclusion の扱いが微妙に異なる。複数ツール間で wiki を共有する際の罠を整理する。

## 比較表

| 観点 | Obsidian | Foam | Dendron | Markdown Notes |
|---|---|---|---|---|
| 基本構文 | `[[Note Name]]` | `[[note-name]]` | `[[hier.note]]` | `[[note]]` |
| エイリアス | `[[Note\|alias]]` | `[[wikilink\|alias]]` | `[[label\|note.path]]`（順序逆） | 限定的 |
| 見出しリンク | `[[Note#Heading]]` | `[[note#Section]]` | サポート | — |
| ブロック参照 | `[[Note#^block-id]]` | — | `![[note]]`（Note Reference） | — |
| 解決ルール | shortest unique path | ファイル名一致 | dot 階層完全名 | ファイル名一致 |
| バックリンク | あり | あり | あり | あり |
| グラフビュー | あり | あり | あり | なし |
| プレースホルダ | リンクをハイライト | Placeholders panel | スタブノート自動生成 | — |
| マルチボルト | 限定的 | 単一ワークスペース | `[[dendron://vault/note]]` | — |

## 設計思想の違い

### Obsidian

- 「リンクは短いほどよい」哲学。`[[Project Alpha]]` のような人間可読名をそのまま使う。
- フロントマターの `aliases:` を autocomplete が拾い、`[[Full Name|alias]]` を自動生成する。
- 設定で wikilink / 標準 markdown link のデフォルトを切り替え可能。

### Foam

- VS Code 拡張。GitHub Pages と組み合わせる前提で設計されている。
- "placeholder"（リンク先なし）と "orphan"（被リンクなし）を別概念として明示的に管理する。
- `Foam: Show Graph` コマンドでグラフビュー、Placeholders パネルで未作成リンクを一覧。

### Dendron

- 階層を **ファイルシステムではなく dot 区切りファイル名** で表現する独特の設計（`recipes.italian.tiramisu.md`）。
- wikilink のエイリアス順序が他ツールと逆：`[[label|note.path]]`。**移植時の罠になりやすい**。
- マルチボルト時は `[[dendron://vault-name/note.path]]` の cross-vault link を使用。
- `![[note]]` の Note Reference で transclusion をサポート。

### Markdown Notes (VS Code)

- Dendron に多くの機能が吸収されており、現在は単独で使う動機が薄い。最小構成で wikilink + backlink だけ欲しい場合の選択肢。

## 移植性の罠

複数ツール間で wiki を共有する際の主な非互換ポイント：

1. **エイリアス順序**: Obsidian/Foam の `[[target|label]]` と Dendron の `[[label|target]]` は逆。
2. **解決ルール**: Obsidian は vault 全体での shortest unique path、Dendron は dot hierarchy の完全名。同じ `[[foo]]` でも振る舞いが違う。
3. **ブロック参照**: Obsidian の `^block-id` は他ツールでほぼ通用しない。
4. **transclusion**: `![[note]]` は Foam/Dendron でサポート、Obsidian コアでは限定的（プラグイン依存）。

本プロジェクトの [[wikilink-link-parser-spec]] ([↗](wikilink-link-parser-spec.md)) は `[a-z0-9-]+` の slug のみを受理する保守的な方針で、これら 4 ツールすべての最小公倍数互換を狙っている。GitHub 上での見え方は [[wikilink-github-interop]] ([↗](wikilink-github-interop.md)) を参照。

## 関連

- [[wikilink-github-interop]] ([↗](wikilink-github-interop.md)) — GitHub 上での wikilink の扱い
- [[wikilink-conversion-strategies]] ([↗](wikilink-conversion-strategies.md)) — リーダー間/出力先での変換戦略
- [[wikilink-link-parser-spec]] ([↗](wikilink-link-parser-spec.md)) — 本プロジェクトの抽出パーサ仕様

## 出典

- [Internal links — Obsidian Help](https://help.obsidian.md/links)
- [Aliases — Obsidian Help](https://help.obsidian.md/aliases)
- [Wikilinks — Foam](https://foambubble.github.io/foam/user/features/wikilinks)
- [foambubble/foam (GitHub)](https://github.com/foambubble/foam)
- [Wiki Link — Dendron Wiki](https://wiki.dendron.so/notes/90mrtp10ucyyvt60qekuj4y/)
- [Cross Vault Link — Dendron Wiki](https://wiki.dendron.so/notes/wb1k8li6r5kfzhla7p3ty80/)
- [Hierarchies — Dendron Wiki](https://wiki.dendron.so/notes/f3a41725-c5e5-4851-a6ed-5f541054d409/)
