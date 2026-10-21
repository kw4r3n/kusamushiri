# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

- A held receipt (`blocked`, `ambiguous`, ...) now names the exact `resolve` command to clear it.
  The batch summary counts scheduled pauses separately from rate-limit cooldowns, and the CLI
  progress line is clipped to the terminal width so narrow terminals no longer join lines.
- Fix deleting a single checked row or retrying one failed post in the GUI: it now opens the post
  instead of failing on the current list page. Only the target-URL field acts on the open page.
- Treat profile names case-insensitively, so deleting `Default` can no longer remove the default
  profile's folder on Windows/macOS.
- Never overwrite an unreadable deleted-post file; cap `Retry-After` at one day so a bad header
  cannot block a profile for years.
- Recipe keyword lists keep each item whole (`"New York, NY"` no longer also matches `NY`); TOML
  datetimes in `since`/`until` are read as dates; `older_than_days = N` now excludes the day
  exactly N days ago.
- CLI: reject list rows without a numeric post ID before the batch starts; `unfollow` leaves out
  mutual follows unless `--include-mutual` and needs `--interval` of at least 1 second; the recipe
  delete prompt requires `yes`.
- Keep failures detected before anything is sent resumable instead of holding them as ambiguous;
  log the cause when a batch stops on an internal error; skip unchanged journal rewrites.
- Pin GitHub Actions to commit SHAs and ignore local CLI output files.

- Make `update-recipe` add all supported missing advanced fields, including
  `interval_jitter`, while preserving explicit zero values and effective behavior.

- Accept explicit zero-second client waits without restoring defaults; stop safe
  retries/cooldowns when neither a local delay nor a server deadline is available.
  Add optional `interval_jitter` / `--interval-jitter` (default 0), expose wait flags
  in CLI help, and document pacing separately from server deadlines and readiness.

- Distinguish journal-lock startup failures from network-control errors. Explicit
  `recover --legacy-owner-stopped` backs up an empty lock (legacy or an interrupted lock write) after the operator
  confirms all profile jobs stopped; receipts, deadlines and account identity remain unchanged.

- Default retry/cooldown limits now apply to each wait, without adding earlier waits. Valid server
  deadlines are never shortened and do not exhaust the fallback cooldown count; explicit
  `wait_limit_mode = "total"` preserves the previous cumulative behavior.
- Add offline `progress`, guarded dead-owner `recover`, manually verified `resolve`, and
  `resume LIST --expected-viewer ACCOUNT`. Resume waits before opening Chromium, skips successes,
  rechecks the account before mutation, and holds uncertain receipts. SIGTERM follows the same
  safe-stop path as the first Ctrl+C; abrupt interruption remains conservative.
- Recheck predicted and durable deadlines after event dispatch and reconnection, bind/check
  the verified journal account for ordinary CLI/GUI batches too, mark submission only after
  the final cancellation guard, and use crash-released POSIX advisory recovery exclusion.

- Experimental `--traffic-policy` / `[delete].traffic_policy` (`off` default, `report`, `enforce`):
  a name-based allowlist for post-action batches. `report` records a sanitized `traffic_decision`
  event per request (host class, rule, public identity); `enforce` aborts unlisted requests, admits a
  mutation only once at the confirmation click and holds/stops on any unsent failure.
- `isolate-known` also quarantines `CreatorStudioTabBarItemQuery` GraphQL GETs and `api.x.com`
  `/1.1/account/settings.json` GETs. Both load on every target page and hit HTTP 429 after about
  49 loads, which exhausted the cooldowns and stopped long delete batches.
- Verify the live session, target, viewer and pinned controls before a single-post delete;
  the GUI reuses the current view without navigation, single-post actions disable automatic
  retries and unknown outcomes stay held in the existing action journal.
- Verify delete menu dialog ancestors by ownership and retain the specific unsent control failure reason.
- Match delete menu fallback labels exactly across selection, control verification and diagnostics;
  reject ambiguous controls on every action path.
- GUI target URL entry, one-post delete and a menu diagnostic that reads an already open menu
  without clicking, or opens only the owned caret once; it never selects a menu item or uses the journal.

- `isolate-known`: keep a deadline quarantined during a pause on disk, count the 128 local
  suppression cap per item instead of per batch (retries of one item keep their count), and match only the exact `only_spaces=true` query.

- Report validated public operation names and fixed route templates in sanitized diagnostics;
  retain unavailable identities and safe digests without changing 403/429 stop or retry gates.

### Added
- HTTP403 evidence distinguishes confirmed authentication/account codes from unproven
  request blocks. All blocks remain held and stop without automatic retries. Diagnostics
  include closed error categories and operation-token digests without raw names/URLs/bodies.
- Actionable privacy-safe diagnostics: fixed resource/API-family labels, per-trace endpoint groups,
  a diagnostic-only API catalog, package source fingerprints and `diagnostics --summary`.
- Exact idle-target reuse after known-only, positively unsent deferrals; early rate/cancel gates
  avoid unnecessary menu operations. Unknown/mutation/target limits remain conservative.
- Batch stop/wait/result summaries and partial-result retention on unexpected faults; explicit
  cancellation propagation, nonrenewing Retry-After bookkeeping, global-mode auth stops and
  conservative mutation receipt host/method/path/count/error-envelope validation.
- Compact deletion templates (12 to 2 default keys) and focused tuning help (11 to 3 visible options).
  All advanced keys/flags and explicit values remain compatible; the updater does not re-add defaults.
- Correct ancillary isolation stop/storage paths: later missing views/controls stop without target
  reload retries; blank duplicate Spaces parameters and malformed URLs stay unknown; deadlines
  are stored synchronously with a durable lock and POSIX directory sync. Storage/control failures
  cancel before offline attempts and retain restart protection. Unreadable/non-object mutation
  bodies are ambiguous and never recorded as success or retried. Live opt-in awaits independent re-review.
- Opt-in `--ancillary-rate-limit-mode isolate-known` / `[delete].ancillary_rate_limit_mode`
  quarantines exact nonessential ViewerBadgeCounts and Spaces-only fleetline GETs after 420/429.
  Requires a new service-worker-blocked routed context, preserves retry deadlines on exit,
  suppresses endpoint retries until context close, and falls back/stops conservatively on missing
  control or a local retry storm. Existing recipes/wizard/GUI default to global handling;
  mutation refusals, authentication, uncertain outcomes and cancellation remain conservative.
- Privacy-safe request/response correlation and operation phases in `KS_DIAG` events,
  plus `diagnostics` to export only sanitized JSONL. Late/unmatched responses retain
  their original or unknown association; logging changes no rate-limit policy or waits.
- `update-recipe FILE [--dry-run]` adds missing concrete TOML defaults while retaining
  values, comments and legacy aliases, with a diff, exclusive backup and validated
  atomic replacement. Repeated updates are no-ops; it never enables an absent `[delete]`.
- `update-recipe --all [--dry-run]` lists and updates saved recipes only in the current
  directory, excluding artifacts/symlinks, validating all targets before writing and
  reporting any partial completion on write failure with per-file backups.
- `kusamushiri-cli`, included in the downloads, for semi-automated runs without the window.
  Started without arguments it asks for the filters once, saves them to `kusamushiri.toml`, and
  then signs in, collects, saves the list for review and asks before deleting; `run FILE` repeats it.
  Several named setups (`kusamushiri-<name>.toml`) can be kept side by side and picked at start.
  The subcommands `login`, `collect`, `archive`, `delete`, `following`, `unfollow` and `last-posts`
  pass the same CSV / JSON lists the app exports, and `--headless` runs Chromium without a window.
- Export an account's following list to CSV (UTF-8 with BOM for Excel) or JSON.
- Include and exclude keyword filters. Posts are matched against their visible text, ignoring
  case and full-width/half-width differences; keywords are saved per profile.
- English user interface. Choose 日本語 or English from the language menu at the bottom of the
  window; the choice is saved and applied after a restart. Japanese remains the default.
- Save the checked posts to CSV or JSON (text, URL, date, counts) as a record before deleting.
- Load posts from an X data archive (zip or extracted folder) instead of searching, so old posts
  that search misses can be deleted too. The current filters apply; reposts in the archive are skipped.
  Posts deleted with this app are remembered per profile and skipped when the archive is loaded again.
  Check "Load archive oldest first" to list the oldest posts first, up to the maximum count.
  Archives load in the background in about half the time and with less memory, so the window
  stays responsive with large archives.
- Undo likes. Choose "Liked posts (undo likes)" as the post type to collect your profile's Likes
  tab; date, keyword, media and minimum-like filters still apply, and checked rows are unliked.
- Bulk unfollow: "Manage follows" lists the accounts you follow, can hide mutual follows, and
  unfollows only the checked ones with a confirmation, a 10-second default interval, stop, and
  retry of failures.
- Fetch the last post date of checked followed accounts (pinned posts and reposts are ignored) and
  record when it was fetched; results are kept per profile, shown as sortable columns, and exported.

### Changed
- Post deletion/undo uses conservative defaults: 3-second item spacing and a 120-second quiet
  pause after 25 new confirmed successes, with saved explicit values preserved.
- Distinguish mutation/target-load/ancillary/unknown rate observations without assuming separate
  server quota buckets. All observed X limits trigger bounded quiet cooldowns; unsent actions remain
  pending rather than failed. Separate action retry and cooldown counts, preserve Retry-After and
  deadlines, pace remaining items after a limit, and stop on exhaustion or unsafe outcomes.
- Suspend browser networking during long waits, process queued responses before resuming, and
  block browser startup before a saved cooldown deadline. Cancellation preserves the pause/deadline.
- Expose the same nine controls in CLI, invocation-only recipe overrides and saved GUI settings.
  Atomic receipts skip successes and hold uncertain/authentication/permanent outcomes; failed-list
  exports include unprocessed rows. Other jobs continue to stop on HTTP 429.
- Reuse the existing TOML `[delete]` settings with `delete --recipe`, add `run --interval`, and
  apply explicit CLI overrides before TOML/defaults without rewriting files or safety deadlines.
  Reject nonfinite intervals before execution; direct-delete confirmation/account choices remain explicit.
- Document every tuning key/unit/default and batch-wide cooldown counting, add a commented
  dry-run-oriented TOML example, and explain diagnostic 33/66-success stops without quota claims.

## [0.3.0] - 2026-10-01

First public release.

### Added
- Prebuilt apps for Windows, macOS and Linux on GitHub Releases.
- Chromium is downloaded automatically on the first browser start.
- English README, MIT license and contribution guide.

### Changed
- Renamed from *xposdeleter* to *kusamushiri*. Profiles and settings saved by xposdeleter are
  moved or copied automatically on first launch.
- Source installs use uv and the smaller `PySide6-Essentials` package.
