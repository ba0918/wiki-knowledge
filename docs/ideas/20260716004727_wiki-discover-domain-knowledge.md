# wiki-discover: コードベースからのドメイン知識自動抽出

**Created:** 2026-07-16 00:47:27
**Status:** 💡 Idea
**Tags:** `domain-discovery`, `onboarding`, `source-code-analysis`, `multi-repo`
**Mode:** Brainstorm (with Codex second opinion)
**Rounds:** 4（R1-3 収束済み → R4 ingest 差別化・テスト走査・cross-repo 統合を確定）

---

## Summary

プロジェクト参入時に1回叩くと、リポジトリのソースコードから LLM がドメイン知識（アーキテクチャ、DB スキーマ、API ルート、ビジネスルール、状態遷移、用語集）を自動抽出し、wiki の raw 記事として蓄積するフェーズ。既存の wiki パイプライン（compile → graph_gen → lint → query）にそのまま流せる。複数リポジトリの横断知識も集約可能。

## Key Discussion Points

### 起点: mino-design-skills の分析から

- inspired-mino-design-skills リポジトリ（ミノ駆動氏の設計原則の AI Skill 化）を分析した際、`mino-domain-model-completeness` の12次元監査や `domain-discovery` の視点が参考になった
- ただし mino の形式主義（YAML schema、formal completeness package）は重すぎる。「参入初日に先輩に聞くこと」くらいの粒度が理想
- 盗むべきは**視点だけ**: actor + purpose、term ledger、context boundary、invisible concepts

### Codex 独立評価の結論: 「新スキャナ作るな、wiki に2ピース足せ」

Codex が wiki-knowledge のコード（`repo_ingest.py`、`repo_clone.py`、`architecture.md`）を実際に読んで出した結論:

1. **repo_ingest は既にある**が、やってるのは「ドキュメントファイルの発見・tier 分類」だけ。ソースコードを LLM で読解するパスがない
2. **独立スキルにすると鮮度管理・graph・lint・query を全部再実装**することになる → wiki 統合一択
3. 足りないのは2点だけ: **(1) ソースコード読解パス** と **(2) 確認対話**
4. 蓄積先は `.wiki/` 一択。CLAUDE.md 追記は肥大化、メモリは非構造、`.agents/artifacts/domain/` は wiki の再発明

### wiki パイプラインへの統合設計

```
[既存] repo_ingest: clone → doc ファイル発見 → manifest 生成
        ↓
[新規] discover: ソースコードを LLM で読解 → 構造化 raw 記事を生成
        ↓ raw/files/{slug}/ に格納
[新規] confirm: 人間に「この理解で合ってる？」確認対話
        ↓
[既存] compile → graph_gen → lint → query
```

discover が `raw/` に構造化記事を書けば、あとは既存 compile が `concepts/` に wikilink 付きで昇華してくれる。人間の確認対話は discover→raw の境界に置く（architecture.md「Source層は immutable、人間がキュレーション」の思想とも整合）。

### discover が生成する raw 記事（リポジトリごと）

| 記事 | 抽出元 |
|---|---|
| `architecture.md` | ディレクトリ構成, フレームワーク検出, レイヤー構造 |
| `db-schema.md` | migration ファイル, ORM モデル定義 |
| `api-routes.md` | ルート定義, コントローラー |
| `business-rules.md` | バリデーション, ドメインロジック, 定数 |
| `state-machines.md` | enum, 状態遷移, ステータス管理 |
| `glossary.md` | ドメイン用語, 文脈別の意味の違い |

### マルチリポジトリ対応

```
/wiki-discover https://github.com/org/web-backend https://github.com/org/web-frontend
```

- 各リポジトリが `raw/files/{slug}/` に独立して格納される
- compile で横断的な相互リンクが生成される（例: backend の API エンドポイントと frontend の呼び出し側）
- 鮮度管理は `source_revision`（commit hash）で個別追跡
- 1リポジトリだけ更新された場合は差分 re-discover 可能
- 既存の cross-repo-wiki-gitlab-fetcher アイデアとの接続点あり

### mino-skills から盗む視点（散文プロンプトとして discover に埋める）

- **actor + purpose**: 同じ名詞でも context で意味が違うケースの発見
- **term ledger**: 用語集、多義語の文脈別定義
- **context boundary**: 意味・ルール・状態が変わる境界
- **invisible concepts**: 名詞ではなく判断・制約・失敗をモデル化

YAML schema 機構は使わない。出力は「5-6個の人間が読める raw 記事」であって formal completeness package ではない。

## Decisions

1. **独立スキル vs wiki 統合**: wiki 統合。鮮度管理・graph・lint・query を再実装しない
2. **蓄積先**: `.wiki/raw/files/{slug}/` → 既存 compile で `concepts/` へ昇華
3. **粒度**: 「参入初日に先輩に聞くこと」レベル。mino 的な12次元監査は不要
4. **確認対話**: discover → raw の境界に AskUserQuestion 型で配置
5. **mino からの取り込み**: 視点（4つ）のみ散文プロンプトとして。schema は捨てる
6. **トリガー**: `wiki discover` 独立サブコマンド（ingest とは役割が完全に異なるため）
7. **テストコード**: 全読する。テスト名＝仕様、境界条件・例外ケース・用語の主要ソース
8. **cross-repo-wiki-gitlab-fetcher**: 補完関係として統合 plan 化（discover = fetcher の実現手段）

## 新規実装が必要な箇所（2点のみ）

1. **ソース走査ロジック**: doc tier ではなく migrations/models/routes/validators を狙い撃ちする traversal。repo_ingest の doc-tiering は doc ファイル指向（max-docs 50 も doc 前提）なので、ソース走査は別ロジックが要る
2. **確認対話**: 生成した raw 記事の内容を人間に見せて「合ってる？」と聞くフロー

## 実装しないもの

- 鮮度管理 → `source_revision` pin が既にある
- graph / lint / query → 既存がそのまま使える
- YAML schema / 12次元監査 → mino 的な重さは不要
- CLAUDE.md への直接追記 → 肥大化するため `.wiki/` に集約

## Round 4（2026-07-16 壁打ち再開）

### ingest vs discover の差別化 → 独立サブコマンド確定

コード分析の結果、役割が完全に異なることを確認:

| | ingest | discover |
|---|---|---|
| **問い** | 「何がある？」 | 「何を意味する？」 |
| **読む対象** | ドキュメントファイル（.md, config） | ソースコード（models, routes, migrations, tests） |
| **処理** | パターンマッチ（LLM 不使用） | LLM による意味読解 |
| **出力** | manifest + inventory（ファイル目録） | ドメイン知識の raw 記事群 |

ingest はファイルの中身を一切読まない静的スキャナ。discover は LLM がソースコードを読んで知識化する。パイプライン: `ingest → discover → compile`（discover は ingest の clone 結果と manifest を再利用）

### テストコードは全部読む

テストこそ仕様の体現（Information Placement: Tests tell you What）:
- テスト名からビジネスルールを逆引き（`test('expired coupon cannot be applied twice')`）
- 境界条件・例外ケースの発見（プロダクションコードからは読み取りにくい「やってはいけないこと」）
- glossary の補強材料（describe/context で使われるドメイン用語）

### cross-repo-wiki-gitlab-fetcher との関係 → 補完関係

- **gitlab-fetcher** = 「5リポジトリを横断して wiki を作る」ユースケース（何を wiki 化するか）
- **discover** = 「ソースコードからドメイン知識を抽出する」機能（どう wiki 化するか）
- discover は gitlab-fetcher の実現手段の一部。gitlab-fetcher で clone した5リポジトリに discover を走らせれば横断ドメイン知識が自動で raw に蓄積される
- plan 化時に統合して進める（discover 実装 → gitlab-fetcher の「残り4割」が埋まる）

## Open Questions

_Round 4 で全て解決済み → plan 化へ_

## 参考資料

- inspired-mino-design-skills: `.agents/skills/mino-core/references/domain-discovery.md`（視点の出典）
- inspired-mino-design-skills: `.agents/skills/mino-domain-model-completeness/SKILL.md`（重い形式主義の反面教師）
- wiki-knowledge: `skills/wiki/scripts/repo_ingest.py`（既存の repo clone + doc discovery）
- wiki-knowledge: `skills/wiki/references/architecture.md`（3層構造 + source_revision pin）
