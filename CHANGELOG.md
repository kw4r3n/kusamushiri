# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Export an account's following list to CSV (UTF-8 with BOM for Excel) or JSON.

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
