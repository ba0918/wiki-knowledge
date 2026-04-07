---
title: lint-wiki.py wikilink パーサ仕様
type: wiki
source_refs:
  - "raw/articles/20260407-wikilink-link-parser-spec.md"
created: 2026-04-07
updated: 2026-04-07
category: references
tags: [wikilink, parser, lint, spec, internal]
related:
  - "concepts/wikilink-github-interop.md"
  - "concepts/wikilink-reader-comparison.md"
  - "concepts/wikilink-conversion-strategies.md"
---

# lint-wiki.py wikilink パーサ仕様

> 本プロジェクト `skills/wiki/scripts/lib/inventory.py` の `find_wikilinks()` の振る舞いを仕様として固定する。`[a-z0-9-]+` の slug のみを受理する保守的な方針で、コードフェンスとインラインコードを除外する。

## 抽出対象の正規表現

`skills/wiki/scripts/lib/inventory.py` から：

```python
_FENCE_RE       = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_WIKILINK_RE    = re.compile(r"\[\[([a-z0-9-]+)(?:\|[^\]]*)?\]\]")
```

## 抽出アルゴリズム

`find_wikilinks(text)` の処理順：

1. fenced code block (` ```…``` `) を `_FENCE_RE` で全削除（DOTALL なので改行を跨ぐ）。
2. inline code span (`` `…` ``) を `_INLINE_CODE_RE` で削除（改行を跨がない）。
3. 残りに対して `_WIKILINK_RE.findall(...)` を実行し、キャプチャ 1（slug）のリストを返す。
4. 出現順を保持し、**重複は除去しない**。

## 受理する形式

| 入力 | 抽出される slug | 備考 |
|---|---|---|
| `[[trust-score]]` | `trust-score` | 基本形 |
| `[[trust-score\|信頼度スコア]]` | `trust-score` | パイプ後はエイリアスとして無視 |
| `[[gap-detection]]` を 3 回出現 | 3 回とも抽出 | 重複保持 |

## 拒否する形式

- `[[TrustScore]]` — 大文字を含む（slug は `[a-z0-9-]+` のみ）
- `[[trust_score]]` — アンダースコア不可
- `[[trust score]]` — スペース不可
- `[[ trust-score ]]` — 前後空白不可
- `[[trust-score#heading]]` — `#` 不可（見出し/ブロック参照は未サポート）
- `[[]]` — 空 slug 不可
- `[trust-score]` — 単一角括弧は標準 markdown link で wikilink ではない

## コードスパン除外の境界

### 除外される

- フェンス内の任意の行（言語タグ付きでも）
- インラインコード ``` `[[trust-score]]` ```

### 除外されない（既知の限界）

- **チルダ三連フェンス** `~~~ … ~~~` は `_FENCE_RE` がバッククォートのみ対象のため除外されない
- **インデント 4 スペースのコードブロック**（CommonMark の indented code）も除外されない
- **HTML コメント** `<!-- [[slug]] -->` も除外されない

これらは現状の wiki でほぼ使われないため明示的に未対応。問題化したら拡張する。

## エッジケース

| ケース | 振る舞い |
|---|---|
| ネストした `[[a[[b]]]]` | 内側 `[[b]]` のみマッチ |
| 同一行に複数 | すべて出現順で抽出 |
| 改行を跨ぐ `[[trust-\nscore]]` | マッチしない |
| パイプ内に `]` を含む `[[a\|x]y]]` | パイプ後は `[^\]]*` で `]` を含めず終端 |

## 呼び出し側の使い方

`parse_article()` が `find_wikilinks(text)` の結果を `ArticleInventory.wikilinks: tuple[str, ...]` として保持する。`lint-wiki.py` 側はこれを参照して以下を検査する：

- **dead_link**: `wikilinks` の各 slug が他記事の `slug` と一致するか
- **link_quality / related_mismatch**: 本文 wikilink と `frontmatter.related` の整合
- **orphan**: 全記事を走査し、被参照ゼロの記事を検出
- **coverage_gap**: `gap_topics`（QueryLog 由来）との突き合わせ

## 設計上の意思決定

- **PyYAML 非依存**: `find_wikilinks` は純粋な regex 処理のみ。テストは `text` を直接渡せばよく、I/O を伴わない。`design-principles` の "Pure Functions at Module Boundaries" に整合する。
- **正規化**: `parse_article()` が CRLF/CR を LF に正規化したうえで `find_wikilinks` に渡すため、改行コードによる差は出ない。
- **slug 制約と命名規則の一致**: `[a-z0-9-]+` は `categories.json` / `page-template.json` 由来の slug 命名規則 `^[a-z0-9-]+$` と一致しており、wiki 全体で 1 つのルールに集約される。これが [[wikilink-reader-comparison]] で挙げた 4 ツールすべての最小公倍数互換にもなっている。

## テスト

`skills/wiki/scripts/lib/test_inventory.py` に `find_wikilinks` の単体テストが存在し、基本抽出・パイプエイリアスの剥離・フェンス除外・インラインコード除外・重複保持・出現順序を検証する。

## 関連

- [[wikilink-github-interop]] — `[[…]]` 構文の GitHub 上での扱い
- [[wikilink-reader-comparison]] — 他リーダー実装との互換範囲
- [[wikilink-conversion-strategies]] — 本パーサが支える併記方式 lint

## 出典

- `skills/wiki/scripts/lib/inventory.py`（リポジトリ内コード）
- `skills/wiki/scripts/lint-wiki.py`（リポジトリ内コード）
- `skills/wiki/scripts/lib/test_inventory.py`（リポジトリ内テスト）
