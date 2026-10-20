# 停止と復旧 / Stop and resume

この変更を含むソースCLIの手順です。起動済みの旧版プロセスの挙動は変わりません。
実行中のプロファイルを別プロセスで開いたり、ロックや実行記録を手で消したりしないでください。

## 待機上限

`[delete].retry_max_wait` / `--retry-max-wait` の数値は、既定では**1回の必要待機の上限秒数**です。
たとえば上限900秒なら、600秒のサーバー指定待機を何度でも待てます。累計1200秒になっても止めません。
1回の残り待機が901秒なら、短縮して再接続せず、期限と `single_wait_limit` を保存して停止します。
境界値900秒は待ちます。待機中に新しい有効な期限が届けば、その期限も保存して残り時間を再判定します。
`Retry-After` の秒数・HTTP日付と、既存のパーサーが受け付ける `x-rate-limit-reset` のうち遅い期限を守ります。

サーバー期限がある場合は、その期限まで待ちます。ローカルの `cooldown_delay` は期限がない場合の代替値です。
`max_cooldowns` は、サーバー期限がない通信休止だけを数えます。サーバー期限がある休止は繰り返せます。
`max_cooldowns = 0` は自動再開自体を無効にします。`max_retries` は、明示的な削除拒否等に対する
項目ごとの安全な再試行上限として残ります。送信前の429保留はこの回数を使いません。
既存の単投稿専用確認経路は引き続き一度だけの操作で、自動再試行を無効にします。
通常の項目間隔と制限のない定期休止は上限の対象外です。

既存ファイル・GUI設定の数値は書き換えません。**既存の `retry_max_wait` も、省略時は意味が累計から1回へ変わります。**
旧方式を明示的に保つには `[delete].wait_limit_mode = "total"` または `--wait-limit-mode total` を指定してください。
その場合は従来通り、待機時間と通信休止回数が累計になります。GUIは1回ごとの方式を使います。
診断の `waited_seconds` は統計であり、per-waitモードの停止予算ではありません。

## 安全に止める

前面のCLIは **Ctrl+Cを1回**。待機は速やかに中断し、新しい確定操作を止めます。
POSIXの新しいCLIは `kill -TERM <PID>` も同じ経路です。GUIの中止ボタンも従来のキャンセル経路を使います。
送信済みの操作は、既存の10秒のDOM完了待ちの範囲で結果を確認して記録します。
未完了の通信を追加で無期限に待って成功扱いにはしません。
もう一度Ctrl+C、強制終了、クラッシュの場合、`inflight` / `ambiguous` は自動再送しません。
待機期限は残り、期限前の通常起動は拒否されます。

## 状態確認と通常の再開

以下の `ACCOUNT` は保存プロファイル名、`X_USERNAME` は実際のログインアカウント名です。
リストは確認済みの元のCSV/JSON、または `--failed-output` に保存した失敗・未処理リストを使います。
`progress` はブラウザを開かず、件数・未解決の投稿ID・停止理由・待機期限を表示します。

```bash
uv run kusamushiri-cli progress --profile ACCOUNT
uv run kusamushiri-cli resume reviewed.json --profile ACCOUNT --expected-viewer X_USERNAME --headless --recipe cleanup.toml --retry-max-wait 900 --failed-output remaining.json
```

`resume` は確認を求めてから、ブラウザを開く前に保存済みの期限を待ちます。この待機も中止できます。
保存期限は設定を変えても短縮されません。`resume` の起動前待機は1回の上限で判定し、旧totalモードの
操作バッチ予算はブラウザ起動後から数えます。
通常のCLI／GUIバッチも、開始時に実際のログインアカウントを記録の所有者へ照合・固定し、
各確定操作前に再確認します。新しい記録には検証済みの所有者を初回から保存します。
その後、ログインアカウント、プロファイルの記録、対象投稿を照合します。
自分の投稿のURL作者が一致しない場合は止めます。成功済みは送信せずスキップします。
所有者情報のない旧記録に既存の結果がある場合、通常のdelete／GUIバッチでは所有者を推測しません。
`resume --expected-viewer` で明示した期待アカウントを実際のログイン状態と照合してから保存します。
既にviewerが保存されていれば `--expected-viewer` は省略できます。
`resume ... --dry-run` はリストの確認だけで、待機も削除も行いません。
実行中のジョブと同じプロファイルの再開はロックで拒否します。

## 強制終了後

新しいCLIが残したロックに対し、`recover` は同じホストの所有者PIDが終了済みであり、保存が完了したことを
確認できた場合だけロックを解除します。操作結果や期限は変更しません。
復旧操作と通常起動の競合は、POSIXのadvisory lock（flock）で排他します。
復旧プロセスがクラッシュしてもOSがロックを解放するため、`.recovering` ファイルが残っても再試行できます。
このファイルは排他の共有先として保持し、存在するだけでは起動を妨げません。
この所有者検査はPOSIX対応です。Windows、別ホスト、空ロック、中身が途中まで書かれた不正なロックは
安全を証明できないため自動では解除しません。PIDが別のプロセスで再利用されていても解除しません。

**空ロックだけ**は、所有者情報がなく自動判定できません。空ロックは旧版が残したものか、現行版が所有者情報を
書き込む途中で停止したものです。どちらかは区別できません。GUI・CLI・Chromiumなど、同じ
プロファイルを使うすべてのジョブが終了済みと確認した場合に限り、次を明示的に使えます。
`--legacy-owner-stopped` はその確認を利用者が行ったという指定で、プロセスの停止や自動検出はしません。
保存ジャーナルを検証し、元の空ロックを同じディレクトリの `.bak-legacy-*` へ退避します。
実行記録・成功済み項目・待機期限・viewerは変更しません。空でない不正ロックやシンボリックリンクは拒否します。

```bash
uv run kusamushiri-cli recover --profile ACCOUNT --legacy-owner-stopped
```

旧記録にviewerがない場合は、実際のXアカウント名を自分で確認して `X_USERNAME` に指定してください。
プロファイル名から推測して記録を書き換えることはしません。既に作成したCSVをそのまま使えます。

```bash
# オフラインでリストを確認するだけ。ログインアカウントの検証・保存は行いません。
uv run kusamushiri-cli resume reviewed.csv --profile ACCOUNT --expected-viewer X_USERNAME --recipe cleanup.toml --dry-run
# 以下は実際に削除を再開するため、実行前にリストとアカウントを確認してください。
uv run kusamushiri-cli resume reviewed.csv --profile ACCOUNT --expected-viewer X_USERNAME --recipe cleanup.toml --headless --failed-output remaining.json
```

実行時にはURLの作者と期待アカウントを照合し、ログイン中のアカウントが一致した後にviewerを保存します。
通常のウィザードは、既存結果があるviewer未保存の旧記録に対してこの確認を代行しません。

```bash
uv run kusamushiri-cli recover --profile ACCOUNT
uv run kusamushiri-cli progress --profile ACCOUNT
```

未解決の `inflight` / `ambiguous` があれば、**先にXで対象投稿・操作の状態を人が確認**してください。
次のコマンドは確認した結果を記録するだけで、Xには接続しません。期限は保持します。

```bash
# Xで操作済みと確認した場合
uv run kusamushiri-cli resolve --profile ACCOUNT --post-id POST_ID --kind post --verified-applied
# Xで未実行と確認した場合のみ、未処理へ戻す
uv run kusamushiri-cli resolve --profile ACCOUNT --post-id POST_ID --kind post --verified-not-applied
```

`POST_ID` は `progress` の `held` に表示される数値ID、`--kind` は post / repost / like です。
確認できなければ未解決のまま止めてください。確認後、元のリストで `resume` を明示的に実行します。
`resolve` は既存の保留記録だけを変更でき、成功記録を再送可能には戻しません。
