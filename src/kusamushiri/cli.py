"""Command-line interface for semi-automated cleanup without the Qt window.

Posts and follows travel between subcommands as the same CSV / JSON files the GUI exports,
so a list can be collected, reviewed or edited, and then passed to a delete/unfollow step.
"""

import argparse
import csv
import json
import logging
import signal
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import FrameType
from typing import TypeVar

from kusamushiri.actions import action_label_for
from kusamushiri.archive import ArchiveError, load_archive_posts, select_archive_posts
from kusamushiri.core import XDeleterCore
from kusamushiri.deleted_posts import DeletedPostStore
from kusamushiri.exporting import EXPORT_SUFFIXES, write_post_list, write_records
from kusamushiri.follows import DEFAULT_UNFOLLOW_INTERVAL_SECONDS, FollowRecord, write_follow_list
from kusamushiri.i18n import set_language
from kusamushiri.last_posts import (
    DEFAULT_LAST_POST_INTERVAL_SECONDS,
    LastPostResult,
    LastPostStore,
    apply_last_post_entries,
)
from kusamushiri.logger import logger
from kusamushiri.models import (
    DEFAULT_ACTION_INTERVAL_SECONDS,
    CollectRequest,
    ExecuteActionsRequest,
    PostActionTarget,
    PostKind,
    parse_keywords,
)
from kusamushiri.parsing import extract_post_id
from kusamushiri.paths import configure_frozen_browser_path, migrate_legacy_app_data

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
DEFAULT_MAX_POSTS = 50
DEFAULT_LOGIN_TIMEOUT_SECONDS = 600.0
# X renders the home timeline after the page load event; give a saved session time to show up.
LOGIN_SETTLE_SECONDS = 15.0
LOGIN_POLL_SECONDS = 2.0
POST_KINDS: frozenset[str] = frozenset({"post", "repost", "like"})
TRUE_TEXTS = frozenset({"true", "1", "yes"})

_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


class CliError(Exception):
    """A failure reported as one line on stderr with a non-zero exit code."""

    def __init__(self, message: str, exit_code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _info(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


_progress_width = 0


def _progress(message: str) -> None:
    # Overwrite one status line on a terminal; stay quiet when stderr is redirected.
    # Pad with spaces instead of ANSI codes so a plain Windows console shows it correctly.
    global _progress_width
    if sys.stderr.isatty():
        print(f"\r{message.ljust(_progress_width)}", end="", file=sys.stderr, flush=True)
        _progress_width = len(message)


def _end_progress() -> None:
    global _progress_width
    if sys.stderr.isatty() and _progress_width:
        print(file=sys.stderr, flush=True)
    _progress_width = 0


def _configure_console_logging(verbose: bool) -> None:
    # The logger writes INFO to stdout for the GUI; keep stdout clean and terse here.
    for handler in logger.handlers:
        if type(handler) is logging.StreamHandler:
            handler.stream = sys.stderr  # setStream() would flush a stream that may be closed
            handler.setLevel(logging.INFO if verbose else logging.WARNING)


def _output_path(text: str) -> Path:
    path = Path(text)
    if path.suffix.lower() not in EXPORT_SUFFIXES:
        raise argparse.ArgumentTypeError("output file must end with .csv or .json")
    return path


def _non_negative_float(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return value


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD") from error


def read_rows(path: Path) -> Sequence[Mapping[str, object]]:
    """Read a CSV or JSON list written by the GUI or this CLI."""
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
                raise CliError(f"{path}: expected a JSON array of objects")
            return data
        with path.open(encoding="utf-8-sig", newline="") as file:
            return list(csv.DictReader(file))
    except (OSError, ValueError) as error:
        raise CliError(f"{path}: {error}") from error


def _text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    return "" if value is None else str(value).strip()


def _flag(row: Mapping[str, object], field: str) -> bool:
    value = row.get(field)
    return value if isinstance(value, bool) else _text(row, field).casefold() in TRUE_TEXTS


def post_targets_from_rows(rows: Sequence[Mapping[str, object]]) -> list[PostActionTarget]:
    targets: list[PostActionTarget] = []
    for number, row in enumerate(rows, start=1):
        url = _text(row, "url")
        kind = _text(row, "kind") or "post"
        if not url:
            raise CliError(f"row {number}: missing url")
        if kind not in POST_KINDS:
            raise CliError(f"row {number}: kind must be post, repost or like (got {kind!r})")
        post_kind: PostKind = kind  # type: ignore[assignment]
        targets.append(PostActionTarget(url=url, kind=post_kind))
    return targets


def follow_records_from_rows(rows: Sequence[Mapping[str, object]]) -> list[FollowRecord]:
    records: list[FollowRecord] = []
    for number, row in enumerate(rows, start=1):
        username = _text(row, "username").removeprefix("@")
        if not username:
            raise CliError(f"row {number}: missing username")
        records.append(
            FollowRecord(
                username=username,
                display_name=_text(row, "display_name"),
                profile_url=_text(row, "profile_url"),
                follows_you=_flag(row, "follows_you"),
            )
        )
    return records


def build_collect_request(args: argparse.Namespace, username: str) -> CollectRequest:
    kind = args.kind
    return CollectRequest(
        username=username.strip().removeprefix("@"),
        max_posts=args.max_posts,
        media_filter=args.media,
        is_reply=args.replies_only,
        min_likes=args.min_likes,
        min_replies=args.min_replies,
        # Likes are listed only on the profile's likes timeline, never via search.
        search_mode="profile" if kind == "likes" else args.mode,
        post_kind_filter=kind,
        since_date=args.since,
        until_date=args.until,
        include_keywords=parse_keywords(args.include),
        exclude_keywords=parse_keywords(args.exclude),
    )


def _validate(request: CollectRequest) -> None:
    try:
        request.validate()
    except ValueError as error:
        raise CliError(str(error), EXIT_USAGE) from error


def _wait_for_login(core: XDeleterCore, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if core.is_logged_in():
            return True
        if core.is_cancel_requested() or time.monotonic() >= deadline:
            return False
        core.wait_for_cancel(LOGIN_POLL_SECONDS)


@contextmanager
def browser_session(
    profile: str | None,
    *,
    headless: bool,
    login_timeout: float | None = None,
    not_logged_in_message: str | None = None,
) -> Iterator[XDeleterCore]:
    """Start the browser on a saved profile, require a login, and stop it on exit.

    The first Ctrl+C asks the running batch to stop after the current item; a second one aborts.
    """
    core = XDeleterCore(headless=headless)

    def on_interrupt(_signum: int, _frame: FrameType | None) -> None:
        if core.is_cancel_requested():
            raise KeyboardInterrupt
        core.request_cancel()
        _end_progress()
        _info("Stopping after the current item (press Ctrl+C again to abort).")

    previous_handler = signal.signal(signal.SIGINT, on_interrupt)
    try:
        _info("Starting the browser (the first run downloads Chromium)...")
        core.start_browser(account_name=profile)
        core.go_to_home()
        if not _wait_for_login(core, LOGIN_SETTLE_SECONDS if login_timeout is None else login_timeout):
            if core.is_cancel_requested():
                raise CliError("Interrupted.", EXIT_INTERRUPTED)
            hint = f" --profile {profile}" if profile else ""
            raise CliError(not_logged_in_message or f"Not logged in to X. Run `kusamushiri-cli login{hint}` first.")
        yield core
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        core.stop_browser()


def _confirm(prompt: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise CliError("Refusing to run without confirmation; pass --yes.", EXIT_USAGE)
    answer = input(f"{prompt} Type 'yes' to continue: ")
    if answer.strip().casefold() != "yes":
        raise CliError("Cancelled.", EXIT_FAILURE)


def _run_batch(
    core: XDeleterCore,
    items: Sequence[_Item],
    interval_seconds: float,
    run: Callable[[_Item], _Result],
    describe: Callable[[_Item], str],
) -> list[_Result]:
    """Run items one by one with a pause between them, stopping when cancellation is requested."""
    results: list[_Result] = []
    for index, item in enumerate(items, start=1):
        if core.is_cancel_requested():
            break
        _progress(f"[{index}/{len(items)}] {describe(item)}")
        results.append(run(item))
        if interval_seconds > 0 and index < len(items) and core.wait_for_cancel(interval_seconds):
            break
    _end_progress()
    return results


def cmd_login(args: argparse.Namespace) -> int:
    # Signing in needs a visible window, so --headless never applies here.
    with browser_session(
        args.profile,
        headless=False,
        login_timeout=args.timeout,
        not_logged_in_message=f"No sign-in was detected within {args.timeout:g} seconds.",
    ) as core:
        username = core.get_logged_in_username()
        _info(f"Logged in as @{username}." if username else "Logged in.")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    if args.user:
        _validate(build_collect_request(args, args.user))
    with browser_session(args.profile, headless=args.headless) as core:
        username = args.user or core.get_logged_in_username() or ""
        request = build_collect_request(args, username)
        _validate(request)
        posts = core.search_and_collect_posts(
            request,
            on_progress=lambda scanned, current, total: _progress(f"scanned {scanned}, matched {current}/{total}"),
        )
        _end_progress()
        cancelled = core.is_cancel_requested()
        limit_reached = core.collection_limit_reached
    write_post_list(args.output, posts)
    _info(f"Wrote {len(posts)} posts to {args.output}.")
    if limit_reached:
        _info("Stopped at the collection safety limit; the list may be incomplete.")
    return EXIT_INTERRUPTED if cancelled else 0


def cmd_archive(args: argparse.Namespace) -> int:
    request = build_collect_request(args, args.user or "")
    # Archive filtering ignores the account ID, so only check the other conditions.
    _validate(replace(request, username=request.username or "i"))
    try:
        posts = load_archive_posts(
            args.archive,
            fallback_username=args.user,
            on_progress=lambda done, total: _progress(f"reading archive {done}/{total}"),
        )
    except ArchiveError as error:
        raise CliError(str(error)) from error
    finally:
        _end_progress()
    deleted_ids = DeletedPostStore.for_account(args.profile).load()
    selection = select_archive_posts(posts, request, deleted_ids, oldest_first=args.oldest_first)
    write_post_list(args.output, selection.posts)
    _info(
        f"Wrote {len(selection.posts)} of {selection.total} archived posts to {args.output} "
        f"(skipped {selection.skipped_reposts} reposts, {selection.skipped_deleted} already deleted)."
    )
    return 0


def _write_failed_rows(path: Path | None, rows: Sequence[Mapping[str, object]]) -> None:
    if path is None or not rows:
        return
    fields = list(rows[0].keys())
    write_records(path, fields, [{field: row.get(field) for field in fields} for row in rows])
    _info(f"Wrote {len(rows)} failed rows to {path}.")


def cmd_delete(args: argparse.Namespace) -> int:
    rows = read_rows(args.file)
    targets = post_targets_from_rows(rows)
    request = ExecuteActionsRequest(targets=targets, interval_seconds=args.interval)
    try:
        request.validate()
    except ValueError as error:
        raise CliError(str(error), EXIT_USAGE) from error
    if args.dry_run:
        for target in targets:
            print(f"{action_label_for(target.kind)}\t{target.url}")
        _info(f"{len(targets)} items (dry run, nothing changed).")
        return 0
    _confirm(f"Delete or undo {len(targets)} items listed in {args.file}? This cannot be undone.", args.yes)

    with browser_session(args.profile, headless=args.headless) as core:
        results = _run_batch(
            core,
            targets,
            args.interval,
            core.execute_post_action,
            lambda target: f"{action_label_for(target.kind)} {target.url}",
        )
        cancelled = core.is_cancel_requested()

    deleted_ids = [
        extract_post_id(result.target.url) for result in results if result.success and result.target.kind == "post"
    ]
    if deleted_ids:
        try:
            DeletedPostStore.for_account(args.profile).record(deleted_ids)
        except OSError as error:
            _info(f"Could not save the deleted post IDs: {error}")

    failed_indexes = [index for index, result in enumerate(results) if not result.success]
    for index in failed_indexes:
        result = results[index]
        _info(f"failed: {result.target.url}: {result.error_message or 'unknown error'}")
    succeeded = len(results) - len(failed_indexes)
    _info(f"{succeeded}/{len(targets)} succeeded" + (" (stopped early)." if cancelled else "."))
    _write_failed_rows(args.failed_output, [rows[index] for index in failed_indexes])
    if cancelled:
        return EXIT_INTERRUPTED
    return EXIT_FAILURE if failed_indexes else 0


def cmd_following(args: argparse.Namespace) -> int:
    with browser_session(args.profile, headless=args.headless) as core:
        username = (args.user or core.get_logged_in_username() or "").strip().removeprefix("@")
        if not username:
            raise CliError("Could not tell the logged-in account; pass --user.", EXIT_USAGE)
        result = core.collect_following(username, on_progress=lambda count: _progress(f"collected {count}"))
        _end_progress()
        cancelled = core.is_cancel_requested()
    if cancelled:
        _info("Stopped; no file was written.")
        return EXIT_INTERRUPTED
    records = result.records
    if args.skip_mutual:
        records = [record for record in records if not record.follows_you]
    write_follow_list(args.output, apply_last_post_entries(records, LastPostStore.for_account(args.profile).load()))
    _info(f"Wrote {len(records)} accounts to {args.output}.")
    if result.limit_reached:
        _info("Stopped at the collection safety limit; the list may be incomplete.")
    return 0


def _read_follow_targets(args: argparse.Namespace) -> list[FollowRecord]:
    records = follow_records_from_rows(read_rows(args.file))
    if getattr(args, "skip_mutual", False):
        records = [record for record in records if not record.follows_you]
    if not records:
        raise CliError("No accounts to process.", EXIT_USAGE)
    return records


def cmd_unfollow(args: argparse.Namespace) -> int:
    records = _read_follow_targets(args)
    if args.dry_run:
        for record in records:
            print(f"@{record.username}")
        _info(f"{len(records)} accounts (dry run, nothing changed).")
        return 0
    _confirm(f"Unfollow {len(records)} accounts listed in {args.file}?", args.yes)

    with browser_session(args.profile, headless=args.headless) as core:
        results = _run_batch(
            core,
            records,
            args.interval,
            lambda record: (record, *core.unfollow_account(record.username)),
            lambda record: f"unfollow @{record.username}",
        )
        cancelled = core.is_cancel_requested()

    failed = [(record, error) for record, success, error in results if not success]
    for record, error in failed:
        _info(f"failed: @{record.username}: {error or 'unknown error'}")
    _info(f"{len(results) - len(failed)}/{len(records)} succeeded" + (" (stopped early)." if cancelled else "."))
    if cancelled:
        return EXIT_INTERRUPTED
    return EXIT_FAILURE if failed else 0


def cmd_last_posts(args: argparse.Namespace) -> int:
    records = _read_follow_targets(args)
    with browser_session(args.profile, headless=args.headless) as core:
        results: list[LastPostResult] = _run_batch(
            core,
            records,
            args.interval,
            lambda record: core.fetch_last_post(record.username),
            lambda record: f"checking @{record.username}",
        )
        cancelled = core.is_cancel_requested()

    entries = LastPostStore.for_account(args.profile).record(results)
    for result in results:
        if not result.success:
            _info(f"failed: @{result.username}: {result.error_message}")
    succeeded = sum(1 for result in results if result.success)
    _info(f"{succeeded}/{len(records)} checked" + (" (stopped early)." if cancelled else "."))
    if args.output is not None:
        write_follow_list(args.output, apply_last_post_entries(records, entries))
        _info(f"Wrote {len(records)} accounts to {args.output}.")
    if cancelled:
        return EXIT_INTERRUPTED
    return EXIT_FAILURE if succeeded < len(results) else 0


def _add_profile_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", help="saved browser profile (same names as the GUI; default profile if omitted)")


def _add_browser_args(parser: argparse.ArgumentParser) -> None:
    _add_profile_arg(parser)
    parser.add_argument(
        "--headless", action="store_true", help="run Chromium without a window (sign in with `login` first)"
    )


def _add_filter_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("filters")
    group.add_argument("--user", help="account ID to collect from (default: the logged-in account)")
    group.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS, help="maximum posts (default: %(default)s)")
    group.add_argument("--kind", choices=("posts", "reposts", "all", "likes"), default="posts")
    group.add_argument(
        "--mode", choices=("profile", "search"), default="profile", help="collect from the profile or via search"
    )
    group.add_argument("--media", choices=("all", "with_media", "without_media"), default="all")
    group.add_argument("--replies-only", action="store_true")
    group.add_argument("--min-likes", type=int, default=0)
    group.add_argument("--min-replies", type=int, default=0)
    group.add_argument("--since", type=_iso_date, help="YYYY-MM-DD, inclusive")
    group.add_argument("--until", type=_iso_date, help="YYYY-MM-DD, inclusive")
    group.add_argument("--include", default="", help="comma-separated keywords; a post must contain one")
    group.add_argument("--exclude", default="", help="comma-separated keywords; posts containing one are skipped")


def _add_confirm_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="list the targets without opening the browser")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kusamushiri-cli",
        description="Collect, review and delete X posts or follows from the command line.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show log messages on stderr")
    parser.add_argument("--lang", choices=("ja", "en"), default="ja", help="language of validation messages")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    login = commands.add_parser("login", help="open a browser window and wait for you to sign in")
    _add_profile_arg(login)
    login.add_argument(
        "--timeout",
        type=_non_negative_float,
        default=DEFAULT_LOGIN_TIMEOUT_SECONDS,
        help="seconds to wait for the sign-in (default: %(default)s)",
    )
    login.set_defaults(handler=cmd_login)

    collect = commands.add_parser("collect", help="collect matching posts into a CSV/JSON file")
    _add_browser_args(collect)
    _add_filter_args(collect)
    collect.add_argument("-o", "--output", type=_output_path, required=True, help="output .csv or .json")
    collect.set_defaults(handler=cmd_collect)

    archive = commands.add_parser("archive", help="select posts from an X data archive into a CSV/JSON file")
    archive.add_argument("archive", type=Path, help="archive .zip, extracted folder or tweets.js")
    _add_profile_arg(archive)
    _add_filter_args(archive)
    archive.add_argument("--oldest-first", action="store_true", help="pick the oldest matching posts first")
    archive.add_argument("-o", "--output", type=_output_path, required=True, help="output .csv or .json")
    archive.set_defaults(handler=cmd_archive)

    delete = commands.add_parser("delete", help="delete posts, undo reposts or remove likes listed in a file")
    delete.add_argument("file", type=Path, help="CSV/JSON from collect, archive or the GUI (needs url and kind)")
    _add_browser_args(delete)
    _add_confirm_args(delete)
    delete.add_argument(
        "--interval",
        type=_non_negative_float,
        default=DEFAULT_ACTION_INTERVAL_SECONDS,
        help="seconds between items (default: %(default)s)",
    )
    delete.add_argument("--failed-output", type=_output_path, help="write rows that failed here for a retry")
    delete.set_defaults(handler=cmd_delete)

    following = commands.add_parser("following", help="export the accounts you follow to a CSV/JSON file")
    _add_browser_args(following)
    following.add_argument("--user", help="account ID (default: the logged-in account)")
    following.add_argument("--skip-mutual", action="store_true", help="leave out accounts that follow you back")
    following.add_argument("-o", "--output", type=_output_path, required=True, help="output .csv or .json")
    following.set_defaults(handler=cmd_following)

    unfollow = commands.add_parser("unfollow", help="unfollow the accounts listed in a file")
    unfollow.add_argument("file", type=Path, help="CSV/JSON from `following` or the GUI (needs username)")
    _add_browser_args(unfollow)
    _add_confirm_args(unfollow)
    unfollow.add_argument("--skip-mutual", action="store_true", help="leave out accounts that follow you back")
    unfollow.add_argument(
        "--interval",
        type=_non_negative_float,
        default=DEFAULT_UNFOLLOW_INTERVAL_SECONDS,
        help="seconds between accounts (default: %(default)s)",
    )
    unfollow.set_defaults(handler=cmd_unfollow)

    last_posts = commands.add_parser("last-posts", help="fetch the last post date of the accounts listed in a file")
    last_posts.add_argument("file", type=Path, help="CSV/JSON from `following` or the GUI (needs username)")
    _add_browser_args(last_posts)
    last_posts.add_argument(
        "--interval",
        type=_non_negative_float,
        default=DEFAULT_LAST_POST_INTERVAL_SECONDS,
        help="seconds between accounts (default: %(default)s)",
    )
    last_posts.add_argument("-o", "--output", type=_output_path, help="write the list with the dates added")
    last_posts.set_defaults(handler=cmd_last_posts)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_console_logging(args.verbose)
    set_language(args.lang)
    if migrate_legacy_app_data():
        logger.info("Moved profiles from the previous xposdeleter data directory.")
    configure_frozen_browser_path()
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except CliError as error:
        _end_progress()
        _info(f"kusamushiri-cli: {error}")
        return error.exit_code
    except KeyboardInterrupt:
        _end_progress()
        _info("kusamushiri-cli: aborted.")
        return EXIT_INTERRUPTED
    except Exception as error:
        if args.verbose:
            raise
        _end_progress()
        # The traceback goes to the log file only; -v shows it on the terminal.
        logger.debug("Command %s failed.", args.command, exc_info=True)
        _info(f"kusamushiri-cli: {error or type(error).__name__} (run with -v for details)")
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
