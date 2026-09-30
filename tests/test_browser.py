from unittest.mock import Mock

import pytest
from playwright.sync_api import Error as PlaywrightError

from kusamushiri.browser import BrowserManager


@pytest.mark.parametrize("page_state", ["open", "closed", "pending_close"])
def test_start_reuses_only_open_page(monkeypatch, tmp_path, page_state):
    manager = BrowserManager()
    manager.profile_dir = tmp_path
    manager.page = Mock()
    manager.page.is_closed.return_value = page_state == "closed"
    if page_state == "pending_close":
        manager.page.wait_for_timeout.side_effect = [PlaywrightError("Target closed"), None]
    manager.context = Mock()
    old_context = manager.context
    manager.playwright = Mock()
    old_playwright = manager.playwright
    new_context = Mock()
    new_context.pages = [Mock()]
    starter = Mock()
    starter.start.return_value.chromium.launch_persistent_context.return_value = new_context
    monkeypatch.setattr("kusamushiri.browser.sync_playwright", lambda: starter)

    manager.start(tmp_path)

    if page_state != "open":
        old_context.close.assert_called_once()
        old_playwright.stop.assert_called_once()
        assert manager.context is new_context
        assert manager.page is new_context.pages[0]
    else:
        old_context.close.assert_not_called()
        old_playwright.stop.assert_not_called()
        starter.start.assert_not_called()
        assert manager.context is old_context


@pytest.mark.parametrize("fail_after_launch", [False, True])
def test_start_failure_cleans_up_and_allows_retry(monkeypatch, tmp_path, fail_after_launch):
    manager = BrowserManager()
    starter = Mock()
    playwright = starter.start.return_value
    context = Mock()
    context.pages = []
    launch = playwright.chromium.launch_persistent_context
    if fail_after_launch:
        launch.return_value = context
        context.new_page.side_effect = RuntimeError("page startup failed")
    else:
        launch.side_effect = RuntimeError("browser startup failed")
    monkeypatch.setattr("kusamushiri.browser.sync_playwright", lambda: starter)

    with pytest.raises(RuntimeError, match="startup failed"):
        manager.start(tmp_path)

    playwright.stop.assert_called_once()
    if fail_after_launch:
        context.close.assert_called_once()
    assert manager.playwright is None
    assert manager.browser is None
    assert manager.context is None
    assert manager.page is None
    assert manager.profile_dir is None

    launch.side_effect = None
    launch.return_value = context
    context.pages = [Mock()]
    manager.start(tmp_path)
    assert manager.page is context.pages[0]


@pytest.mark.parametrize("error_message", ["Executable doesn't exist at /x/chrome", "Target crashed"])
def test_start_installs_chromium_only_when_executable_is_missing(monkeypatch, tmp_path, error_message):
    manager = BrowserManager()
    starter = Mock()
    context = Mock()
    context.pages = [Mock()]
    launch = starter.start.return_value.chromium.launch_persistent_context
    launch.side_effect = [PlaywrightError(error_message), context]
    install = Mock()
    monkeypatch.setattr("kusamushiri.browser.sync_playwright", lambda: starter)
    monkeypatch.setattr("kusamushiri.browser.install_chromium", install)

    if "Executable" in error_message:
        manager.start(tmp_path)
        install.assert_called_once()
        assert launch.call_count == 2
        assert manager.context is context
    else:
        with pytest.raises(PlaywrightError, match="Target crashed"):
            manager.start(tmp_path)
        install.assert_not_called()
        assert manager.context is None
