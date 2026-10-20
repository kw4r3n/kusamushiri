import subprocess
import sys
import time
import warnings
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlparse

from playwright._impl._driver import compute_driver_executable, get_driver_env
from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, Request, Response, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from kusamushiri.ancillary_isolation import AncillaryIsolation
from kusamushiri.i18n import tr
from kusamushiri.logger import logger
from kusamushiri.network_diagnostics import NetworkDiagnostics
from kusamushiri.parsing import extract_profile_username
from kusamushiri.paths import ensure_private_directory, get_account_profile_dir
from kusamushiri.progress import ActionJournal
from kusamushiri.traffic_policy import TrafficMode, TrafficPolicy

BASE_X_URL = "https://x.com/"
HOME_PATH = "home"
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
HEADLESS_CHANNEL = "chromium"  # new headless mode of the full Chromium build
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
# 429 Too Many Requests, and 420 which Twitter historically sent for the same limit.
RATE_LIMIT_STATUSES = frozenset({420, 429})
RATE_LIMITED_HOSTS = ("x.com", "twitter.com")


def is_rate_limit_response(url: str, status: int) -> bool:
    """Whether X itself refused a request for sending too many."""
    if status not in RATE_LIMIT_STATUSES:
        return False
    host = (urlparse(url).hostname or "").lower()
    return any(host == name or host.endswith(f".{name}") for name in RATE_LIMITED_HOSTS)


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


def _headed_user_agent(playwright: Playwright) -> str:
    """Read the installed browser's own UA so the platform and version stay exact."""
    browser = playwright.chromium.launch(channel=HEADLESS_CHANNEL, headless=True)
    try:
        user_agent = browser.new_page().evaluate("navigator.userAgent")
    finally:
        browser.close()
    return str(user_agent).replace("HeadlessChrome/", "Chrome/")


class BrowserManager:
    def __init__(self, *, headless: bool = False) -> None:
        self.headless = headless
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.profile_dir: Path | None = None
        self.network_suspended = False
        self.network_resume_at = 0.0
        self.action_control_failed = False
        self.post_actions_active = False
        self.diagnostics = NetworkDiagnostics()
        self.block_service_workers = False
        self.ancillary_isolation = AncillaryIsolation(self.diagnostics, self._isolation_failed)
        self.traffic_mode: TrafficMode = "off"
        self.traffic_policy = TrafficPolicy(self.diagnostics, self._traffic_failed)
        self.on_traffic_failed: Callable[[], None] | None = None
        self.on_isolation_failed: Callable[[Response, float], None] | None = None
        self.on_action_auth_failed: Callable[[], None] | None = None
        # Called with the response whenever X answers a request with a rate-limit status.
        self.on_rate_limited: Callable[[Response], None] | None = None
        # Called with every response while post actions run, for rate-limit budget headers.
        self.on_action_response: Callable[[Response], None] | None = None

    def _watch_response(self, response: Response) -> None:
        if (self.post_actions_active and response.status in {401, 403}
                and is_rate_limit_response(response.url, 429) and self.on_action_auth_failed is not None):
            self.on_action_auth_failed()
        # Install the stop flag before any optional bounded diagnostic body read.
        self.diagnostics.response(response)
        if self.on_rate_limited is not None and is_rate_limit_response(response.url, response.status):
            self.on_rate_limited(response)
        if self.post_actions_active and self.on_action_response is not None:
            self.on_action_response(response)

    def _isolation_failed(self, response: Response, deadline: float) -> None:
        # Install cancellation/global refusal independently of network control.
        try:
            if self.on_isolation_failed is not None:
                self.on_isolation_failed(response, deadline)
        finally:
            self.disable_action_network(deadline)

    def disable_action_network(self, deadline: float) -> None:
        self.action_control_failed = True
        self.network_resume_at = max(self.network_resume_at, deadline)
        try:
            self.suspend_action_network(deadline)
        except Exception:
            logger.warning("Ancillary offline control failed; closing the context and disabling further actions.")
            try:
                if self.context is not None:
                    self.context.close()
            except Exception:
                logger.warning("Context close failed; further automated actions remain disabled.")
            finally:
                self.page = None
            raise RuntimeError("Ancillary offline control failure; further actions are disabled.") from None

    def _watch_request(self, request: Request) -> None:
        self.diagnostics.request(request)
        self.traffic_policy.observe(request)

    def _traffic_failed(self) -> None:
        if self.on_traffic_failed is not None:
            self.on_traffic_failed()

    def _watch_request_failed(self, request: Request) -> None:
        self.diagnostics.request_failed(request)

    def suspend_action_network(self, deadline: float) -> None:
        self.diagnostics.phase("network_pause")
        self.network_resume_at = max(self.network_resume_at, deadline)
        if self.context is not None and not self.network_suspended:
            self.context.set_offline(True)
            self.network_suspended = True

    def resume_action_network(self) -> None:
        if self.action_control_failed:
            raise RuntimeError("Action network control failed; start a new context after reviewing the saved deadline.")
        if self.network_resume_at > time.time():
            raise RuntimeError(tr("保存された待機期限 {deadline} まで通信を休止しています。期限後に再開してください。",
                                  deadline=datetime.fromtimestamp(self.network_resume_at, UTC).isoformat(timespec="seconds")))
        if self.context is not None and self.network_suspended:
            self.diagnostics.phase("network_resume")
            self.context.set_offline(False)
        self.network_suspended = False
        self.network_resume_at = 0.0
        self.diagnostics.phase("idle")

    def _require_page(self) -> Page:
        if self.action_control_failed:
            raise RuntimeError("Action network control failed; further actions are disabled.")
        if self.page is None:
            raise RuntimeError("Browser page is not initialized.")
        if self.network_suspended:
            self.resume_action_network()
        return self.page

    def start(self, user_data_dir: str | Path | None = None, account_name: str | None = None) -> None:
        """ブラウザを起動し、指定されたディレクトリにプロファイルを保存してログイン状態を維持する

        Args:
            user_data_dir: カスタムプロファイルディレクトリ。指定時はaccount_nameより優先
            account_name: アカウント名。profile_dirが指定されない場合に使用
        """
        profile_dir = Path(user_data_dir) if user_data_dir is not None else get_account_profile_dir(account_name)
        # Read only the action receipt metadata, before launching/restoring tabs.
        saved = ActionJournal(profile_dir / "action-progress.json", profile_dir.name.removeprefix("chromium-profile-"))
        if self.action_control_failed:
            raise RuntimeError("Action network control failed; no browser was started.")
        if saved._lock_path.exists():
            raise RuntimeError("Action journal lock exists; no browser was started. Inspect progress and use recover only after stopping the owner.")
        saved._load()
        if saved.cooldown_until > time.time():
            raise RuntimeError(tr("保存された待機期限 {deadline} まで通信を休止しています。期限後に再開してください。",
                                  deadline=datetime.fromtimestamp(saved.cooldown_until, UTC).isoformat(timespec="seconds")))
        ensure_private_directory(profile_dir)
        if self.network_suspended:
            self.resume_action_network()

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
        self.diagnostics.phase("browser_start")
        try:
            if not self.playwright:
                self.playwright = sync_playwright().start()

            try:
                self.context = self._launch_action_context(self.playwright, profile_dir)
            except PlaywrightError as error:
                if MISSING_BROWSER_MESSAGE not in str(error):
                    raise
                install_chromium()
                self.context = self._launch_action_context(self.playwright, profile_dir)
            self.context.on("request", self._watch_request)
            self.context.on("response", self._watch_response)
            self.context.on("requestfailed", self._watch_request_failed)
            # Isolation is registered last so it runs first and falls back to the allowlist.
            self.traffic_policy.mode = self.traffic_mode
            self.traffic_policy.install(self.context, service_workers_blocked=self.block_service_workers)
            self.ancillary_isolation.install(self.context, service_workers_blocked=self.block_service_workers)
            self.browser = self.context.browser
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.profile_dir = profile_dir
            logger.info("Browser successfully launched.")
            self.diagnostics.phase("idle")
        except Exception:
            logger.exception("Failed to start browser.")
            self.stop()
            raise

    def _launch_action_context(self, playwright: Playwright, profile_dir: Path) -> BrowserContext:
        if self.block_service_workers:
            return self._launch_context(playwright, profile_dir, headless=self.headless, service_workers="block")
        return self._launch_context(playwright, profile_dir, headless=self.headless)

    @staticmethod
    def _launch_context(playwright: Playwright, profile_dir: Path, *, headless: bool = False,
                        service_workers: Literal["allow", "block"] | None = None) -> BrowserContext:
        # The default headless shell names HeadlessChrome in its UA and client hints, and X
        # refuses some of its own page requests with HTTP403. New headless keeps the headed
        # client hints, so only its UA token needs the headed spelling.
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=HEADLESS_CHANNEL if headless else None,
            headless=headless,
            user_agent=_headed_user_agent(playwright) if headless else None,
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            args=["--disable-blink-features=AutomationControlled"],
            service_workers=service_workers,
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
            self.network_suspended = False
            self.network_resume_at = 0.0
            self.action_control_failed = False
            self.post_actions_active = False
            self.ancillary_isolation = AncillaryIsolation(self.diagnostics, self._isolation_failed)
            self.traffic_policy = TrafficPolicy(self.diagnostics, self._traffic_failed)
            self.diagnostics.phase("idle")

    def go_to_home(self) -> None:
        """X のホームへ遷移"""
        page = self._require_page()
        self.diagnostics.phase("home_load")
        home_url = urljoin(BASE_X_URL, HOME_PATH)
        logger.debug("Navigating to %s", home_url)
        try:
            page.goto(home_url, timeout=NAVIGATION_TIMEOUT_MS)
        except Exception:
            logger.exception("Failed to navigate to home page.")
            raise
        finally:
            self.diagnostics.phase("idle")

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
