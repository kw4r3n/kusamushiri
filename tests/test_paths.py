import errno
import os
import shutil
import stat
from pathlib import Path

import pytest

from kusamushiri import paths as x_deleter_paths


@pytest.fixture
def app_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "app-data"
    monkeypatch.setattr(x_deleter_paths, "_app_data_path_override", data_dir)
    monkeypatch.setattr(
        x_deleter_paths,
        "LEGACY_BROWSER_PROFILE_DIR",
        tmp_path / "repo-chrome-data",
    )
    return data_dir


def test_get_account_profile_dir_migrates_legacy_default_profile(app_data_dir: Path) -> None:
    legacy_profile_dir = app_data_dir / "chromium-profile"
    legacy_profile_dir.mkdir(parents=True)
    (legacy_profile_dir / "login-state.txt").write_text("session", encoding="utf-8")

    profile_dir = x_deleter_paths.get_account_profile_dir("default")

    assert profile_dir == app_data_dir / "chromium-profile-default"
    assert (profile_dir / "login-state.txt").read_text(encoding="utf-8") == "session"
    assert not legacy_profile_dir.exists()


def _fail_cross_device_rename_from(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_rename = Path.rename

    def rename(self: Path, target: Path) -> Path:
        if self == source:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)


def test_get_account_profile_dir_falls_back_when_move_crosses_filesystems(
    app_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_profile_dir = app_data_dir / "chromium-profile"
    legacy_profile_dir.mkdir(parents=True)
    (legacy_profile_dir / "login-state.txt").write_text("session", encoding="utf-8")

    _fail_cross_device_rename_from(legacy_profile_dir, monkeypatch)

    profile_dir = x_deleter_paths.get_account_profile_dir("default")

    assert profile_dir == app_data_dir / "chromium-profile-default"
    assert (profile_dir / "login-state.txt").read_text(encoding="utf-8") == "session"
    assert not legacy_profile_dir.exists()


def test_get_account_profile_dir_migrates_repo_chrome_data_when_default_is_empty(
    app_data_dir: Path,
) -> None:
    repo_profile_dir = x_deleter_paths.LEGACY_BROWSER_PROFILE_DIR
    repo_profile_dir.mkdir(parents=True)
    (repo_profile_dir / "profile.txt").write_text("legacy", encoding="utf-8")

    profile_dir = x_deleter_paths.get_account_profile_dir(None)

    assert profile_dir == app_data_dir / "chromium-profile-default"
    assert (profile_dir / "profile.txt").read_text(encoding="utf-8") == "legacy"
    assert not repo_profile_dir.exists()


def test_get_account_profile_dir_normalizes_account_name(app_data_dir: Path) -> None:
    profile_dir = x_deleter_paths.get_account_profile_dir(" @foo/bar\\baz ")

    assert profile_dir == app_data_dir / "chromium-profile-foo_bar_baz"


def test_list_saved_accounts_returns_default_first_and_sorted(app_data_dir: Path) -> None:
    (app_data_dir / "chromium-profile-zeta").mkdir(parents=True)
    (app_data_dir / "chromium-profile-default").mkdir()
    (app_data_dir / "chromium-profile-alpha").mkdir()

    assert x_deleter_paths.list_saved_accounts() == ["default", "alpha", "zeta"]


def test_list_saved_accounts_does_not_create_app_data_dir(app_data_dir: Path) -> None:
    assert x_deleter_paths.list_saved_accounts() == []
    assert not app_data_dir.exists()


def test_failed_cross_device_copy_leaves_no_target_and_retries(
    app_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_profile_dir = app_data_dir / "chromium-profile"
    legacy_profile_dir.mkdir(parents=True)
    (legacy_profile_dir / "a.txt").write_text("a", encoding="utf-8")
    (legacy_profile_dir / "b.txt").write_text("b", encoding="utf-8")
    _fail_cross_device_rename_from(legacy_profile_dir, monkeypatch)
    original_copytree = shutil.copytree

    def partial_copytree(source: Path, target: Path, **kwargs: object) -> Path:
        Path(target).mkdir()
        shutil.copy2(Path(source) / "a.txt", Path(target) / "a.txt")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(x_deleter_paths.shutil, "copytree", partial_copytree)
    with pytest.raises(OSError):
        x_deleter_paths.get_account_profile_dir("default")

    target = app_data_dir / "chromium-profile-default"
    assert not target.exists()
    assert not (app_data_dir / ".chromium-profile-default.migrating").exists()
    assert sorted(path.name for path in legacy_profile_dir.iterdir()) == ["a.txt", "b.txt"]

    monkeypatch.setattr(x_deleter_paths.shutil, "copytree", original_copytree)
    profile_dir = x_deleter_paths.get_account_profile_dir("default")

    assert sorted(path.name for path in profile_dir.iterdir()) == ["a.txt", "b.txt"]
    assert not legacy_profile_dir.exists()


def test_stale_migration_staging_dir_is_removed_and_not_listed(app_data_dir: Path) -> None:
    staging = app_data_dir / ".chromium-profile-default.migrating"
    staging.mkdir(parents=True)
    (staging / "partial.txt").write_text("partial", encoding="utf-8")
    legacy_profile_dir = app_data_dir / "chromium-profile"
    legacy_profile_dir.mkdir()
    (legacy_profile_dir / "login-state.txt").write_text("session", encoding="utf-8")

    assert x_deleter_paths.list_saved_accounts() == ["default"]
    profile_dir = x_deleter_paths.get_account_profile_dir("default")

    assert not staging.exists()
    assert [path.name for path in profile_dir.iterdir()] == ["login-state.txt"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_app_data_and_profile_dirs_are_private(app_data_dir: Path) -> None:
    app_data_dir.mkdir(mode=0o755)
    app_data_dir.chmod(0o755)
    existing_profile = app_data_dir / "chromium-profile-alice"
    existing_profile.mkdir(mode=0o755)
    existing_profile.chmod(0o755)

    x_deleter_paths.get_account_profile_dir("alice")
    new_profile = x_deleter_paths.get_account_profile_dir("bob")

    for path in (app_data_dir, existing_profile, new_profile):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


@pytest.mark.parametrize("frozen", [False, True])
def test_configure_frozen_browser_path_uses_app_data_only_when_frozen(monkeypatch, app_data_dir, frozen):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(x_deleter_paths.sys, "frozen", frozen, raising=False)

    x_deleter_paths.configure_frozen_browser_path()

    expected = str(app_data_dir / "ms-playwright") if frozen else None
    assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == expected


def test_migrate_legacy_app_data_moves_old_profiles_once(app_data_dir: Path, tmp_path: Path) -> None:
    legacy_dir = tmp_path / "xposdeleter"
    (legacy_dir / "chromium-profile-default").mkdir(parents=True)
    (legacy_dir / "chromium-profile-default" / "state.txt").write_text("session", encoding="utf-8")

    assert x_deleter_paths.migrate_legacy_app_data(legacy_dir) is True
    assert (app_data_dir / "chromium-profile-default" / "state.txt").read_text(encoding="utf-8") == "session"
    assert not legacy_dir.exists()
    assert x_deleter_paths.migrate_legacy_app_data(legacy_dir) is False


def test_migrate_legacy_app_data_keeps_existing_current_data(app_data_dir: Path, tmp_path: Path) -> None:
    legacy_dir = tmp_path / "xposdeleter"
    (legacy_dir / "chromium-profile-old").mkdir(parents=True)
    (app_data_dir / "chromium-profile-new").mkdir(parents=True)

    assert x_deleter_paths.migrate_legacy_app_data(legacy_dir) is False
    assert (legacy_dir / "chromium-profile-old").exists()
    assert not (app_data_dir / "chromium-profile-old").exists()
