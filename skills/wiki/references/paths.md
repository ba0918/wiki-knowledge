# パス解決ルール

全操作で共通。混同するとリンク切れになるので、必ずこの表に従う。

## wiki_root の取得

全操作の前に、プロジェクトルートの `AGENTS.md`（または `CLAUDE.md`）を読み、`wiki_root` フィールドからベースパスを取得する。
見つからない場合は `wiki-init` の実行を促す。

```
AGENTS.md → wiki_root: .wiki（デフォルト）
```

## パス解決表

フロントマターと本文で基準が異なる。

| 場所 | 基準 | 例（concepts/foo.md から書く場合） |
|------|------|----------------------------------|
| フロントマター `source_refs` | `{wiki_root}` からの相対 | `raw/articles/20260405-bar.md` |
| フロントマター `related` | `{wiki_root}` からの相対 | `concepts/bar.md` |
| 本文 `[[wikilink]]` | slug → `concepts/{slug}.md` | `[[bar]]` |
| 本文 Markdown リンク | **書いてるファイルからの相対** | `[出典](../raw/articles/20260405-bar.md)` |

## スクリプトパス

全スキルでプロジェクトルート基準のパスを使用する:

```bash
python3 skills/wiki/scripts/xxx.py --wiki-root {wiki_root}
```
