# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
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
- A run stops as soon as X answers with HTTP 429 (rate limit) instead of moving on to the next
  item, in both the app and the CLI.

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
