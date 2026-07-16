# 共通後処理

compile と query(promote) の両方で使用する後処理手順。

## Backlink Audit（必須）

記事生成後、既存の全記事を `grep` で走査し、新記事に言及すべき箇所を特定する。
該当する既存記事に `[[new-slug]]` リンクと `related` フロントマターを追加し、その記事の `updated` フロントマターも実行日に更新する。

このステップを skip すると Wiki が一方向リンクの blog に退化する。**必ず実行すること。**

## index / AGENTS.md 更新

1. `{wiki_root}/index.md` に新記事を追加（カテゴリ別、1行サマリー）
2. `AGENTS.md` の Articles セクションを更新

## wikilink rendering

```bash
python3 skills/wiki/scripts/wikilink_render.py --write {wiki_root}/concepts/
```

`[[slug]]` を GitHub Web UI で踏める `[[slug]] ([↗](slug.md))` 形式に併記する（idempotent）。

**注**: outputs/queries/ 内の `[[wikilink]]` には GitHub 併記は不要（wikilink_render の対象は `concepts/` のみ）。

## log_append

各操作に応じたサブコマンドで `log.md` に追記する（フォーマットはスクリプトが管理）:

```bash
# compile
python3 skills/wiki/scripts/log_append.py compile --wiki-root {wiki_root} --title "{Title}" --word-count {N} --sources {N}

# promote
python3 skills/wiki/scripts/log_append.py promote --wiki-root {wiki_root} --title "{Title}"

# discover
python3 skills/wiki/scripts/log_append.py discover --wiki-root {wiki_root} --slug {slug} --articles N
```
