from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from kusamushiri import paths as x_deleter_paths
from kusamushiri import settings as x_deleter_settings
from kusamushiri.settings import AccountSettings, AccountSettingsManager


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    return tmp_path / "settings.ini"


@pytest.fixture
def qsettings(settings_file: Path) -> QSettings:
    settings = QSettings(str(settings_file), QSettings.Format.IniFormat)
    settings.clear()
    return settings


def test_save_account_settings_writes_prefixed_values_and_current_account(qsettings: QSettings) -> None:
    manager = AccountSettingsManager(qsettings)
    account_settings = AccountSettings(
        username="alice",
        search_mode="search",
        max_posts=123,
        media_filter="with_media",
        post_kind_filter="all",
        reply_only=True,
        min_likes=10,
        min_replies=2,
        action_interval_seconds=2.5,
        since_date="2026-01-02",
        until_date="2026-02-03",
        since_date_enabled=True,
        until_date_enabled=True,
    )

    manager.save_account_settings(" @alice/main ", account_settings)

    assert qsettings.value("current_account", type=str) == "alice_main"
    assert qsettings.value("account_alice_main_username", type=str) == "alice"
    assert qsettings.value("account_alice_main_search_mode", type=str) == "search"
    assert qsettings.value("account_alice_main_max_posts", type=int) == 123
    assert qsettings.value("account_alice_main_reply_only", type=bool) is True
    assert qsettings.value("account_alice_main_action_interval_seconds", type=float) == 2.5
    assert qsettings.value("account_alice_main_since_date", type=str) == "2026-01-02"


def test_load_account_settings_reads_prefixed_values(qsettings: QSettings) -> None:
    prefix = "account_bob_"
    qsettings.setValue(f"{prefix}username", "bob")
    qsettings.setValue(f"{prefix}search_mode", "search")
    qsettings.setValue(f"{prefix}max_posts", 77)
    qsettings.setValue(f"{prefix}media_filter", "without_media")
    qsettings.setValue(f"{prefix}post_kind_filter", "reposts")
    qsettings.setValue(f"{prefix}reply_only", True)
    qsettings.setValue(f"{prefix}min_likes", 8)
    qsettings.setValue(f"{prefix}min_replies", 3)
    qsettings.setValue(f"{prefix}action_interval_seconds", 4.5)
    qsettings.setValue(f"{prefix}since_date", "2026-03-04")
    qsettings.setValue(f"{prefix}until_date", "2026-04-05")
    qsettings.setValue(f"{prefix}since_date_enabled", True)
    qsettings.setValue(f"{prefix}until_date_enabled", False)

    loaded = AccountSettingsManager(qsettings).load_account_settings("bob")

    assert loaded == AccountSettings(
        username="bob",
        search_mode="search",
        max_posts=77,
        media_filter="without_media",
        post_kind_filter="reposts",
        reply_only=True,
        min_likes=8,
        min_replies=3,
        action_interval_seconds=4.5,
        since_date="2026-03-04",
        until_date="2026-04-05",
        since_date_enabled=True,
        until_date_enabled=False,
    )


def test_load_account_settings_falls_back_to_legacy_unprefixed_values(qsettings: QSettings) -> None:
    qsettings.setValue("username", "legacy")
    qsettings.setValue("max_posts", 88)

    loaded = AccountSettingsManager(qsettings).load_account_settings("legacy")

    assert loaded.username == "legacy"
    assert loaded.max_posts == 88
    assert loaded.search_mode == "profile"


def test_rename_and_delete_profile_preserves_other_accounts(qsettings, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(x_deleter_paths, "_app_data_path_override", tmp_path / "data")
    manager = AccountSettingsManager(qsettings)
    source = x_deleter_paths.get_account_profile_dir("alice")
    (source / "session").write_text("cookie", encoding="utf-8")
    manager.save_account_settings("alice", AccountSettings(username="alice", max_posts=123))
    manager.save_account_settings("alice_other", AccountSettings(username="other"))
    manager.rename_account("alice", "renamed")
    assert not source.exists()
    assert (x_deleter_paths.get_account_profile_dir("renamed") / "session").read_text() == "cookie"
    assert manager.load_account_settings("renamed").max_posts == 123
    assert not qsettings.contains("account_alice_username")
    manager.delete_account("renamed")
    assert "renamed" not in manager.list_accounts()
    assert not qsettings.contains("account_renamed_username")
    assert manager.load_account_settings("alice_other").username == "other"
    assert manager.get_last_account() == "default"


def test_profile_management_refuses_default_and_existing_target(qsettings, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(x_deleter_paths, "_app_data_path_override", tmp_path / "data")
    manager = AccountSettingsManager(qsettings)
    for name in ("alice", "bob"):
        x_deleter_paths.get_account_profile_dir(name)
    with pytest.raises(ValueError):
        manager.rename_account("alice", "bob")
    with pytest.raises(ValueError):
        manager.rename_account("default", "new")
    with pytest.raises(ValueError):
        manager.delete_account(" @default ")
    assert manager.list_accounts() == ["alice", "bob"]


def test_get_last_account_normalizes_saved_value(qsettings: QSettings) -> None:
    qsettings.setValue("current_account", " @foo/bar ")

    assert AccountSettingsManager(qsettings).get_last_account() == "foo_bar"


def test_list_accounts_delegates_to_saved_profile_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qsettings: QSettings,
) -> None:
    app_data_dir = tmp_path / "app-data"
    (app_data_dir / "chromium-profile-zeta").mkdir(parents=True)
    (app_data_dir / "chromium-profile-default").mkdir()
    monkeypatch.setattr(x_deleter_paths, "_app_data_path_override", app_data_dir)
    monkeypatch.setattr(x_deleter_settings, "list_saved_accounts", x_deleter_paths.list_saved_accounts)

    assert AccountSettingsManager(qsettings).list_accounts() == ["default", "zeta"]


def test_save_account_settings_removes_cleared_dates(qsettings: QSettings) -> None:
    manager = AccountSettingsManager(qsettings)
    manager.save_account_settings("alice", AccountSettings(since_date="2026-01-02", until_date="2026-02-03"))
    manager.save_account_settings("alice", AccountSettings(since_date=None, until_date="2026-03-04"))

    assert not qsettings.contains("account_alice_since_date")
    loaded = manager.load_account_settings("alice")
    assert loaded.since_date is None
    assert loaded.until_date == "2026-03-04"


def test_rename_account_rejects_case_insensitive_duplicates(qsettings, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(x_deleter_paths, "_app_data_path_override", tmp_path / "data")
    manager = AccountSettingsManager(qsettings)
    for name in ("Alice", "bob"):
        x_deleter_paths.get_account_profile_dir(name)
    with pytest.raises(ValueError):
        manager.rename_account("bob", "ALICE")
    with pytest.raises(ValueError):
        manager.rename_account("bob", "Default")

    manager.save_account_settings("Alice", AccountSettings(username="alice", max_posts=7))
    manager.rename_account("Alice", "alice")

    assert manager.list_accounts() == ["alice", "bob"]
    assert manager.load_account_settings("alice").max_posts == 7
    assert not qsettings.contains("account_Alice_max_posts")


def test_rename_account_rolls_back_folder_and_settings_when_save_fails(qsettings, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(x_deleter_paths, "_app_data_path_override", tmp_path / "data")
    manager = AccountSettingsManager(qsettings)
    source = x_deleter_paths.get_account_profile_dir("alice")
    (source / "session").write_text("cookie", encoding="utf-8")
    manager.save_account_settings("alice", AccountSettings(username="alice", max_posts=123))
    original_save = manager.save_account_settings

    def failing_save(account_name: str, settings: AccountSettings) -> None:
        if account_name == "renamed":
            raise OSError("disk full")
        original_save(account_name, settings)

    monkeypatch.setattr(manager, "save_account_settings", failing_save)
    with pytest.raises(OSError):
        manager.rename_account("alice", "renamed")

    assert (source / "session").read_text(encoding="utf-8") == "cookie"
    assert manager.list_accounts() == ["alice"]
    assert manager.load_account_settings("alice").max_posts == 123
    assert not qsettings.contains("account_renamed_max_posts")


def test_delete_account_surfaces_rmtree_failure_without_live_settings(qsettings, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(x_deleter_paths, "_app_data_path_override", tmp_path / "data")
    manager = AccountSettingsManager(qsettings)
    x_deleter_paths.get_account_profile_dir("alice")
    manager.save_account_settings("alice", AccountSettings(username="alice"))

    def failing_rmtree(path: Path) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(x_deleter_settings.shutil, "rmtree", failing_rmtree)
    with pytest.raises(OSError):
        manager.delete_account("alice")

    assert not qsettings.contains("account_alice_username")
    assert manager.get_last_account() == "default"
    assert "alice" in manager.list_accounts()

    monkeypatch.undo()
    monkeypatch.setattr(x_deleter_paths, "_app_data_path_override", tmp_path / "data")
    manager.delete_account("alice")
    assert manager.list_accounts() == []


def test_migrate_legacy_settings_copies_only_into_empty_settings(tmp_path: Path, qsettings: QSettings) -> None:
    legacy = QSettings(str(tmp_path / "legacy.ini"), QSettings.Format.IniFormat)
    legacy.setValue("default/username", "old_user")

    assert x_deleter_settings.migrate_legacy_settings(qsettings, legacy) is True
    assert qsettings.value("default/username") == "old_user"
    assert legacy.value("default/username") == "old_user"

    legacy.setValue("default/username", "changed")
    assert x_deleter_settings.migrate_legacy_settings(qsettings, legacy) is False
    assert qsettings.value("default/username") == "old_user"
