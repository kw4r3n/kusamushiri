#!/usr/bin/env bash
set -u

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir" || exit 1

pause_if_interactive() {
    if [ -t 0 ]; then
        printf "\nPress Enter to close this window..."
        read -r _ || true
    fi
}

if ! command -v uv >/dev/null 2>&1; then
    printf "uv was not found in PATH.\n" >&2
    printf "Install uv first, then double-click this file again.\n" >&2
    printf "https://docs.astral.sh/uv/getting-started/installation/\n" >&2
    pause_if_interactive
    exit 1
fi

# uv creates the environment on first run; Chromium downloads on the first browser start.
printf "Starting Kusamushiri...\n"
if ! uv run --locked kusamushiri; then
    printf "\nKusamushiri could not start. See the messages above.\n" >&2
    pause_if_interactive
    exit 1
fi
