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
- Adjustable pacing, prompt cancellation, bounded retries for safe failures, and resume of unprocessed items
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
5. If a batch stops, review the details and resume the remaining items; only proven safe failures retry automatically.

Settings, filters, and login state are saved per profile. Profiles and logs live in your
user data directory (e.g. `%LOCALAPPDATA%\kusamushiri` on Windows, `~/.local/share/kusamushiri` on Linux).

## Sharing sanitized diagnostics

Diagnostic request/response logging is enabled automatically. It uses local trace,
item, attempt, operation, navigation and request IDs; safe endpoint names/methods;
request-event and response times; HTTP status and parsed Retry-After. `origin_operation`
is the recorded request-start association; `operation` is the operation active when
the response arrives. These can differ across retries. Unmatched/evicted requests or
start times predating the current operation are reported as unknown rather than guessed.
Association records timing, not the JavaScript initiator or a shared server quota.
Outcomes identify submission, mutation request IDs, deferred/ambiguous/auth results and
GraphQL code88 refusals without copying error messages or response bodies.

After your intended operation with this version, export the latest trace:

```nu
uv run kusamushiri-cli diagnostics | save -f kusamushiri-diagnostic.jsonl
```

Share that JSONL excerpt. It excludes credentials, full URLs/query strings, post IDs,
post/response bodies and unrelated legacy log lines; do not share the full app log.
Optional: `diagnostics --trace <12-character ID> --max-events 1000`, or `--log PATH`
to read another log (including its three rotations). Request records are bounded;
missing origins stay unknown. This command reads logs only and starts no browser.
If investigating the first 429/deferral, Ctrl+C can stop the intended run once that event
appears; completing a long cooldown is unnecessary for capturing it. Cancellation and
saved deadlines still apply. Logging adds no requests/waits and changes no rate policy.

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
To keep several setups (for example one for old posts and one for likes), choose "Create new settings"
and give it a name such as `likes`; it is saved as `kusamushiri-likes.toml` next to the others.
Each start lists every `kusamushiri*.toml` in the current folder to pick from, and
`kusamushiri-cli run kusamushiri-likes.toml` runs one directly.
The file is plain text with comments; edit it to change filters. Without a `[delete]` table
a run only writes the list. `confirm = false` under `[delete]` skips the question for scheduled runs. When reading an archive, `order = "oldest"` or `"newest"` under `[collect]` picks which posts come first.

### Add missing recipe defaults

Updating the app does not rewrite saved TOML. Older recipes still use current defaults
for omitted settings. To make those defaults explicit while retaining your settings:

```nu
cd /path/to/kusamushiri
^.venv/bin/python -m kusamushiri.cli update-recipe kusamushiri-omote-post.toml --dry-run
^.venv/bin/python -m kusamushiri.cli update-recipe kusamushiri-omote-post.toml
```

Installed/prebuilt CLI: `kusamushiri-cli update-recipe FILE [--dry-run]` (default file:
`kusamushiri.toml`). Both forms show a diff. `--dry-run` writes no recipe or backup;
the actual update creates an exclusive `FILE.bak-<UTC timestamp>` backup before
validated atomic replacement. Repeating it makes no changes or additional backups.
Keep the backup to restore the original text if needed.

Only missing settings with concrete current defaults are added. Existing values,
comments, statement order, inline/dotted tables and legacy `oldest_first` are retained.
An absent `[delete]` stays absent, and optional unset values such as `profile`, `archive`,
`user` and dates stay unset. Explicitly adding defaults pins them to those values,
so future default changes will not alter them. Unknown settings are preserved and
reported; the normal loader still rejects them. Malformed TOML, invalid supported
values, conflicting aliases, symlinks or a concurrent edit stop the update.
It never starts a browser, migrates account data or performs collection/deletion.
Ordinary recipe loading remains read-only. Rebuilding a recipe in the question wizard
overwrites the file and its comments; use `update-recipe` to retain them.

To preview every saved recipe in the current directory, then update that same scope:

```nu
^.venv/bin/python -m kusamushiri.cli update-recipe --all --dry-run
^.venv/bin/python -m kusamushiri.cli update-recipe --all
```

`--all` uses the wizard's `kusamushiri*.toml` naming convention in the current directory
only, with `kusamushiri.toml` first. It does not recurse or include unrelated TOML files
such as `pyproject.toml` or `examples/delete-tuning.toml`. Symlinks and dotted filename
components `toml`, `bak`, `backup`, `tmp`, `temp`, `new`, `updated`, `generated`, `preview`,
`candidate` or `review` before the final `.toml` are excluded (case-insensitive, also with
`-`/`_` suffixes), so backup/temporary copies are not selected. For example,
`kusamushiri-old.new.toml` is excluded; the saved name `kusamushiri-new.toml` is included.
The command prints the exact matched paths, proposed diffs and unchanged-file status.
A positional file path cannot be combined with `--all`.

All targets are validated before any writes. Invalid input aborts the entire update
without creating backups (exit 2); a discovery or write failure exits 1. If a write fails
after earlier files succeeded, it stops and reports the completed files, their backups,
the failed file and the remaining unattempted count; the earlier updates remain in place.
Each successful file gets its own backup and atomic replacement. Zero matches, a complete
update and a successful dry-run exit 0. Repeated updates create no extra backups.

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
- New post deletion/undo defaults use a 3-second item interval and a 120-second quiet pause after
  25 new confirmed successes. These are conservative starting points, not known safe X thresholds.
  Explicit saved intervals and settings remain unchanged.
- In the default `global` mode, every observed X HTTP 420/429 remains a global pacing signal. Logs distinguish mutation,
  target-load, recognized ancillary (`fleetline` / `ViewerBadgeCounts`) and unknown requests;
  these labels describe request roles, not independent server quota buckets. Unknown limits are
  treated conservatively too. An ancillary limit never implies that the deletion API rejected an action.
- A limit before the irreversible click leaves that item **pending/unprocessed**, without adding
  a deletion failure or consuming action retries. Rate cooldowns have their own maximum of three
  per run, starting at 120, 240 and 480 seconds. Actual safe action failures still have at most
  three additional retries, starting at 60, 120 and 240 seconds. Persistent limits stop the run.
- Long waits put the browser context offline so new polling requests cannot keep hitting X during
  the cooldown. Already-sent requests/queued callbacks can still arrive; callbacks are dispatched
  while waiting and newly observed `Retry-After` extends the deadline. Numeric seconds and HTTP
  dates are honored even beyond the 900-second backoff cap. If the next wait exceeds the
  1,800-second retry/cooldown budget, the browser stays paused and the run stops. Routine item
  intervals and proactive batch pauses are excluded, unless a rate limit arrives during that pause.
- After any rate limit or saved cooldown resume, the remaining item interval is at least 5 seconds (or the configured
  interval if longer). Successful in-flight deletions are recorded and never repeated.
- Ctrl+C/the stop button cancels waits promptly. A cancelled/exhausted cooldown preserves the
  reason and deadline in the per-profile journal and keeps an existing browser offline. A new
  browser start before that saved deadline refuses to connect and displays the deadline; after it
  expires, explicitly start/resume the job. Remaining items are not resumed automatically.
- Authentication/permission errors, permanent failures and uncertain outcomes stop immediately.
  A missing mutation response, server 5xx after submission, or unconfirmed 2xx completion is uncertain
  and never automatically repeated. `--failed-output` includes failed **and unprocessed** rows.
- `--dry-run` lists input rows without opening a browser, migrating profiles, writing receipts or
  waiting. Collection, unfollow and last-post jobs continue to stop on HTTP 429.
- Exit codes: 0 success, 1 some items failed, 2 invalid input, 130 interrupted or stopped by a rate limit.
  Run `kusamushiri-cli COMMAND --help` for every option.

### Simple deletion settings

New recipes need only two deletion settings:

```toml
[delete]
interval = 3.0
confirm = true
```

Change the item interval for normal pacing. Confirmation protects irreversible operations.
The ten existing advanced keys remain accepted with the same CLI > TOML > default precedence;
explicit saved values are preserved, including values equal to the current defaults.
No key is deprecated or silently migrated: action retries, batch pauses and global cooldowns
serve different purposes. Generated templates omit defaults, and `update-recipe` / `--all`
never add advanced defaults back. Existing files and their comments are not rewritten.
`delete --help` / `run --help` show interval, total wait budget and ancillary mode; the other
eight tuning flags remain accepted and are listed below. No preset or parallel tuning system is added.

<details>
<summary>Advanced compatibility settings and safety behavior</summary>

### Wait and retry settings

These controls describe the current source/Unreleased changes; an existing prebuilt app does not
automatically gain them. Verify the executable's `--help`, or use this checkout's source entrypoint.
The commands below assume the source environment has been created and run from the repository.
Use [examples/delete-tuning.toml](examples/delete-tuning.toml) as a commented starting point:
copy its keys into the existing single `[delete]` table rather than duplicating the table/keys.
The example uses 8-second spacing for conservative tuning; the default remains 3 seconds.

Expand **Wait / retry settings** above the preview in the app; values are saved per profile.
The item interval, nine timing controls and the optional ancillary policy are available on `delete` and as overrides on `run`.
From the repository in Nushell, this is a browser-free preview; replace the input placeholder:

```nu
^.venv/bin/python -m kusamushiri.cli delete "/path/to/reviewed-posts.json" --dry-run --interval 3 --batch-size 25 --batch-pause 120 --max-retries 3 --retry-delay 60 --retry-max-delay 900 --retry-max-wait 1800 --max-cooldowns 3 --cooldown-delay 120 --rate-limit-interval 5
```

Both `run FILE` and `delete LIST --recipe FILE` reuse the existing recipe loader and `[delete]`
schema. Precedence is **explicit CLI flag > TOML `[delete]` value > default** for the item interval
and all retry/batch/cooldown controls and the ancillary policy. `run --interval` can override the saved interval too.
Overrides apply only to the invocation; they never rewrite the TOML file. A collect-only recipe
without `[delete]` remains collect-only; attempting deletion-tuning overrides there is an error.

`delete --recipe` imports only pacing/retry values, and operates on the supplied reviewed list;
it does not collect, apply `[collect]` filters, or inherit the recipe's profile/headless/confirmation.
Use the existing `--profile`, `--headless` and explicit `--yes` options when appropriate. Without
`--recipe`, `delete` still uses flags/defaults. Its interval fallback is 3 seconds.
`run` retains the recipe's profile, collection and confirmation behavior and has no `--dry-run`.
A browser-free way to preview a list with TOML tuning in Nushell is:

```nu
^.venv/bin/python -m kusamushiri.cli delete "/path/to/reviewed-posts.csv" --recipe "kusamushiri-cleanup.toml" --dry-run
```

The GUI offers the same timing fields/defaults using its saved per-profile settings; it does not
automatically import a TOML file. CLI tuning and GUI preferences are separate existing stores.
Safety deadlines/receipts in `action-progress.json` are runtime state, not recipe settings;
changing flags/TOML never erases those deadlines or overrides a server's `Retry-After`.

```toml
[delete]
interval = 3.0
confirm = true
batch_size = 25          # new confirmed successes; 0 disables proactive pauses
batch_pause = 120.0
max_retries = 3          # actual safe action retries; does not include unsent 429 deferrals
retry_delay = 60.0
retry_max_delay = 900.0  # never shortens Retry-After
retry_max_wait = 1800.0
max_cooldowns = 3        # independent rate-limit cooldown budget; 0 stops immediately on a limit
cooldown_delay = 120.0   # doubles on recurring limits, up to the backoff cap
rate_limit_interval = 5.0 # minimum item interval after the first limit in a run
ancillary_rate_limit_mode = "global" # opt-in alternative: "isolate-known"
```


| TOML `[delete]` key | CLI option | Unit | Default |
|---|---|---|---:|
| `interval` | `--interval` | seconds between items | 3 |
| `confirm` | recipe workflow only; direct `delete` uses explicit `--yes` | boolean | true |
| `batch_size` | `--batch-size` | new confirmed successes | 25 |
| `batch_pause` | `--batch-pause` | seconds | 120 |
| `max_retries` | `--max-retries` | additional safe action retries per item | 3 |
| `retry_delay` | `--retry-delay` | seconds | 60 |
| `retry_max_delay` | `--retry-max-delay` | seconds | 900 |
| `retry_max_wait` | `--retry-max-wait` | seconds per batch | 1800 |
| `max_cooldowns` | `--max-cooldowns` | rate cooldowns across the whole batch | 3 |
| `cooldown_delay` | `--cooldown-delay` | seconds | 120 |
| `rate_limit_interval` | `--rate-limit-interval` | minimum item spacing in seconds | 5 |
| `ancillary_rate_limit_mode` | `--ancillary-rate-limit-mode` | global / isolate-known | global |

`global` preserves existing behavior and is the default for old TOML, newly generated recipes,
the wizard and GUI. To opt in for one source CLI invocation, add
`--ancillary-rate-limit-mode isolate-known` to `delete` or `run`. To retain that selection,
set `ancillary_rate_limit_mode = "isolate-known"` inside the existing `[delete]` table.
No existing TOML is rewritten and an unchanged wizard recipe remains `global`.
This Unreleased mode is awaiting independent re-review of its stop/storage corrections.
Keep previews in `--dry-run`; live activation is withheld pending that review.

The opt-in mode quarantines only HTTPS GETs on `x.com` / `twitter.com` at
`/i/api/graphql/<hash>/ViewerBadgeCounts` and `/i/api/fleets/v1/fleetline?only_spaces=true`
after their HTTP 420/429. It blocks further matching browser requests locally until the
context closes, even after the retry deadline; it never fabricates a successful response or
automatically retries that endpoint. Other fleetline paths/queries, non-GETs, unknown endpoints,
target reads and mutations retain global handling. A visible target, normal controls,
acknowledged mutation and DOM completion are still required; a missing dependency stops safely.
This classification does not establish independent server quotas or guarantee deletion is allowed.

Reliable routing requires a new context with Service Workers blocked. Opt-in CLI starts that
context; a locked action journal with durable deadline storage is also required. Missing storage,
existing contexts or routing-install failures fall back to `global`. Playwright routing
also disables the context's HTTP cache. An abort failure or 128 locally suppressed requests
stops conservatively and preserves the deadline. Already-sent responses remain observable;
diagnostic origin IDs are retained, with quarantine/suppression/fallback events added.
Direct deletion 420/429, GraphQL code88, authentication/lock errors, ambiguous results and
cancellation retain their stops and retry limits. Confirmed successes remain recorded.
An unreadable or non-object mutation JSON response is ambiguous, even after apparent DOM completion;
it is never recorded as success or automatically retried. Readable objects retain the existing
no-error plus DOM verification; the complete server response schema is not validated.
Once any endpoint is quarantined, a later required-view/control failure stops immediately,
including when local aborts produce no further server 429. No target reload retry is issued.
Only the post-action batch scope uses isolation; startup, home and collection retain global handling.

The affected endpoint's deadline is synchronously saved when the 429 is observed, before allowing
continuation. POSIX writes sync the data and parent directory; a durable journal lock protects
the interval before the write completes. The deadline is at least `cooldown_delay` or its longer numeric/date
`Retry-After`; `retry_max_delay` never shortens it. Isolation does not spend a wait budget on
a wait that did not happen. After a limit, normal item spacing retains the configured floor.
At completion, cancellation or error, an unexpired deadline is saved in `action-progress.json`
and the context stays offline. Restarting before it expires is refused, including in `global`
mode. Other global cooldowns still obey `max_cooldowns` and `retry_max_wait`.
`max_cooldowns = 0` retains global handling for every limit, including in opt-in mode.
If storage fails, the batch cancels and retains the journal lock, blocking a fresh browser start
even if the deadline could not be written. Stop all jobs and review storage/the journal before
repairing that lock; the program never automatically removes a retained failure lock. Network
control failure installs cancellation/global refusal first, then attempts offline mode and context
close. Failed offline transitions are not reported as successful; further actions remain disabled.

A local preview of a reviewed small list in Nushell is:

```nu
uv run kusamushiri-cli delete "reviewed-small.csv" --recipe "kusamushiri-cleanup.toml" --profile "YOUR_PROFILE" --ancillary-rate-limit-mode isolate-known --dry-run
```

Keep `--dry-run` while independent re-review is pending. Existing saved diagnostics can be
exported with `uv run kusamushiri-cli diagnostics | save -f kusamushiri-diagnostic.jsonl`.

`max_cooldowns` accumulates across the whole batch; successful recovery and further successes
do not reset it. `max_retries` counts safe action retries for each item; unsent rate deferrals
do not consume that counter. Saved-wait resumes count toward `retry_max_wait`. Ordinary item
spacing and rate-free proactive pauses do not; a proactive pause that receives a rate limit does.
`rate_limit_interval` is a floor: a configured longer interval is never shortened after a limit.

Retry/cooldown counts must be integers 0–20; `batch_size` must be an integer 0–10000.
`interval` must be finite and nonnegative (0 removes ordinary spacing); delays, the wait budget
and post-limit floor must be finite and positive. The backoff cap must be at least both initial delays. `max_retries = 0` disables actual action retries; use `max_cooldowns = 0`
to stop immediately on a rate limit. Neither permits traffic before a known cooldown deadline.

Tune conservatively: retain/increase the item interval and quiet pause if limits recur, and stop
when cooldowns exhaust. Increasing counts alone does not establish that a limit has cleared.
Defaults do not promise a safe throughput or quota window. Endpoint classification never asserts
quota independence, and no requests are routed through another account or address.

The journal is `action-progress.json` inside `chromium-profile-<profile>`. It contains IDs, action
kinds, receipt states, a completion counter, cooldown reason and deadline; no cookies, tokens or
post text. Legacy deleted-ID receipts are honored. `inflight` is written before attempting an action;
a crash there remains uncertain. A returned unsent rate deferral becomes `pending` and is resumable.
An atomic write and local `.lock` protect receipts; do not run an older app concurrently.

Uncertain, authentication and permanent receipts remain held across restarts. After individually
verifying the item on X and fixing login/permissions as needed, stop all jobs using that profile and
back up the journal. For a confirmed completed item change only its `kind:postID` entry to `success`;
for a verified unsubmitted/refused item that you explicitly want to retry, remove only its entry.
Preserve the cooldown deadline and all other receipts. Never reset the whole journal to retry a batch.
Remove a crash-left `.lock` only after verifying no job uses the profile; `inflight` still requires
verification. Corrupt/mismatched journals stop execution before an action. Keep private profiles/logs
out of bug reports; rate diagnostics log safe endpoint roles, normalized waits and deadlines.

### Reading a stop at 33 or 66 successes

Those totals are diagnostic examples; they do not establish X's server-side quota values. In one observed pattern, ancillary
`fleetline` 429 occurred after 33 successes. The batch used its three 120/240/480-second cooldowns,
then resumed and reached 66 successes. Another 429 before item 67 stopped the batch because all
three cooldowns were already used; successful recovery did not reset the batch-wide counter.
Item 67 was recorded as `pending`/unprocessed, not a failed or uncertain deletion. The earlier
33-success stop involved repeated pre-submit deferrals; these totals do not establish quota independence.

Read the exact logged reason: action retry exhaustion, cooldown count exhaustion and total wait
budget exhaustion are different stops. The default three cooldowns total 840 seconds, so count
exhaustion can occur before the 1800-second budget. A saved future deadline can be the app's
next backoff estimate when no usable `Retry-After` was received; it is not proof that X will clear
the limit then. Diagnose with safe endpoint roles/reasons and receipt states, not the success count alone.
Tune one pacing value at a time (for example 5 to 8 seconds between items), retain the stop limits,
and review the next manual observation. Increasing retries or repeatedly restarting does not verify
that a limit has cleared; a new explicit batch still respects old receipts/deadlines.

</details>

## Limitations

- Changes to X's web UI can break the tool. Please open an issue if it stops working.
- Deleting many posts quickly may trigger rate limits; keep a reasonable interval.
- Do not interact with the automated browser window while it is running.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)


### Lower-amplification batches and actionable diagnostics

HTTP403 alone is reported as `blocked`, with its request role and provenance, rather than
as proof of expired login. A401 or a bounded JSON error envelope containing only existing
known authentication/account codes can support the `auth` category. Both categories stop
and remain held in the journal; this grants no endpoint exemption or automatic retry.
Existing `auth` receipts are retained without reclassification or removal.
`diagnostics --summary` includes `http_errors`: status, role, fixed category, fixed known
error codes, the existing per-trace group, known catalog names and `operation_sha256`.
Unknown operation names are not printed. The digest hashes only a canonical GraphQL operation
token or a REST basename, excluding query/hash IDs, full routes, credentials and user content.
REST basenames can collide across routes; the digest is an identity aid, not a quota bucket.
It cannot recover the missing names from old g34/g25 records. Only declared JSON API403
error envelopes from completed requests, up to 8192 actual bytes, are parsed for fixed codes; messages/bodies are never
retained or logged. Missing, oversized or malformed evidence stays blocked and unconfirmed.
An unfinished body is never awaited for diagnostic enrichment. Later action evidence can
link completed error-code evidence to the original anonymous response ID.
Check login manually when a401/known auth code is recorded. An unknown403 may instead be
an endpoint permission failure; login failure has not been established. A manual browser
check is useful for the missing permission/operation context, not a reason to reset journals.
`held_receipt` with `held_receipts` in the summary identifies a saved failure encountered
without execution. The same previously held item produces no fresh endpoint evidence;
an old `auth` receipt is not proof of a new login failure and is never cleared automatically.

Rate/cancellation checks now run before navigation and before opening reversible menus.
Only a positively unsent **post deletion** blocked exclusively by the exact known
ViewerBadgeCounts/Spaces fleetline GETs can reuse its idle target view after the full cooldown.
The same page, URL identity, unique visible owned article, exact connected DOM node and absence
of dialogs/menus are required. Unknown, target and mutation limits never qualify. A changed view
stays pending and stops without a reload or mutation. This is automatic; no extra configuration key.

The saved 48-success pattern still stops at 48 in an offline persistent-unknown replay: all 452
remaining rows stay pending, while eight pointless menu clicks are eliminated. A synthetic known
ancillary episode reduces two navigations/six target reads to one navigation/three reads, with
the original cooldown and exactly one mutation. These are local measurements, not a live throughput guarantee.

Diagnostics add fixed resource types, request families, per-trace endpoint groups and a closed
catalog of API-name labels. All unrecognized names stay anonymous. A label/group does not change
the unknown request's global stop policy or identify a server quota bucket. Full URLs, queries,
post IDs, credentials, payloads and unknown operation-name strings remain excluded. Source fingerprint
`package_source_sha256` hashes local package files at trace creation; it is not a proof of hot-loaded
in-memory code. Existing older traces cannot recover metadata that was never recorded.

```nu
uv run --offline --no-sync kusamushiri-cli diagnostics --summary | save -f kusamushiri-diagnostic-summary.json
uv run --offline --no-sync kusamushiri-cli diagnostics --max-events 1000 | save -f kusamushiri-diagnostic.jsonl
```

The summary uses all **available** rotated-log events and reports whether a trace start survives.
It distinguishes pending deferrals, actual safe retries, cooldowns, wait time and stop reason.
Unexpected faults keep confirmed partial results; uncertain current submissions are never retried,
and CLI failed/unprocessed output is still written. External cancellation also sets the core stop flag.
Retry-After is preserved as a deadline instead of being restarted by repeated bookkeeping calls.
Authentication observations stop both global and isolated batches. Foreign/GET/noncanonical or
multiple mutation requests cannot provide a confirmed batch receipt. Malformed `errors` envelopes
are ambiguous. Readable objects without errors still use the existing HTTP + DOM checks;
complete DeleteTweet acknowledgement schema validation has not been inferred or added.
