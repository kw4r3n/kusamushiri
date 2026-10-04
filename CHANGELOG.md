# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Export an account's following list to CSV (UTF-8 with BOM for Excel) or JSON.
- Include and exclude keyword filters. Posts are matched against their visible text, ignoring
  case and full-width/half-width differences; keywords are saved per profile.
- English user interface. Choose 日本語 or English from the language menu at the bottom of the
  window; the choice is saved and applied after a restart. Japanese remains the default.
- Save the checked posts to CSV or JSON (text, URL, date, counts) as a record before deleting.
- Load posts from an X data archive (zip or extracted folder) instead of searching, so old posts
  that search misses can be deleted too. The current filters apply; reposts in the archive are skipped.

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
