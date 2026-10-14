#!/bin/sh
set -eu
runner_root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec /home/kw4/Documents/Codex/2026-10-14/task-18/http403-staging/.venv/bin/python -B "$runner_root/loopback_fixture_runner.py" "$@"
