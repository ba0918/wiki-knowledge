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

スクリプト・テンプレートはプラグイン本体に同梱されている。全スキルで `${CLAUDE_PLUGIN_ROOT}`（プラグインのインストール先ルートを指す環境変数）基準のパスを使用する:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/xxx.py --wiki-root {wiki_root}
```

`${CLAUDE_PLUGIN_ROOT}` が未設定の場合（プラグイン経由でなく、このリポジトリ自体を開いて開発しているとき）は、リポジトリルートからの相対パス `skills/wiki/scripts/xxx.py` を使う。
