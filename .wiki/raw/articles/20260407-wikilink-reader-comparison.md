---
title: wikilink リーダー実装比較（Obsidian / Foam / Dendron / VS Code Markdown Notes）
scraped: 2026-04-07
tags: [wikilink, obsidian, foam, dendron, vscode, comparison]
---

# wikilink リーダー実装比較

## 対象ツール

ローカル Markdown ベースの PKM (Personal Knowledge Management) 系で wikilink を主言語として扱う 4 ツールを比較する。

- **Obsidian**: スタンドアロンの Electron アプリ。ローカルファイル直編集。
- **Foam**: VS Code 拡張。GitHub Pages との統合を前提とした設計。
- **Dendron**: VS Code 拡張。階層的命名（dot notation）を中心に据えた設計。
- **Markdown Notes (VS Code)**: シンプルな wikilink + backlink 拡張。Dendron に多くの機能を吸収された。

## 比較表

| 観点 | Obsidian | Foam | Dendron | Markdown Notes |
|---|---|---|---|---|
| 基本構文 | `[[Note Name]]` | `[[note-name]]` | `[[hierarchical.note]]` | `[[note]]` |
| エイリアス | `[[Note\|alias]]` | `[[wikilink\|alias]]` | `[[label\|note.path]]`（順序が逆） | 限定的 |
| 見出しリンク | `[[Note#Heading]]` | `[[note#Section]]` | サポート | — |
| ブロック参照 | `[[Note#^block-id]]` | — | Note Reference (`![[note]]`) | — |
| 解決ルール | shortest unique path | ファイル名一致 | hierarchical（dot 階層） | ファイル名一致 |
| バックリンク | あり（コアプラグイン） | あり | あり | あり |
| グラフビュー | あり | あり（Foam: Show Graph） | あり | なし |
| プレースホルダ | 未作成リンクをハイライト | あり（Placeholders panel） | スタブノート自動生成 | — |
| マルチボルト | 限定的 | 単一ワークスペース | `[[dendron://vault/note]]` | — |

## 各ツールの設計思想の違い

### Obsidian

- リンクは「短ければ短いほどよい」という哲学。`[[Project Alpha]]` のように人間可読な名前をそのまま使う。
- フロントマターで `aliases:` を定義すると autocomplete が `[[Full Name|alias]]` を自動生成する。
- 設定で wikilink / 標準 markdown link のどちらをデフォルトにするか選択可能。

### Foam

- VS Code 内で動作することを前提に、Git/GitHub Pages との親和性を重視。
- "placeholder" の概念を明示的に扱い、未作成リンクを Placeholders パネルで可視化する。
- "orphans"（被リンクなし）と "placeholders"（リンク先なし）を別概念として区別。

### Dendron

- 階層を **ファイルシステムではなく dot 区切りファイル名** で表現する独特の設計（`recipes.italian.tiramisu.md`）。
- wikilink のエイリアス順序が他ツールと逆：`[[label|note.path]]`（Obsidian は `[[note|label]]`）。**移植時の罠になりやすい**。
- マルチボルト時に `[[dendron://vault-name/note.path]]` の cross-vault link を提供。
- "Note Reference" (`![[note]]`) で他ノートの内容を埋め込み可能（transclusion）。

### Markdown Notes

- Dendron に大半の機能が取り込まれ、現在は単独で使う動機が薄い。最小構成で wikilink + backlink だけ欲しい場合に選ばれる。

## 移植性の罠

複数ツール間で wiki を共有する際の主な非互換ポイント：

1. **エイリアス順序**: Obsidian/Foam の `[[target|label]]` と Dendron の `[[label|target]]` は逆。
2. **解決ルール**: Obsidian は vault 全体での shortest unique path、Dendron は dot hierarchy の完全名。同じ `[[foo]]` でも振る舞いが違う。
3. **ブロック参照**: Obsidian の `^block-id` は他ツールでほぼ通用しない。
4. **transclusion**: `![[note]]` は Foam でサポートされるが Obsidian のコアでは限定的（プラグイン依存）。

## 出典

- [Internal links — Obsidian Help](https://help.obsidian.md/links)
- [Aliases — Obsidian Help](https://help.obsidian.md/aliases)
- [Wikilinks — Foam](https://foambubble.github.io/foam/user/features/wikilinks)
- [foambubble/foam (GitHub)](https://github.com/foambubble/foam)
- [Wiki Link — Dendron Wiki](https://wiki.dendron.so/notes/90mrtp10ucyyvt60qekuj4y/)
- [Cross Vault Link — Dendron Wiki](https://wiki.dendron.so/notes/wb1k8li6r5kfzhla7p3ty80/)
- [Hierarchies — Dendron Wiki](https://wiki.dendron.so/notes/f3a41725-c5e5-4851-a6ed-5f541054d409/)
- [Obsidian vs Dendron — Dendron Wiki](https://wiki.dendron.so/notes/a84ff014-e871-445d-9366-d97f1ad882f1/)
