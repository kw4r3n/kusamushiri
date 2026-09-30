# Contributing

Issues and pull requests are welcome, in English or Japanese.

## Setup

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv sync                                  # Python 3.11 + runtime and dev dependencies
uv run playwright install chromium       # Chromium for the local DOM tests
```

## Checks

```bash
uv run ruff check
uv run pyright
uv run pytest -q        # full suite (offscreen Qt + local Chromium DOM tests)
```

The tests never access X; DOM tests run against local HTML fixtures. To keep your real
profiles and settings untouched, point the XDG directories at a temporary location:

```bash
export XDG_CONFIG_HOME=$(mktemp -d) XDG_DATA_HOME=$(mktemp -d) XDG_STATE_HOME=$(mktemp -d)
uv run pytest -q -p no:cacheprovider
```

The DOM tests use short timeouts; avoid running other Chromium workloads in parallel.

## Building the app

```bash
uv run --group build pyinstaller --noconfirm --clean packaging/kusamushiri.spec
```

This writes `dist/kusamushiri` (and `kusamushiri.app` on macOS). Pushing a `v*` tag builds
Windows, macOS and Linux archives in GitHub Actions and creates a draft release.

## Conventions

- [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:` …)
- Add a line to `CHANGELOG.md` under *Unreleased* for user-visible changes.
- Never include real account data, cookies or logs with personal posts in issues, tests or fixtures.
