import os
import stat
import tomllib
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from kusamushiri import cli, recipe_update
from kusamushiri.recipe import RecipeError, load_recipe
from kusamushiri.recipe_update import apply_recipe_update, discover_update_recipes, plan_recipe_update

OLD_RECIPE = """# my recipe: keep this comment
profile = "main"
headless = true
output_dir = "kusamushiri-lists"
archive = "archive.zip"

[collect] # retain the legacy spelling
kind = "posts"
mode = "profile"
media = "all"
replies_only = false
min_likes = 0
min_replies = 0
older_than_days = 30
include = []
exclude = ["keep"]
max_posts = 50
oldest_first = true # oldest archive first

[delete]
interval = 0.5  # custom pace
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "r.toml"
    path.write_bytes(text.encode("utf-8"))
    return path


def test_old_recipe_preserves_effective_values_comments_and_order(tmp_path):
    path = write(tmp_path, OLD_RECIPE)
    before = load_recipe(path)
    update = plan_recipe_update(path)
    assert path.read_text() == OLD_RECIPE  # planning is read-only
    assert update.added_keys == ("delete.confirm",)
    backup = apply_recipe_update(update)
    assert backup is not None and backup.read_bytes() == OLD_RECIPE.encode()
    assert path.read_bytes().startswith(OLD_RECIPE.encode())
    assert load_recipe(path) == before
    assert load_recipe(path).delete.interval == 0.5
    assert load_recipe(path).delete.confirm is True
    assert 'order = "oldest"' not in path.read_text()
    second = plan_recipe_update(path)
    assert second.added_keys == () and second.diff == ""
    assert apply_recipe_update(second) is None
    assert len(list(tmp_path.glob("*.bak-*"))) == 1


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# only a comment",
        "[delete]\n",
        "[collect]\n",
        "[collect]\nkind = 'likes'\nmode = 'search'\n",
        "headless = true\n[unknown]\ncustom = 1\n",
        "collect.oldest_first = true\ndelete.interval = 0.5\ndelete.confirm = false\n",
        "collect.kind = 'posts'\n[delete]\ninterval = 0.5\n",
        'collect = { oldest_first = true, include = ["# [delete]", "}"] } # }\ndelete = { interval = 0.5, confirm = false }\n',
        "collect = {}\ndelete = {}\n",
        "collect={kind='posts'}\n[other]\nflag=true\n[delete]\ninterval=0.5\n",
        '"collect" . "oldest_first" = true\n"delete".interval=0.5\n',
        "[ 'collect' ]\noldest_first = true\n[ 'delete' ]\nconfirm=false\n",
        '[collect]\ninclude = [\n "a", # [delete]\n "b",\n]\n[delete]\nconfirm=false',
        '"headless" = true\n[collect]\ninclude = ["a\\"b"]\n[delete]\nconfirm=false\n',
        'notes = """\n[collect]\n[delete]\n{ = } # pretend syntax\n"""\n[collect]\nkind="posts"\n',
        "notes = '''\n[collect]\n[delete]\n'''\ncollect = {kind='posts'}\n",
        '[collect]\ninclude = ["""line\n[delete]\nend"""]\n',
        '[collect]\ninclude = ["""quote ending""""]\n',
        "[collect]\ninclude = ['''quote ending''''']\n",
        "[collect]\nkind = 'posts'\n[collect.extension]\ncustom = 'retain'\n[delete]\nconfirm = false\n",
        "[[extension]]\nnotes = 'unknown array table'\n[[extension]]\nnotes = 'second'\n",
        "[collect]\noldest_first = true\norder = 'oldest'\n[delete]\ninterval = 0.5\n",
        "headless = false\r\n[collect]\r\noldest_first = true\r\n[delete]\r\nconfirm = false\r\n",
    ],
)
def test_valid_toml_forms_remain_valid_preserve_unknowns_and_are_idempotent(tmp_path, text):
    path = write(tmp_path, text)
    before = tomllib.loads(text)
    update = plan_recipe_update(path)
    apply_recipe_update(update)
    after = tomllib.loads(path.read_text())
    assert recipe_update._contains_original(before, after)
    assert plan_recipe_update(path).updated == path.read_bytes()
    assert ("delete" in after) == ("delete" in before)
    if "\r\n" in text:
        assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")


def test_unknown_fields_preserved_and_reported_without_weakening_normal_loader(tmp_path):
    text = "future = nan # preserve\n[collect]\ncustom = 123\n[delete]\nextra = {x='y'}\n"
    path = write(tmp_path, text)
    update = plan_recipe_update(path)
    assert update.unknown_keys == ("collect.custom", "delete.extra", "future")
    apply_recipe_update(update)
    assert "future = nan # preserve" in path.read_text()
    assert "custom = 123" in path.read_text()
    assert "extra = {x='y'}" in path.read_text()
    with pytest.raises(RecipeError, match="unknown setting"):
        load_recipe(path)


@pytest.mark.parametrize(
    "text",
    [
        "profile =",
        "[delete]\ninterval=0.5\ninterval=1\n",
        "collect = {kind='posts'}\ncollect.media='all'\n",
        "[collect]\noldest_first=true\norder='newest'\n",
        "[delete]\nmax_retries=-1\n",
        "delete = false\n",
        "collect = []\n",
        "[delete]\nretry_max_delay=1\n",
    ],
)
def test_invalid_or_conflicting_input_never_changes_file_or_creates_backup(tmp_path, text):
    path = write(tmp_path, text)
    with pytest.raises(RecipeError):
        plan_recipe_update(path)
    assert path.read_bytes() == text.encode()
    assert list(tmp_path.iterdir()) == [path]


def test_invalid_utf8_and_symlink_rejected(tmp_path):
    path = tmp_path / "r.toml"
    path.write_bytes(b"\xff")
    with pytest.raises(RecipeError):
        plan_recipe_update(path)
    target = write(tmp_path, OLD_RECIPE)
    link = tmp_path / "link.toml"
    link.symlink_to(target)
    with pytest.raises(RecipeError, match="symlinks"):
        plan_recipe_update(link)
    assert target.read_text() == OLD_RECIPE


def test_backup_and_atomic_replacement_preserve_private_permissions(tmp_path):
    path = write(tmp_path, OLD_RECIPE)
    path.chmod(0o600)
    update = plan_recipe_update(path)
    backup = apply_recipe_update(update)
    assert backup is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert path.stat().st_ino != update.inode


@pytest.mark.parametrize("operation", ["replace", "open", "fsync"])
def test_write_failures_preserve_original_and_clean_staging(tmp_path, monkeypatch, operation):
    path = write(tmp_path, OLD_RECIPE)
    update = plan_recipe_update(path)
    original_operation = getattr(os, operation)

    def fail(*args, **kwargs):
        if operation == "open" and "bak-" not in str(args[0]):
            return original_operation(*args, **kwargs)
        raise OSError("simulated failure")

    monkeypatch.setattr(recipe_update.os, operation, fail)
    with pytest.raises(RecipeError, match="simulated failure"):
        apply_recipe_update(update)
    assert path.read_text() == OLD_RECIPE
    assert not list(tmp_path.glob(".*.tmp"))
    backups = list(tmp_path.glob("*.bak-*"))
    if operation == "replace":
        assert len(backups) == 1 and backups[0].read_text() == OLD_RECIPE
    else:
        assert not backups


def test_failed_backup_flush_leaves_no_partial_backup(tmp_path, monkeypatch):
    path = write(tmp_path, OLD_RECIPE)
    update = plan_recipe_update(path)
    fsync = os.fsync
    calls = 0

    def fail_second(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("backup flush failed")
        return fsync(descriptor)

    monkeypatch.setattr(recipe_update.os, "fsync", fail_second)
    with pytest.raises(RecipeError, match="backup flush failed"):
        apply_recipe_update(update)
    assert path.read_text() == OLD_RECIPE
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_edit_is_not_overwritten(tmp_path):
    path = write(tmp_path, OLD_RECIPE)
    update = plan_recipe_update(path)
    path.write_text(OLD_RECIPE + "# concurrent edit\n")
    with pytest.raises(RecipeError, match="changed since preview"):
        apply_recipe_update(update)
    assert path.read_text().endswith("# concurrent edit\n")
    assert not list(tmp_path.glob("*.bak-*"))


def test_existing_backup_never_overwritten(tmp_path, monkeypatch):
    path = write(tmp_path, OLD_RECIPE)
    update = plan_recipe_update(path)
    now = recipe_update.datetime.now(recipe_update.UTC)
    monkeypatch.setattr(recipe_update, "datetime", Mock(now=Mock(return_value=now)))
    backup = tmp_path / f"r.toml.bak-{now.strftime('%Y%m%dT%H%M%S.%fZ')}"
    backup.write_text("previous backup")
    with pytest.raises(RecipeError):
        apply_recipe_update(update)
    assert backup.read_text() == "previous backup"
    assert path.read_text() == OLD_RECIPE


def test_validation_before_writing_rejects_changed_effective_values(tmp_path):
    path = write(tmp_path, OLD_RECIPE)
    update = plan_recipe_update(path)
    unsafe = replace(update, updated=update.updated.replace(b"confirm = true", b"confirm = false"))
    with pytest.raises(RecipeError, match="effective behavior"):
        apply_recipe_update(unsafe)
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_text() == OLD_RECIPE


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_only_updates_recipe_without_profile_migration_or_account_actions(tmp_path, monkeypatch, capsys, dry_run):
    path = write(tmp_path, OLD_RECIPE)
    guards = ["migrate_legacy_app_data", "configure_frozen_browser_path", "XDeleterCore", "run_recipe"]
    for name in guards:
        monkeypatch.setattr(cli, name, Mock(side_effect=AssertionError(f"must not call {name}")))
    args = ["update-recipe", str(path), *(["--dry-run"] if dry_run else [])]
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert "+confirm = true" in output.out
    if dry_run:
        assert path.read_text() == OLD_RECIPE
        assert not list(tmp_path.glob("*.bak-*"))
    else:
        assert "backup:" in output.err
        assert len(list(tmp_path.glob("*.bak-*"))) == 1


def test_cli_bad_recipe_fails_without_writes(tmp_path, capsys):
    path = write(tmp_path, "profile =")
    assert cli.main(["update-recipe", str(path)]) == cli.EXIT_USAGE
    assert "kusamushiri-cli:" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [path]


def test_cli_unknown_fields_warns_and_noop_does_not_make_backup(tmp_path, capsys):
    path = write(tmp_path, "unknown = 1\n")
    assert cli.main(["update-recipe", str(path)]) == 0
    assert "normal recipe loader still rejects" in capsys.readouterr().err
    backups = list(tmp_path.glob("*.bak-*"))
    assert cli.main(["update-recipe", str(path)]) == 0
    assert "nothing changed" in capsys.readouterr().err
    assert list(tmp_path.glob("*.bak-*")) == backups


def test_ordinary_loading_never_rewrites(tmp_path):
    path = write(tmp_path, OLD_RECIPE)
    load_recipe(path)
    assert path.read_text() == OLD_RECIPE
    assert list(tmp_path.iterdir()) == [path]


def test_cli_default_path_and_preview_without_write_calls(tmp_path, monkeypatch, capsys):
    path = tmp_path / "kusamushiri.toml"
    path.write_text(OLD_RECIPE)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "apply_recipe_update", Mock(side_effect=AssertionError("preview must not apply")))
    assert cli.main(["update-recipe", "--dry-run"]) == 0
    assert "Preview only" in capsys.readouterr().err
    assert path.read_text() == OLD_RECIPE
    assert list(tmp_path.iterdir()) == [path]


def test_missing_recipe_is_not_created(tmp_path, capsys):
    assert cli.main(["update-recipe", str(tmp_path / "missing.toml")]) == cli.EXIT_USAGE
    assert "kusamushiri-cli:" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_diff_marks_missing_trailing_newline(tmp_path):
    update = plan_recipe_update(write(tmp_path, "[delete]\nconfirm = false"))
    assert "-confirm = false\n\\ No newline at end of file\n+confirm = false\n" in update.diff


def test_edit_during_backup_is_not_overwritten(tmp_path, monkeypatch):
    path = write(tmp_path, OLD_RECIPE)
    update = plan_recipe_update(path)
    fsync = os.fsync
    calls = 0

    def edit_after_backup(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text(OLD_RECIPE + "# concurrent edit during backup\n")
        return fsync(descriptor)

    monkeypatch.setattr(recipe_update.os, "fsync", edit_after_backup)
    with pytest.raises(RecipeError, match="changed since preview"):
        apply_recipe_update(update)
    assert path.read_text().endswith("# concurrent edit during backup\n")
    backups = list(tmp_path.glob("*.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == OLD_RECIPE
    assert not list(tmp_path.glob(".*.tmp"))


def test_all_discovery_uses_wizard_names_current_directory_and_excludes_artifacts(tmp_path):
    names = ["kusamushiri.toml", "kusamushiri-z.toml", "kusamushiri-a.v2.toml", "kusamushiri-new.toml"]
    for name in names:
        (tmp_path / name).write_text(OLD_RECIPE)
    excluded = [
        "pyproject.toml",
        "settings.toml",
        "config.toml",
        ".kusamushiri-hidden.toml",
        "kusamushiri-a.toml.bak-20260101",
        "kusamushiri-a.toml.bak-20260101.toml",
        "kusamushiri-a.bak.toml",
        "kusamushiri-a.backup.toml",
        "kusamushiri-a.tmp.toml",
        "kusamushiri-a.temp-123.toml",
        "kusamushiri-a.new.toml",
        "kusamushiri-a.updated.toml",
        "kusamushiri-a.generated.toml",
        "kusamushiri-a.preview.toml",
        "kusamushiri-a.candidate.toml",
        "kusamushiri-a.review.toml",
        "kusamushiri-a.BAK.toml",
        "kusamushiri-a.toml.new.toml",
        "kusamushiri-a.bak_20260101.toml",
        "kusamushiri-a.tmp_123.toml",
    ]
    for name in excluded:
        (tmp_path / name).write_text("invalid TOML, must not be read")
    folder = tmp_path / "nested"
    folder.mkdir()
    (folder / "kusamushiri-child.toml").write_text(OLD_RECIPE)
    (tmp_path / "kusamushiri-folder.toml").mkdir()
    (tmp_path / "kusamushiri-link.toml").symlink_to(tmp_path / "kusamushiri.toml")
    assert [path.name for path in discover_update_recipes(tmp_path)] == [
        "kusamushiri.toml",
        "kusamushiri-a.v2.toml",
        "kusamushiri-new.toml",
        "kusamushiri-z.toml",
    ]


@pytest.mark.parametrize("dry_run", [False, True])
def test_all_zero_matches_is_success_without_default_creation(tmp_path, monkeypatch, capsys, dry_run):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("unrelated")
    monkeypatch.setattr(cli, "apply_recipe_update", Mock(side_effect=AssertionError("must not apply")))
    assert cli.main(["update-recipe", "--all", *(["--dry-run"] if dry_run else [])]) == 0
    output = capsys.readouterr()
    assert f"Matched 0 recipe(s) in {tmp_path}" in output.out
    assert "No matching recipes" in output.err
    assert [path.name for path in tmp_path.iterdir()] == ["pyproject.toml"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_all_lists_exact_targets_preserves_each_recipe_and_repeated_run_is_noop(tmp_path, monkeypatch, capsys, dry_run):
    monkeypatch.chdir(tmp_path)
    targets = [tmp_path / "kusamushiri.toml", tmp_path / "kusamushiri-b.toml"]
    originals = [OLD_RECIPE, OLD_RECIPE.replace("interval = 0.5", "interval = 9.0") + "extra = 42 # keep unknown\n"]
    for path, text in zip(targets, originals, strict=True):
        path.write_text(text)
    for name in ["pyproject.toml", "kusamushiri-b.tmp.toml"]:
        (tmp_path / name).write_text("do not update")
    for name in ["migrate_legacy_app_data", "configure_frozen_browser_path", "XDeleterCore", "run_recipe"]:
        monkeypatch.setattr(cli, name, Mock(side_effect=AssertionError(f"must not call {name}")))
    args = ["update-recipe", "--all", *(["--dry-run"] if dry_run else [])]
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert output.out.startswith(f"Matched 2 recipe(s) in {tmp_path}:\n  {targets[0]}\n  {targets[1]}\n")
    assert output.out.count("+confirm = true") == 2
    assert f"{targets[1]}: Preserved unknown settings: delete.extra" in output.err
    if dry_run:
        assert "2 would change, 0 unchanged" in output.err
        assert [path.read_text() for path in targets] == originals
        assert not list(tmp_path.glob("*.bak-*"))
    else:
        assert "2 updated, 0 unchanged, 0 failed" in output.err
        for path, text in zip(targets, originals, strict=True):
            assert path.read_bytes().startswith(text.encode())
            backups = list(tmp_path.glob(f"{path.name}.bak-*"))
            assert len(backups) == 1 and backups[0].read_text() == text
        existing_backups = list(tmp_path.glob("*.bak-*"))
        assert cli.main(args) == 0
        assert "0 updated, 2 unchanged, 0 failed" in capsys.readouterr().err
        assert list(tmp_path.glob("*.bak-*")) == existing_backups
    assert (tmp_path / "pyproject.toml").read_text() == "do not update"
    assert (tmp_path / "kusamushiri-b.tmp.toml").read_text() == "do not update"


@pytest.mark.parametrize("dry_run", [False, True])
def test_all_preflight_invalid_file_reports_failure_and_writes_nothing(tmp_path, monkeypatch, capsys, dry_run):
    monkeypatch.chdir(tmp_path)
    good = tmp_path / "kusamushiri.toml"
    bad = tmp_path / "kusamushiri-bad.toml"
    good.write_text(OLD_RECIPE)
    bad.write_text("profile =")
    monkeypatch.setattr(cli, "apply_recipe_update", Mock(side_effect=AssertionError("must not apply any file")))
    assert cli.main(["update-recipe", "--all", *(["--dry-run"] if dry_run else [])]) == cli.EXIT_USAGE
    output = capsys.readouterr()
    assert "Matched 2 recipe(s)" in output.out
    assert "+confirm = true" in output.out  # valid-file diff remains reviewable
    assert f"{bad}: validation failed:" in output.err
    assert "Validation failed for 1 of 2 recipes; no recipes or backups were written" in output.err
    assert good.read_text() == OLD_RECIPE and bad.read_text() == "profile ="
    assert len(list(tmp_path.iterdir())) == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["--all", "kusamushiri.toml"],
        ["kusamushiri.toml", "--all"],
        ["--all", "kusamushiri.toml", "--dry-run"],
        ["--all", "pyproject.toml"],
    ],
)
def test_all_cannot_be_combined_with_explicit_positional_path(tmp_path, monkeypatch, capsys, arguments):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "discover_update_recipes", Mock(side_effect=AssertionError("must not discover")))
    assert cli.main(["update-recipe", *arguments]) == cli.EXIT_USAGE
    assert "--all cannot be combined with a recipe path" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_all_runtime_write_failure_reports_partial_completion_and_stops(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    paths = [tmp_path / name for name in ["kusamushiri.toml", "kusamushiri-a.toml", "kusamushiri-b.toml"]]
    for path in paths:
        path.write_text(OLD_RECIPE)
    apply = cli.apply_recipe_update
    attempted = []

    def fail_second(update):
        attempted.append(update.path)
        if update.path == paths[1]:
            raise RecipeError("simulated write failure")
        return apply(update)

    monkeypatch.setattr(cli, "apply_recipe_update", fail_second)
    assert cli.main(["update-recipe", "--all"]) == cli.EXIT_FAILURE
    output = capsys.readouterr()
    assert f"Updated {paths[0]}; backup:" in output.err
    assert f"{paths[1]}: update failed: simulated write failure" in output.err
    assert "Stopped: 1 updated, 0 unchanged, 1 failed, 1 not attempted" in output.err
    assert "Earlier successful updates remain in place" in output.err
    assert attempted == paths[:2]
    assert paths[0].read_bytes().startswith(OLD_RECIPE.encode()) and paths[0].read_text() != OLD_RECIPE
    assert paths[1].read_text() == paths[2].read_text() == OLD_RECIPE
    backups = list(tmp_path.glob("*.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == OLD_RECIPE


def test_all_mixed_unchanged_and_pending_preview_is_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    complete = tmp_path / "kusamushiri.toml"
    complete.write_text(OLD_RECIPE)
    apply_recipe_update(plan_recipe_update(complete))
    pending = tmp_path / "kusamushiri-pending.toml"
    pending.write_text(OLD_RECIPE)
    snapshot = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    monkeypatch.setattr(cli, "apply_recipe_update", Mock(side_effect=AssertionError("preview must not apply")))
    assert cli.main(["update-recipe", "--all", "--dry-run"]) == 0
    output = capsys.readouterr()
    assert "1 would change, 1 unchanged" in output.err
    assert f"{complete}: Recipe already has all available defaults" in output.err
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == snapshot


def test_all_discovery_failure_has_failure_exit_status(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "discover_update_recipes", Mock(side_effect=OSError("cannot list directory")))
    assert cli.main(["update-recipe", "--all"]) == cli.EXIT_FAILURE
    assert "recipe discovery failed: cannot list directory" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
