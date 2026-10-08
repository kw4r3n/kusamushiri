from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from kusamushiri.app import XDeleterWorker
from kusamushiri.core import XDeleterCore
from kusamushiri.gui import XDeleterWindow
from kusamushiri.models import CollectRequest, ExecuteActionsRequest, PostActionResult, PostActionTarget, PostRecord
from kusamushiri.paths import DEFAULT_ACCOUNT_NAME


class FakeCore:
    """Mock XDeleterCore for integration testing."""

    rate_limit_status: int | None = None


    def __init__(self) -> None:
        self.calls: list[str] = []
        self.account_name: str | None = None
        self.page: object | None = None
        self.fail_on: str | None = None
        self.collected_posts = [
            PostRecord(
                id="1",
                url="https://x.com/user/status/1",
                author_username="user",
                text="Test post",
                date="2026-01-01",
                likes=0,
                replies=0,
                has_media=False,
                is_reply=False,
                kind="post",
            )
        ]
        self.action_result = PostActionResult(
            target=PostActionTarget(url="https://x.com/user/status/1", kind="post"),
            action_label="ポスト削除",
            success=True,
            error_message=None,
        )
        self.executed_targets: list[PostActionTarget] = []
        self.cancel_requested = False
        self.collection_limit_reached = False

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def clear_cancel(self) -> None:
        self.cancel_requested = False

    def start_browser(self, user_data_dir: object | None = None, account_name: str | None = None) -> None:
        del user_data_dir
        self.calls.append("start_browser")
        self.account_name = account_name
        self.page = object()

    def go_to_home(self) -> None:
        self.calls.append("go_to_home")

    def is_logged_in(self) -> bool:
        self.calls.append("is_logged_in")
        return True

    def get_logged_in_username(self) -> str:
        self.calls.append("get_logged_in_username")
        return "logged_in_user"

    def search_and_collect_posts(self, request: CollectRequest, on_progress=None) -> list[PostRecord]:
        call = f"search_and_collect_posts:{request.username}"
        self.calls.append(call)
        if self.fail_on == "search_and_collect_posts":
            raise RuntimeError("Collection failed")
        if on_progress is not None:
            on_progress(1, 1, 100)
        return self.collected_posts

    def execute_post_action(self, target: PostActionTarget) -> PostActionResult:
        self.calls.append(f"execute_post_action:{target.url}")
        self.executed_targets.append(target)
        if self.fail_on == "execute_post_action":
            return PostActionResult(
                target=target,
                action_label="ポスト削除",
                success=False,
                error_message="Failed",
            )
        return self.action_result

    def stop_browser(self) -> None:
        self.calls.append("stop_browser")
        self.page = None


@pytest.fixture
def worker(qtbot: object, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakeCore]:
    """Create a real XDeleterWorker with a FakeCore."""
    del qtbot, monkeypatch
    core = FakeCore()
    w = XDeleterWorker(core=core)
    yield w, core
    w.shutdown()


@pytest.fixture
def app_window(qtbot: object, monkeypatch: pytest.MonkeyPatch) -> XDeleterWindow:
    """Create a XDeleterWindow with mocked paths."""
    del qtbot
    monkeypatch.setattr("kusamushiri.settings.list_saved_accounts", lambda: [])
    w = XDeleterWindow()
    yield w
    QApplication.instance().processEvents()


def wait_until(qtbot: object, condition: Callable[[], bool], timeout: int = 3000) -> None:
    """Wait until condition is true, processing Qt events."""
    del qtbot
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        QApplication.instance().processEvents()
        if condition():
            return
        time.sleep(0.01)
    raise TimeoutError(f"Condition not met within {timeout}ms")


def build_collect_request(username: str = "user") -> CollectRequest:
    return CollectRequest(
        username=username,
        max_posts=10,
        media_filter="all",
        is_reply=False,
        min_likes=0,
        min_replies=0,
        search_mode="search",
    )


def connect_window_to_worker(window: XDeleterWindow, worker: Any) -> None:
    window.start_browser_requested.connect(worker.enqueue_start_browser)
    window.stop_browser_requested.connect(worker.enqueue_stop_browser)
    window.check_login_requested.connect(worker.enqueue_check_login)
    window.collect_requested.connect(worker.enqueue_collect_posts)
    window.delete_requested.connect(worker.enqueue_delete_posts)
    worker.events.status_changed.connect(window.update_status)
    worker.events.browser_ready.connect(window.show_login_wait_dialog)
    worker.events.browser_stopped.connect(window.on_browser_stopped)
    worker.events.login_checked.connect(window.on_login_checked)
    worker.events.posts_collected.connect(window.display_posts)
    worker.events.delete_progress.connect(window.on_delete_progress)
    worker.events.delete_completed.connect(window.on_delete_done)
    worker.events.error_occurred.connect(window.show_error)


def release_window(window: XDeleterWindow) -> None:
    window.hide()
    window.deleteLater()
    QApplication.instance().processEvents()


def patch_message_boxes(monkeypatch: pytest.MonkeyPatch, errors: list[str] | None = None) -> None:
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *args, **kwargs: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    def record_critical(parent: object, title: str, message: str) -> None:
        del parent, title
        if errors is not None:
            errors.append(message)

    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.critical", record_critical)


def test_app_startup_and_shutdown(qtbot: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kusamushiri.settings.list_saved_accounts", lambda: [])
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    window = XDeleterWindow()
    try:
        connect_window_to_worker(window, worker)

        assert window.windowTitle() == "Kusamushiri"
        assert window.status_label.text() == "待機中..."
    finally:
        worker.shutdown()
        release_window(window)
    assert worker._thread.is_alive() is False


def test_full_collect_and_delete_flow(qtbot: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kusamushiri.settings.list_saved_accounts", lambda: [])
    patch_message_boxes(monkeypatch)
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    window = XDeleterWindow()
    try:
        connect_window_to_worker(window, worker)

        window.start_browser_requested.emit(DEFAULT_ACCOUNT_NAME)
        wait_until(qtbot, lambda: "go_to_home" in core.calls)
        assert core.account_name == DEFAULT_ACCOUNT_NAME

        login_events: list[tuple[bool, str]] = []
        worker.events.login_checked.connect(lambda logged_in, username: login_events.append((logged_in, username)))
        window.check_login_requested.emit()
        wait_until(qtbot, lambda: (True, "logged_in_user") in login_events)

        window.collect_requested.emit(build_collect_request())
        wait_until(qtbot, lambda: window.table.rowCount() == 1)
        assert window.table.item(0, 1).text() == "Test post"
        assert window.table.item(0, 2).text() == "https://x.com/user/status/1"

        completed_events: list[list[PostActionResult]] = []
        worker.events.delete_completed.connect(completed_events.append)
        request = ExecuteActionsRequest(targets=window._selected_targets(), interval_seconds=0)
        window.delete_requested.emit(request)
        wait_until(qtbot, lambda: completed_events == [[core.action_result]])

        assert core.executed_targets == [PostActionTarget(url="https://x.com/user/status/1", kind="post")]
        assert window.status_label.text() == "削除/解除処理完了: 1 / 1 件成功"
    finally:
        worker.shutdown()
        release_window(window)


def test_error_propagation(qtbot: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kusamushiri.settings.list_saved_accounts", lambda: [])
    errors: list[str] = []
    patch_message_boxes(monkeypatch, errors)
    core = FakeCore()
    core.page = object()
    core.fail_on = "search_and_collect_posts"
    worker = XDeleterWorker(core=core)
    window = XDeleterWindow()
    try:
        connect_window_to_worker(window, worker)

        window.collect_requested.emit(build_collect_request())

        wait_until(qtbot, lambda: errors == ["Collection failed"])
        assert window.status_label.text() == "エラーが発生しました: Collection failed"
        assert window.start_button.isEnabled() is True
    finally:
        worker.shutdown()
        release_window(window)


def test_worker_graceful_shutdown(worker: tuple[Any, FakeCore]) -> None:
    w, core = worker

    w._commands.put(None)
    w._thread.join(timeout=1)

    assert w._thread.is_alive() is False
    assert core.calls == ["stop_browser"]


def test_gui_to_worker_signal_chain(app_window: XDeleterWindow) -> None:
    received: queue.Queue[tuple[str, object | None]] = queue.Queue()
    imported_types: tuple[type[QObject], type[QTimer], type[threading.Thread], type[XDeleterCore]] = (
        QObject,
        QTimer,
        threading.Thread,
        XDeleterCore,
    )
    del imported_types

    app_window.start_browser_requested.connect(lambda account_name: received.put(("start_browser", account_name)))
    app_window.check_login_requested.connect(lambda: received.put(("check_login", None)))
    app_window.collect_requested.connect(lambda request: received.put(("collect_posts", request)))
    app_window.delete_requested.connect(lambda request: received.put(("delete_posts", request)))

    collect_request = build_collect_request()
    delete_request = ExecuteActionsRequest(
        targets=[PostActionTarget(url="https://x.com/user/status/1", kind="post")],
        interval_seconds=0,
    )

    app_window.start_browser_requested.emit(DEFAULT_ACCOUNT_NAME)
    app_window.check_login_requested.emit()
    app_window.collect_requested.emit(collect_request)
    app_window.delete_requested.emit(delete_request)

    assert received.get_nowait() == ("start_browser", DEFAULT_ACCOUNT_NAME)
    assert received.get_nowait() == ("check_login", None)
    assert received.get_nowait() == ("collect_posts", collect_request)
    assert received.get_nowait() == ("delete_posts", delete_request)
