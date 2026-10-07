"""Saved cleanup recipes for `kusamushiri-cli run`, and the wizard that writes them.

A recipe is a small TOML file: top-level browser settings, a [collect] table with the same
filters as the app, and an optional [delete] table. Without [delete] a run only writes the list.
"""

import json
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Final, TypeVar, cast

from kusamushiri.i18n import tr
from kusamushiri.models import (
    DEFAULT_ACTION_INTERVAL_SECONDS,
    CollectRequest,
    MediaFilter,
    PostKindFilter,
    SearchMode,
    parse_keywords,
)
from kusamushiri.paths import list_saved_accounts

DEFAULT_RECIPE_NAME: Final = "kusamushiri.toml"
DEFAULT_OUTPUT_DIR: Final = "kusamushiri-lists"
DEFAULT_MAX_POSTS: Final = 50
POST_KIND_FILTERS: Final = ("posts", "reposts", "all", "likes")
SEARCH_MODES: Final = ("profile", "search")
MEDIA_FILTERS: Final = ("all", "with_media", "without_media")
ARCHIVE_ORDERS: Final = ("newest", "oldest")

_Value = TypeVar("_Value")


class RecipeError(ValueError):
    """The recipe file is missing, unreadable or has an invalid value."""


@dataclass(frozen=True, slots=True)
class CollectOptions:
    """The app's post filters, shared by the subcommands and recipes."""

    user: str | None = None
    kind: PostKindFilter = "posts"
    mode: SearchMode = "profile"
    media: MediaFilter = "all"
    replies_only: bool = False
    min_likes: int = 0
    min_replies: int = 0
    since: date | None = None
    until: date | None = None
    older_than_days: int | None = None
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_posts: int = DEFAULT_MAX_POSTS
    oldest_first: bool = False

    def effective_until(self, today: date) -> date | None:
        if self.older_than_days is None:
            return self.until
        cutoff = today - timedelta(days=self.older_than_days)
        return cutoff if self.until is None else min(self.until, cutoff)

    def to_request(self, username: str, today: date | None = None) -> CollectRequest:
        return CollectRequest(
            username=username.strip().removeprefix("@"),
            max_posts=self.max_posts,
            media_filter=self.media,
            is_reply=self.replies_only,
            min_likes=self.min_likes,
            min_replies=self.min_replies,
            # Likes are listed only on the profile's likes timeline, never via search.
            search_mode="profile" if self.kind == "likes" else self.mode,
            post_kind_filter=self.kind,
            since_date=self.since,
            until_date=self.effective_until(today or date.today()),
            include_keywords=self.include,
            exclude_keywords=self.exclude,
        )


@dataclass(frozen=True, slots=True)
class DeleteOptions:
    interval: float = DEFAULT_ACTION_INTERVAL_SECONDS
    confirm: bool = True


@dataclass(frozen=True, slots=True)
class Recipe:
    path: Path
    profile: str | None = None
    headless: bool = False
    output_dir: str = DEFAULT_OUTPUT_DIR
    archive: str | None = None
    collect: CollectOptions = field(default_factory=CollectOptions)
    delete: DeleteOptions | None = None

    def resolve(self, relative: str) -> Path:
        """Paths in a recipe are relative to the recipe file, not the working directory."""
        path = Path(relative).expanduser()
        return path if path.is_absolute() else self.path.parent / path

    @property
    def output_path(self) -> Path:
        return self.resolve(self.output_dir)

    @property
    def archive_path(self) -> Path | None:
        return None if self.archive is None else self.resolve(self.archive)


def _check(table: Mapping[str, object], key: str, kind: type[_Value], where: str) -> _Value | None:
    if key not in table:
        return None
    value = table[key]
    # bool is a subclass of int; keep `true` out of number fields.
    if isinstance(value, bool) and kind is not bool:
        raise RecipeError(f"{where}{key}: expected {kind.__name__}")
    if kind is float and isinstance(value, int):
        value = float(value)
    if not isinstance(value, kind):
        raise RecipeError(f"{where}{key}: expected {kind.__name__}")
    return cast(_Value, value)


def _choice(table: Mapping[str, object], key: str, choices: tuple[str, ...], default: str) -> str:
    value = _check(table, key, str, "collect.") or default
    if value not in choices:
        raise RecipeError(f"collect.{key}: must be one of {', '.join(choices)}")
    return value


def _date(table: Mapping[str, object], key: str) -> date | None:
    value = table.get(key)
    if value is None or value == "":
        return None
    # tomllib returns bare dates as date; quoted ones arrive as text.
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise RecipeError(f"collect.{key}: use YYYY-MM-DD")


def _keywords(table: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = table.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        return parse_keywords(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return parse_keywords(",".join(value))
    raise RecipeError(f"collect.{key}: expected a list of strings")


def _non_negative(value: _Value | None, where: str) -> _Value | None:
    if isinstance(value, (int, float)) and value < 0:
        raise RecipeError(f"{where}: must be 0 or greater")
    return value


def _reject_unknown(table: Mapping[str, object], allowed: set[str], where: str) -> None:
    # A misspelled filter would otherwise be ignored and widen what gets deleted.
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise RecipeError(f"unknown setting {where}{unknown[0]}")


def _table(data: Mapping[str, object], key: str) -> Mapping[str, object] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RecipeError(f"[{key}] must be a table")
    return value


def parse_recipe(data: Mapping[str, object], path: Path) -> Recipe:
    _reject_unknown(data, {"profile", "headless", "output_dir", "archive", "collect", "delete"}, "")
    collect_table = _table(data, "collect") or {}
    # `oldest_first` is the older spelling of `order`, kept so saved recipes still load.
    _reject_unknown(collect_table, {*CollectOptions.__dataclass_fields__, "order"}, "collect.")
    max_posts = _check(collect_table, "max_posts", int, "collect.")
    if max_posts is not None and max_posts < 1:
        raise RecipeError("collect.max_posts: must be 1 or greater")
    legacy_oldest_first = _check(collect_table, "oldest_first", bool, "collect.")
    order = _choice(collect_table, "order", ARCHIVE_ORDERS, "oldest" if legacy_oldest_first else "newest")
    if legacy_oldest_first is not None and legacy_oldest_first != (order == "oldest"):
        raise RecipeError("collect.order: conflicts with collect.oldest_first; keep only order")
    collect = CollectOptions(
        user=_check(collect_table, "user", str, "collect.") or None,
        kind=_choice(collect_table, "kind", POST_KIND_FILTERS, "posts"),  # type: ignore[arg-type]
        mode=_choice(collect_table, "mode", SEARCH_MODES, "profile"),  # type: ignore[arg-type]
        media=_choice(collect_table, "media", MEDIA_FILTERS, "all"),  # type: ignore[arg-type]
        replies_only=bool(_check(collect_table, "replies_only", bool, "collect.")),
        min_likes=_non_negative(_check(collect_table, "min_likes", int, "collect."), "collect.min_likes") or 0,
        min_replies=_non_negative(_check(collect_table, "min_replies", int, "collect."), "collect.min_replies") or 0,
        since=_date(collect_table, "since"),
        until=_date(collect_table, "until"),
        older_than_days=_non_negative(
            _check(collect_table, "older_than_days", int, "collect."), "collect.older_than_days"
        ),
        include=_keywords(collect_table, "include"),
        exclude=_keywords(collect_table, "exclude"),
        max_posts=max_posts or DEFAULT_MAX_POSTS,
        oldest_first=order == "oldest",
    )

    delete: DeleteOptions | None = None
    delete_table = _table(data, "delete")
    if delete_table is not None:
        _reject_unknown(delete_table, {"interval", "confirm"}, "delete.")
        interval = _non_negative(_check(delete_table, "interval", float, "delete."), "delete.interval")
        confirm = _check(delete_table, "confirm", bool, "delete.")
        delete = DeleteOptions(
            interval=DEFAULT_ACTION_INTERVAL_SECONDS if interval is None else interval,
            confirm=True if confirm is None else confirm,
        )

    return Recipe(
        path=path,
        profile=_check(data, "profile", str, "") or None,
        headless=bool(_check(data, "headless", bool, "")),
        output_dir=_check(data, "output_dir", str, "") or DEFAULT_OUTPUT_DIR,
        archive=_check(data, "archive", str, "") or None,
        collect=collect,
        delete=delete,
    )


def load_recipe(path: Path) -> Recipe:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RecipeError(f"{path}: file not found") from error
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise RecipeError(f"{path}: {error}") from error
    try:
        return parse_recipe(data, path.resolve())
    except RecipeError as error:
        raise RecipeError(f"{path}: {error}") from error


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, date):
        return json.dumps(value.isoformat())
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    # A JSON string literal is a valid TOML basic string.
    return json.dumps(str(value), ensure_ascii=False)


def _line(key: str, value: object, note: str = "", example: str = "") -> str:
    """One `key = value` line; an unset value becomes a commented-out example."""
    line = f"# {key} = {example}" if value is None else f"{key} = {_toml(value)}"
    return f"{line}  # {note}" if note else line


def format_recipe(recipe: Recipe) -> str:
    collect = recipe.collect
    lines = [
        "# kusamushiri-cli run <this file>",
        _line("profile", recipe.profile, example='"main"'),
        _line("headless", recipe.headless),
        _line("output_dir", recipe.output_dir),
        _line("archive", recipe.archive, "read posts from an X data archive", '"twitter-archive.zip"'),
        "",
        "[collect]",
        _line("user", collect.user, "default: the logged-in account", '"your_id"'),
        _line("kind", collect.kind, "posts / reposts / all / likes"),
        _line("mode", collect.mode, "profile / search"),
        _line("media", collect.media, "all / with_media / without_media"),
        _line("replies_only", collect.replies_only),
        _line("min_likes", collect.min_likes),
        _line("min_replies", collect.min_replies),
        _line("since", collect.since, example='"2020-01-01"'),
        _line("until", collect.until, example='"2020-12-31"'),
        _line("older_than_days", collect.older_than_days, example="30"),
        _line("include", list(collect.include)),
        _line("exclude", list(collect.exclude)),
        _line("max_posts", collect.max_posts),
        _line("order", "oldest" if collect.oldest_first else "newest", "newest / oldest (archive only)"),
    ]
    if recipe.delete is None:
        lines += ["", "# Add a [delete] table to delete the collected posts:", "# [delete]", "# interval = 1.0"]
    else:
        lines += [
            "",
            "[delete]",
            _line("interval", recipe.delete.interval, "seconds between items"),
            _line("confirm", recipe.delete.confirm, "false runs without asking"),
        ]
    return "\n".join(lines) + "\n"


def save_recipe(recipe: Recipe) -> None:
    recipe.path.parent.mkdir(parents=True, exist_ok=True)
    recipe.path.write_text(format_recipe(recipe), encoding="utf-8")


Ask = Callable[[str], str]


def _ask_text(ask: Ask, prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = ask(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_yes_no(ask: Ask, prompt: str, default: bool) -> bool:
    while True:
        answer = ask(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().casefold()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def _ask_number(ask: Ask, prompt: str, default: _Value | None, parse: Callable[[str], _Value]) -> _Value | None:
    while True:
        answer = _ask_text(ask, prompt, "" if default is None else str(default))
        if not answer:
            return None
        try:
            value = parse(answer)
        except ValueError:
            print(tr("数値を入力してください。"))
            continue
        if isinstance(value, (int, float)) and value < 0:
            print(tr("0 以上で入力してください。"))
            continue
        return value


def _ask_choice(ask: Ask, prompt: str, choices: list[tuple[str, str]], default: str) -> str:
    print(prompt)
    for number, (_value, label) in enumerate(choices, start=1):
        print(f"  {number}) {label}")
    default_number = next(number for number, (value, _) in enumerate(choices, start=1) if value == default)
    while True:
        answer = _ask_text(ask, tr("番号"), str(default_number))
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1][0]


def run_wizard(path: Path, ask: Ask = input) -> Recipe:
    """Ask for the usual settings, save them to `path`, and return the recipe."""
    print(tr("質問に答えると、掃除の設定ファイルを作ります。空欄で Enter を押すと [ ] 内の値を使います。"))
    saved = list_saved_accounts()
    if saved:
        print(tr("保存済みのプロファイル: {names}", names=", ".join(saved)))
    profile = _ask_text(ask, tr("プロファイル名"), saved[0] if saved else "main")

    source = _ask_choice(
        ask,
        tr("ポストをどこから集めますか？"),
        [("x", tr("X で検索する")), ("archive", tr("X のデータのアーカイブから読む"))],
        "x",
    )
    archive: str | None = None
    kind: PostKindFilter = "posts"
    oldest_first = False
    if source == "archive":
        while not archive:
            archive = _ask_text(ask, tr("アーカイブ (.zip またはフォルダ) のパス")).strip("\"'") or None
        oldest_first = (
            _ask_choice(
                ask,
                tr("どの順に集めて削除しますか？"),
                [("oldest", tr("古い順")), ("newest", tr("新しい順"))],
                "oldest",
            )
            == "oldest"
        )
    else:
        kind = _ask_choice(  # type: ignore[assignment]
            ask,
            tr("対象"),
            [
                ("posts", tr("通常ポスト")),
                ("reposts", tr("リポスト")),
                ("all", tr("通常ポスト + リポスト")),
                ("likes", tr("いいね")),
            ],
            "posts",
        )
    # 0 means no date limit.
    older_than_days = _ask_number(ask, tr("何日より前のポストを対象にしますか（0 で期間指定なし）"), 30, int) or None
    include = parse_keywords(_ask_text(ask, tr("含むキーワード（カンマ区切り、空欄で指定なし）")))
    exclude = parse_keywords(_ask_text(ask, tr("除外キーワード（残したいポストを守ります）")))
    max_posts = _ask_number(ask, tr("1 回に集める上限件数"), DEFAULT_MAX_POSTS, int) or DEFAULT_MAX_POSTS

    delete: DeleteOptions | None = None
    if _ask_yes_no(ask, tr("集めたポストを削除/解除まで行いますか？（実行前に毎回確認します）"), True):
        interval = _ask_number(ask, tr("削除/解除の間隔（秒）"), DEFAULT_ACTION_INTERVAL_SECONDS, float)
        delete = DeleteOptions(interval=DEFAULT_ACTION_INTERVAL_SECONDS if interval is None else interval)
    headless = _ask_yes_no(ask, tr("ブラウザのウィンドウを表示せずに実行しますか？"), False)

    recipe = Recipe(
        path=path.resolve(),
        profile=profile or None,
        headless=headless,
        archive=archive,
        collect=replace(
            CollectOptions(),
            kind=kind,
            older_than_days=older_than_days,
            include=include,
            exclude=exclude,
            max_posts=max_posts,
            oldest_first=oldest_first,
        ),
        delete=delete,
    )
    save_recipe(recipe)
    print(tr("設定を {path} に保存しました。テキストエディタで編集できます。", path=recipe.path))
    return recipe
