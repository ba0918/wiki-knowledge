# 5リポジトリ横断 Q&A Wiki（社内 GitLab + repo fetcher）

**Created:** 2026-07-03 22:23:07
**Status:** 💡 Idea
**Tags:** `cross-repo`, `gitlab`, `fetcher`, `dogfooding`, `source-agnostic-pipeline`
**Mode:** Brainstorm (solo)
**Rounds:** 2（収束完了 — 2026-07-03 ユーザー回答により確定）

---

## Summary

仕事の5リポジトリ（frontend / backend-admin / backend-api / client / gameserver、社内サーバホスティングの GitLab）を横断して質問に答えられる wiki を作る。リポジトリ境界をまたぐ知識（例: client → backend-api → gameserver の通信フロー）は単一リポジトリの clone + 直接質問ではコンテキストに乗り切らないため、事前合成された wiki の価値が最大化する題材。Karpathy gist の Business/team ユースケースの実践であり、wiki-query 複利ループ（query → promote → querylog 蓄積）の初の実運用ドッグフーディングを兼ねる。

## Key Discussion Points

- **今すぐコード変更ゼロで6割できる**: 社内 GitLab でも clone できれば ingest はファイルパス入力で動く（WebFetch が社内 URL に届かない問題は clone 経由なら無関係）。残り4割 = 自動化・更新追従が実装ネタ
- **ingest 対象の深さ（論点1・最重要）**: 推奨は「docs + LLM 生成モジュールサマリー」。LLM がコードを読んで各リポジトリの責務・構造・主要フロー・他リポジトリとの接点を記事化する。コード自体は raw に入れない（Karpathy 理念: raw → 合成知識。肥大化・陳腐化を回避）
- **更新追従（論点2）**: 推奨は「まずスナップショット」。commit hash だけ記録しておき、差分 re-compile は価値実証後に実装。pipeline 計画の `sources[].content_hash` / `fetched_at` がこれ用の基礎工事（伏線回収）
- **置き場所（論点3）**: 5リポジトリ横断のため、どのリポジトリにも属さない独立 wiki 置き場が必要。社内コードの知識が入るため置き場所 = 機密境界の判断（社内 GitLab 専用リポジトリ vs ローカルのみ）→ **未確認**
- **進め方（論点4）**: 推奨は二段構え。(A) 軽量ルート先行: 手動 clone + batch ingest + compile + query をまず1リポジトリで試して価値と摩擦を確認 → (B) 摩擦が見えたら source-agnostic pipeline Phase 1 の Fetcher registry に repo fetcher（`@register_fetcher("repo")`、local clone 前提）を TDD 実装。gist の「まず使え、ツールは必要になってから」と一致
- **実務上の注意**: ingest のセキュリティスキャン（メールアドレス正規表現等）は社内コード/ドキュメントで誤検知しやすい。閾値・許可リストの調整が摩擦ポイントになる見込み

## Decisions（2026-07-03 確定）

1. **ingest 対象の深さ**: docs + LLM 生成モジュールサマリー。コード自体は raw に入れない
2. **更新追従**: スナップショット先行。commit hash を source_version として記録し、差分 re-compile は価値実証後
3. **wiki 置き場所**: 実装は置き場所非依存にする（wiki_root で吸収）。運用者の匙加減 — 当面は本人利用のローカル
4. **ルート**: repo fetcher を実装する。**clone まで自動化**が要件:
   - `ghq` がある環境では `ghq get` で一元管理（`~/ghq/<host>/<owner>/<repo>`）
   - なければ `git clone` にフォールバック（gh は GitHub 専用のため、社内 GitLab も通る素の git を基本にする）
   - clone 済みならスキップ or pull（スナップショット方針なので初期は fetch せず既存 checkout を使う判断もあり）

## Next Steps

1. plan 化（独立 plan or source-agnostic pipeline Phase 1 への織り込みかを判断）
2. MVP スコープ案: repo fetcher（ghq/git 自動 clone + commit hash 記録）→ ingest/compile 拡張（モジュールサマリー記事の生成手順を SKILL.md に定義）→ 公開リポジトリ or 本リポジトリで試行 → 仕事の5リポジトリに適用
3. 摩擦リスト（セキュリティスキャン誤検知・記事粒度・選定手間）を試行時に記録

## Related

- `docs/plans/20260408163658_source-agnostic-knowledge-pipeline.md` — Fetcher registry 設計（Phase 0 部分完了）。GitLab repo fetcher はこの設計の第2号 Fetcher 候補（第1号は Slack 予定だったが、順序入れ替えも検討余地あり）
- `.wiki/raw/articles/20260405-karpathy-llm-wiki-pattern.md` — 原典。Business/team ユースケースと「batch-ingest with less supervision」の記述
