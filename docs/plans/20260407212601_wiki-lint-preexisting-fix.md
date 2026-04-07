## Wiki Lint Pre-existing Issues 修正

**Cycle ID:** `20260407212601`
**Started:** 2026-04-07 21:26:01
**Status:** 🟢 Complete
**Issue:** 20260407212412_wiki-lint-preexisting

---

## 📝 What & Why

`/wiki:wiki lint` で検出される pre-existing な lint ノイズ（例示用 `[[wikilink]]` の dead_link 誤検出 4 件 + 古い related_mismatch 10 件）を解消し、lint を品質ゲートとして信頼できる状態にする。

## 🎯 Goals

- `lint-wiki.py` が **コードスパン (`` `...` ``) およびコードフェンス (```` ``` ````) 内の `[[wikilink]]` を抽出対象から除外** するようにし、根本対応する
- 既存 4 記事の例示用 `[[wikilink]]` `[[foo]]` を併せてバッククォート囲みに統一（応急処置 + 可読性向上）
- 4 記事に「## 関連」セクションを追加して related_mismatch warning 10 件を解消
- lint レポート上で Errors=0、related_mismatch warning=0 を達成

## 📐 Design

### Files to Change

```
skills/wiki/scripts/
  lint-wiki.py                  - find_wikilinks(): コードスパン/フェンス除外を追加
  test_lint_wiki.py             - コードスパン内 wikilink 無視のテスト追加（red→green）

.wiki/concepts/
  trust-score.md                - 例示 [[wikilink]] [[foo]] をバッククォート囲みに
  querylog.md                   - 例示 [[wikilink]] をバッククォート囲みに
  wiki-knowledge-architecture.md - 例示 [[wikilink]] をバッククォート + ## 関連 追加
  llm-wiki-knowledge-base.md    - ## 関連 追加（gap-detection, trust-score）
  llm-wiki-tooling.md           - ## 関連 追加（wiki-knowledge-architecture, llm-wiki-knowledge-base, llm-wiki-use-cases）
  llm-wiki-use-cases.md         - ## 関連 追加（wiki-knowledge-architecture, llm-wiki-knowledge-base, llm-wiki-tooling）
```

### Key Points

- **コードスパン/フェンス除外（根本対応）**: `find_wikilinks(text)` の前処理で
  1. ```` ```...``` ```` フェンスブロックを正規表現で除去
  2. 行内の `` `...` `` インラインコードスパンを除去
  3. 残った text に対して既存の `\[\[([a-z0-9-]+)\]\]` を適用
  これにより lint がドキュメント記述（例示）と本物の参照を区別できる
- **テスト先行 (TDD)**: `test_lint_wiki.py` に「コードスパン内の `[[foo]]` は dead_link を発生させない」「コードフェンス内の `[[bar]]` も同様」のケースを追加して red を確認 → 実装で green
- **既存記事の修正**: 例示部分の可読性向上のためバッククォート囲み（`` `[[wikilink]]` ``）を併用。lint 修正の root fix と二重防御
- **「## 関連」セクション統一**: graphify ingest セッションで採用した形式（末尾に `## 関連` 見出し → 箇条書きで `[[slug]]` — 短い説明）を踏襲
- **検証**: 修正後に `python3 skills/wiki/scripts/lint-wiki.py --wiki-root .wiki --format report` を実行し、Errors=0、related_mismatch=0 を確認

## ✅ Tests

- [ ] `test_find_wikilinks_ignores_inline_code_span` — `` `[[foo]]` `` は抽出されない
- [ ] `test_find_wikilinks_ignores_fenced_code_block` — ```` ```\n[[bar]]\n``` ```` は抽出されない
- [ ] `test_find_wikilinks_still_extracts_plain` — 通常の `[[slug]]` は従来通り抽出される（regression guard）
- [ ] `test_dead_link_check_skips_example_wikilinks` — コードスパン例示のみを含む記事は dead_link finding ゼロ
- [ ] 既存の 44 テストすべてが green を維持

## 🔒 Security

- N/A（lint スクリプトの内部ロジック改修と Wiki 記事の編集のみ。外部入力なし）

## 📊 Progress

| Step | Status |
|------|--------|
| Tests | 🟢 |
| Implementation (lint-wiki.py) | 🟢 |
| Article fixes (6 articles) | 🟢 |
| Lint report verification | 🟢 |
| Commit | 🟢 |

**Legend:** ⚪ Pending · 🟡 In Progress · 🟢 Done

---

**Next:** Write tests → Implement → Fix articles → Verify lint → Commit 🚀
