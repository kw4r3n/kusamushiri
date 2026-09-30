import contextlib
import os
import shutil
import sys
from pathlib import Path

from platformdirs import user_data_dir, user_log_dir

APP_NAME = "kusamushiri"
LEGACY_APP_NAME = "xposdeleter"
DEFAULT_ACCOUNT_NAME = "default"
ACCOUNT_PROFILE_PREFIX = "chromium-profile-"
LEGACY_PROFILE_DIR_NAME = "chromium-profile"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_BROWSER_PROFILE_DIR = PROJECT_ROOT / "chrome_data"
PRIVATE_DIRECTORY_MODE = 0o700
MIGRATION_STAGING_SUFFIX = ".migrating"
_app_data_path_override: Path | None = None


def ensure_private_directory(path: Path) -> Path:
    """ディレクトリを作成し、POSIXでは所有者のみアクセス可能にする"""
    path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    if os.name == "posix":
        # Some filesystems (e.g. FAT mounts) do not support POSIX modes.
        with contextlib.suppress(OSError):
            path.chmod(PRIVATE_DIRECTORY_MODE)
    return path


def _ensure_directory(path: Path) -> Path:
    return ensure_private_directory(path)


def _get_app_data_path() -> Path:
    if _app_data_path_override is not None:
        return _app_data_path_override
    return Path(user_data_dir(APP_NAME, appauthor=False))


def migrate_legacy_app_data(legacy_dir: Path | None = None) -> bool:
    """Move profiles saved under the pre-rename app name (xposdeleter) once.

    Must run before anything creates the current data directory.
    """
    target = _get_app_data_path()
    source = legacy_dir if legacy_dir is not None else Path(user_data_dir(LEGACY_APP_NAME, appauthor=False))
    if _has_directory_contents(target) or not _has_directory_contents(source):
        return False
    ensure_private_directory(target.parent)
    if target.exists():
        target.rmdir()
    _move_directory_atomically(source, target)
    return True


def get_app_data_dir() -> Path:
    return _ensure_directory(_get_app_data_path())


def configure_frozen_browser_path() -> None:
    """Keep Chromium downloads in the user data directory for packaged builds.

    A frozen app bundle may be read-only (macOS .app, Program Files), so the
    Playwright browser cache must not live next to the bundled driver.
    """
    if getattr(sys, "frozen", False):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(get_app_data_dir() / "ms-playwright")


def _has_directory_contents(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def normalize_account_name(account_name: str | None) -> str:
    if account_name is None:
        return DEFAULT_ACCOUNT_NAME
    normalized = account_name.strip().removeprefix("@")
    if not normalized:
        return DEFAULT_ACCOUNT_NAME
    return normalized.replace("/", "_").replace("\\", "_")


def _profile_dir_for_account(app_data_dir: Path, account_name: str) -> Path:
    return app_data_dir / f"{ACCOUNT_PROFILE_PREFIX}{account_name}"


def _migration_staging_dir(target: Path) -> Path:
    # Hidden and without the profile prefix, so it is never listed as an account.
    return target.with_name(f".{target.name}{MIGRATION_STAGING_SUFFIX}")


def _move_directory_atomically(source: Path, target: Path) -> None:
    """sourceをtargetへ移動する。失敗時はtargetを作らずsourceを残す"""
    try:
        # Same filesystem: a single atomic rename.
        source.rename(target)
        return
    except OSError:
        # Typically EXDEV (cross-device); fall back to a staged copy.
        pass

    staging = _migration_staging_dir(target)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(source, staging, symlinks=True)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # target is complete; a leftover legacy copy is harmless and not re-migrated.
    shutil.rmtree(source, ignore_errors=True)


def _migrate_legacy_default_profile(target: Path, legacy_profile_dir: Path) -> None:
    # A staging dir left by an interrupted copy is never the only copy of the data.
    shutil.rmtree(_migration_staging_dir(target), ignore_errors=True)
    if _has_directory_contents(target):
        return

    for source in (legacy_profile_dir, LEGACY_BROWSER_PROFILE_DIR):
        if source == target or not _has_directory_contents(source):
            continue
        ensure_private_directory(target.parent)
        if target.exists() and not _has_directory_contents(target):
            target.rmdir()
        _move_directory_atomically(source, target)
        return


def get_account_profile_dir(account_name: str | None = None) -> Path:
    """アカウント別のブラウザプロファイルディレクトリを取得

    Args:
        account_name: アカウント名。Noneの場合はデフォルトアカウント
    """
    app_data_dir = get_app_data_dir()
    normalized_account_name = normalize_account_name(account_name)
    target = _profile_dir_for_account(app_data_dir, normalized_account_name)
    if normalized_account_name == DEFAULT_ACCOUNT_NAME:
        _migrate_legacy_default_profile(
            target,
            app_data_dir / LEGACY_PROFILE_DIR_NAME,
        )
    return _ensure_directory(target)


def get_browser_profile_dir() -> Path:
    """レガシー互換: デフォルトアカウントのプロファイルディレクトリ"""
    return get_account_profile_dir(DEFAULT_ACCOUNT_NAME)


def list_saved_accounts() -> list[str]:
    """保存済みのアカウント一覧を取得"""
    app_data_dir = _get_app_data_path()
    if not app_data_dir.exists():
        return []

    account_names: set[str] = set()
    if _has_directory_contents(app_data_dir / LEGACY_PROFILE_DIR_NAME):
        account_names.add(DEFAULT_ACCOUNT_NAME)

    for path in app_data_dir.iterdir():
        if not path.is_dir() or not path.name.startswith(ACCOUNT_PROFILE_PREFIX):
            continue
        account_name = path.name[len(ACCOUNT_PROFILE_PREFIX):]
        if account_name:
            account_names.add(account_name)

    return sorted(
        account_names,
        key=lambda account_name: (account_name != DEFAULT_ACCOUNT_NAME, account_name.casefold()),
    )


def get_legacy_browser_profile_dir() -> Path:
    """レガシー単一プロファイルディレクトリ（移行用）"""
    return get_app_data_dir() / LEGACY_PROFILE_DIR_NAME


def get_log_file_path() -> Path:
    log_dir = _ensure_directory(Path(user_log_dir(APP_NAME, appauthor=False)))
    return log_dir / "kusamushiri.log"
