---
title: RECIPE_TITLE（〜の抽出）
type: wiki
source_refs: []
created: YYYY-MM-DD
updated: YYYY-MM-DD
category: practices
tags: [selection-recipe, DOMAIN_TAG]
related: []
---

# RECIPE_TITLE（〜の抽出）

> どんな依頼が来たときに、何をどう判断して取得するかの Recipe。1-2 文で対象を要約する。

## 対象定義

業務の言葉で 1 行（例: ev-2026 参加登録者のうち返金を受けていない人 = 補填対象者）。

- tool: `TOOL_ID`（接続先・上限は `tools/catalog.json` が真実源。ここには写さない）
- 主要テーブル: `table_a` / `table_b`
- key_columns: `user_id`（結果の一意性を検証する列）

## 選定条件と判断

### inclusion

| 条件 | SQL への落とし方 | なぜこの表現か |
|---|---|---|
| （例）ev-2026 の登録者 | `registrations.event = 'ev-2026'` | イベント ID は … で採番される |

### exclusion（Why not — 最重要）

| 除外するもの | SQL への落とし方 | なぜ除外するか |
|---|---|---|
| （例）返金済みの人 | `NOT IN (SELECT user_id FROM refunds)` | 返金済みは補填対象外という業務ルール（YYYY-MM-DD の裁定） |

## ファネル構成

承認者が検算しやすい順に条件を足す:

1. `全登録者` — inclusion のみ
2. `返金なし` — exclusion を適用

想定件数レンジの目安と根拠: （例: 登録者総数の 90〜100%。返金率は例年 5% 未満のため）

## 実施ログ

| 日付 | plan_id | 件数 | 所要時間（受領〜引き渡し） | 気づき・条件の変化 |
|---|---|---|---|---|
| YYYY-MM-DD | | | | |

## 関連

- [[related-slug]] — 関連する業務知識・用語
