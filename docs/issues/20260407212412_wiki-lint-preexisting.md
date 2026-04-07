---
title: Wiki lint pre-existing issues — 例示記法の dead_link 誤検出と古い related_mismatch
created: 2026-04-07 21:24:12
status: 🔴 Open
tags: [wiki, lint, tech-debt]
---

# Wiki lint pre-existing issues

`/wiki:wiki lint` 実行時に検出される、今回の graphify ingest セッション以前から存在していた lint 問題をまとめて整理する。

## 検出状況

`.wiki/outputs/reports/20260407-lint.md` 参照。

- 🔴 Errors: 4 件
- 🟡 Warnings: 10 件
- 🔵 Info: 1 件

## 問題 1: 例示用 `[[wikilink]]` `[[foo]]` 記法の dead_link 誤検出 (Error 4 件)

### 詳細

以下の記事で、wikilink 記法を**説明するために本文に書いた** `[[wikilink]]` `[[foo]]` が、lint-wiki.py によって本物の dead link として誤検出される。

| 記事 | 例示 |
|------|------|
| `concepts/trust-score.md` | `[[wikilink]]`, `[[foo]]` |
| `concepts/querylog.md` | `[[wikilink]]` |
| `concepts/wiki-knowledge-architecture.md` | `[[wikilink]]` |

Info にも `coverage_gap: [[wikilink]] referenced 3 times but no page exists` が同根で出ている。

### 修正案

以下のいずれか:

- **(A) 記法の escape**: 本文中の例示部分を `` `[[wikilink]]` `` のようにバッククォートで囲み、lint がコードスパン内を無視するようにする
- **(B) lint の誤検出回避**: `lint-wiki.py` のパース時にコードスパン (`` `...` ``) およびコードフェンス内の wikilink を抽出対象から除外する
- **(C) 両方**: コードスパン除外を lint に実装しつつ、既存記事もバッククォートで囲む

(B) が根本対応として望ましい。(A) は応急処置。

## 問題 2: 古い related_mismatch warning (Warning 10 件)

### 詳細

`related` フロントマターには記載されているが、本文中に対応する `[[wikilink]]` がない記事ペア。

| 記事 | 不足リンク先 |
|------|------|
| `llm-wiki-knowledge-base` | `gap-detection`, `trust-score` |
| `llm-wiki-tooling` | `wiki-knowledge-architecture`, `llm-wiki-knowledge-base`, `llm-wiki-use-cases` |
| `llm-wiki-use-cases` | `wiki-knowledge-architecture`, `llm-wiki-knowledge-base`, `llm-wiki-tooling` |
| `wiki-knowledge-architecture` | `llm-wiki-use-cases`, `llm-wiki-tooling` |

これらは graphify セッション以前から存在しており、複数の記事間で「related FM だけ更新して body wikilink を追加し忘れた」状態。

### 修正案

各記事の本文末尾または適切な箇所に「## 関連」セクションを追加し、`[[slug]]` を埋める。今回の graphify ingest セッションで採用した「## 関連」セクションパターンを踏襲する。

## 影響範囲

- lint レポートのノイズが増え、本物の問題が埋もれるリスク
- Wiki 自体の機能には影響なし（wikilink は機能している）

## 優先度

🟡 中 — 機能上は問題ないが、lint を品質ゲートとして活用する上でノイズ除去は必要。

## 関連

- 今回の graphify ingest セッション: `.wiki/concepts/graphify-knowledge-graph-concepts.md`
- Lint 強化計画: `docs/plans/20260406053703_lint-enhancement.md`
- 今回の lint レポート: `.wiki/outputs/reports/20260407-lint.md`
