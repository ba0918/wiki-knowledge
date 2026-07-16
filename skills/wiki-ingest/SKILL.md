---
name: wiki-ingest
description: >
  ソースドキュメント（URL、ファイル、記事、git リポジトリ）を Wiki の raw/ にステージングする。
  「ソースを取り込む」「ingest」「wiki に追加」「URL を wiki に入れて」「リポジトリを取り込む」で使用する。
---

# Wiki Ingest

ソースドキュメントを `{wiki_root}/raw/` にステージングする。raw/ は immutable（一度保存したら変更しない）。

**wiki_root の取得**: `AGENTS.md` の `wiki_root:` フィールドを読む（未設定なら wiki-init を案内）。パス解決の詳細は [paths.md](skills/wiki/references/paths.md) を参照。

## 入力

入力タイプに応じて以下のフローで処理する:

```
入力 → git URL / git リポジトリパス? → repo フロー（下記「repo ソースの ingest」）
     → URL?          → WebFetch で取得       → article として保存
     → ファイルパス? → Read で読み込み       → file として保存
     → テキスト直接? → そのまま使用          → article として保存
```

git URL の判定: `https://…/owner/repo(.git)` / `ssh://…` / `git@host:owner/repo.git`、またはローカルパスで `.git` ディレクトリを含む場合。

## セキュリティチェック（必須）

[security.md](skills/wiki/references/security.md) に従い `security_scan.py` を実行する。exit 1 で処理を中断。

スクリプトの ✅/❌ サマリー出力をそのまま表示する:
```
✅ パス traversal: OK
✅ 機密データ: OK
✅ プロンプトインジェクション: OK
```

## プロセス

1. **保存先とファイル名を決定**:
   | 入力タイプ | 保存先 | ファイル名 |
   |-----------|--------|-----------|
   | URL / テキスト直接 | `raw/articles/` | `{YYYYMMDD}-{slug}.md` |
   | 単一ローカルファイル | `raw/files/` | 元のファイル名そのまま |
   | repo フロー | `raw/files/{repo-slug}/` | 元のファイル名そのまま |
2. セキュリティチェックを実行（`--filename` には手順 1 で決定した名前を渡す）
3. フロントマターを付与:
   ```yaml
   ---
   title: ドキュメントタイトル        # 必須。ソースの H1 をベースに、内容を反映した記述的タイトルにする
   scraped: YYYY-MM-DD               # 必須（処理日）
   source_url: https://example.com   # URL入力の場合のみ付与
   source_path: path/to/original.md  # ローカルファイル入力の場合のみ付与（元パスの追跡用）
   tags: [自動推定タグ]               # 本文の主題から推定、推定できなければ空配列 []
   ---
   ```
4. 手順 1 で決定した保存先に保存
5. `log.md` に追記:
   ```bash
   python3 skills/wiki/scripts/log_append.py ingest --wiki-root {wiki_root} --slug {slug} --source-kind {source_kind}
   ```

## 完了メッセージ

```
── ingest 完了 ──
保存先: {wiki_root}/raw/articles/{filename}
フロントマター:
  title: {title}
  source_url: {url}        ← URL入力の場合のみ
  scraped: {date}
  tags: [{tags}]
次のステップ: `wiki-compile` で記事を生成、または `wiki-cycle --compile-only` で compile + lint を一括実行
```

## repo ソースの ingest

git リポジトリ（URL またはローカルパス）を取り込む。**複数リポジトリは 3 段で処理する**（横断 wikilink は全リポジトリが出揃って初めて張れるため、段の順序を守る）:

**段1 — 全リポジトリを clone + manifest 生成**（1コマンドで複数可）:

```bash
python3 skills/wiki/scripts/repo_ingest.py <url-or-path>... --wiki-root {wiki_root}
```

- clone は自動: `ghq` があれば `ghq get --shallow`、なければ `git clone --depth 1` で `{wiki_root}/.cache/repos/` へ
- manifest は `{wiki_root}/.cache/manifests/{slug}.json` に出力。**全部読まず、必要な tier だけ Read する**
- 機械生成の `repo-inventory.md` が `raw/files/{slug}/` に保存される

**段2 — 全リポジトリの docs 選定 + ingest**:

1. manifest の tier1（README / architecture / adr）を基本とし、ユーザーと選定を確認
2. 各ファイルを既存の file ingest フロー（セキュリティチェック込み）で `raw/files/{slug}/` に保存
3. フロントマターに `source_url` + `source_revision`（commit hash）+ `source_path` を付与（[frontmatter-schemas.md](skills/wiki/references/frontmatter-schemas.md) の repo 節参照）
4. log.md に追記: `python3 skills/wiki/scripts/log_append.py ingest --wiki-root {wiki_root} --slug {slug} --source-kind "repo @ {short-hash}"`

**段3 — 一括 compile**:

全リポジトリの ingest 完了後に wiki-compile を実行する。手順は [compilation-guide.md](skills/wiki/references/compilation-guide.md) の「repo ソースの compile」節に従う。
