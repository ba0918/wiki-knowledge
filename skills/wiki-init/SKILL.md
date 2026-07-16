---
name: wiki-init
description: >
  プロジェクトに Wiki 構造（ディレクトリ・テンプレート・AGENTS.md）を初期化する。
  「wiki 初期化」「wiki init」「新しい wiki を作りたい」「ナレッジベースを作成」で使用する。
---

# Wiki Init

プロジェクトに Wiki 構造をブートストラップする。

パス解決は [paths.md](../wiki/references/paths.md) に従う。

## 事前チェック

プロジェクトルートの `AGENTS.md`（または `CLAUDE.md`）に `wiki_root` が既に存在する場合、再初期化するか確認する（どちらも存在しない場合は確認不要）。

## プロセス

1. Wiki パスを決定（デフォルト: `.wiki`、ユーザ指定可）。`wiki_root` はプロジェクトルート基準の相対パスで表記する
2. ディレクトリを作成:
   ```
   {wiki_root}/
   ├── raw/articles/
   ├── raw/files/
   ├── concepts/
   ├── outputs/queries/
   ├── outputs/reports/
   └── schema/
   ```
   ※ `index.md` と `log.md` はファイル — 手順 3 のテンプレコピーで作成する
3. テンプレートファイルを配置（テンプレートは全て `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/` に実体がある）:
   - `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/page-template.json` → `{wiki_root}/schema/page-template.json`（そのままコピー）
   - `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/categories.json` → `{wiki_root}/schema/categories.json`（そのままコピー）
   - `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/index-template.md` → `{wiki_root}/index.md`（そのままコピー）
   - `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/log-template.md` → `{wiki_root}/log.md`（`[YYYY-MM-DD]` を実行日に置換）
   - `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/wiki-gitignore-template` → `{wiki_root}/.gitignore`
     - 既に存在する場合は上書きせず、未記載の行だけを追記（merge 方式）
4. プロジェクトルートの `AGENTS.md` を設定:
   - **AGENTS.md がない場合**: `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/agents-md-template.md` を元に新規作成。プレースホルダを全て埋める:
     - `wiki_root` に実パスを設定
     - 本文中の `{wiki_root}` も実パスに展開（`{slug}` 等は残す）
     - `SCOPE_DESCRIPTION` は目的が判別できれば1〜2文、できなければ「_スコープ未設定。最初の ingest 時に記述する_」
   - **AGENTS.md に既に `wiki_root` がある場合**: 既存値を保持
   - **CLAUDE.md が存在しない場合**: `@AGENTS.md` のみを記述した `CLAUDE.md` を作成する
5. 完了メッセージで次のステップ（wiki-ingest）を案内

## 完了メッセージ

```
── init 完了 ──
Wiki ルート: {wiki_root}/
作成ディレクトリ: raw/articles/, raw/files/, concepts/, outputs/queries/, outputs/reports/, schema/
次のステップ: `wiki-ingest <URL or file>` でソースを取り込む
```
