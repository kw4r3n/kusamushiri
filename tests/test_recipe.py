from datetime import date, timedelta
from pathlib import Path

import pytest

from kusamushiri import recipe as recipe_module
from kusamushiri.recipe import (
    CollectOptions,
    DeleteOptions,
    Recipe,
    RecipeError,
    choose_recipe_path,
    find_recipes,
    format_recipe,
    load_recipe,
    recipe_path_for,
    run_wizard,
    save_recipe,
)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_saved_recipe_loads_back_unchanged(tmp_path) -> None:
    recipe = Recipe(
        path=(tmp_path / "weekly.toml").resolve(),
        profile="main",
        headless=True,
        archive="archive.zip",
        collect=CollectOptions(
            user="alice",
            kind="likes",
            media="with_media",
            since=date(2020, 1, 1),
            older_than_days=30,
            include=("a", 'quote "b"'),
            exclude=("残す",),
            max_posts=10,
            oldest_first=True,
        ),
        delete=DeleteOptions(interval=2.5, confirm=False),
    )

    save_recipe(recipe)

    assert load_recipe(recipe.path) == recipe


def test_recipe_without_delete_table_only_collects(tmp_path) -> None:
    recipe = Recipe(path=(tmp_path / "r.toml").resolve())
    save_recipe(recipe)

    loaded = load_recipe(recipe.path)

    assert loaded.delete is None
    assert "# [delete]" in format_recipe(loaded)


def test_dates_may_be_bare_or_quoted(tmp_path) -> None:
    path = write(tmp_path / "r.toml", '[collect]\nsince = 2020-01-02\nuntil = "2020-03-04"\n')

    collect = load_recipe(path).collect

    assert (collect.since, collect.until) == (date(2020, 1, 2), date(2020, 3, 4))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[collect]\nexlude = ['x']\n", "collect.exlude"),
        ("headles = true\n", "headles"),
        ("[collect]\nmin_likes = true\n", "collect.min_likes"),
        ("[collect]\nkind = 'quotes'\n", "collect.kind"),
        ("[collect]\nmax_posts = 0\n", "collect.max_posts"),
        ("[delete]\ninterval = -1\n", "delete.interval"),
        ("[collect]\nsince = 'yesterday'\n", "collect.since"),
        ("profile = \n", "r.toml"),
    ],
)
def test_invalid_recipes_name_the_setting(tmp_path, text, message) -> None:
    with pytest.raises(RecipeError, match=message):
        load_recipe(write(tmp_path / "r.toml", text))


def test_paths_resolve_next_to_the_recipe(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "settings"
    folder.mkdir()
    path = write(folder / "r.toml", 'output_dir = "lists"\narchive = "x/archive.zip"\n')
    monkeypatch.chdir(tmp_path)

    loaded = load_recipe(Path("settings/r.toml"))

    assert loaded.output_path == folder.resolve() / "lists"
    assert loaded.archive_path == folder.resolve() / "x" / "archive.zip"
    assert path.exists()


def test_older_than_days_sets_the_end_date() -> None:
    today = date(2026, 10, 7)

    assert CollectOptions(older_than_days=30).effective_until(today) == today - timedelta(days=30)
    assert CollectOptions(older_than_days=30, until=date(2020, 1, 1)).effective_until(today) == date(2020, 1, 1)
    assert CollectOptions(kind="likes", mode="search").to_request("@alice", today).search_mode == "profile"


def test_wizard_writes_a_runnable_recipe(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(recipe_module, "list_saved_accounts", lambda: ["main"])
    answers = iter(
        [
            "",  # profile: main
            "1",  # search on X
            "4",  # likes
            "abc",  # not a number, asked again
            "90",
            "",  # include
            "残す, keep",
            "",  # max posts
            "y",  # delete
            "3",
            "n",  # headless
        ]
    )
    path = tmp_path / "kusamushiri.toml"

    recipe = run_wizard(path, ask=lambda _prompt: next(answers))

    assert load_recipe(path) == recipe
    assert recipe.profile == "main"
    assert recipe.collect.kind == "likes"
    assert recipe.collect.older_than_days == 90
    assert recipe.collect.exclude == ("残す", "keep")
    assert recipe.delete == DeleteOptions(interval=3.0)
    assert recipe.headless is False


@pytest.mark.parametrize(
    ("collect", "oldest_first"),
    [("", False), ('order = "newest"', False), ('order = "oldest"', True), ("oldest_first = true", True)],
)
def test_order_is_explicit_and_old_oldest_first_still_loads(tmp_path, collect: str, oldest_first: bool) -> None:
    loaded = load_recipe(write(tmp_path / "r.toml", f"[collect]\n{collect}\n"))

    assert loaded.collect.oldest_first is oldest_first
    expected = "oldest" if oldest_first else "newest"
    assert f'order = "{expected}"' in format_recipe(loaded)


@pytest.mark.parametrize("collect", ['order = "random"', 'order = "newest"\noldest_first = true'])
def test_invalid_or_conflicting_order_is_rejected(tmp_path, collect: str) -> None:
    with pytest.raises(RecipeError, match="collect.order"):
        load_recipe(write(tmp_path / "r.toml", f"[collect]\n{collect}\n"))


@pytest.mark.parametrize(("answer", "oldest_first"), [("", True), ("2", False)])
def test_wizard_asks_the_archive_order(tmp_path, monkeypatch, answer: str, oldest_first: bool) -> None:
    monkeypatch.setattr(recipe_module, "list_saved_accounts", lambda: [])
    answers = iter(["", "2", "archive.zip", answer, "", "", "", "", "n", "n"])

    recipe = run_wizard(tmp_path / "kusamushiri.toml", ask=lambda _prompt: next(answers))

    assert recipe.archive == "archive.zip"
    assert recipe.collect.oldest_first is oldest_first


def test_find_recipes_lists_the_default_first(tmp_path) -> None:
    for name in ("kusamushiri-likes.toml", "kusamushiri.toml", "kusamushiri-a.toml", "pyproject.toml"):
        (tmp_path / name).write_text("", encoding="utf-8")

    assert [path.name for path in find_recipes(tmp_path)] == [
        "kusamushiri.toml",
        "kusamushiri-a.toml",
        "kusamushiri-likes.toml",
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("likes", "kusamushiri-likes.toml"),
        (" likes.toml ", "kusamushiri-likes.toml"),
        ("kusamushiri-likes", "kusamushiri-likes.toml"),
        ("kusamushiri", "kusamushiri.toml"),
        ("古いポスト", "kusamushiri-古いポスト.toml"),
        ("", None),
        ("../x", None),
        ("a:b", None),
        (".hidden", None),
    ],
)
def test_recipe_path_for_names(tmp_path, name: str, expected: str | None) -> None:
    path = recipe_path_for(tmp_path, name)
    assert (path.name if path else None) == expected
    assert path is None or path.parent == tmp_path


def test_choose_recipe_path_defaults_without_saved_recipes(tmp_path) -> None:
    def ask(_prompt: str) -> str:
        raise AssertionError("no question expected")

    assert choose_recipe_path(tmp_path, ask=ask) == tmp_path / "kusamushiri.toml"


def test_choose_recipe_path_picks_a_saved_recipe(tmp_path) -> None:
    (tmp_path / "kusamushiri.toml").write_text("", encoding="utf-8")
    (tmp_path / "kusamushiri-likes.toml").write_text("", encoding="utf-8")
    answers = iter(["2"])

    assert choose_recipe_path(tmp_path, ask=lambda _prompt: next(answers)) == tmp_path / "kusamushiri-likes.toml"


def test_choose_recipe_path_names_a_new_recipe(tmp_path) -> None:
    (tmp_path / "kusamushiri.toml").write_text("", encoding="utf-8")
    (tmp_path / "kusamushiri-likes.toml").write_text("", encoding="utf-8")
    answers = iter(["3", "", "a/b", "likes", "old"])  # new; empty, invalid and taken names are asked again

    assert choose_recipe_path(tmp_path, ask=lambda _prompt: next(answers)) == tmp_path / "kusamushiri-old.toml"
