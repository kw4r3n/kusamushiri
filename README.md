# kusamushiri 🌱

*Kusamushiri* (草むしり) is Japanese for "weeding". A desktop app for tidying up your X (Twitter) account.

English | [日本語](README.ja.md)

It bulk-deletes posts and undoes reposts on your own account. It drives a real browser with Playwright and lets you **review every matching post in a
Qt (PySide6) list before anything is deleted**.

![Screenshot](docs/design/gui-populated.png)

> [!WARNING]
> - **Deleted posts cannot be restored.** Review the list carefully. Consider downloading
>   your X data archive first.
> - This is an unofficial tool, not affiliated with X Corp. Browser automation may conflict
>   with X's Terms of Service and can lead to account restrictions. **Use at your own risk.**
> - The app never handles your password. You sign in manually in the browser window it opens;
>   the session is stored only on your computer.

> [!NOTE]
> The user interface is available in Japanese (default) and English. Switch it from the language
> menu at the bottom of the window and restart the app.

## Features

- Filter by date range, minimum likes/replies, media presence, replies only, and keywords to include or exclude
- Delete regular posts and undo reposts
- Undo likes: collect the posts on your profile's Likes tab and remove your like from the ones you check
- Preview the collected list and post text; run only the rows you check
- Save the checked posts to CSV or JSON as a record before deleting them
- Load posts from your X data archive to reach old posts that search misses (posts already deleted with this app are skipped; optionally oldest first)
- Adjustable interval between actions, stop at any time, retry only failed items
- Multiple accounts via separate browser profiles
- Export an account's following list to CSV (Excel-ready) or JSON
- Review the accounts you follow (optionally hiding mutual follows) and unfollow only the checked ones
- Fetch each checked account's last post date, kept per profile and shown in sortable columns

## Install

### Prebuilt app (recommended)

Download the archive for your OS from [Releases](https://github.com/kw4r3n/kusamushiri/releases) and extract it.

| OS | File | Run |
|---|---|---|
| Windows | `kusamushiri-windows-x64.zip` | `kusamushiri\kusamushiri.exe` |
| macOS (Apple Silicon) | `kusamushiri-macos-arm64.zip` | `kusamushiri.app` |
| Linux | `kusamushiri-linux-x64.tar.gz` | `kusamushiri/kusamushiri` |

- Chromium (~150 MB) is downloaded automatically the first time you start the browser.
- The builds are unsigned, so your OS may warn on first launch.
  - Windows: SmartScreen → "More info" → "Run anyway"
  - macOS: right-click the app → "Open", or run `xattr -dr com.apple.quarantine kusamushiri.app`

### From source

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.11 for you).

```bash
git clone https://github.com/kw4r3n/kusamushiri.git
cd kusamushiri
uv run kusamushiri
```

Or double-click `start-kusamushiri.bat` (Windows) / `start-kusamushiri.sh` (Linux/macOS).

With pip: `pip install .`, then run `kusamushiri`.

## Usage

1. Enter the target account ID and filters. **含むキーワード** (include) keeps only posts containing
   any of the keywords; **除外キーワード** (exclude) drops posts containing any of them, which is a
   way to protect posts you want to keep. Separate keywords with commas; case and full-width/half-width
   differences are ignored. Only the text visible on the timeline is checked, not text folded behind "Show more".
2. Click **ブラウザ起動＆ログイン** (Start browser & log in) and sign in to X manually in the opened window.
3. Click **ポストを収集＆プレビュー** (Collect & preview) to list matching posts.
4. Check the rows to process and click **選択項目を削除/解除** (Delete/undo selected).
5. If some items fail, review the details and retry only the failed ones.

Settings, filters, and login state are saved per profile. Profiles and logs live in your
user data directory (e.g. `%LOCALAPPDATA%\kusamushiri` on Windows, `~/.local/share/kusamushiri` on Linux).

## Command line (semi-automation)

The download also contains `kusamushiri-cli` (Windows: `kusamushiri-cli.exe`, macOS:
`kusamushiri.app/Contents/MacOS/kusamushiri-cli`, from source: `uv run kusamushiri-cli`).
Nothing else needs installing.

### The easy way: answer questions once, then repeat

Start `kusamushiri-cli` without arguments (on Windows, double-click `kusamushiri-cli.exe`).
It asks for the profile, filters (e.g. "older than 30 days", keywords to keep) and whether to delete,
saves them to `kusamushiri.toml`, and runs:

1. Signs in if needed (a browser window opens the first time).
2. Collects the matching posts and saves the list to `kusamushiri-lists/posts-<date>.csv`.
3. Shows a preview and asks before deleting. Edit or delete rows in the CSV before answering
   to change what runs; only the rows left in the file are processed.

Next time, starting it again offers to reuse `kusamushiri.toml`, or run `kusamushiri-cli run kusamushiri.toml`.
The file is plain text with comments; edit it to change filters. Without a `[delete]` table
a run only writes the list. `confirm = false` under `[delete]` skips the question for scheduled runs. When reading an archive, `order = "oldest"` or `"newest"` under `[collect]` picks which posts come first.

### Individual commands

```bash
kusamushiri-cli login --profile main                 # sign in once in the opened window
kusamushiri-cli collect --profile main --headless --older-than 30 --exclude keep -o posts.json
kusamushiri-cli archive twitter-archive.zip --profile main --order oldest -o old.json
kusamushiri-cli delete posts.json --profile main --headless --dry-run   # list only
kusamushiri-cli delete posts.json --profile main --headless --interval 3 --failed-output failed.json
kusamushiri-cli following --profile main --skip-mutual -o follows.csv
kusamushiri-cli last-posts follows.csv --profile main -o follows.csv
kusamushiri-cli unfollow follows.csv --profile main --headless
```

- Lists are the same CSV / JSON files the app exports, and profiles are shared with the app.
- `login` always opens a window; other commands accept `--headless` and stop if the profile is
  not signed in. Headless Chromium reports itself as `HeadlessChrome`, so X may treat it differently;
  if a headless run fails, try it without `--headless`.
- `delete` and `unfollow` ask for confirmation; pass `--yes` for unattended runs. Ctrl+C stops
  after the current item, and a second Ctrl+C aborts. Messages are in Japanese; add `--lang en` for English.
- Many actions in a short time make X rate limit you and stop showing posts; those items fail with a
  rate-limit or "post did not appear" message. Use a longer `--interval` and retry the `--failed-output` list later.
- Exit codes: 0 success, 1 some items failed, 2 invalid input, 130 interrupted.
  Run `kusamushiri-cli COMMAND --help` for every option.

## Limitations

- Changes to X's web UI can break the tool. Please open an issue if it stops working.
- Deleting many posts quickly may trigger rate limits; keep a reasonable interval.
- Do not interact with the automated browser window while it is running.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
