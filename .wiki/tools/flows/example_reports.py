# 固定フローコードの例（B1: TSV/CSV export）— PR レビュー必須の実行契約の真実源。
#
# フローは raw Playwright を触らず、runner が提供する capability API（ctx）にのみ書く。
# origin は常に catalog（named route）から構成され、パラメータはセレクタの値バインディングで
# のみ使う（文字列補間しない）。この例は AST ゲート（import/exec/eval/dunder 禁止・単一
# run(ctx, params)）を通り、catalog の flow.sha256 に pin される。
#
# 登録時はこのフローを http 還元ゲート（network log から export リクエストを捕捉して
# tool_connector_http で replay 試行）に先に通し、再現できたら http connector で登録して
# browser tool は作らない（保証水準が高い方を選ぶ）。


def run(ctx, params):
    # named route へ遷移（origin は catalog、period は検証済みパラメータ）
    ctx.goto("reports", period=params["period"])
    ctx.wait_stable("navigation_settled")

    # フィルタ表示を読み戻して filter_readback の証拠にする
    period_shown = ctx.read_text(ctx.get_by_label("Period"))
    ctx.record_readback("period", period_shown)

    # loading 収束を待ってから export（素の sleep は使わない）
    ctx.wait_stable("loading_indicator_gone")

    # role + accessible name の複合条件で export ボタンを確認してから download。
    # サーバー指定 filename は使わず runner がランダム名 + atomic rename で保存する
    export_button = ctx.get_by_role("button", name="Export CSV")
    ctx.download(export_button, role="button", name="Export CSV")
