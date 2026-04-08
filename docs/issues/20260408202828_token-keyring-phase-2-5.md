---
title: Token 管理の keyring 化 (Phase 2.5+)
status: open
created: 2026-04-08 20:28:28
tags: source-agnostic-pipeline,phase-2.5,scope-cut,security,token,keyring
source: docs/plans/20260408163658_source-agnostic-knowledge-pipeline.md
---

## 概要

Source-Agnostic Knowledge Pipeline の Phase 2 Fetcher (Slack) では、
Token 解決を多層フォールバックで提供する（Q12-3 対応）:

1. `--token-file <path>` (CLI 明示指定、最優先)
2. `$SLACK_USER_TOKEN` 環境変数
3. `.wiki/config/secrets.env`（プロジェクトローカル、`.gitignore` 対象）
4. `~/.config/wiki/secrets.env`（ユーザーグローバル）

OS の secure storage (keyring) を利用した Token 管理は Phase 2.5+ に送った。
チーム共有フェーズで複数ユーザーの Token 管理が必要になったタイミングで実装する。

## 備考

### スコープアウト理由
- Q12-2 A 案（implementation 最小主義）に準拠
- 個人ドッグフーディング段階では多層フォールバックで十分
- 環境変数のインタラクティブ/非インタラクティブ問題は多層フォールバックで解消済み
  （シェル profile に依存しない `.wiki/config/secrets.env` パスが確実に通る）

### 着手判断基準
- チーム共有フェーズで以下が発生したら着手検討:
  - 複数ユーザーが同一プロジェクトで異なる Token を使い分ける必要が発生
  - 平文ファイルでの Token 保管がセキュリティ要件を満たさなくなった
  - CI/CD からの自動実行時に keyring 経由の Token 注入が必要になった

### 関連設計
- 多層フォールバックは `wiki slack check-auth` 診断コマンドと併用:
  - Token 発見箇所の表示
  - `auth.test` 経由の有効性確認
  - 不足スコープの検出
- エラーメッセージは全探索箇所を列挙し、修正方法を明示する

### 関連ファイル
- plan 本体: `docs/plans/20260408163658_source-agnostic-knowledge-pipeline.md`
- Phase 2 Fetcher セクション参照

---

> **Note:** Do not include sensitive information (passwords, tokens, personal data, etc.) in this file.
