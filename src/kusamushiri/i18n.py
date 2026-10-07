"""UI text translation.

Japanese source text is the key, so untranslated text falls back to Japanese.
The language is chosen once at startup; switching it takes effect after a restart.
"""

from typing import Final

DEFAULT_LANGUAGE: Final = "ja"
# Display names are shown in their own language so either can be found.
LANGUAGE_NAMES: Final = {"ja": "日本語", "en": "English"}

ENGLISH: Final[dict[str, str]] = {
    # Window and footer
    "待機中...": "Idle...",
    "ポストを確認して、必要なものだけ整理。": "Review your posts and clean up only what you choose.",
    "すべて選択/解除": "Select / deselect all",
    "ショートカットキー一覧 (Ctrl+H)": "Keyboard shortcuts (Ctrl+H)",
    "ブラウザ停止 / 処理中止": "Stop browser / cancel",
    "選択項目を削除/解除": "Delete / undo selected",
    "ショートカットキー一覧": "Keyboard shortcuts",
    "Ctrl+Enter : 選択項目を削除/解除\nCtrl+R    : 検索/収集を実行\nCtrl+H    : このヘルプを表示": (
        "Ctrl+Enter : Delete / undo selected\nCtrl+R    : Search / collect\nCtrl+H    : Show this help"
    ),
    "言語 / Language": "Language / 言語",
    "言語を変更しました": "Language changed",
    "表示言語はアプリケーションの再起動後に切り替わります。": "The new language is applied after you restart the application.",
    # 1. Basic settings
    "1. 基本設定": "1. Basic settings",
    "プロファイル選択": "Profile",
    "プロファイル追加": "Add profile",
    "新しいログイン用プロファイルを作成して選択します。": "Create and select a new login profile.",
    "名前変更": "Rename",
    "プロファイル削除": "Delete profile",
    "X アカウントID": "X account ID",
    "収集モード": "Collection mode",
    "プロフィール走査": "Scan profile",
    "高度な検索": "Advanced search",
    "取得上限件数": "Max posts",
    "自動保存間隔(秒)": "Auto-save interval (s)",
    "設定を自動保存する間隔です。": "How often settings are saved automatically.",
    "ブラウザ起動＆ログイン": "Start browser and log in",
    "ログイン未確認": "Login not verified",
    "ログイン中: @{username}": "Logged in: @{username}",
    "フォローリストをエクスポート": "Export following list",
    "X アカウントIDのフォロー一覧を CSV（または JSON）で保存します。": (
        "Save the accounts this X account ID follows as CSV (or JSON)."
    ),
    # 2. Filters
    "2. 絞り込み条件": "2. Filters",
    "メディア条件": "Media",
    "すべて": "All",
    "画像・動画ありのみ": "With images or videos only",
    "画像・動画なしのみ": "Without images or videos only",
    "対象種別": "Post type",
    "通常ポストのみ": "Posts only",
    "リポストのみ": "Reposts only",
    "通常ポスト + リポスト": "Posts + reposts",
    "いいねしたポスト（いいね取り消し）": "Liked posts (undo likes)",
    "プロフィールのいいね欄を走査し、チェックしたポストのいいねを取り消します。": (
        "Scans the profile's Likes tab and removes your like from the checked posts."
    ),
    "いいねはプロフィールのいいね欄から収集するため、収集モードは使いません。": (
        "Likes are collected from the profile's Likes tab, so the collection mode is not used."
    ),
    "リプライのみ": "Replies only",
    "最低いいね数": "Min likes",
    "最低返信数": "Min replies",
    "この日以降": "On or after",
    "指定した日付を含む、それ以降のポストを対象にします。": "Include posts from this date onward.",
    "この日以前": "On or before",
    "指定した日付を含む、それ以前のポストを対象にします。": "Include posts up to and including this date.",
    "含むキーワード": "Include keywords",
    "例: 懸賞, キャンペーン": "e.g. giveaway, campaign",
    "いずれかのキーワードを本文に含むポストだけを対象にします。カンマ区切りで複数指定できます。": (
        "Only include posts whose text contains any of these keywords. Separate multiple keywords with commas."
    ),
    "除外キーワード": "Exclude keywords",
    "例: 固定, 大事": "e.g. pinned, important",
    "いずれかのキーワードを本文に含むポストを対象から外します。カンマ区切りで複数指定できます。": (
        "Skip posts whose text contains any of these keywords. Separate multiple keywords with commas."
    ),
    "ポストを収集＆プレビュー": "Collect and preview posts",
    # 3. Preview and actions
    "3. プレビューと削除/解除実行": "3. Preview and delete / undo",
    "0 件 / 0 件選択": "0 items / 0 selected",
    "{count} 件 / {selected} 件選択": "{count} items / {selected} selected",
    "削除/解除間隔": "Interval",
    " 秒": " s",
    "各項目の削除/解除の間に待機する秒数です。": "Seconds to wait between deleting or undoing each item.",
    "0 秒で連続実行": "0 s runs back to back",
    "削除": "Delete",
    "本文": "Text",
    "日時": "Date",
    "種別": "Type",
    "いいね": "Likes",
    "返信": "Replies",
    "メディア": "Media",
    "リプライ": "Reply",
    "リポスト": "Repost",
    "いいね済み": "Liked",
    "ポスト": "Post",
    "あり": "Yes",
    "はい": "Yes",
    "まだポストがありません": "No posts yet",
    "ログイン後、条件を指定してポストを収集してください。": "After logging in, set your filters and collect posts.",
    "行を選択すると本文とURLが表示されます。": "Select a row to see its text and URL.",
    "ログ": "Log",
    "ステータスメッセージがここに記録されます。": "Status messages are recorded here.",
    # Filter summary
    "画像/動画あり": "With media",
    "画像/動画なし": "Without media",
    "ポストのみ": "Posts only",
    "メディア: {value}": "Media: {value}",
    "種別: {value}": "Type: {value}",
    "最大: {count}件": "Max: {count}",
    "いいね≥{count}": "Likes ≥ {count}",
    "返信≥{count}": "Replies ≥ {count}",
    "{date}〜": "From {date}",
    "〜{date}": "Until {date}",
    "含む: {keywords}": "Include: {keywords}",
    "除外: {keywords}": "Exclude: {keywords}",
    # Dialogs
    "エラー": "Error",
    "情報": "Information",
    "確認": "Confirm",
    "完了": "Done",
    "終了": "Quit",
    "入力エラー": "Input error",
    "ログイン待ち": "Waiting for login",
    "ブラウザが開きました。\nX に手動でログインし、ホーム画面が表示されたら OK を押してください。": (
        "The browser is open.\nLog in to X manually, then press OK once your home timeline appears."
    ),
    "ブラウザを停止": "Stop browser",
    "ブラウザを停止しますか？\n進行中の操作は安全な区切りで中断されます。": (
        "Stop the browser?\nAny operation in progress stops at the next safe point."
    ),
    "新しいプロファイル名:": "New profile name:",
    "'{name}' のログイン情報と設定を完全に削除しますか？\nXのアカウントやポストは削除されません。": (
        "Permanently delete the login data and settings for '{name}'?\nYour X account and posts are not deleted."
    ),
    "プロファイルを作成できませんでした: {error}": "Could not create the profile: {error}",
    "アカウント不一致": "Account mismatch",
    "現在ログイン中のアカウントと入力された対象アカウントが一致していません。\n"
    "ログイン中: @{logged_in}\n入力値: @{entered}\n\n"
    "このまま実行すると削除/解除に失敗する可能性があります。続行しますか？": (
        "The logged-in account does not match the target account you entered.\n"
        "Logged in: @{logged_in}\nEntered: @{entered}\n\n"
        "Deleting or undoing may fail if you continue. Continue anyway?"
    ),
    "現在ログイン中のアカウントと入力された対象アカウントが一致していません。\n"
    "ログイン中: @{logged_in}\n入力値: @{entered}\n\n"
    "必要に応じて対象アカウントを修正してください。": (
        "The logged-in account does not match the target account you entered.\n"
        "Logged in: @{logged_in}\nEntered: @{entered}\n\n"
        "Correct the target account if needed."
    ),
    "ログイン確認が必要": "Login required",
    "先にブラウザを起動してログインを確認してください。": "Start the browser and verify your login first.",
    "現在のアカウントでログインを確認してください。": "Verify your login with the current account.",
    "日付範囲エラー": "Invalid date range",
    "開始日が終了日より後の日付になっています。\n正しい範囲を指定してください。": (
        "The start date is after the end date.\nPlease choose a valid range."
    ),
    "条件に一致するポストはありませんでした": "No posts matched your filters",
    "期間やフィルタを広げて、もう一度収集してください。": "Widen the date range or filters and collect again.",
    "選択した項目をすべて処理しました": "All selected items were processed",
    "続けて整理する場合は、条件を指定してもう一度収集してください。": (
        "To keep cleaning up, set your filters and collect again."
    ),
    "フォローリストの保存先": "Save following list as",
    "X アカウントIDを入力してください（英数字とアンダースコア、15文字まで）。": (
        "Enter an X account ID (letters, numbers and underscores, up to 15 characters)."
    ),
    "エクスポート完了": "Export complete",
    "{count} 件のフォローを保存しました。\n{path}": "Saved {count} followed accounts.\n{path}",
    "選択された {count} 件の項目に対して、{summary} を実行しますか？\n（この操作は元に戻せません）": (
        "Apply to the {count} selected items: {summary}?\n(This cannot be undone.)"
    ),
    "ポスト削除 {count} 件": "delete {count} posts",
    "リポスト解除 {count} 件": "undo {count} reposts",
    "いいね取り消し {count} 件": "undo {count} likes",
    "一部失敗": "Some items failed",
    "失敗した項目の詳細を確認し、失敗分だけ再試行できます。": (
        "Check the details of the failed items; you can retry just those."
    ),
    "失敗分だけ再試行": "Retry failed items",
    "アプリケーションを終了しますか？": "Quit the application?",
    "処理中ですがアプリケーションを終了しますか？": "An operation is in progress. Quit the application anyway?",
    # Status messages
    "ブラウザを起動しています...": "Starting the browser...",
    "処理を中断してブラウザを停止しています...": "Cancelling and stopping the browser...",
    "アカウント '{name}' を選択しました。": "Selected account '{name}'.",
    "プロファイル名を '{name}' に変更しました。": "Renamed the profile to '{name}'.",
    "プロファイル '{name}' を削除しました。": "Deleted profile '{name}'.",
    "プロファイル '{name}' を追加しました。ブラウザを起動してログインしてください。": (
        "Added profile '{name}'. Start the browser and log in."
    ),
    "ログイン確認済み: @{username}": "Login verified: @{username}",
    "対象を収集しています...": "Collecting posts...",
    "対象を収集中: {scanned} 件走査済み ({current}/{total})": (
        "Collecting posts: {scanned} scanned ({current}/{total})"
    ),
    "収集完了: {count} 件の対象を取得しました。": "Collection complete: found {count} items.",
    "@{username} のフォローリストを取得しています...": "Fetching the following list of @{username}...",
    "フォローリストを取得中: {count} 件": "Fetching following list: {count}",
    "フォローリスト {count} 件を保存しました: {path}": "Saved a following list of {count} accounts: {path}",
    "削除/解除処理を実行しています...": "Deleting / undoing...",
    "失敗分を再試行しています...": "Retrying failed items...",
    "{action}中 ({current}/{total}): {url}": "{action} ({current}/{total}): {url}",
    "削除/解除処理を中止": "Delete / undo cancelled",
    "削除/解除処理完了": "Delete / undo finished",
    "{heading}: {success} / {requested} 件成功": "{heading}: {success} / {requested} succeeded",
    "{count} 件失敗": "{count} failed",
    "{count} 件未処理": "{count} not processed",
    "（{notes}）": " ({notes})",
    "、": ", ",
    "詳細不明": "No details",
    # Worker
    "{action} に失敗しました。": "{action} failed.",
    "エラーが発生しました: {message}": "An error occurred: {message}",
    "先にブラウザを起動してログインしてください。": "Start the browser and log in first.",
    "ブラウザを起動しています…（初回は Chromium のダウンロードに数分かかることがあります）": (
        "Starting the browser… (the first run may take a few minutes to download Chromium)"
    ),
    "ブラウザを起動しました。ログイン後に確認を実行します。": (
        "The browser has started. Your login is checked once you log in."
    ),
    "ブラウザを停止しました。": "The browser has stopped.",
    "ログインが確認されました。現在のログイン: @{username}": "Login verified. Logged in as @{username}",
    "ログインが確認されました。ポストを収集できます。": "Login verified. You can now collect posts.",
    "ログインを確認できませんでした。X のホーム画面が開いた状態で再度確認してください。": (
        "Could not verify your login. Open your X home timeline and check again."
    ),
    "収集処理を中断しました。": "Collection cancelled.",
    "収集の安全上限に到達したため走査を終了しました。": "Stopped scanning at the collection safety limit.",
    "削除/解除処理を中断しました。": "Delete / undo cancelled.",
    "フォローリストの取得を中断しました。ファイルは保存していません。": (
        "Fetching the following list was cancelled. No file was saved."
    ),
    "取得の安全上限に到達したため、途中までのフォローリストを保存しました。": (
        "Reached the fetch safety limit; saved the following list collected so far."
    ),
    # Post actions
    "リポスト解除": "Undo repost",
    "いいね取り消し": "Undo like",
    "ポスト削除": "Delete post",
    "応答なし": "no response",
    "{mutation} の通信に失敗しました: {detail}": "{mutation} request failed: {detail}",
    "{mutation} がエラーを返しました (HTTP {status})。": "{mutation} returned an error (HTTP {status}).",
    "{mutation} がエラーを返しました: {detail}": "{mutation} returned an error: {detail}",
    "操作対象のポストIDをURLから確認できませんでした。": "Could not read the target post ID from the URL.",
    "リポスト解除ボタンが見つかりませんでした。": "Could not find the undo repost button.",
    "リポスト解除の確認ボタンが見つかりませんでした。": "Could not find the undo repost confirmation button.",
    "いいね取り消しボタンが見つかりませんでした。": "Could not find the undo like button.",
    "対象ポストの読み込み": "Loading the target post",
    "「…」メニューボタンの表示待ち": "Waiting for the \"…\" menu button",
    "削除メニューが見つかりませんでした。": "Could not find the delete menu.",
    "X の利用制限 (HTTP 429) でポストを表示できませんでした。しばらく待ってから再試行してください。": (
        "X is rate limiting (HTTP 429) and did not show the post. Wait a while and try again."
    ),
    "ポストが表示されませんでした。削除済み・非公開か、X の利用制限の可能性があります。時間をおいて再試行してください。": (
        "The post did not appear. It may be deleted or protected, or X may be rate limiting. Try again later."
    ),
    "削除メニュー項目の表示待ち": "Waiting for the delete menu item",
    "削除メニュー項目が見つかりませんでした。自分のポストではない可能性があります。": (
        "Could not find the delete menu item. The post may not be yours."
    ),
    "削除確認画面の表示": "Opening the delete confirmation",
    "削除確定後の完了確認（結果不明。再試行前にXで確認してください）": (
        "Confirming the deletion (result unknown; check on X before retrying)"
    ),
    "削除確認ボタンが見つかりませんでした。": "Could not find the delete confirmation button.",
    # Validation
    "実行対象が選択されていません。": "Nothing is selected.",
    "削除/解除間隔は0秒以上で指定してください。": "The interval must be 0 seconds or more.",
    "アカウントIDを入力してください。": "Enter an account ID.",
    "取得上限件数は1以上で指定してください。": "Max posts must be 1 or more.",
    "最低いいね数と最低返信数は0以上で指定してください。": "Min likes and min replies must be 0 or more.",
    "不正なメディア条件が指定されました。": "Invalid media filter.",
    "収集モードは profile または search を指定してください。": "Collection mode must be profile or search.",
    "対象種別は posts / reposts / all / likes のいずれかで指定してください。": (
        "Post type must be posts, reposts, all or likes."
    ),
    "いいねはプロフィールのいいね欄からのみ収集できます。収集モードを profile にしてください。": (
        "Likes can only be collected from the profile's Likes tab. Set the collection mode to profile."
    ),
    "開始日は YYYY-MM-DD 形式の日付で指定してください。": "The start date must be a YYYY-MM-DD date.",
    "終了日は YYYY-MM-DD 形式の日付で指定してください。": "The end date must be a YYYY-MM-DD date.",
    "開始日は終了日以前で指定してください。": "The start date must be on or before the end date.",
    "空のキーワードは指定できません。": "Keywords cannot be empty.",
    "ユーザー名は1〜15文字の英数字またはアンダースコアで指定してください。": (
        "Usernames must be 1 to 15 letters, numbers or underscores."
    ),
    "media_filter は all / with_media / without_media のいずれかで指定してください。": (
        "media_filter must be all, with_media or without_media."
    ),
    "search_mode は profile / search のいずれかで指定してください。": "search_mode must be profile or search.",
    "post_kind_filter は posts / reposts / all / likes のいずれかで指定してください。": (
        "post_kind_filter must be posts, reposts, all or likes."
    ),
    "保存先の拡張子は .csv または .json を指定してください。": "The file extension must be .csv or .json.",
    # Archive import
    "X のアーカイブから読み込み": "Load from X archive",
    "X の「データのアーカイブ」(zip) からポストを読み込み、上の条件で絞り込みます。検索で見つからない古いポストも対象にできます。": (
        "Loads posts from your X data archive (zip) and filters them with the conditions above. "
        "This includes old posts that search cannot find."
    ),
    "X のアーカイブを選択": "Choose your X archive",
    "X のアーカイブ (*.zip tweets.js tweets-part*.js tweet.js)": "X archive (*.zip tweets.js tweets-part*.js tweet.js)",
    "読み込みエラー": "Load error",
    "アーカイブを読み込めませんでした: {error}": "Could not read the archive: {error}",
    "アーカイブは古い順に読み込む": "Load archive oldest first",
    "アーカイブから読み込むとき、最も古いポストから取得上限件数までを古い順に並べます。": (
        "When loading an archive, list posts from the oldest, up to the maximum count."
    ),
    "アーカイブを読み込み中…": "Loading the archive…",
    "アーカイブを読み込み中… ({done}/{total})": "Loading the archive… ({done}/{total})",
    "アーカイブから {count} 件を読み込みました（全 {total} 件、リポスト {reposts} 件と削除済み {deleted} 件は対象外）。": (
        "Loaded {count} posts from the archive ({total} in total; {reposts} reposts and {deleted} already "
        "deleted posts are not included)."
    ),
    "削除済みポストの記録を保存できませんでした: {message}": "Could not save the record of deleted posts: {message}",
    # Post export
    "選択項目を保存": "Save selected",
    "チェックした項目の本文や URL を CSV（または JSON）で保存します。削除前の控えに使えます。": (
        "Saves the text and URL of the checked items as CSV (or JSON), as a record before deleting."
    ),
    "保存する項目が選択されていません。": "No items are selected to save.",
    "選択項目の保存先": "Save selected items as",
    "保存エラー": "Save error",
    "ファイルを保存できませんでした: {error}": "Could not save the file: {error}",
    "選択項目 {count} 件を保存しました: {path}": "Saved {count} selected items: {path}",
    "{count} 件の項目を保存しました。\n{path}": "Saved {count} items.\n{path}",
    # Profiles
    "プロファイル名を入力してください。": "Enter a profile name.",
    "default プロファイルの名前は変更できません。": "The default profile cannot be renamed.",
    "同じ名前のプロファイルが既にあります。": "A profile with that name already exists.",
    "default プロファイルは削除できません。": "The default profile cannot be deleted.",
    "設定を保存できませんでした: {status}": "Could not save settings: {status}",
    # Unfollow
    "フォローを整理": "Manage follows",
    "フォロー一覧を取得して確認し、選んだアカウントだけフォローを解除します。": (
        "Load the accounts you follow, review them, and unfollow only the ones you check."
    ),
    "フォロー {count} 件を取得しました。": "Loaded {count} followed accounts.",
    "フォローリストの取得を中断しました。": "Stopped loading the following list.",
    "相互フォローを除外": "Hide mutual follows",
    "フォローされているアカウントを一覧から隠し、選択から外します。": (
        "Hide accounts that follow you back and remove them from the selection."
    ),
    "ユーザー名": "Username",
    "表示名": "Display name",
    "フォローされている": "Follows you",
    "@{username} のフォロー {count} 件（うち相互フォロー {mutual} 件）": (
        "@{username} follows {count} accounts ({mutual} follow back)"
    ),
    "取得の安全上限に到達したため、一覧は途中までです。": "The safety limit was reached, so the list is incomplete.",
    "解除間隔": "Interval",
    "各アカウントのフォロー解除の間に待機する秒数です。X の制限を避けるため長めにしています。": (
        "Seconds to wait between unfollows. It is long on purpose to stay within X's limits."
    ),
    "選択したフォローを解除": "Unfollow selected",
    "フォロー解除するアカウントが選択されていません。": "No accounts are selected to unfollow.",
    "フォロー解除の間隔は0秒以上で指定してください。": "The unfollow interval must be 0 seconds or more.",
    "{count} 件のフォローを解除します。\n（この操作は元に戻せません）\n各アカウントの間に {interval} 秒待機します。続行しますか？": (
        "Unfollow {count} accounts?\n(This cannot be undone.)\nWaits {interval} seconds between accounts."
    ),
    "フォロー解除を実行しています...": "Unfollowing...",
    "フォロー解除中 ({current}/{total}): @{username}": "Unfollowing ({current}/{total}): @{username}",
    "フォロー解除を中止": "Unfollow stopped",
    "フォロー解除完了": "Unfollow finished",
    "フォロー解除を中断しました。": "Unfollowing was stopped.",
    "プロフィールの読み込み": "Loading the profile",
    "フォロー解除ボタンの表示待ち": "Waiting for the Following button",
    "フォロー解除ボタンが見つかりませんでした。": "The Following button was not found.",
    "このアカウントをフォローしていません。": "You do not follow this account.",
    "フォロー解除確認画面の表示": "Opening the unfollow confirmation",
    "フォロー解除の確認ボタンが見つかりませんでした。": "The unfollow confirmation button was not found.",
    "フォロー解除後の完了確認（結果不明。再試行前にXで確認してください）": (
        "Confirming the unfollow (result unknown; check on X before retrying)"
    ),
    # Last post dates of followed accounts
    "最終ポスト": "Last post",
    "取得日": "Checked at",
    "不明": "Unknown",
    "チェックした項目の最終ポスト日を取得": "Fetch last post dates of checked accounts",
    "チェックしたアカウントのプロフィールを順に開き、固定ポストとリポストを除いた最新ポストの日付を記録します。": (
        "Opens each checked account's profile in turn and records the date of its newest post, "
        "ignoring pinned posts and reposts."
    ),
    "最終ポスト日を取得するアカウントが選択されていません。": "No accounts are checked to fetch last post dates for.",
    "取得の間隔は0秒以上で指定してください。": "The fetch interval must be 0 seconds or more.",
    "取得を中断しました。": "Fetching was stopped.",
    "タイムラインを読み込めませんでした。": "The timeline did not load.",
    "ポストが見つかりません。": "No posts were found.",
    "固定ポストとリポスト以外のポストが見つかりません。": "No posts were found other than pinned posts and reposts.",
    "最終ポスト日の取得を中断しました。": "Fetching last post dates was stopped.",
    "最終ポスト日を取得しています...": "Fetching last post dates...",
    "最終ポスト日を取得中 ({current}/{total}): @{username}": "Fetching last post dates ({current}/{total}): @{username}",
    "最終ポスト日の取得を中止": "Last post fetch stopped",
    "最終ポスト日の取得完了": "Last post fetch finished",
    "取得結果を保存できませんでした: {message}": "Could not save the fetched results: {message}",
    # Command line
    "X にログインしていません。先に `kusamushiri-cli login{hint}` を実行してください。": (
        "Not logged in to X. Run `kusamushiri-cli login{hint}` first."
    ),
    "開いたブラウザで X にログインしてください（最大 {minutes} 分待ちます）。": (
        "Sign in to X in the opened browser (waiting up to {minutes} minutes)."
    ),
    "ログインを確認できませんでした。": "No sign-in was detected.",
    "{seconds} 秒以内にログインを確認できませんでした。": "No sign-in was detected within {seconds} seconds.",
    "@{username} でログインしています。": "Logged in as @{username}.",
    "ログインしています。": "Logged in.",
    "ログイン中のアカウントを判別できませんでした。--user を指定してください。": (
        "Could not tell the logged-in account; pass --user."
    ),
    "現在の項目が終わったら停止します（もう一度 Ctrl+C で即中断）。": (
        "Stopping after the current item (press Ctrl+C again to abort)."
    ),
    "中断しました。": "Interrupted.",
    "kusamushiri-cli: 中断しました。": "kusamushiri-cli: aborted.",
    "kusamushiri-cli: {error}（詳細は -v を付けて実行）": "kusamushiri-cli: {error} (run with -v for details)",
    "確認なしでは実行しません。--yes を付けてください。": "Refusing to run without confirmation; pass --yes.",
    "{prompt} 続ける場合は yes と入力してください: ": "{prompt} Type 'yes' to continue: ",
    "キャンセルしました。": "Cancelled.",
    "{succeeded}/{total} 件成功。": "{succeeded}/{total} succeeded.",
    "{succeeded}/{total} 件成功（途中で停止）。": "{succeeded}/{total} succeeded (stopped early).",
    "失敗: {target}: {error}": "failed: {target}: {error}",
    "失敗した {count} 件を {path} に保存しました。": "Wrote {count} failed rows to {path}.",
    "{count} 件を {path} に保存しました。": "Wrote {count} items to {path}.",
    "{count} 件（確認のみ、何も変更していません）。": "{count} items (dry run, nothing changed).",
    "{path} の {count} 件を削除/解除しますか？元に戻せません。": (
        "Delete or undo {count} items listed in {path}? This cannot be undone."
    ),
    "{path} の {count} 件をフォロー解除しますか？": "Unfollow {count} accounts listed in {path}?",
    "中断したため、ファイルは保存していません。": "Stopped; no file was written.",
    "対象のアカウントがありません。": "No accounts to process.",
    "走査 {scanned} 件、該当 {current}/{total} 件": "scanned {scanned}, matched {current}/{total}",
    "アーカイブを読み込み中… {done}/{total}": "Reading the archive… {done}/{total}",
    "{count} 件取得": "collected {count}",
    "アーカイブの {total} 件から {count} 件を選びました（リポスト {reposts} 件、削除済み {deleted} 件は対象外）。": (
        "Selected {count} of {total} archived posts (skipped {reposts} reposts, {deleted} already deleted)."
    ),
    "削除済みポスト ID を保存できませんでした: {error}": "Could not save the deleted post IDs: {error}",
    "  …ほか {count} 件": "  …and {count} more",
    "confirm = true のため、対話できない環境では実行できません。": (
        "confirm = true needs an interactive terminal; set confirm = false for unattended runs."
    ),
    "一覧のファイルを編集すると、残した行だけを実行します（行を消すと対象外）。": (
        "Edit the list file now to change the targets: only the rows left in it are run."
    ),
    "{count} 件を削除/解除しますか？元に戻せません。": "Delete or undo {count} items? This cannot be undone.",
    "削除/解除は行いませんでした。": "Nothing was deleted.",
    "一覧が空になったため、何もしませんでした。": "The list is empty; nothing was done.",
    "どの設定を使いますか？": "Which settings do you want to use?",
    "新しい設定を作る": "Create new settings",
    "新しい設定の名前（例: likes → kusamushiri-likes.toml）": "Name for the new settings (e.g. likes → kusamushiri-likes.toml)",
    "ファイル名に使える名前を入力してください。": "Enter a name that can be used as a file name.",
    "{name} は既にあります。別の名前を入力してください。": "{name} already exists. Enter another name.",
    "保存済みの設定があります: {path}": "Found saved settings: {path}",
    "この設定で実行しますか？（n で設定を作り直します）": "Run with these settings? (n to set them up again)",
    "今すぐ実行しますか？": "Run it now?",
    "次回からは kusamushiri-cli run {path} でも実行できます。": "Next time you can also run: kusamushiri-cli run {path}",
    "Enter キーで終了します…": "Press Enter to close…",
    # Command-line wizard
    "質問に答えると、掃除の設定ファイルを作ります。空欄で Enter を押すと [ ] 内の値を使います。": (
        "Answer a few questions to create a cleanup settings file. Press Enter to accept the value in [ ]."
    ),
    "保存済みのプロファイル: {names}": "Saved profiles: {names}",
    "プロファイル名": "Profile name",
    "番号": "Number",
    "ポストをどこから集めますか？": "Where should posts come from?",
    "X で検索する": "Search on X",
    "X のデータのアーカイブから読む": "Read an X data archive",
    "アーカイブ (.zip またはフォルダ) のパス": "Path to the archive (.zip or folder)",
    "どの順に集めて削除しますか？": "Which posts should be collected and deleted first?",
    "古い順": "Oldest first",
    "新しい順": "Newest first",
    "対象": "Target",
    "通常ポスト": "Posts",
    "何日より前のポストを対象にしますか（0 で期間指定なし）": "Only posts older than how many days? (0 for any date)",
    "含むキーワード（カンマ区切り、空欄で指定なし）": "Keywords to include (comma-separated, empty for none)",
    "除外キーワード（残したいポストを守ります）": "Keywords to exclude (protects posts you want to keep)",
    "1 回に集める上限件数": "Maximum posts per run",
    "集めたポストを削除/解除まで行いますか？（実行前に毎回確認します）": (
        "Also delete/undo the collected posts? (you are asked before every run)"
    ),
    "削除/解除の間隔（秒）": "Seconds between deletions",
    "ブラウザのウィンドウを表示せずに実行しますか？": "Run without showing the browser window?",
    "設定を {path} に保存しました。テキストエディタで編集できます。": (
        "Saved the settings to {path}. You can edit it in a text editor."
    ),
    "数値を入力してください。": "Enter a number.",
    "0 以上で入力してください。": "Enter 0 or greater.",
}

TRANSLATIONS: Final[dict[str, dict[str, str]]] = {"en": ENGLISH}

_language = DEFAULT_LANGUAGE


def normalize_language(language: str | None) -> str:
    return language if language in LANGUAGE_NAMES else DEFAULT_LANGUAGE


def set_language(language: str | None) -> None:
    global _language
    _language = normalize_language(language)


def get_language() -> str:
    return _language


def translate(text: str, language: str, **values: object) -> str:
    translated = TRANSLATIONS.get(language, {}).get(text, text)
    return translated.format(**values) if values else translated


def tr(text: str, **values: object) -> str:
    """Return the Japanese source text in the current UI language, formatted with values."""
    return translate(text, _language, **values)
