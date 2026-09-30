import contextlib
import shutil
from dataclasses import dataclass, fields
from typing import TypeVar, overload

from PySide6.QtCore import QSettings

from kusamushiri.models import DEFAULT_ACTION_INTERVAL_SECONDS
from kusamushiri.paths import (
    ACCOUNT_PROFILE_PREFIX,
    DEFAULT_ACCOUNT_NAME,
    get_app_data_dir,
    list_saved_accounts,
    normalize_account_name,
)

SettingValue = TypeVar("SettingValue", str, int, float, bool)


@dataclass(frozen=True, slots=True)
class AccountSettings:
    username: str = ""
    search_mode: str = "profile"
    max_posts: int = 50
    media_filter: str = "all"
    post_kind_filter: str = "posts"
    reply_only: bool = False
    min_likes: int = 0
    min_replies: int = 0
    action_interval_seconds: float = DEFAULT_ACTION_INTERVAL_SECONDS
    since_date: str | None = None
    until_date: str | None = None
    since_date_enabled: bool = False
    until_date_enabled: bool = False
    auto_save_interval_seconds: int = 60
    include_keywords: str = ""
    exclude_keywords: str = ""


LEGACY_SETTINGS_SCOPE = ("Unknown Organization", "xposdeleter")


def migrate_legacy_settings(target: QSettings | None = None, legacy: QSettings | None = None) -> bool:
    """Copy settings saved under the pre-rename app name once; the old copy is kept."""
    target = target if target is not None else QSettings()
    legacy = legacy if legacy is not None else QSettings(*LEGACY_SETTINGS_SCOPE)
    if target.allKeys() or not legacy.allKeys():
        return False
    for key in legacy.allKeys():
        target.setValue(key, legacy.value(key))
    target.sync()
    return True


class AccountSettingsManager:
    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings if settings is not None else QSettings()

    def save_account_settings(self, account_name: str, settings: AccountSettings) -> None:
        normalized_account_name = normalize_account_name(account_name)
        prefix = self._account_prefix(normalized_account_name)

        self._settings.setValue("current_account", normalized_account_name)
        self._settings.setValue(f"{prefix}username", settings.username)
        self._settings.setValue(f"{prefix}search_mode", settings.search_mode)
        self._settings.setValue(f"{prefix}max_posts", settings.max_posts)
        self._settings.setValue(f"{prefix}media_filter", settings.media_filter)
        self._settings.setValue(f"{prefix}post_kind_filter", settings.post_kind_filter)
        self._settings.setValue(f"{prefix}reply_only", settings.reply_only)
        self._settings.setValue(f"{prefix}min_likes", settings.min_likes)
        self._settings.setValue(f"{prefix}min_replies", settings.min_replies)
        self._settings.setValue(f"{prefix}action_interval_seconds", settings.action_interval_seconds)
        self._settings.setValue(f"{prefix}since_date_enabled", settings.since_date_enabled)
        self._settings.setValue(f"{prefix}until_date_enabled", settings.until_date_enabled)
        self._settings.setValue(f"{prefix}auto_save_interval_seconds", settings.auto_save_interval_seconds)
        self._settings.setValue(f"{prefix}include_keywords", settings.include_keywords)
        self._settings.setValue(f"{prefix}exclude_keywords", settings.exclude_keywords)
        for key, value in (("since_date", settings.since_date), ("until_date", settings.until_date)):
            if value is None:
                self._settings.remove(f"{prefix}{key}")
            else:
                self._settings.setValue(f"{prefix}{key}", value)
        self._settings.sync()

    def load_account_settings(self, account_name: str) -> AccountSettings:
        prefix = self._account_prefix(normalize_account_name(account_name))
        return AccountSettings(
            username=self._account_setting_value(prefix, "username", "", str),
            search_mode=self._account_setting_value(prefix, "search_mode", "profile", str),
            max_posts=self._account_setting_value(prefix, "max_posts", 50, int),
            media_filter=self._account_setting_value(prefix, "media_filter", "all", str),
            post_kind_filter=self._account_setting_value(prefix, "post_kind_filter", "posts", str),
            reply_only=self._account_setting_value(prefix, "reply_only", False, bool),
            min_likes=self._account_setting_value(prefix, "min_likes", 0, int),
            min_replies=self._account_setting_value(prefix, "min_replies", 0, int),
            action_interval_seconds=self._account_setting_value(
                prefix,
                "action_interval_seconds",
                DEFAULT_ACTION_INTERVAL_SECONDS,
                float,
            ),
            since_date=self._account_setting_value(prefix, "since_date", None, str),
            until_date=self._account_setting_value(prefix, "until_date", None, str),
            since_date_enabled=self._account_setting_value(prefix, "since_date_enabled", False, bool),
            until_date_enabled=self._account_setting_value(prefix, "until_date_enabled", False, bool),
            auto_save_interval_seconds=self._account_setting_value(prefix, "auto_save_interval_seconds", 60, int),
            include_keywords=self._account_setting_value(prefix, "include_keywords", "", str),
            exclude_keywords=self._account_setting_value(prefix, "exclude_keywords", "", str),
        )

    def list_accounts(self) -> list[str]:
        return list_saved_accounts()

    def rename_account(self, old_name: str, new_name: str) -> None:
        old_name = normalize_account_name(old_name)
        if not new_name.strip().removeprefix("@").strip():
            raise ValueError("プロファイル名を入力してください。")
        if old_name == DEFAULT_ACCOUNT_NAME:
            raise ValueError("default プロファイルの名前は変更できません。")
        new_name = normalize_account_name(new_name)
        if new_name == old_name:
            return
        folded_new_name = new_name.casefold()
        if folded_new_name == DEFAULT_ACCOUNT_NAME.casefold() or any(
            account.casefold() == folded_new_name and account != old_name for account in self.list_accounts()
        ):
            raise ValueError("同じ名前のプロファイルが既にあります。")
        source = get_app_data_dir() / f"{ACCOUNT_PROFILE_PREFIX}{old_name}"
        target = source.parent / f"{ACCOUNT_PROFILE_PREFIX}{new_name}"
        # On case-insensitive filesystems "Foo" -> "foo" resolves to the same directory.
        if target.exists() and not (source.exists() and target.samefile(source)):
            raise ValueError("同じ名前のプロファイルが既にあります。")
        settings = self.load_account_settings(old_name)
        source.rename(target)
        try:
            # Remove before saving so case-insensitive backends keep the new keys.
            self._remove_account_settings(old_name)
            self.save_account_settings(new_name, settings)
            self._raise_on_settings_error()
        except BaseException:
            with contextlib.suppress(OSError):
                self._remove_account_settings(new_name)
                self.save_account_settings(old_name, settings)
            with contextlib.suppress(OSError):
                target.rename(source)
            raise

    def delete_account(self, account_name: str) -> None:
        account_name = normalize_account_name(account_name)
        if account_name == DEFAULT_ACCOUNT_NAME:
            raise ValueError("default プロファイルは削除できません。")
        profile_dir = get_app_data_dir() / f"{ACCOUNT_PROFILE_PREFIX}{account_name}"
        # Drop settings first: a failed rmtree then leaves a still-listed profile
        # that can be deleted again, never stale settings for a half-deleted one.
        self._remove_account_settings(account_name)
        self._settings.setValue("current_account", DEFAULT_ACCOUNT_NAME)
        self._settings.sync()
        self._raise_on_settings_error()
        if profile_dir.exists():
            shutil.rmtree(profile_dir)

    def _raise_on_settings_error(self) -> None:
        status = self._settings.status()
        if status != QSettings.Status.NoError:
            raise OSError(f"設定を保存できませんでした: {status.name}")

    def _remove_account_settings(self, account_name: str) -> None:
        prefix = self._account_prefix(account_name)
        for field in fields(AccountSettings):
            self._settings.remove(f"{prefix}{field.name}")
        self._settings.sync()

    def get_last_account(self) -> str:
        current_account = self._settings.value("current_account", DEFAULT_ACCOUNT_NAME, type=str)
        return normalize_account_name(
            current_account if isinstance(current_account, str) else DEFAULT_ACCOUNT_NAME
        )

    @overload
    def _account_setting_value(
        self,
        prefix: str,
        key: str,
        default: None,
        value_type: type[str],
    ) -> str | None: ...

    @overload
    def _account_setting_value(
        self,
        prefix: str,
        key: str,
        default: SettingValue,
        value_type: type[SettingValue],
    ) -> SettingValue: ...

    def _account_setting_value(
        self,
        prefix: str,
        key: str,
        default: SettingValue | None,
        value_type: type[SettingValue],
    ) -> SettingValue | None:
        account_key = f"{prefix}{key}"
        if self._settings.contains(account_key):
            value = self._settings.value(account_key, default, type=value_type)
        elif self._settings.contains(key):
            value = self._settings.value(key, default, type=value_type)
        else:
            return default
        if value is None:
            return default
        if isinstance(value, value_type):
            return value
        return default

    def _account_prefix(self, account_name: str) -> str:
        return f"account_{account_name}_"
