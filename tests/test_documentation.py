"""Public docs/examples must stay aligned with the existing schema and CLI."""

import re
import tomllib
from pathlib import Path

import pytest

from kusamushiri import cli
from kusamushiri.models import DEFAULT_ACTION_INTERVAL_SECONDS, RetryPolicy
from kusamushiri.recipe import DELETE_KEYS, DeleteOptions, Recipe, format_recipe, load_recipe

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", ["README.md", "README.ja.md"])
def test_documented_settings_cover_schema_defaults_and_loadable_examples(filename):
    text = (ROOT / filename).read_text()
    defaults = {name: getattr(RetryPolicy(), attribute) for name, attribute, *_ in cli.RETRY_ARGUMENTS}
    defaults.update(interval=DEFAULT_ACTION_INTERVAL_SECONDS, confirm=True)
    defaults.update(ancillary_rate_limit_mode=RetryPolicy().ancillary_rate_limit_mode)
    for key, value in defaults.items():
        row = next(line for line in text.splitlines() if line.startswith(f"| `{key}` |"))
        expected = "true" if value is True else value if isinstance(value, str) else f"{value:g}"
        assert row.endswith(f"| {expected} |")
    blocks = re.findall(r"```toml\n(.*?)\n```", text, re.S)
    assert blocks
    for block in blocks:
        data = tomllib.loads(block)
        assert {"interval", "confirm"} <= set(data["delete"]) <= DELETE_KEYS
        from kusamushiri.recipe import parse_recipe
        recipe = parse_recipe(data, ROOT / "examples" / "placeholder.toml")
        recipe.delete.retry_policy.validate()
        assert recipe.delete.confirm is True


def test_public_tuning_example_uses_existing_loader_without_inheriting_account_or_confirmation():
    path = ROOT / "examples" / "delete-tuning.toml"
    recipe = load_recipe(path)
    assert recipe.profile is None and recipe.delete.confirm is True
    assert recipe.delete.interval == 8 and recipe.delete.retry_policy == RetryPolicy()
    args = cli.build_parser().parse_args(["delete", "placeholder.json", "--recipe", str(path), "--dry-run"])
    options = cli._delete_tuning_from_args(args)
    assert options == recipe.delete and args.profile is None and not args.headless and not args.yes


def test_generated_recipe_comments_use_current_defaults_and_keep_values_roundtrippable(tmp_path):
    collect_only = format_recipe(Recipe(path=tmp_path / "collect.toml"))
    assert f"# interval = {DEFAULT_ACTION_INTERVAL_SECONDS:g}" in collect_only
    recipe = Recipe(path=tmp_path / "recipe.toml", delete=DeleteOptions())
    text = format_recipe(recipe)
    assert "seconds" in text and "Advanced values are optional" in text
    assert set(tomllib.loads(text)["delete"]) == {"interval", "confirm"}
    from kusamushiri.recipe import parse_recipe
    assert parse_recipe(tomllib.loads(text), recipe.path) == recipe


@pytest.mark.parametrize("command", ["run", "delete"])
def test_help_explains_preview_scope_batch_cooldown_and_deadlines(command, capsys):
    with pytest.raises(SystemExit) as result:
        cli.build_parser().parse_args([command, "--help"])
    assert result.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--retry-max-wait" in help_text and "--max-cooldowns" not in help_text
    assert "saved" in help_text and "--dry-run" in help_text
    if command == "run":
        assert "No --dry-run here" in help_text and "--interval" in help_text
    else:
        assert "does not inherit profile" in help_text and "Tuning precedence" in help_text
