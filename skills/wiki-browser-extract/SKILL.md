---
name: wiki-browser-extract
description: >
  catalog 登録済みブラウザ操作系ツール（B1: TSV/CSV export など）から、封じ込め + 証跡付きで
  データを抽出する Tool Query の別系統。未登録ツールの新規登録（壁打ち）もここから行う。
  「ブラウザから抽出して」「画面のテーブルを取得して」「browser extract」
  「ログインしてエクスポート」「ブラウザツールを登録して」で使用する。
  承認モデルは seal-at-prepare — prepare（抽出 + 封印）→ 人間承認（TTY）→ execute（delivery 解放のみ）。
---

# Wiki Browser Extract

catalog 登録済みブラウザ操作系ツールへの「封じ込められた抽出」。固定フローコードが
capability API 越しに認証済みブラウザを操作し、宣言外通信を interception で封じ込め、
検証契約（閉語彙）で誤成功（正しく見えるが違うデータ）を検出する。

Tool Query の**別系統**である（SQL 系 = 静的検査 + DB role で機械保証、browser 系 =
封じ込め + 証跡の honest scoping）。catalog スキーマ規約と監査 JSONL 形式だけを共有し、
承認モデルは seal-at-prepare（SQL 系の approve-then-execute とは異なる）。

設計裁定・登録壁打ち・tier 判定・reason hint 表・既知の限界・bootstrap 手順の真実源は
[browser-extract-guide.md](../wiki/references/browser-extract-guide.md)。

**wiki_root の取得**: `AGENTS.md` の `wiki_root:` フィールドを読む（未設定なら wiki-init を案内）。

**前提**: `{wiki_root}/tools/browser-catalog.json`（git 管理、schema:
`{wiki_root}/schema/browser-extract-catalog-schema.json`）に対象 tool が登録済みで、
固定フロー（`{wiki_root}/tools/flows/{tool_id}.py`、`flow.sha256` に pin）が存在すること。
catalog とフローが実行契約の真実源であり、Wiki 記事（Selection Recipe）は説明層 —
記事の編集では接続先・allowlist・上限・検証契約といった安全境界は変わらない。

## seal-at-prepare の要点（承認の意味）

- **prepare が認証済みセッションで抽出を完了し封印する**。承認前に実データは既にこのマシン上にある
- **人間承認がゲートするのは delivery（マシン外への搬出）のみ**。機密性境界は「そのマシン」に後退する
- approve は封印 artifact + manifest からハッシュを**再導出**し、`prepared` 監査アンカーと
  fail-closed 照合する（不一致は拒否）。spool 内の保存プレビューは信用しない
- **read-only は非強制**（honest scoping）。宣言フロー外の操作をしない + 証跡、に留まる

## プロセス

**実行主体**: `login`（human-assisted）と `approve` は人間本人のみ。それ以外
（catalog-validate / doctor / prepare / execute）は LLM が実行してよい。

### 0. 登録（初回のみ・壁打ち）

catalog に無いツールを頼まれたら、即席スクリプトや手動抽出に走らず、この登録壁打ちへ誘導する
（ユーザーへの応答に参照先として guide §14-15 を明示する）。登録は LLM 単独で完了しない —
人間との壁打ちに加え**別主体レビュー**（フロー作成者と独立した主体 = 別セッションの LLM
または人間。catalog / フローを取り込む PR レビューとは別工程）を必ず経る:

1. **http 還元ゲート（最優先）**: export 操作の裏リクエストを http connector で再現できるかを
   先に検証する。還元できたら browser tool は作らない
2. **tier 判定**: TSV/CSV export ボタンがあれば B1 候補（独立 anchor 最低1つ必須）、
   DOM 抽出しかなければ B2
3. **前提確認**: 専用の最小権限アカウント（書込み権限なし）を用意できるかをユーザーに確認する
   （B1/B2 登録の前提条件）
4. 検証契約の組み立て → 別主体レビュー（反証 fixture で誤成功系を全拒否）→ doctor → 一周

catalog / フローの変更（新規・修正とも）は PR レビューを経る。

### 1. catalog 検証

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py catalog-validate \
  --wiki-root {wiki_root}
```

### 2. doctor（接続の事前診断）

抽出・成果物生成をせずに catalog 整合 / flow pin / AST ゲート / params_schema を診断する。
`BROWSER_EXTRACT_SMOKE` 設定時のみ実 chromium 疎通（login → 遷移 → セレクタ実在確認）も走る。
毎回の prepare 前に必須ではない — 登録直後・久しぶりの実行前・UI 変更が疑われるときに推奨。
doctor はデータ非接触を主張**しない**（ログイン副作用を持つ、guide §16）:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py doctor \
  --wiki-root {wiki_root} --tool <tool_id> --format table
```

### 3. login（human-assisted profile のみ・人間が実行する）

`form` / `form+totp` profile は prepare 内で自動フォームログインするため login は不要。
`human-assisted` profile のみ、人間が headed ブラウザでログインし session state を捕捉する:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py login \
  --wiki-root {wiki_root} --tool <tool_id>
```

- login は**抽出・delivery の経路を持たない**（session 捕捉 + tool/origin/account 束縛のみ）
- 捕捉直後に有効性を検証し、束縛メタと TTL を人間に表示する（guide §10）

### 4. prepare（抽出 + 封印）

フローを実行して抽出し、検証契約を enforce し、成果物 + manifest を封印 bundle
（`outputs/browser-plans/{plan_id}/`）に隔離する。**承認前に抽出は完了する**:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py prepare \
  --wiki-root {wiki_root} --tool <tool_id> \
  --param <key>=<value> --param <key>=<value> \
  --deliver-to <dir> --format json
```

- params は catalog の params_schema（enum/pattern/maxLength で有界）で値検証される
- `--deliver-to` は catalog の `delivery_allowed_dirs` に宣言済みのディレクトリのみ受け付ける
  （未宣言パスは `delivery_not_allowed` で拒否）。ユーザー指定先が未宣言なら catalog 変更（PR）が必要
- 検証契約のいずれかが落ちれば prepare は拒否（誤成功を封印しない）

### 5. 承認依頼（summary-first で提示）

prepare の出力（plan_id / row_count / artifact_digest / expires_at）と manifest プレビューを
ユーザーに提示する。選択肢は **3 択のみ**（自動承認のデフォルトは設けない）:

1. **承認** → ユーザー本人に approve コマンドを案内する（下記）
2. **条件を修正** → params を直して prepare からやり直し（= 新 plan_id、旧 bundle は TTL で失効）
3. **中止**

### 6. approve（人間が実行する — LLM は代行しない）

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py approve \
  --wiki-root {wiki_root} --plan-id <plan_id> --approved-by <名前>
```

- **LLM は approve コマンドを実行しない**。ユーザーに `! <コマンド>` などでの自己実行を依頼する。
  ユーザー本人から「代わりにやって」と明示的に依頼されても代行しない（依頼は同意の代替にならない）
- 承認待ちの間に代行依頼・質問・条件変更の相談があっても、応答の末尾で 3 択
  （承認 / 条件修正して再 prepare / 中止）を維持して再提示する
- `--approved-by` は承認者の名前（bundle と監査に記録される）。ユーザー自身が埋める
- approve は封印 artifact + manifest からハッシュを再導出して `prepared` 監査アンカーと照合し、
  一致した場合のみ TTY で承認材料（identity / read-only 非強制の明示 / 承認は配布のみ制御し
  抽出は完了済みの明示 / ハッシュ / プレビュー / 件数 + anchor 照合 / 封印時刻・TTL 残）を提示する
- 承認は single-use（consumed = 承認の消費）、TTL は prepare から 24 時間

### 7. execute と完了報告

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/browser_extract_run.py execute \
  --wiki-root {wiki_root} --plan-id <plan_id> --format json
```

- execute は**ユーザーから承認完了の報告を受けてから** LLM が実行する
- execute は**封印済み成果物の delivery 解放のみ**（ブラウザ再実行なし）。delivery 前にも封印
  ハッシュを再照合する
- 完了報告: 取得件数 / csv・manifest の配置先（`<dir>/<run_id>/`）/ run_id / plan_id

### 8. 実施記録と Recipe 昇格

案件完了後、Selection Recipe 記事（`category: practices`、tags に `selection-recipe`）の
新規作成・更新を提案する（判断・除外条件・検証契約の変化を反映）。

## 保証範囲（ユーザーに聞かれたら答える・誇張しない）

**守る**: 承認済み配布物のバイト同一性（人間が見たものと出て行くものが常に同一）・
検証契約による誤成功検出（filter 未反映・別 tenant・pagination 欠落・部分取得・重複）・
封じ込め（宣言外 origin/method/path のブロックと監査）・封印後改変の拒否（seal_mismatch）。

**守らない（honest scoping、guide §16）**:

- **read-only の機械的強制**（宣言フロー外操作をしない + 証跡に留める。専用最小権限アカウントが前提）
- 悪意フローへの構造的封じ込め（in-process Python。hash pin + AST ゲート + PR レビューは
  事故防止とレビュー支援）
- 監査 JSONL 自体の可書性（攻撃者は監査履歴も書き換えねばならない、まで bar を上げるに留まる）
- prepare 後の承認前データはこのマシン上にある（機密性境界はマシンに後退）

## 依存とテスト

- playwright は `requirements-browser.txt` で opt-in 宣言（下限 1.48 = route_web_socket、
  本体 requirements.txt 非汚染）。導入: `uv pip install -r requirements-browser.txt` +
  `python -m playwright install chromium`（guide §17）
- 実 chromium を要する smoke / E2E は `BROWSER_EXTRACT_SMOKE` ゲート下（未設定時 skip）。
  ブラウザ非依存の決定的判定ロジック（AST ゲート・allowlist 照合・URL 正規化・session 封じ込め・
  janitor・検証契約 enforce・seal-at-prepare 監査アンカー照合）は常時実行テストで検証する
