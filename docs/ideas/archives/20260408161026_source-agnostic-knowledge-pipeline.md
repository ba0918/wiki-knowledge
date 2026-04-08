# Source-Agnostic Knowledge Pipeline（Slack 起点の汎用ナレッジ昇華基盤）

**Created:** 2026-04-08 16:10:26
**Status:** 💡 Idea
**Tags:** `ingest`, `slack`, `architecture`, `knowledge-graph`, `schema-design`, `conflict-resolution`

---

## Summary

Slack 検索性の破綻により埋もれている 20 年分の業務ドメイン知識（意思決定・サポート対応）を、LLM で引ける検索可能な Wiki に変換する。ただし実装は Slack にロックインせず、**「任意の一次情報を構造化知識へ昇華する汎用パイプライン」**として設計する。Slack は最初の Fetcher 実装にすぎない。

当初「Slack ingest 機能の追加」としてスタートしたが、壁打ち中に本質が **「ソース非依存の知識昇華パイプライン」** であることが判明し、スコープが根本から再定義された。個人ドッグフーディングで価値実証 → チーム資産化という段階導入を想定。

## Key Discussion Points

### 背景とモチベーション
- 20 年続く業務プロジェクトの知識が Slack に埋もれており検索性が終わっている
- 意思決定の経緯・サポート対応パターンを LLM 経由で引ける状態にできれば「人が少ない今」のナレッジ継承に効く
- 情報性質は業務情報（プライベート ch 含む）。ただし共有先は個人 or チーム内限定で外部公開なし
- ユーザーの職場はコンプラ意識が極端に低く、罰則リスクは実質ゼロという前提
- 一方で、**退職時の営業秘密持ち出しリスク（不正競争防止法）** は別レイヤの問題として残る → チーム資産化方針により緩和される
- 最終ゴールは**チーム共有のナレッジ基盤**だが、まず個人ドッグフーディングで価値実証する

### コアの発想転換：「ingest のソース種別は本質じゃない。内容の質だけが本質」
- Slack / Teams / メール / 会議録 / GitHub Issue / ヒアリングメモは全て「対話形式の一次情報」という共通構造を持つ
- 既存 `source_kind: article | file` に加えて `conversation` を第 3 の種別として追加
- Slack 固有の処理は Fetcher adapter に閉じ込め、共通スキーマに normalize する

### 論点 B：知識の競合解決ルール（最重要）
- 20 年分では「2015 年はこう → 2020 年に変更 → 2023 年に差し戻し」のような時系列矛盾が必ず起きる
- LLM に「記事 vs 記事の丸読み比較」で矛盾検出させるのは不安定
- **claim 抽象**（subject / attribute / value / valid_from / valid_to / source / attributed_to / status）に分解して、同一 subject × 同一 attribute × 期間重複のみ衝突候補とする
- 格納戦略は **「現行記事 + 経緯サブセクション」ハイブリッド**（Codex 推奨）
  - 現行は 1 つ、過去主張は消さず時系列で下にぶら下げる
  - query デフォルトは `status=current` のみ返し、「経緯」「なぜ変わった」等の要求時だけ historical も含める
  - 回答では「現行」「過去」を必ず明示分離
- 時効管理は **事後クローズ型**：事前に `valid_until` を埋めない。後続証拠が出たら閉じる
- 5 年経過で `要検証` タグ自動付与は、運用手順・組織名など劣化しやすい知識に限定
- **「自動で警報、手動で裁定」**：LLM は候補抽出と説明生成に使えるが最終判定器にはしない

### 【決定的論点】基礎工事とMVPの分離（ユーザー鋭指摘 → Codex 確認）
- **「段階的実装」と称して後回しにしていいのは「UI・推論・検索の高度化」だけ**
- **「再解釈不能な保存形式」は後から直せない**。数百記事たまった後に全記事 LLM 再編集は不可能
- 後戻りできないライン = 最初から固定すべきフィールド：
  - `schema_version`（migration の唯一の道）
  - `article_id`（不変 ID）
  - `sources[]`（構造化された出典）
  - `captured_at`（取り込み時刻）
  - `knowledge_time.valid_from / valid_to`（時間軸）
  - `claims: []`（空でいい、しかし必ず存在）
  - `extensions: {}`（名前空間付き拡張点）
- 後から足して OK：confidence / tags / embeddings / relations / summary / 検索最適化メタ
- migration 不要化 4 原則：
  1. 保存層と解釈層を分離（記事は生データ、検索 index/グラフは再生成可能な派生物）
  2. `schema_version` を最初から持つ
  3. `extensions` / namespaced field で拡張点予約
  4. compile 済み記事を immutable に近く扱い、読む側を賢くする

### 論点 D：記事型設計
- 初期 **4 種**：`decision` / `runbook` / `reference` / `concept`（拡張で `incident` 追加）
  - 型が増えるコストは検索性より誤分類コストが先に効く
- **共通部分**：frontmatter 基礎工事 + 本文の `Summary / Current Understanding / Evidence Notes / Open Questions`
- **型固有部分**：
  - `decision`: `Options Considered / Decision / Rationale / Consequences / Sunset or Revisit Conditions` + `status / decided_at / owners / alternatives / supersedes / sunset_conditions`（sunset_conditions は**観測可能条件**に寄せる）
  - `runbook`: `Symptoms / Preconditions or Triage Checks / Actions / Validation / Escalation` + `severity_scope / entry_conditions / stop_conditions / escalation_to / estimated_time_to_try`（**症状と判定条件を分ける**）
  - `reference`: `Specification / Usage Examples / Related Specs`
  - `concept`: 既存構造
- 型判定軸は**読者タスク × 失効しやすさ × 更新責務**の 3 軸
- **1 ソース → 複数記事分割は前提**。Slack 障害スレッド 1 本 → `incident` + `runbook` + `decision` の 3 本に分かれることを compile パイプラインで扱う
  - `source thread → extracted facts/events/actions/decisions → article candidates[] → per-type rendering` の段階化
  - 分割後も全記事が同じ source provenance を共有
- **型間リンクは wikilink + typed relations**（frontmatter に `supersedes / superseded_by / caused_by / derived_from / implements / depends_on / related_to`）
- **型変更は migration 不要で可能**：`article_type` は可変、`article_id` は不変。型は「view + required fields set」であって保存形式ではない

### 論点 A：抽象レイヤの責務境界
- 5 層構成：
  ```
  Orchestrator (薄い、実行制御/retry/partial failure)
    ├ Fetcher → [Differ service] → Normalizer
    ├ Compiler (LLM 必須)
    └ Validator (rule-based 第一段 + LLM 第二段)
              ↕ Provenance（全層横断の共通データ契約、独立層にしない）
  ```
- **Differ は独立層ではなく Fetcher + Normalizer 間の service**
- **Provenance は各層に埋め込む共通契約**。独立層化すると責務が曖昧になる
- 入出力型は厳密化：
  - `RawSource`: `source_id / source_kind / fetch_cursor / fetched_at / raw_payload / content_hash / provenance`
  - `NormalizedSource`: `source_id / source_version / source_kind / title / participants[] / segments[] / attachments[] / timestamps / provenance / normalization_warnings[]`
  - `Segment`（conversation 型）: `segment_id / speaker / ts / text / reactions / reply_to`
  - **発言者・時系列を本文文字列に潰さない**（後段の型分類精度が落ちる）
  - `Article`: frontmatter + rendered_body + `claim_refs[]`（差分 merge の整合性に必須）
- 依存方向：`Domain(pure) → Service(I/O) → Handler(CLI)`
  - Normalizer 整形規則 / Compiler article planning / Validator 整合性判定 → pure に寄せる
  - Fetcher / Wiki 読み書き → Service
- **Plugin は継承より registry**：`@register_fetcher("slack_thread")` デコレータで callable 登録
- **増分更新**：`source_id` 不変、`source_version` 増分
  - full recompile ではなく `normalized diff → 影響 claim だけ再抽出 → 関係 article を selective recompile`
  - 差分 merge の単位は source ではなく claim / article candidate
- **LLM 依存**：Compiler 必須、Validator 任意（rule-based 第一段 + LLM 第二段）
  - interface 注入でテスト時 deterministic mock 化
- **エラー境界**：
  - Fetcher 失敗 → source 単位 retry
  - Normalizer 失敗 → quarantine
  - Compiler 失敗 → article candidate 単位で skip、raw claims 保持
  - Validator 矛盾検出 → publish せず `status=draft|conflicted`
  - 停止すべき：source 消失 / provenance 欠落 / article_id 衝突
  - 停止すべきでない：LLM 一時失敗 / ネットワーク一時エラー

### 既存スクリプトの再編方針
- `skills/wiki/scripts/lib/` を `domain/` と `service/` に再編
- `graph_gen.py / lint-wiki.py / trust_score.py / gap_detect.py` は handler 薄化して lib を呼ぶ形に
- 既存コードの pure check 構造（`lib/inventory.py` 等）は Codex も「方向として正しい」と評価

## Decisions & Conclusions

### スコープ定義
- **プロジェクト位置付けを更新**：Slack ingest 機能 → **「ソース非依存の知識昇華パイプライン」**
- システムは個人用途とチーム資産化の両方をサポート。**Slack にロックインしない**
- Slack は Fetcher の最初の一実装にすぎない
- 初期は個人ドッグフーディング、価値実証後にチーム化

### 記事型
- 初期 4 種：`decision` / `runbook` / `reference` / `concept`
- 拡張で `incident` 追加
- 型変更は migration 不要（`article_type` は view、`article_id` は不変）

### article_id フォーマット
- `YYYYMMDDHHMMSS-{slug}`（例: `20260408161026-customer-a-ops`）
- 同秒衝突時のみ `-2`, `-3` サフィックスで回避
- ファイル名で時系列が分かる可読性と、採番ロジックの簡素さを両立

### MVP フロントマター（基礎工事、後から migration 不要）
```yaml
schema_version: 1
article_id: 20260408161026-customer-a-ops  # 不変
article_type: runbook                       # 可変（view）
title: 顧客A 運用フロー

captured_at: 2026-04-08
knowledge_time:
  valid_from: 2023-04-01
  valid_to: null
status: current  # current | historical | disputed | unverified

sources:
  - id: src-001
    type: slack_thread
    ref: raw/slack/20230401-thread.md
    permalink: https://...  # optional

relations:
  supersedes: []
  superseded_by: null
  caused_by: []
  derived_from: []
  implements: []
  depends_on: []
  related_to: []

claims: []        # 空でいい、将来埋める
extensions: {}    # 名前空間付き拡張点

# 型固有フィールドは型別に続く
tags: []
```

### 本文骨格（共通部分）
```markdown
## Summary
## Current Understanding
## History / Changes
## Evidence Notes
## Open Questions
```
型固有セクションは Current Understanding の後に挿入。

### 抽象レイヤ
- 5 層構成（Orchestrator / Fetcher / Normalizer / Compiler / Validator）
- Differ は Fetcher + Normalizer 間の service
- Provenance は独立層にせず全層横断の共通契約
- Plugin は registry パターン（継承より callable 登録）
- LLM client は interface 注入
- Segment 粒度：**Slack 1 メッセージ = 1 segment**（単純、実装楽）

### 既存スクリプトの再編タイミング
- **並行アプローチ**：新機能（Slack Fetcher 等）は新構造 `lib/domain/ + lib/service/` で実装、既存スクリプトは徐々に移行
- 先に再編は既存機能破壊リスク、後回しは二重実装になるため

### 設計原則（忘れてはならない）
- **「自動で警報、手動で裁定」**（Codex）
- **LLM は候補抽出と説明生成に使え、最終判定器にするな**
- **会話は segments[] のまま後段に渡せ（文字列に潰すな）**
- **元データを壊さず、後から読む側を賢くする**
- **`article_type` は view、`article_id` は不変**
- **claim を完全運用する必要はないが、claim を保存できない設計は避けろ**

## Open Questions

### 実装レベル
- `lib/domain/ + lib/service/` への既存コード再編の具体的な順序とマイルストーン
- Slack User Token の保管方法（`.env` ではなく OS keyring / 1Password 連携推奨 by Codex）
- Slack App の必要スコープ具体化（`channels:history / groups:history / users:read` の最小構成）
- Fetcher registry の実装詳細（デコレータ vs 設定ファイル宣言）
- Compiler の article intent classifier プロンプト設計
- Validator 第一段 rule-based の実装（subject × attribute × 期間マッチのロジック）

### 運用レベル
- 名寄せ問題（製品名変遷、人名表記揺れ）の扱い：glossary ファイル手動管理 or LLM 自動検出
- 個人ドッグフーディング期の評価メトリクス（論点 C、未議論）
  - 一次情報到達率
  - 検索時間短縮
  - 重複質問削減
- 評価用の質問セット（20-30 個）をどう集めるか
- 週 1 回の「裁定セッション」運用（警報リストから矛盾を人間判断で解決）

### チーム移行
- Slack Token を個人 xoxp → チーム Bot xoxb に切り替えるタイミング
- 権限を踏まえた visibility フィールドの設計（チーム化時に必須）
- 個人→チーム移行時のデータマイグレーションと権限設計

### 将来拡張
- DM / Canvas / Huddle の扱い（Codex は後回し推奨）
- 添付ファイル（画像・PDF）の `raw/files/` 連携
- 定期実行での新着スレッド自動 ingest
- 他 Fetcher 追加（Teams / GitHub Issues / Email / 会議録）

## Next Steps

1. **この idea を plan に変換** (`claude-skills:brainstorm-plan`)
2. 論点 C（ドッグフーディング評価メトリクス）は plan 作成後に別セッションで掘る
3. plan では以下を段階分けすべき：
   - **Phase 0（基礎工事）**: schema_version / article_id / sources[] / knowledge_time / claims[] / extensions{} を含む新フロントマター schema 確定、本文骨格確定、既存記事 13 本の migration
   - **Phase 1（抽象レイヤ骨格）**: `lib/domain/ + lib/service/` 再編、registry パターンの Fetcher 基盤、NormalizedSource / Article 型定義、LLM client interface
   - **Phase 2（Slack Fetcher）**: Slack App 作成、User Token 管理、slack_thread Fetcher 実装、permalink URL パーサ、`slack://` スキーム対応
   - **Phase 3（Compiler 強化）**: 4 記事型のレンダリングプロンプト、article intent classifier、1 ソース複数記事分割
   - **Phase 4（Validator）**: 矛盾検出 rule-based 第一段、claim 抽出（任意 LLM）、時効管理
   - **Phase 5（ドッグフーディング）**: 評価メトリクス計測、質問セット整備、運用改善
4. 各 Phase で「後から migration 不要」原則を必ず検証する
