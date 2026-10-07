import subprocess
import sys
import warnings
from pathlib import Path
from urllib.parse import urljoin

from playwright._impl._driver import compute_driver_executable, get_driver_env
from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from kusamushiri.logger import logger
from kusamushiri.parsing import extract_profile_username
from kusamushiri.paths import ensure_private_directory, get_account_profile_dir

BASE_X_URL = "https://x.com/"
HOME_PATH = "home"
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
NAVIGATION_TIMEOUT_MS = 30_000
LOGIN_CHECK_SELECTORS = (
    '[data-testid="SideNav_NewTweet_Button"]',
    '[data-testid="AppTabBar_Profile_Link"]',
)
MISSING_BROWSER_MESSAGE = "Executable doesn't exist"
BROWSER_INSTALL_TIMEOUT_SECONDS = 600
PROFILE_LINK_SELECTORS = (
    '[data-testid="AppTabBar_Profile_Link"]',
    'a[aria-label="Profile"]',
)


def find_first_visible_locator(root: Page | Locator, selectors: tuple[str, ...]) -> Locator | None:
    """Find the first visible locator matching any of the given selectors."""
    for selector in selectors:
        locator = root.locator(selector)
        if locator.count() > 0 and locator.first.is_visible():
            return locator.first
    return None


def install_chromium() -> None:
    """Download Playwright's Chromium with the bundled driver (also works in frozen builds)."""
    node, cli = compute_driver_executable()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    logger.info("Installing Playwright Chromium. This can take a few minutes.")
    subprocess.run(
        [node, cli, "install", "chromium"],
        env=get_driver_env(),
        check=True,
        timeout=BROWSER_INSTALL_TIMEOUT_SECONDS,
        creationflags=creationflags,
    )
    logger.info("Playwright Chromium installed.")


class BrowserManager:
    def __init__(self, *, headless: bool = False) -> None:
        self.headless = headless
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.profile_dir: Path | None = None

    def _require_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized.")
        return self.page

    def start(self, user_data_dir: str | Path | None = None, account_name: str | None = None) -> None:
        """ブラウザを起動し、指定されたディレクトリにプロファイルを保存してログイン状態を維持する

        Args:
            user_data_dir: カスタムプロファイルディレクトリ。指定時はaccount_nameより優先
            account_name: アカウント名。profile_dirが指定されない場合に使用
        """
        profile_dir = Path(user_data_dir) if user_data_dir is not None else get_account_profile_dir(account_name)
        ensure_private_directory(profile_dir)

        if (
            self.context is not None
            and self.page is not None
            and self.profile_dir == profile_dir
        ):
            try:
                if not self.page.is_closed():
                    # The worker can sit in its command queue while the user
                    # closes Chromium. Dispatch pending Playwright events first.
                    self.page.wait_for_timeout(0)
                if not self.page.is_closed():
                    logger.info("Browser is already running. Reusing existing context.")
                    return
            except PlaywrightError:
                logger.info("Previous browser page is unavailable. Restarting browser.")

        if self.context is not None or self.playwright is not None:
            logger.info("Restarting browser to switch profile from %s to %s.", self.profile_dir, profile_dir)
            self.stop()

        logger.info("Starting browser with profile directory: %s", profile_dir)
        try:
            if not self.playwright:
                self.playwright = sync_playwright().start()

            try:
                self.context = self._launch_context(self.playwright, profile_dir, headless=self.headless)
            except PlaywrightError as error:
                if MISSING_BROWSER_MESSAGE not in str(error):
                    raise
                install_chromium()
                self.context = self._launch_context(self.playwright, profile_dir, headless=self.headless)
            self.browser = self.context.browser
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.profile_dir = profile_dir
            logger.info("Browser successfully launched.")
        except Exception:
            logger.exception("Failed to start browser.")
            self.stop()
            raise

    @staticmethod
    def _launch_context(playwright: Playwright, profile_dir: Path, *, headless: bool = False) -> BrowserContext:
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            args=["--disable-blink-features=AutomationControlled"],
        )

    def stop(self) -> None:
        """ブラウザを終了する"""
        logger.info("Stopping browser.")
        try:
            try:
                if self.context:
                    self.context.close()
            finally:
                if self.playwright:
                    self.playwright.stop()
        except Exception:
            logger.exception("Error while stopping browser.")
        finally:
            self.browser = None
            self.context = None
            self.page = None
            self.playwright = None
            self.profile_dir = None

    def go_to_home(self) -> None:
        """X のホームへ遷移"""
        page = self._require_page()
        home_url = urljoin(BASE_X_URL, HOME_PATH)
        logger.debug("Navigating to %s", home_url)
        try:
            page.goto(home_url, timeout=NAVIGATION_TIMEOUT_MS)
        except Exception:
            logger.exception("Failed to navigate to home page.")
            raise

    def go_to_twitter(self) -> None:
        warnings.warn("go_to_twitter() is deprecated, use go_to_home()", DeprecationWarning, stacklevel=2)
        logger.warning("go_to_twitter() called - deprecated, delegating to go_to_home()")
        self.go_to_home()

    def is_logged_in(self) -> bool:
        """現在ログイン状態かどうかを簡易判定する"""
        if not self.page:
            logger.warning("is_logged_in called but page object is None")
            return False
        page = self.page
        try:
            is_visible = any(
                page.locator(selector).count() > 0 and page.locator(selector).first.is_visible()
                for selector in LOGIN_CHECK_SELECTORS
            )
            logger.debug("is_logged_in check result: %s", is_visible)
            return is_visible
        except Exception:
            logger.exception("Exception during login check.")
            return False

    def get_logged_in_username(self) -> str | None:
        page = self._require_page()
        profile_link = find_first_visible_locator(page, PROFILE_LINK_SELECTORS)
        if profile_link is None:
            return None
        return extract_profile_username(profile_link.get_attribute("href"))
