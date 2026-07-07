# SKILL.md 決定論ロジックのスクリプト抽出 — LLM/スクリプト責務境界の引き直し

**Cycle ID:** 20260707204744
**Type:** Refactor + Hardening（Pitch 3）
**Created:** 2026-07-07 20:47:44
**Status:** 🟢 Complete（2026-07-07 全 Work Item 完了、592 tests pass）
**Related:** docs/plans/20260707194819_schema-regime-decision.md（lib/ 現役層の使用を裁定）/ docs/plans/20260707200608_query-derived-layer-consumer.md（Pitch 2）

## Overview

SKILL.md には LLM が散文を読んで毎回手実行する決定論ロジックが 3 箇所埋まっていた:

1. **ingest セキュリティチェック** — 7 本の正規表現（機密データ 4 + プロンプトインジェクション 3）+ ファイル名検証を LLM が目視でパターン照合
2. **QueryLog JSONL 手組み** — id 生成・`sources_cited` の wikilink 正規表現抽出・9 フィールドの JSON 組み立てを LLM が手作業で実施（schema 検証なし・排他制御なし）
3. **log.md 定型追記** — `## [YYYY-MM-DD] {op} | ...` テンプレート 5 種を LLM が都度整形（実ログには「(new, 1 source)」等のフォーマットドリフトが既に発生していた）

いずれも入力→出力が完全に決定論的で、LLM に残す判断が一つもない。目視 regex 照合は見落としリスク（セキュリティチェックの実効性低下）、JSON 手組みは schema 逸脱リスクを常に抱える。design-principles の「機械検証可能な正しさ」に従い、TDD でスクリプトに抽出した。

## 成果物

| スクリプト | 責務 | exit code |
|-----------|------|-----------|
| `skills/wiki/scripts/security_scan.py` | パス traversal / 機密データ / プロンプトインジェクションの 3 チェック。✅/❌ サマリーは SKILL.md 指定形式をそのまま出力 | 0=クリーン / 1=検出（中断） / 2=引数エラー |
| `skills/wiki/scripts/querylog_append.py` | QueryLog エントリの組み立て（id 生成・cited 抽出・`gap_noted` 導出・concepts フィルタ）→ schema 準拠検証 → flock 付き JSONL 追記 | 0=成功 / 1=検証エラー（追記しない） / 2=引数エラー |
| `skills/wiki/scripts/log_append.py` | log.md 定型エントリ 5 種（ingest/compile/promote/query/lint）の整形と追記。単複（1 source / 2 sources）はスクリプトが処理。`--note` で自由記述を付加可 | 0=成功 / 1=log.md 不在 / 2=引数エラー |

テスト: `test_security_scan.py` / `test_querylog_append.py` / `test_log_append.py`（計 79 テスト、TDD で作成）。

## 責務境界（引き直し後）

- **LLM に残る判断**: 何を ingest するか、質問要約・タイトル・gap topic の言語化、consulted/cited の元になる記事の読解、検出時の対処案の提示
- **スクリプトに移った実行**: パターン照合、id/タイムスタンプ生成、JSON 組み立てと検証、フォーマット整形、追記 I/O

## 設計メモ

- **pure core + thin CLI**: `scan_text` / `check_filename` / `extract_cited` / `build_entry` / `validate_entry` / `format_entry` は全て純関数。I/O は `append_jsonl` / `append_line` に隔離（query_retrieve.py / graph_gen.py の前例に従う）
- **lib/ 現役層の消費**: `querylog_append.py` は `lib.service.clock.SystemClock` を使用（schema 裁定の「今後の決定論スクリプトもここを使う」に従う）。`--now` で再現可能に上書き可
- **schema 同期の機械検証**: `REQUIRED_FIELDS` と `.wiki/schema/querylog-schema.json` の `required` の一致をテストが検証 — スキーマ改訂時にテストが割れて教えてくれる
- **filelock 非依存**: SKILL.md のスクリプトは system python3 で実行される（venv は開発用）ため、`lib/service/file_lock.py`（third-party `filelock` 必須）ではなく stdlib `fcntl.flock` を使用。非 POSIX ではロックなしにフォールバック
- **正規表現は SKILL.md の旧記載と同一**: 抽出であって仕様変更ではない。パターン改善（例: api_key の大文字小文字）は将来の別サイクル

## Acceptance Criteria

- [x] 3 スクリプト + 79 テストが TDD（RED→GREEN）で作成され、全 pass
- [x] 既存スイート回帰なし: 513 → 592 passed / 1 xfailed
- [x] system python3（本番実行経路）でのスモークテスト 3 本成功
- [x] SKILL.md の該当 9 箇所（セキュリティ節 / QueryLog 節 / log 追記 5 箇所ほか）がスクリプト呼び出しに置換され、regex・JSON フィールド定義の散文重複が消えた
- [x] CLAUDE.md に Security Scan / Operation Log 節を追加、QueryLog 節に追記スクリプトを記載

## Non-Goals

- 検出パターン自体の強化（Slack トークン・GitHub PAT 等の追加）— 別サイクル
- init のディレクトリ作成・テンプレート配置のスクリプト化 — 頻度が低く、テンプレート埋め（SCOPE_DESCRIPTION 等）に LLM 判断が混ざるため見送り
- compile の「未コンパイル検出」のスクリプト化 — source_refs 走査は inventory 層との統合設計が必要（issue 候補）
