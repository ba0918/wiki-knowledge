---
title: lint-wiki.py wikilink パーサ仕様
scraped: 2026-04-07
tags: [wikilink, parser, lint, spec, internal]
---

# lint-wiki.py wikilink パーサ仕様

## 出典コード

- `skills/wiki/scripts/lib/inventory.py` — `find_wikilinks()` 本体
- `skills/wiki/scripts/lint-wiki.py` — リンク品質検査の呼び出し側

このドキュメントは上記実装の振る舞いを仕様として固定する。

## 抽出対象の正規表現

```python
_FENCE_RE       = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_WIKILINK_RE    = re.compile(r"\[\[([a-z0-9-]+)(?:\|[^\]]*)?\]\]")
```

## 抽出アルゴリズム（`find_wikilinks`）

1. 入力テキストから **fenced code block** (` ```…``` `) を `_FENCE_RE` で全削除（DOTALL なので改行を跨ぐ）。
2. 続いて **inline code span** (`` `…` ``) を `_INLINE_CODE_RE` で削除（改行を跨がない）。
3. 残ったテキストに対して `_WIKILINK_RE.findall(...)` を実行し、キャプチャ 1（slug 部分）のリストを返す。
4. 出現順は保持し、**重複は除去しない**（後段の lint がカウントベースで判定するため）。

## 受理する形式

| 入力 | 抽出される slug | 備考 |
|---|---|---|
| `[[trust-score]]` | `trust-score` | 基本形 |
| `[[trust-score\|信頼度スコア]]` | `trust-score` | パイプ後はエイリアスとして無視 |
| `[[gap-detection]]` を 3 回出現 | 3 回とも抽出 | 重複保持 |

## 拒否する形式（マッチしない）

- `[[TrustScore]]` — 大文字を含む。slug は `[a-z0-9-]+` のみ。
- `[[trust_score]]` — アンダースコア不可。
- `[[trust score]]` — スペース不可。
- `[[ trust-score ]]` — 前後空白不可。
- `[[trust-score#heading]]` — `#` 不可（ブロック/見出し参照は未サポート）。
- `[[]]` — 空 slug 不可（`+` 量化子）。
- 単一角括弧 `[trust-score]` — 標準 markdown link として扱われ wikilink にはならない。

## コードスパン除外の境界

### 除外される

- フェンス内の任意の行（言語タグ付きでも）：

  ~~~markdown
  ```python
  link = "[[trust-score]]"  # ← 抽出されない
  ```
  ~~~

- インラインコード：``` `[[trust-score]]` ``` も抽出されない。

### 除外されない（既知の限界）

- **チルダ三連フェンス** `~~~ … ~~~` は `_FENCE_RE` がバッククォートのみを対象にしているため除外されない。チルダフェンス内の wikilink は誤って抽出される。
- **インデント 4 スペースのコードブロック**（CommonMark の indented code）も除外されない。
- **HTML コメント** `<!-- [[slug]] -->` も除外されない。

これらは現状の lint で実害がない（プロジェクト内でほぼ使われない）ため明示的に未対応。問題化したら拡張する。

## エッジケース整理

| ケース | 振る舞い |
|---|---|
| ネストした角括弧 `[[a[[b]]]]` | 内側 `[[b]]` のみマッチ（`b` が抽出）。外側は `[[a` が残り破棄。 |
| 同一行に複数 | すべて出現順で抽出 |
| 改行を跨ぐ wikilink `[[trust-\nscore]]` | マッチしない（`[a-z0-9-]+` は改行を含まない） |
| パイプ内に `]` `[[a\|x]y]]` | パイプ後の `[^\]]*` が `]` を含めないため、最初の `]` でマッチ終端。`a` のみ抽出される可能性あり |

## 呼び出し側の使い方

`parse_article()` が `find_wikilinks(text)` の結果を `ArticleInventory.wikilinks: tuple[str, ...]` として保持する。`lint-wiki.py` 側はこれを参照して以下の検査を行う：

- **dead_link**: `wikilinks` の各 slug が他記事の `slug` と一致するか
- **link_quality / related_mismatch**: 本文 wikilink と `frontmatter.related` の整合
- **orphan**: 全記事を走査し、被参照ゼロの記事を検出
- **coverage_gap**: `gap_topics`（QueryLog 由来）との突き合わせ

## 設計上の意思決定

- **PyYAML 非依存**: `find_wikilinks` は純粋な regex 処理のみ。テストは `text` を直接渡せばよく、I/O を伴わない。
- **正規化**: `parse_article()` が CRLF/CR を LF に正規化したうえで `find_wikilinks` に渡すため、改行コードによる差は出ない。
- **slug 制約と命名規則の一致**: `[a-z0-9-]+` は `categories.json` / `page-template.json` 由来の slug 命名規則 `^[a-z0-9-]+$` と一致しており、wiki 全体で 1 つのルールに集約される。

## テスト

`skills/wiki/scripts/lib/test_inventory.py` に `find_wikilinks` の単体テストが存在し、以下を検証する：

- 基本抽出
- パイプエイリアスの剥離
- フェンス除外
- インラインコード除外
- 重複保持
- 出現順序

## 出典

- `skills/wiki/scripts/lib/inventory.py`（リポジトリ内コード）
- `skills/wiki/scripts/lint-wiki.py`（リポジトリ内コード）
- `skills/wiki/scripts/lib/test_inventory.py`（リポジトリ内テスト）
