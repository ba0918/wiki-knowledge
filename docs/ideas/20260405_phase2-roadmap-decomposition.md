# Phase 2+ ロードマップ分解 & 優先度評価

**Created:** 2026-04-05
**Status:** 🎯 Plan Ready
**Tags:** `phase2`, `querylog`, `trust-score`, `gap-detection`

---

## Summary

Phase 2+ の機能群を分解し、価値/コスト比で優先度を再評価した。
ロードマップの番号順ではなく、依存関係と即効性で並び替え。

## 分解結果

| ID | 機能 | 価値 | コスト | 優先度 |
|----|------|------|--------|--------|
| 2a | QueryLog 蓄積 | 高（後続の基盤） | 低 | **P0 — 最優先** |
| 3a | Trust Score | 中 | 低 | **P1** |
| 2b | Gap Detection | 高（成長エンジン） | 中 | **P2**（QueryLog 蓄積後） |
| 2c | Auto Ingest 提案 | 中 | 低 | P2 とセット |
| 3b | Lint 強化 | 中 | 低〜中 | 既存 Lint の延長 |
| 4a | Multi-Resolution | 面白いが早い | 高 | 保留 |
| 4b | Intent Detection | 面白いが早い | 高 | 保留 |
| 5a | Portal Adapter | ニッチ | 高 | 保留 |
| 5b | Self-Healing Adapter | ニッチ | 高 | 保留 |

## 実装順序

```
QueryLog (2a) → Trust Score (3a) → Gap Detection + Auto Ingest (2b+2c)
```

それぞれ独立した小さいプランとして実装する。

## 設計判断

- **QueryLog 形式**: `outputs/querylog.jsonl` — 集計しやすさ優先
- **Trust Score 計算要素**: ソース数 + 鮮度 + 参照頻度(QueryLog) + backlink数
- **Gap Detection タイミング**: lint の一部として定期実行
- **Phase 4/5**: 当面ロードマップから外し、必要時に再検討

## Next Steps

- `/plan-create` で QueryLog 蓄積（2a）の実装計画を作成
- 完了後に Trust Score（3a）へ進む
