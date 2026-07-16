---
name: wiki-query
description: >
  Wiki の知識に基づいて質問に回答する。一般知識ではなく Wiki を情報源として、出典付きで回答を合成する。
  「wiki で調べて」「query」「wiki に聞いて」「ナレッジベースから回答」「wiki の知識で答えて」で使用する。
---

# Wiki Query

Wiki の知識に基づいて質問に回答する。一般知識ではなく Wiki を情報源とする。

**wiki_root の取得**: `AGENTS.md` の `wiki_root:` フィールドを読む（未設定なら wiki-init を案内）。パス解決の詳細は [paths.md](../wiki/references/paths.md) を参照。

## プロセス

1. **候補選定（retrieval pre-pass）**: 質問からキーワードを抽出し（日本語・英語の両方が考えられる場合は両方入れる）:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/query_retrieve.py --wiki-root {wiki_root} --keywords <kw1> <kw2> ...
   ```
   graph layer と Trust Score を消費した候補リスト（スコア・trust・選定理由つき）が返る。`outputs/graph.json` が無い場合は exit 2 で停止するので、先に `python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/graph_gen.py --wiki-root {wiki_root}` を実行してから `query_retrieve.py` を再実行する
2. **関連記事を読む**: 候補リストの上位から、回答の正確性が上がるものだけを選んで全文読み込む。候補外の記事が必要なら `{wiki_root}/index.md` から補ってよい
3. **回答合成**:
   - 主張には必ず `[[slug]]` で出典を付ける
   - **trust-aware 引用**: trust **0.30 未満** の記事を引用する場合「（信頼度低: {trust}）」を付す
   - 記事間の一致点・矛盾点を明示する
   - Wiki にカバーされていない領域を「ギャップ」として指摘し、**トピック名を明示する**
   - 質問の性質に応じてフォーマットを選ぶ（事実→散文、比較→テーブル、手順→番号付きリスト）
4. **保存を提案**: 回答後、Wiki 記事として保存するか確認する

**一般知識から回答しない。** Wiki の記事を必ず先に読む。矛盾がある場合は両方を提示する。

## 回答の保存（Wiki Promote）

ユーザが保存を承認した場合:
1. `{wiki_root}/concepts/{slug}.md` に記事として保存（`tags: [query, synthesis]`）
2. [post-processing.md](../wiki/references/post-processing.md) に従い後処理（Backlink Audit → index/AGENTS.md 更新 → wikilink rendering → log_append promote）

保存しない場合:
1. `{wiki_root}/outputs/queries/{YYYYMMDD}-{slug}.md` に回答を保存
   - `{slug}` は質問の主題から英語 kebab-case で生成
   - 本文には回答全文をそのまま保存（要約しない）
   - フロントマター:
   ```yaml
   ---
   title: 質問の要約
   type: query
   question: 元の質問文
   answered: YYYY-MM-DD
   sources_consulted:
     - "concepts/xxx.md"
   promoted: false
   ---
   ```
2. `log.md` に追記:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/log_append.py query --wiki-root {wiki_root} --summary "{question summary}"
   ```

## QueryLog 追記（保存判断の後に必ず実行）

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/querylog_append.py --wiki-root {wiki_root} \
  --question "{ユーザの元の質問文}" \
  --consulted concepts/{slug1}.md concepts/{slug2}.md \
  --answer-file {保存した回答ファイルのパス} \
  [--gap-topics "{topic1}" "{topic2}"] \
  [--promoted --promoted-to concepts/{slug}.md]
```

- `--consulted`: 読み込んだ全記事パス（`{wiki_root}` からの相対）
- `--answer-file`: 回答テキストの保存先。`sources_cited` はここから抽出される
- `--gap-topics`: ギャップのトピック名（なければ省略）
- exit code: `0` = 成功 / `1` = 検証エラー / `2` = 引数エラー

**⚠** `querylog.jsonl` にはユーザの質問文がそのまま記録される。デフォルトで `.gitignore` 対象。

## 完了メッセージ

```
── query 完了 ──
参照記事: {N} 件（{slug}, ...）
ギャップ: {gap_topics または "なし"}
保存: {保存先パス}
次のステップ: {promote 済みなら省略、未保存なら `wiki-query` で追加質問}
```

`{N}` と `{slug}` は `sources_consulted`（実際に読んだ記事）に基づく。
