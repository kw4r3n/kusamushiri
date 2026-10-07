"""Command-line interface for semi-automated cleanup without the Qt window.

Posts and follows travel between subcommands as the same CSV / JSON files the GUI exports,
so a list can be collected, reviewed or edited, and then passed to a delete/unfollow step.
`run` chains those steps from a saved recipe; starting with no arguments opens a wizard.
"""

import argparse
import csv
import json
import logging
import signal
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path
from types import FrameType
from typing import TypeVar

from kusamushiri.actions import action_label_for
from kusamushiri.archive import ArchiveError, load_archive_posts, select_archive_posts
from kusamushiri.core import XDeleterCore
from kusamushiri.deleted_posts import DeletedPostStore
from kusamushiri.exporting import EXPORT_SUFFIXES, write_post_list, write_records
from kusamushiri.follows import DEFAULT_UNFOLLOW_INTERVAL_SECONDS, FollowRecord, write_follow_list
from kusamushiri.i18n import set_language, tr
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
    PostActionResult,
    PostActionTarget,
    PostKind,
    PostRecord,
    parse_keywords,
)
from kusamushiri.parsing import extract_post_id
from kusamushiri.paths import configure_frozen_browser_path, migrate_legacy_app_data
from kusamushiri.recipe import (
    DEFAULT_MAX_POSTS,
    DEFAULT_RECIPE_NAME,
    CollectOptions,
    Recipe,
    RecipeError,
    load_recipe,
    run_wizard,
)

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
DEFAULT_LOGIN_TIMEOUT_SECONDS = 600.0
# X renders the home timeline after the page load event; give a saved session time to show up.
LOGIN_SETTLE_SECONDS = 15.0
LOGIN_POLL_SECONDS = 2.0
PREVIEW_ROWS = 5
PREVIEW_TEXT_LENGTH = 50
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


def _non_negative_int(text: str) -> int:
    value = int(text)
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


def collect_options_from_args(args: argparse.Namespace) -> CollectOptions:
    return CollectOptions(
        user=args.user,
        kind=args.kind,
        mode=args.mode,
        media=args.media,
        replies_only=args.replies_only,
        min_likes=args.min_likes,
        min_replies=args.min_replies,
        since=args.since,
        until=args.until,
        older_than_days=args.older_than,
        include=parse_keywords(args.include),
        exclude=parse_keywords(args.exclude),
        max_posts=args.max_posts,
        oldest_first=getattr(args, "oldest_first", False),
    )


def build_collect_request(args: argparse.Namespace, username: str) -> CollectRequest:
    return collect_options_from_args(args).to_request(username)


def _validate(request: CollectRequest) -> None:
    try:
        request.validate()
    except ValueError as error:
        raise CliError(str(error), EXIT_USAGE) from error


def _validate_without_username(request: CollectRequest) -> None:
    # The account ID may come from the browser later, or is unused for archives; check the rest now.
    _validate(replace(request, username=request.username or "i"))


def _wait_for_login(core: XDeleterCore, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if core.is_logged_in():
            return True
        if core.is_cancel_requested() or time.monotonic() >= deadline:
            return False
        core.wait_for_cancel(LOGIN_POLL_SECONDS)


def _not_logged_in_message(profile: str | None) -> str:
    hint = f" --profile {profile}" if profile else ""
    return tr("X にログインしていません。先に `kusamushiri-cli login{hint}` を実行してください。", hint=hint)


@contextmanager
def browser_session(
    profile: str | None,
    *,
    headless: bool,
    login_timeout: float | None = None,
    not_logged_in_message: str | None = None,
    require_login: bool = True,
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
        _info(tr("現在の項目が終わったら停止します（もう一度 Ctrl+C で即中断）。"))

    previous_handler = signal.signal(signal.SIGINT, on_interrupt)
    try:
        _info(tr("ブラウザを起動しています…（初回は Chromium のダウンロードに数分かかることがあります）"))
        core.start_browser(account_name=profile)
        core.go_to_home()
        logged_in = _wait_for_login(core, LOGIN_SETTLE_SECONDS if login_timeout is None else login_timeout)
        if core.is_cancel_requested():
            raise CliError(tr("中断しました。"), EXIT_INTERRUPTED)
        if require_login and not logged_in:
            raise CliError(not_logged_in_message or _not_logged_in_message(profile))
        yield core
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        core.stop_browser()


def _signed_in_core(stack: ExitStack, profile: str | None, *, headless: bool, interactive: bool) -> XDeleterCore:
    """Open a signed-in browser in `stack`, asking for a sign-in in a window when needed and possible."""
    core = stack.enter_context(browser_session(profile, headless=headless, require_login=False))
    if core.is_logged_in():
        return core
    if not interactive:
        raise CliError(_not_logged_in_message(profile))
    if headless:
        # Signing in needs a visible window.
        stack.close()
        core = stack.enter_context(browser_session(profile, headless=False, require_login=False))
    _info(
        tr(
            "開いたブラウザで X にログインしてください（最大 {minutes} 分待ちます）。",
            minutes=int(DEFAULT_LOGIN_TIMEOUT_SECONDS // 60),
        )
    )
    if not _wait_for_login(core, DEFAULT_LOGIN_TIMEOUT_SECONDS):
        if core.is_cancel_requested():
            raise CliError(tr("中断しました。"), EXIT_INTERRUPTED)
        raise CliError(tr("ログインを確認できませんでした。"))
    if headless:
        stack.close()
        core = stack.enter_context(browser_session(profile, headless=True))
    return core


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError as error:
        raise CliError(tr("中断しました。"), EXIT_INTERRUPTED) from error


def _confirm(prompt: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise CliError(tr("確認なしでは実行しません。--yes を付けてください。"), EXIT_USAGE)
    answer = _ask(tr("{prompt} 続ける場合は yes と入力してください: ", prompt=prompt))
    if answer.strip().casefold() != "yes":
        raise CliError(tr("キャンセルしました。"), EXIT_FAILURE)


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


def _summary(succeeded: int, total: int, cancelled: bool) -> str:
    if cancelled:
        return tr("{succeeded}/{total} 件成功（途中で停止）。", succeeded=succeeded, total=total)
    return tr("{succeeded}/{total} 件成功。", succeeded=succeeded, total=total)


def _collect_posts(core: XDeleterCore, request: CollectRequest) -> list[PostRecord]:
    posts = core.search_and_collect_posts(
        request,
        on_progress=lambda scanned, current, total: _progress(
            tr("走査 {scanned} 件、該当 {current}/{total} 件", scanned=scanned, current=current, total=total)
        ),
    )
    _end_progress()
    if core.collection_limit_reached:
        _info(tr("収集の安全上限に到達したため走査を終了しました。"))
    return posts


def _select_archive_posts(
    archive_path: Path, request: CollectRequest, profile: str | None, *, oldest_first: bool
) -> list[PostRecord]:
    try:
        posts = load_archive_posts(
            archive_path,
            fallback_username=request.username or None,
            on_progress=lambda done, total: _progress(tr("アーカイブを読み込み中… {done}/{total}", done=done, total=total)),
        )
    except ArchiveError as error:
        raise CliError(str(error)) from error
    finally:
        _end_progress()
    deleted_ids = DeletedPostStore.for_account(profile).load()
    selection = select_archive_posts(posts, request, deleted_ids, oldest_first=oldest_first)
    _info(
        tr(
            "アーカイブの {total} 件から {count} 件を選びました（リポスト {reposts} 件、削除済み {deleted} 件は対象外）。",
            total=selection.total,
            count=len(selection.posts),
            reposts=selection.skipped_reposts,
            deleted=selection.skipped_deleted,
        )
    )
    return selection.posts


def _execute_post_actions(
    core: XDeleterCore, targets: Sequence[PostActionTarget], interval_seconds: float, profile: str | None
) -> list[PostActionResult]:
    results = _run_batch(
        core,
        targets,
        interval_seconds,
        core.execute_post_action,
        lambda target: f"{action_label_for(target.kind)} {target.url}",
    )
    # Remember deleted originals so later archive imports skip them, as the app does.
    deleted_ids = [
        extract_post_id(result.target.url) for result in results if result.success and result.target.kind == "post"
    ]
    if deleted_ids:
        try:
            DeletedPostStore.for_account(profile).record(deleted_ids)
        except OSError as error:
            _info(tr("削除済みポスト ID を保存できませんでした: {error}", error=error))
    return results


def _report_post_actions(
    results: Sequence[PostActionResult],
    total: int,
    cancelled: bool,
    rows: Sequence[Mapping[str, object]],
    failed_output: Path | None,
) -> int:
    failed_indexes = [index for index, result in enumerate(results) if not result.success]
    for index in failed_indexes:
        result = results[index]
        _info(tr("失敗: {target}: {error}", target=result.target.url, error=result.error_message or "?"))
    _info(_summary(len(results) - len(failed_indexes), total, cancelled))
    failed_rows = [rows[index] for index in failed_indexes]
    if failed_output is not None and failed_rows:
        fields = list(failed_rows[0].keys())
        write_records(failed_output, fields, [{field: row.get(field) for field in fields} for row in failed_rows])
        _info(tr("失敗した {count} 件を {path} に保存しました。", count=len(failed_rows), path=failed_output))
    if cancelled:
        return EXIT_INTERRUPTED
    return EXIT_FAILURE if failed_indexes else 0


def _post_action_request(targets: list[PostActionTarget], interval: float) -> ExecuteActionsRequest:
    request = ExecuteActionsRequest(targets=targets, interval_seconds=interval)
    try:
        request.validate()
    except ValueError as error:
        raise CliError(str(error), EXIT_USAGE) from error
    return request


def cmd_login(args: argparse.Namespace) -> int:
    # Signing in needs a visible window, so --headless never applies here.
    with browser_session(
        args.profile,
        headless=False,
        login_timeout=args.timeout,
        not_logged_in_message=tr("{seconds} 秒以内にログインを確認できませんでした。", seconds=f"{args.timeout:g}"),
    ) as core:
        username = core.get_logged_in_username()
        _info(tr("@{username} でログインしています。", username=username) if username else tr("ログインしています。"))
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    _validate_without_username(build_collect_request(args, args.user or ""))
    with browser_session(args.profile, headless=args.headless) as core:
        request = build_collect_request(args, args.user or core.get_logged_in_username() or "")
        _validate(request)
        posts = _collect_posts(core, request)
        cancelled = core.is_cancel_requested()
    write_post_list(args.output, posts)
    _info(tr("{count} 件を {path} に保存しました。", count=len(posts), path=args.output))
    return EXIT_INTERRUPTED if cancelled else 0


def cmd_archive(args: argparse.Namespace) -> int:
    request = build_collect_request(args, args.user or "")
    _validate_without_username(request)
    posts = _select_archive_posts(args.archive, request, args.profile, oldest_first=args.oldest_first)
    write_post_list(args.output, posts)
    _info(tr("{count} 件を {path} に保存しました。", count=len(posts), path=args.output))
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    rows = read_rows(args.file)
    targets = _post_action_request(post_targets_from_rows(rows), args.interval).targets
    if args.dry_run:
        for target in targets:
            print(f"{action_label_for(target.kind)}\t{target.url}")
        _info(tr("{count} 件（確認のみ、何も変更していません）。", count=len(targets)))
        return 0
    _confirm(
        tr("{path} の {count} 件を削除/解除しますか？元に戻せません。", path=args.file, count=len(targets)), args.yes
    )
    with browser_session(args.profile, headless=args.headless) as core:
        results = _execute_post_actions(core, targets, args.interval, args.profile)
        cancelled = core.is_cancel_requested()
    return _report_post_actions(results, len(targets), cancelled, rows, args.failed_output)


def cmd_following(args: argparse.Namespace) -> int:
    with browser_session(args.profile, headless=args.headless) as core:
        username = (args.user or core.get_logged_in_username() or "").strip().removeprefix("@")
        if not username:
            raise CliError(tr("ログイン中のアカウントを判別できませんでした。--user を指定してください。"), EXIT_USAGE)
        result = core.collect_following(
            username, on_progress=lambda count: _progress(tr("{count} 件取得", count=count))
        )
        _end_progress()
        cancelled = core.is_cancel_requested()
    if cancelled:
        _info(tr("中断したため、ファイルは保存していません。"))
        return EXIT_INTERRUPTED
    records = result.records
    if args.skip_mutual:
        records = [record for record in records if not record.follows_you]
    write_follow_list(args.output, apply_last_post_entries(records, LastPostStore.for_account(args.profile).load()))
    _info(tr("{count} 件を {path} に保存しました。", count=len(records), path=args.output))
    if result.limit_reached:
        _info(tr("取得の安全上限に到達したため、途中までのフォローリストを保存しました。"))
    return 0


def _read_follow_targets(args: argparse.Namespace) -> list[FollowRecord]:
    records = follow_records_from_rows(read_rows(args.file))
    if getattr(args, "skip_mutual", False):
        records = [record for record in records if not record.follows_you]
    if not records:
        raise CliError(tr("対象のアカウントがありません。"), EXIT_USAGE)
    return records


def cmd_unfollow(args: argparse.Namespace) -> int:
    records = _read_follow_targets(args)
    if args.dry_run:
        for record in records:
            print(f"@{record.username}")
        _info(tr("{count} 件（確認のみ、何も変更していません）。", count=len(records)))
        return 0
    _confirm(tr("{path} の {count} 件をフォロー解除しますか？", path=args.file, count=len(records)), args.yes)

    with browser_session(args.profile, headless=args.headless) as core:
        results = _run_batch(
            core,
            records,
            args.interval,
            lambda record: (record, *core.unfollow_account(record.username)),
            lambda record: f"@{record.username}",
        )
        cancelled = core.is_cancel_requested()

    failed = [(record, error) for record, success, error in results if not success]
    for record, error in failed:
        _info(tr("失敗: {target}: {error}", target=f"@{record.username}", error=error or "?"))
    _info(_summary(len(results) - len(failed), len(records), cancelled))
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
            lambda record: f"@{record.username}",
        )
        cancelled = core.is_cancel_requested()

    entries = LastPostStore.for_account(args.profile).record(results)
    for result in results:
        if not result.success:
            _info(tr("失敗: {target}: {error}", target=f"@{result.username}", error=result.error_message or "?"))
    succeeded = sum(1 for result in results if result.success)
    _info(_summary(succeeded, len(records), cancelled))
    if args.output is not None:
        write_follow_list(args.output, apply_last_post_entries(records, entries))
        _info(tr("{count} 件を {path} に保存しました。", count=len(records), path=args.output))
    if cancelled:
        return EXIT_INTERRUPTED
    return EXIT_FAILURE if succeeded < len(results) else 0


def _preview(posts: Sequence[Mapping[str, object]]) -> None:
    for row in posts[:PREVIEW_ROWS]:
        text = " ".join(_text(row, "text").split())
        if len(text) > PREVIEW_TEXT_LENGTH:
            text = text[: PREVIEW_TEXT_LENGTH - 1] + "…"
        _info(f"  {_text(row, 'date')}  {_text(row, 'kind')}  {text}")
    if len(posts) > PREVIEW_ROWS:
        _info(tr("  …ほか {count} 件", count=len(posts) - PREVIEW_ROWS))


def run_recipe(recipe: Recipe, *, interactive: bool) -> int:
    """Collect posts as the recipe says, save the list, and delete them after a confirmation."""
    options = recipe.collect
    archive_path = recipe.archive_path
    _validate_without_username(options.to_request(options.user or ""))
    if recipe.delete is not None and recipe.delete.confirm and not interactive:
        raise CliError(tr("confirm = true のため、対話できない環境では実行できません。"), EXIT_USAGE)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    list_path = recipe.output_path / f"posts-{stamp}.csv"
    with ExitStack() as stack:
        core: XDeleterCore | None = None
        if archive_path is None:
            core = _signed_in_core(stack, recipe.profile, headless=recipe.headless, interactive=interactive)
            request = options.to_request(options.user or core.get_logged_in_username() or "")
            _validate(request)
            posts = _collect_posts(core, request)
            cancelled = core.is_cancel_requested()
        else:
            request = options.to_request(options.user or "")
            posts = _select_archive_posts(archive_path, request, recipe.profile, oldest_first=options.oldest_first)
            cancelled = False

        # Always keep the list: it is the review copy and a record of what was targeted.
        list_path.parent.mkdir(parents=True, exist_ok=True)
        write_post_list(list_path, posts)
        _info(tr("{count} 件を {path} に保存しました。", count=len(posts), path=list_path))
        if cancelled:
            return EXIT_INTERRUPTED
        if not posts or recipe.delete is None:
            return 0

        if recipe.delete.confirm:
            _preview([asdict(post) for post in posts])
            _info(tr("一覧のファイルを編集すると、残した行だけを実行します（行を消すと対象外）。"))
            if not _ask_yes_no(tr("{count} 件を削除/解除しますか？元に戻せません。", count=len(posts))):
                _info(tr("削除/解除は行いませんでした。"))
                return 0
        # Re-read the file so edits made while the question was open take effect.
        rows = read_rows(list_path)
        if not rows:
            _info(tr("一覧が空になったため、何もしませんでした。"))
            return 0
        targets = _post_action_request(post_targets_from_rows(rows), recipe.delete.interval).targets
        if core is None:
            core = _signed_in_core(stack, recipe.profile, headless=recipe.headless, interactive=interactive)
        results = _execute_post_actions(core, targets, recipe.delete.interval, recipe.profile)
        cancelled = core.is_cancel_requested()
    failed_path = list_path.with_name(f"failed-{list_path.name}")
    return _report_post_actions(results, len(targets), cancelled, rows, failed_path)


def _ask_yes_no(prompt: str) -> bool:
    return _ask(f"{prompt} [y/N]: ").strip().casefold() in {"y", "yes"}


def cmd_run(args: argparse.Namespace) -> int:
    try:
        recipe = load_recipe(args.recipe)
    except RecipeError as error:
        raise CliError(str(error), EXIT_USAGE) from error
    return run_recipe(recipe, interactive=sys.stdin.isatty())


def cmd_wizard(path: Path) -> int:
    """Reuse the saved recipe or build one with questions, then run it."""
    recipe: Recipe | None = None
    if path.exists():
        try:
            recipe = load_recipe(path)
        except RecipeError as error:
            _info(str(error))
        else:
            _info(tr("保存済みの設定があります: {path}", path=path.resolve()))
            if not _ask_yes_no(tr("この設定で実行しますか？（n で設定を作り直します）")):
                recipe = None
    if recipe is None:
        try:
            recipe = run_wizard(path, ask=_ask)
        except RecipeError as error:
            raise CliError(str(error), EXIT_USAGE) from error
        if not _ask_yes_no(tr("今すぐ実行しますか？")):
            _info(tr("次回からは kusamushiri-cli run {path} でも実行できます。", path=recipe.path))
            return 0
    return run_recipe(recipe, interactive=True)


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
    group.add_argument("--older-than", type=_non_negative_int, metavar="DAYS", help="only posts older than DAYS days")
    group.add_argument("--include", default="", help="comma-separated keywords; a post must contain one")
    group.add_argument("--exclude", default="", help="comma-separated keywords; posts containing one are skipped")


def _add_confirm_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="list the targets without opening the browser")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kusamushiri-cli",
        description=(
            "Collect, review and delete X posts or follows from the command line. "
            "Run without arguments to set up and run a saved cleanup step by step."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show log messages on stderr")
    parser.add_argument("--lang", choices=("ja", "en"), default="ja", help="language of messages")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = commands.add_parser("run", help="collect, review and delete as a saved recipe (TOML) says")
    run.add_argument("recipe", type=Path, nargs="?", default=Path(DEFAULT_RECIPE_NAME), help="default: %(default)s")
    run.set_defaults(handler=cmd_run)

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


def _pause_before_exit() -> None:
    # A double-clicked console window closes on exit; keep the result readable.
    with suppress(EOFError, KeyboardInterrupt):
        input(tr("Enter キーで終了します…"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_console_logging(args.verbose)
    set_language(args.lang)
    wizard = args.command is None
    if wizard and not sys.stdin.isatty():
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if migrate_legacy_app_data():
        logger.info("Moved profiles from the previous xposdeleter data directory.")
    configure_frozen_browser_path()
    try:
        if wizard:
            return cmd_wizard(Path(DEFAULT_RECIPE_NAME))
        handler: Callable[[argparse.Namespace], int] = args.handler
        return handler(args)
    except CliError as error:
        _end_progress()
        _info(f"kusamushiri-cli: {error}")
        return error.exit_code
    except KeyboardInterrupt:
        _end_progress()
        _info(tr("kusamushiri-cli: 中断しました。"))
        return EXIT_INTERRUPTED
    except Exception as error:
        if args.verbose:
            raise
        _end_progress()
        # The traceback goes to the log file only; -v shows it on the terminal.
        logger.debug("Command %s failed.", args.command, exc_info=True)
        _info(tr("kusamushiri-cli: {error}（詳細は -v を付けて実行）", error=error or type(error).__name__))
        return EXIT_FAILURE
    finally:
        if wizard:
            _pause_before_exit()


if __name__ == "__main__":
    raise SystemExit(main())
