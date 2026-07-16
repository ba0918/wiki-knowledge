---
name: wiki-cycle
description: >
  Ingest → Compile → Graph Gen → Lint を一括実行するオーケストレーター。
  「wiki cycle」「一括実行」「ingest から lint まで通して」で使用する。
---

# Wiki Cycle

Ingest → Compile → Lint を一括実行するオーケストレーター。ビジネスロジックは持たず、各 leaf スキルへの委譲のみ行う。

スクリプトパスの解決は [paths.md](../wiki/references/paths.md) に従う。

## 利点

- セキュリティ問題検出時のフロー全体中断が自動適用される
- compile エラー発生時の lint スキップが自動適用される
- 途中で止まっても結果サマリーで状況を把握できる

## 引数

| 引数 | 説明 |
|------|------|
| ソース指定 | ファイルパスまたは URL（ingest 対象） |
| `--compile-only` | Ingest をスキップし、compile + graph_gen + lint を実行 |
| `--lint-only` | graph_gen + Lint のみ実行 |
| `--discover` | repo ソースに対して discover を compile の前に実行する |

## フロー定義

引数に応じて以下のフローを実行する。graph_gen は compile と lint の間に**常に**挟む（skip すると lint が exit 2 で停止する）。

### デフォルトフロー（ソース指定あり）

```
1. Skill ツールで wiki:wiki-ingest を実行（ソースを raw/ にステージング）
2. [--discover 時のみ] Skill ツールで wiki:wiki-compile を実行（引数: discover {slug} --yes）
3. Skill ツールで wiki:wiki-compile を実行（記事を生成）
4. python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/graph_gen.py --wiki-root {wiki_root}
5. Skill ツールで wiki:wiki-lint を実行
6. 結果サマリーを表示
```

discover は repo ソース（manifest あり）の場合のみ有効。cycle 内の discover は非対話モード（`--yes`）。

### --compile-only フロー

```
1. Skill ツールで wiki:wiki-compile を実行（未コンパイルソースを自動検出）
2. python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/graph_gen.py --wiki-root {wiki_root}
3. Skill ツールで wiki:wiki-lint を実行
4. 結果サマリーを表示
```

### --lint-only フロー

```
1. python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/graph_gen.py --wiki-root {wiki_root}
2. Skill ツールで wiki:wiki-lint を実行
3. 結果サマリーを表示
```

## 中断ルール

- ingest のセキュリティチェックで問題が検出された場合 → フロー全体を中断
- compile でエラーが発生した場合 → lint はスキップ
- lint の 🔴 Error → 修復後に再 lint を提案

## 完了メッセージ

```
── cycle 完了 ──
ingest:  {成功/スキップ/中断} — {slug}（{source_kind}）
compile: {成功/スキップ} — {N} 記事生成
lint:    {成功/スキップ} — 🔴 {N}, 🟡 {N}, 🔵 {N}
次のステップ: {Error/Warning があれば修復手順を提示、なければ `wiki-query` で知識を活用}
```
