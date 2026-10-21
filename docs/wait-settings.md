# 待機設定と0秒の意味

単位はすべて秒です。CLIの指定はレシピの `[delete]` より優先します。省略時はレシピ値、
レシピにもなければ下記の既定値を使います。**明示した0は省略扱いにしません。**
既存の設定ファイルや実行中のバッチは書き換えません。

| TOML / CLI | 既定値 | 意味と0の効果 |
| --- | --- | --- |
| `interval` / `--interval` | 3 | 項目間の固定間隔。0で固定分なし |
| `interval_jitter` / `--interval-jitter` | 0 | 任意のランダム追加分。0〜指定値から毎回抽選し固定間隔に加算。0で追加なし |
| `batch_pause` / `--batch-pause` | 120 | 新規成功`batch_size`件ごとの定期休止。0で休止なし。`batch_size=0`でも無効 |
| `rate_limit_interval` / `--rate-limit-interval` | 5 | 制限検知後の項目間隔の最低値。0でこの最低値なし |
| `retry_delay` / `--retry-delay` | 60 | 安全に再試行できる失敗の初回バックオフ。以降2倍。0でローカル分なし |
| `cooldown_delay` / `--cooldown-delay` | 120 | サーバー期限がない制限の代替クールダウン。以降2倍。0で期限なしの自動再開を停止 |
| `retry_max_delay` / `--retry-max-delay` | 900 | 上記バックオフ・クールダウンのローカル上限。0で両方のローカル分なし。サーバー期限は短縮しない |
| `retry_max_wait` / `--retry-max-wait` | 1800 | 1回の必要待機を受け入れる上限。**待機時間そのものではない**。0なら必要待機があれば期限を保存して停止 |

通常の項目間隔は `interval + uniform(0, interval_jitter)` です。制限検知後はこれと
`rate_limit_interval` の大きい方です。例：固定3秒＋ランダム0〜2秒なら3〜5秒、
固定0秒＋ランダム0〜2秒なら0〜2秒です。既定のランダム追加は0で、既存動作は変わりません。

ローカルのエラー待機が0で、サーバーからも有効な待機指定がなければ、自動再送せず停止します。
0秒での429再試行ループは作りません。再試行する場合も`max_retries`の上限を守ります。
`max_cooldowns=0`は既存仕様通り、サーバー期限があっても自動再開自体を無効にします。

## サーバー指定・画面同期は別

`Retry-After`（秒・HTTP日付）、受理可能な`x-rate-limit-reset`、残り枠が少ない場合の
reset期限による先回り待機は0秒指定でも守ります。`wait_limit_mode="per-wait"`ではサーバー期限を
ローカルの代替クールダウンで延ばしません。旧`total`方式では既存のローカル下限も維持します。
ジャーナルに保存された期限は中断・再開をまたいで保持し、由来にかかわらず設定変更では短縮しません。
必要な待機が`retry_max_wait`を超えた場合は、期限を残して停止します。

ページ遷移（最大30秒）、投稿・メニュー・確認ボタンの表示、送信結果確認（最大10秒）、
ログイン判定（未確認時は2秒間隔）などの同期は残ります。必要な状態になれば先に進みます。
これらを0にすると操作成立や結果判定を壊すため、ペーシング設定では無効にしません。
アカウント・対象確認、送信結果不明の保留、成功済みのスキップも維持します。

## クライアントの意図的な休止を外して試す

既存の`[delete]`に次を指定できます。既定ファイルを変更せず、コピーした試験用レシピで試すと戻しやすくなります。

```toml
[delete]
interval = 0
interval_jitter = 0 # ランダム待機を使うなら例：2（0〜2秒）
batch_pause = 0
rate_limit_interval = 0
retry_delay = 0
cooldown_delay = 0
wait_limit_mode = "per-wait"
retry_max_wait = 1800 # サーバー待機の許容上限。待機を守るため0にしない
```

既存レシピを変更せずCLIで上書きすることもできます。以下は利用者が確認済みリストに対して実行する例です。
`--dry-run`を付ければリスト確認だけで、ブラウザも削除も実行しません。外すと`yes`の確認後に実際に再開します。

```bash
uv run kusamushiri-cli resume REVIEWED.csv --profile ACCOUNT --expected-viewer X_USERNAME --recipe cleanup.toml --interval 0 --interval-jitter 0 --batch-pause 0 --rate-limit-interval 0 --retry-delay 0 --cooldown-delay 0 --wait-limit-mode per-wait --headless --failed-output remaining.json --dry-run
```

これは速度・安定性の改善を保証する設定ではありません。間隔を短くすると、制限への到達や
期限なしエラーでの停止が増える可能性があります。実サイトでの動作は別途確認が必要です。
