"""Baseline GUI tests for XDeleterWindow.

NOTE: These tests require `pytest-qt` and run with `QT_QPA_PLATFORM=offscreen`
or under `xvfb-run`. They use qtbot ONLY for QApplication lifecycle and do NOT
use qtbot.add_widget() due to PySide6 6.9.x offscreen teardown hangs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMessageBox

from kusamushiri.follows import FollowRecord, UnfollowRequest, UnfollowResult
from kusamushiri.gui import XDeleterWindow, format_action_failures, remove_successful_rows
from kusamushiri.gui_follows import FollowListDialog
from kusamushiri.models import CollectRequest, ExecuteActionsRequest, PostActionResult, PostActionTarget, PostRecord
from kusamushiri.paths import list_saved_accounts
from kusamushiri.settings import AccountSettingsManager


@pytest.fixture(autouse=True)
def isolated_xdg_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        directory = tmp_path / variable.lower()
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))


def _sample(**kw: object) -> PostRecord:
    kwargs = {
        "id": "1",
        "url": "https://x.com/user/status/1",
        "author_username": "user",
        "text": "hello",
        "date": "2026-01-01",
        "likes": 0,
        "replies": 0,
        "has_media": False,
        "is_reply": False,
        "kind": "post",
    }
    kwargs.update(kw)
    return PostRecord(**kwargs)


def _show_and_activate(window: XDeleterWindow) -> None:
    window.show()
    window.activateWindow()
    window.setFocus()
    QApplication.instance().processEvents()
    assert window.isActiveWindow()


@pytest.fixture
def window(qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> XDeleterWindow:
    monkeypatch.setattr("kusamushiri.settings.list_saved_accounts", lambda: [])
    settings = QSettings(str(tmp_path / "gui-settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr("kusamushiri.gui.AccountSettingsManager", lambda: AccountSettingsManager(settings))
    w = XDeleterWindow()
    # DO NOT use qtbot.add_widget() — it hangs on teardown in offscreen
    yield w
    w.set_busy(False)
    w.hide()
    w.deleteLater()
    QApplication.instance().processEvents()


def test_window_title(window: XDeleterWindow, qtbot) -> None:
    assert window.windowTitle() == "Kusamushiri"


def test_set_busy_disables_controls(window: XDeleterWindow, qtbot) -> None:
    window.set_busy(True)
    assert window.start_button.isEnabled() is False
    assert window.account_combo.isEnabled() is False


def test_selected_targets_checked(window: XDeleterWindow, qtbot) -> None:
    window.display_posts([_sample(), _sample(id="2", url="https://x.com/user/status/2")])
    assert len(window._selected_targets()) == 2


def test_selected_targets_unchecked_row(window: XDeleterWindow, qtbot) -> None:
    window.display_posts([_sample(), _sample(id="2", url="https://x.com/user/status/2")])
    window.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert len(window._selected_targets()) == 1


def test_toggle_all(window: XDeleterWindow, qtbot) -> None:
    window.display_posts([_sample(id=str(i)) for i in range(3)])
    window._handle_toggle_all()
    assert len(window._selected_targets()) == 0
    window._handle_toggle_all()
    assert len(window._selected_targets()) == 3


def test_clear_posts(window: XDeleterWindow, qtbot) -> None:
    window.display_posts([_sample()])
    assert window.table.rowCount() == 1
    window._clear_posts()
    assert window.table.rowCount() == 0


def test_display_posts_columns(window: XDeleterWindow, qtbot) -> None:
    window.display_posts([
        _sample(url="https://x.com/user/status/42", text="Hello World", has_media=True),
        _sample(id="99", url="https://x.com/user/status/99", text="Repost",
                kind="repost", is_reply=True, has_media=False),
    ])
    assert window.table.rowCount() == 2
    assert window.table.item(0, 1).text() == "Hello World"
    assert window.table.item(0, 4).text() == "ポスト"
    assert window.table.item(1, 4).text() == "リポスト"


def test_display_posts_formats_iso_datetime(window: XDeleterWindow, qtbot) -> None:
    window.display_posts([_sample(date="2026-01-02T03:04:05.000Z")])

    assert window.table.item(0, 3).text() == "2026-01-02"


@pytest.mark.parametrize("column", [5, 6])
@pytest.mark.parametrize("order", [Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder])
def test_metric_columns_sort_numerically_without_changing_targets(window, column, order):
    posts = [
        _sample(id=str(count), url=f"https://x.com/user/status/{count}", likes=count, replies=count)
        for count in (2, 100, 10)
    ]
    window.display_posts(posts)
    window.table.sortItems(column, order)

    expected = sorted((2, 100, 10), reverse=order == Qt.SortOrder.DescendingOrder)
    assert [int(window.table.item(row, column).text()) for row in range(3)] == expected
    assert [target.url for target in window._selected_targets()] == [
        f"https://x.com/user/status/{count}" for count in expected
    ]


def test_handle_delete_confirmation_emits_request(
    window: XDeleterWindow,
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted_requests: list[ExecuteActionsRequest] = []
    window.username_input.setText("user")
    window.on_login_checked(True, "user")
    window.display_posts([_sample(url="https://x.com/user/status/42")])
    window.delete_requested.connect(emitted_requests.append)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *a, **kw: QMessageBox.StandardButton.Yes,
    )

    window._handle_delete()

    assert len(emitted_requests) == 1
    request = emitted_requests[0]
    assert request.targets == [PostActionTarget(url="https://x.com/user/status/42", kind="post")]


def test_ctrl_return_shortcut_triggers_delete(
    window: XDeleterWindow,
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted_requests: list[ExecuteActionsRequest] = []
    window.username_input.setText("user")
    window.on_login_checked(True, "user")
    window.display_posts([_sample(url="https://x.com/user/status/42")])
    window.delete_requested.connect(emitted_requests.append)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *a, **kw: QMessageBox.StandardButton.Yes,
    )
    _show_and_activate(window)

    qtbot.keyClick(window, Qt.Key.Key_Return, modifier=Qt.KeyboardModifier.ControlModifier)

    assert len(emitted_requests) == 1


def test_ctrl_r_shortcut_refreshes_search(window: XDeleterWindow, qtbot) -> None:
    emitted_requests: list[object] = []
    window._commit_typed_account(load_settings=False)
    window.username_input.setText("test_user")
    window.on_login_checked(True, "test_user")
    window.collect_requested.connect(emitted_requests.append)
    _show_and_activate(window)

    qtbot.keyClick(window, Qt.Key.Key_R, modifier=Qt.KeyboardModifier.ControlModifier)

    assert len(emitted_requests) == 1


def test_ctrl_return_shortcut_does_nothing_while_busy(
    window: XDeleterWindow,
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted_requests: list[ExecuteActionsRequest] = []
    dialog_calls: list[str] = []
    window.username_input.setText("user")
    window.on_login_checked(True, "user")
    window.display_posts([_sample(url="https://x.com/user/status/42")])
    window.preview_text.setPlainText("existing preview")
    window.delete_requested.connect(emitted_requests.append)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *a, **kw: dialog_calls.append("question") or QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.information",
        lambda *a, **kw: dialog_calls.append("information"),
    )
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.warning",
        lambda *a, **kw: dialog_calls.append("warning"),
    )
    window.set_busy(True)
    _show_and_activate(window)

    qtbot.keyClick(window, Qt.Key.Key_Return, modifier=Qt.KeyboardModifier.ControlModifier)

    assert emitted_requests == []
    assert dialog_calls == []
    assert window.table.rowCount() == 1
    assert window.preview_text.toPlainText() == "existing preview"


def test_ctrl_r_shortcut_does_nothing_while_busy(
    window: XDeleterWindow,
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted_requests: list[object] = []
    dialog_calls: list[str] = []
    window.username_input.setText("test_user")
    window.on_login_checked(True, "test_user")
    window.display_posts([_sample()])
    window.preview_text.setPlainText("existing preview")
    window.collect_requested.connect(emitted_requests.append)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *a, **kw: dialog_calls.append("question") or QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.information",
        lambda *a, **kw: dialog_calls.append("information"),
    )
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.warning",
        lambda *a, **kw: dialog_calls.append("warning"),
    )
    window.set_busy(True)
    _show_and_activate(window)

    qtbot.keyClick(window, Qt.Key.Key_R, modifier=Qt.KeyboardModifier.ControlModifier)

    assert emitted_requests == []
    assert dialog_calls == []
    assert window.table.rowCount() == 1
    assert window.preview_text.toPlainText() == "existing preview"


def test_stop_browser_confirmation_emits_stop_when_accepted(
    window: XDeleterWindow,
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_requests: list[None] = []
    window.stop_browser_requested.connect(lambda: stop_requests.append(None))
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *a, **kw: QMessageBox.StandardButton.Yes,
    )

    window._on_stop_browser_clicked()

    assert stop_requests == [None]
    window.on_browser_stopped()


def test_stop_browser_confirmation_does_not_emit_stop_when_rejected(
    window: XDeleterWindow,
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_requests: list[None] = []
    window.stop_browser_requested.connect(lambda: stop_requests.append(None))
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *a, **kw: QMessageBox.StandardButton.No,
    )

    window._on_stop_browser_clicked()

    assert stop_requests == []


def test_confirm_account_match_returns_true_for_matching_empty_and_none(
    window: XDeleterWindow,
    qtbot,
) -> None:
    window._last_logged_in_username = "test_user"
    window.username_input.setText("@Test_User")
    assert window._confirm_account_match() is True

    window.username_input.setText("")
    assert window._confirm_account_match() is True

    window._last_logged_in_username = None
    window.username_input.setText("other_user")
    assert window._confirm_account_match() is True


def test_confirm_account_match_returns_false_for_rejected_mismatch(
    window: XDeleterWindow,
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window._last_logged_in_username = "logged_in_user"
    window.username_input.setText("other_user")
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *a, **kw: QMessageBox.StandardButton.No,
    )

    assert window._confirm_account_match() is False


def test_on_delete_done_removes_successful_rows_without_dialog_hang(
    window: XDeleterWindow,
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *a, **kw: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *a, **kw: None)
    window.display_posts([
        _sample(url="https://x.com/user/status/1"),
        _sample(id="2", url="https://x.com/user/status/2"),
    ])

    window.on_delete_done([
        PostActionResult(
            target=PostActionTarget(url="https://x.com/user/status/1", kind="post"),
            action_label="削除",
            success=True,
            error_message=None,
        )
    ])

    assert window.table.rowCount() == 1
    assert window.table.item(0, 2).text() == "https://x.com/user/status/2"


def test_format_action_failures_returns_labels_urls_and_error_messages() -> None:
    failures = [
        PostActionResult(
            target=PostActionTarget(url="https://x.com/user/status/1", kind="post"),
            action_label="削除",
            success=False,
            error_message="failed",
        ),
        PostActionResult(
            target=PostActionTarget(url="https://x.com/user/status/2", kind="repost"),
            action_label="リポスト解除",
            success=False,
            error_message=None,
        ),
    ]

    assert format_action_failures(failures) == (
        "[削除] https://x.com/user/status/1\n"
        "  failed\n"
        "[リポスト解除] https://x.com/user/status/2\n"
        "  詳細不明"
    )


def test_remove_successful_rows_removes_matching_targets(window: XDeleterWindow, qtbot) -> None:
    window.display_posts([
        _sample(url="https://x.com/user/status/1"),
        _sample(id="2", url="https://x.com/user/status/2", kind="repost"),
        _sample(id="3", url="https://x.com/user/status/3"),
    ])

    remove_successful_rows(
        window.table,
        [
            PostActionResult(
                target=PostActionTarget(url="https://x.com/user/status/2", kind="repost"),
                action_label="リポスト解除",
                success=True,
                error_message=None,
            ),
            PostActionResult(
                target=PostActionTarget(url="https://x.com/user/status/3", kind="post"),
                action_label="削除",
                success=False,
                error_message="failed",
            ),
        ],
    )

    assert window.table.rowCount() == 2
    assert window.table.item(0, 2).text() == "https://x.com/user/status/1"
    assert window.table.item(1, 2).text() == "https://x.com/user/status/3"


def test_normalize_username(window: XDeleterWindow, qtbot) -> None:
    window.username_input.setText("@test_user")
    assert window._normalized_username_input() == "test_user"


def test_auto_fill_username(window: XDeleterWindow, qtbot) -> None:
    window.username_input.setText("")
    window.on_login_checked(True, "auto_user")
    assert window.username_input.text() == "auto_user"


def test_account_change_clears_preview_and_requires_login(window: XDeleterWindow, qtbot) -> None:
    window.username_input.setText("first")
    window.on_login_checked(True, "first")
    window.display_posts([_sample()])

    window._change_current_account("brand_new_account", load_settings=False)

    assert window.table.rowCount() == 0
    assert window.collect_button.isEnabled() is False
    assert window.delete_button.isEnabled() is False


def test_account_change_stops_running_browser(window: XDeleterWindow, qtbot) -> None:
    stop_requests: list[None] = []
    window.stop_browser_requested.connect(lambda: stop_requests.append(None))
    window._browser_running = True

    window._change_current_account("another_new_account", load_settings=False)

    assert stop_requests == [None]
    assert window._busy is True


def test_add_profile_creates_persistent_isolated_profile(window: XDeleterWindow, qtbot, monkeypatch) -> None:
    window.username_input.setText("old_user")
    window.max_posts_input.setValue(123)
    window.on_login_checked(True, "old_user")
    window.display_posts([_sample()])
    browser_starts: list[str] = []
    window.start_browser_requested.connect(browser_starts.append)
    monkeypatch.setattr("kusamushiri.gui.QInputDialog.getText", lambda *args: (" @new/user ", True))

    window.add_profile_button.click()

    assert window._current_account == "new_user"
    assert window.account_combo.currentText() == "new_user"
    assert list_saved_accounts() == ["new_user"]
    assert window._settings_manager.get_last_account() == "new_user"
    assert window._settings_manager.load_account_settings("default").username == "old_user"
    assert window.username_input.text() == ""
    assert window.max_posts_input.value() == 50
    assert window.table.rowCount() == 0
    assert window.collect_button.isEnabled() is False
    assert browser_starts == []  # Login remains an explicit action.

    window._restore_settings()
    assert window.account_combo.currentText() == "new_user"
    assert window.account_combo.findText("default") >= 0
    assert window.username_input.text() == ""
    window._change_current_account("default", load_settings=True)
    assert window.username_input.text() == "old_user"
    assert window.max_posts_input.value() == 123
    window._change_current_account("new_user", load_settings=True)
    assert window.username_input.text() == ""


@pytest.mark.parametrize("name, accepted", [("", True), ("@", True), ("default", True), ("ignored", False)])
def test_add_profile_rejects_empty_duplicate_or_cancelled_name(
    window: XDeleterWindow, qtbot, monkeypatch, name: str, accepted: bool
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("kusamushiri.gui.QInputDialog.getText", lambda *args: (name, accepted))
    monkeypatch.setattr("kusamushiri.gui.QMessageBox.warning", lambda *args: warnings.append(args[2]))

    window.add_profile_button.click()

    assert window._current_account == "default"
    assert window.account_combo.count() == 1
    assert list_saved_accounts() == []
    assert len(warnings) == (1 if accepted else 0)


def test_add_profile_rejects_existing_profile_not_in_combo(window: XDeleterWindow, qtbot, monkeypatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("kusamushiri.gui.QInputDialog.getText", lambda *args: ("already_saved", True))
    monkeypatch.setattr("kusamushiri.gui.QMessageBox.warning", lambda *args: warnings.append(args[2]))
    monkeypatch.setattr(window._settings_manager, "list_accounts", lambda: ["already_saved"])

    window.add_profile_button.click()

    assert window._current_account == "default"
    assert list_saved_accounts() == []
    assert warnings == ["同じ名前のプロファイルが既にあります。"]


def test_add_profile_reports_profile_creation_failure(window: XDeleterWindow, qtbot, monkeypatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("kusamushiri.gui.QInputDialog.getText", lambda *args: ("new", True))
    monkeypatch.setattr("kusamushiri.gui.QMessageBox.warning", lambda *args: warnings.append(args[2]))

    def fail_to_create(account_name: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("kusamushiri.gui.get_account_profile_dir", fail_to_create)

    window.add_profile_button.click()

    assert window._current_account == "default"
    assert list_saved_accounts() == []
    assert len(warnings) == 1 and "disk full" in warnings[0]


def test_add_profile_stops_active_browser_and_is_disabled_while_busy(
    window: XDeleterWindow, qtbot, monkeypatch
) -> None:
    stop_requests: list[None] = []
    window.stop_browser_requested.connect(lambda: stop_requests.append(None))
    monkeypatch.setattr("kusamushiri.gui.QInputDialog.getText", lambda *args: ("second", True))
    window._browser_running = True

    window.add_profile_button.click()

    assert stop_requests == [None]
    assert window._busy is True
    assert window.add_profile_button.isEnabled() is False
    window._add_profile()  # Even a forced signal must not add another profile.
    assert list_saved_accounts() == ["second"]


def test_partial_browser_start_failure_keeps_profile_management_locked(window, qtbot, monkeypatch) -> None:
    monkeypatch.setattr("kusamushiri.gui.QInputDialog.getText", lambda *args: ("first", True))
    monkeypatch.setattr("kusamushiri.gui.QMessageBox.critical", lambda *args: None)
    window._add_profile()
    window._handle_start_browser()
    window.show_error("navigation failed after launch")
    assert not window.rename_profile_button.isEnabled()
    assert not window.delete_profile_button.isEnabled()
    assert window.stop_button.isEnabled()
    window.on_browser_stopped()
    assert window.rename_profile_button.isEnabled()


def test_profile_management_gui_and_login_label(window, qtbot, monkeypatch) -> None:
    monkeypatch.setattr("kusamushiri.gui.QInputDialog.getText", lambda *args: ("first", True))
    window._add_profile()
    window.on_login_checked(True, "alice")
    assert window.login_account_label.text() == "ログイン中: @alice"
    window._browser_running = True
    window.set_busy(False)
    assert not window.rename_profile_button.isEnabled()
    assert not window.delete_profile_button.isEnabled()
    window._delete_profile()
    assert window._current_account == "first"
    window.on_browser_stopped()
    assert window.login_account_label.text() == "ログイン未確認"
    monkeypatch.setattr("kusamushiri.gui.QInputDialog.getText", lambda *args: ("renamed", True))
    window.rename_profile_button.click()
    assert window._current_account == "renamed"
    assert list_saved_accounts() == ["renamed"]
    monkeypatch.setattr("kusamushiri.gui.QMessageBox.question", lambda *args: QMessageBox.StandardButton.No)
    window.delete_profile_button.click()
    assert list_saved_accounts() == ["renamed"]
    monkeypatch.setattr("kusamushiri.gui.QMessageBox.question", lambda *args: QMessageBox.StandardButton.Yes)
    window.delete_profile_button.click()
    assert list_saved_accounts() == []
    assert window._current_account == "default"
    assert not window.delete_profile_button.isEnabled()
    assert not window.rename_profile_button.isEnabled()


def test_auto_save_timer_uses_configured_interval(window: XDeleterWindow, qtbot) -> None:
    window.auto_save_interval_input.setValue(30)

    assert window._auto_save_timer.isActive() is True
    assert window._auto_save_timer.interval() == 30_000


def test_auto_save_timer_persists_current_settings(window: XDeleterWindow, qtbot, monkeypatch) -> None:
    saved_accounts: list[str] = []
    monkeypatch.setattr(
        window._settings_manager,
        "save_account_settings",
        lambda account_name, settings: saved_accounts.append(account_name),
    )

    window._auto_save_timer.timeout.emit()

    assert saved_accounts == [window._current_account]


def test_ctrl_return_shortcut_does_nothing_while_busy_forced_signal(window: XDeleterWindow, qtbot) -> None:
    emitted_requests: list[ExecuteActionsRequest] = []
    window.username_input.setText("user")
    window.on_login_checked(True, "user")
    window.display_posts([_sample(url="https://x.com/user/status/42")])
    window.delete_requested.connect(emitted_requests.append)
    window.set_busy(True)
    shortcut = next(
        item for item in window.findChildren(QShortcut)
        if item.key().matches(QKeySequence("Ctrl+Return")) == QKeySequence.SequenceMatch.ExactMatch
    )

    shortcut.activated.emit()

    assert shortcut.isEnabled() is False
    assert emitted_requests == []


def test_ctrl_r_shortcut_does_not_change_preview_while_busy_forced_signal(window: XDeleterWindow, qtbot) -> None:
    emitted_requests: list[object] = []
    window._commit_typed_account(load_settings=False)
    window.username_input.setText("test_user")
    window.on_login_checked(True, "test_user")
    window.display_posts([_sample(text="keep this preview")])
    window.table.selectRow(0)
    preview_before = window.preview_text.toPlainText()
    window.collect_requested.connect(emitted_requests.append)
    window.set_busy(True)
    shortcut = next(
        item for item in window.findChildren(QShortcut)
        if item.key().matches(QKeySequence("Ctrl+R")) == QKeySequence.SequenceMatch.ExactMatch
    )

    shortcut.activated.emit()

    assert shortcut.isEnabled() is False
    assert emitted_requests == []
    assert window.preview_text.toPlainText() == preview_before


def test_collect_handler_ignores_direct_requests_while_busy(window: XDeleterWindow, qtbot) -> None:
    emitted_requests: list[object] = []
    window.username_input.setText("test_user")
    window.on_login_checked(True, "test_user")
    window.display_posts([_sample()])
    window.preview_text.setPlainText("keep preview")
    window.collect_requested.connect(emitted_requests.append)
    window.set_busy(True)

    window._handle_collect()

    assert emitted_requests == []
    assert window.table.rowCount() == 1
    assert window.preview_text.toPlainText() == "keep preview"


def test_selection_summary_and_empty_state_follow_model(window, qtbot) -> None:
    _show_and_activate(window)
    assert window.empty_state.isVisible()
    window.display_posts([_sample(), _sample(id="2", url="https://x.com/user/status/2")])
    qtbot.waitUntil(lambda: window.selection_count.text() == "2 件 / 2 件選択")
    assert not window.empty_state.isVisible()
    window.select_all_button.click()
    qtbot.waitUntil(lambda: window.selection_count.text() == "2 件 / 0 件選択")
    window.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    qtbot.waitUntil(lambda: window.selection_count.text() == "2 件 / 1 件選択")
    window._clear_posts()
    qtbot.waitUntil(lambda: window.selection_count.text() == "0 件 / 0 件選択")
    assert window.empty_state.isVisible()


def test_empty_state_explains_no_match_and_all_processed(window, qtbot, monkeypatch) -> None:
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *a, **kw: None)
    _show_and_activate(window)
    window.display_posts([])
    assert window.empty_title.text() == "条件に一致するポストはありませんでした"

    window.display_posts([_sample()])
    window._requested_action_count = 1
    window.on_delete_done([
        PostActionResult(
            target=PostActionTarget(url="https://x.com/user/status/1", kind="post"),
            action_label="削除",
            success=True,
            error_message=None,
        )
    ])
    qtbot.waitUntil(lambda: window.selection_count.text() == "0 件 / 0 件選択")
    assert window.empty_state.isVisible()
    assert window.empty_title.text() == "選択した項目をすべて処理しました"


def test_post_text_column_stays_readable(window, qtbot) -> None:
    long_url = "https://x.com/some_very_long_account_name/status/" + "9" * 19
    for size in ((1440, 960), (980, 640)):
        window.resize(*size)
        _show_and_activate(window)
        window.display_posts([_sample(url=long_url, text="本文" * 80)])
        QApplication.processEvents()
        assert window.table.columnWidth(1) >= 240
        assert window.table.columnWidth(1) > window.table.columnWidth(2)


def test_stop_keeps_controls_locked_until_browser_stopped(window, monkeypatch) -> None:
    dialogs: list[str] = []
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.information", lambda *a, **kw: dialogs.append("info")
    )
    window._browser_running = True
    window._login_verified = True
    window.display_posts([_sample(), _sample(id="2", url="https://x.com/user/status/2")])
    window._requested_action_count = 2
    window._stop_browser()

    window.on_delete_done([
        PostActionResult(
            target=PostActionTarget(url="https://x.com/user/status/1", kind="post"),
            action_label="削除",
            success=True,
            error_message=None,
        )
    ])

    assert window._busy
    assert not window.delete_button.isEnabled()
    assert window.status_label.text() == "削除/解除処理を中止: 1 / 2 件成功（1 件未処理）"
    assert dialogs == []
    window.on_browser_stopped()
    assert not window._busy


def test_retry_failed_rechecks_account_match(window, monkeypatch) -> None:
    emitted: list[object] = []
    window.delete_requested.connect(emitted.append)
    window._login_verified = True
    window._last_logged_in_username = "user"
    window.username_input.setText("someone_else")
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question", lambda *a, **kw: QMessageBox.StandardButton.No
    )
    failure = PostActionResult(
        target=PostActionTarget(url="https://x.com/user/status/1", kind="post"),
        action_label="削除",
        success=False,
        error_message="failed",
    )

    window._retry_failed([failure])
    assert emitted == []

    window.username_input.setText("user")
    window._retry_failed([failure])
    assert len(emitted) == 1


def test_log_starts_collapsed_and_can_expand(window, qtbot) -> None:
    _show_and_activate(window)
    assert not window.log_text.isVisible()
    group = window.log_text.parentWidget()
    group.setChecked(True)
    assert window.log_text.isVisible()
    group.setChecked(False)
    assert not window.log_text.isVisible()


def test_compact_desktop_keeps_actions_and_settings_accessible(window, qtbot) -> None:
    window.resize(980, 640)
    _show_and_activate(window)
    assert window.width() == 980
    assert window.height() == 640
    assert window.table.height() >= 150
    assert window.rect().contains(window.delete_button.mapTo(window, window.delete_button.rect().bottomRight()))
    window.settings_scroll.ensureWidgetVisible(window.collect_button)
    QApplication.processEvents()
    viewport = window.settings_scroll.viewport()
    assert viewport.rect().contains(window.collect_button.mapTo(viewport, window.collect_button.rect().center()))


def test_export_following_requires_login_and_emits_request(window, qtbot, monkeypatch, tmp_path) -> None:
    assert not window.export_following_button.isEnabled()
    window.on_login_checked(True, "alice")
    assert window.export_following_button.isEnabled()

    emitted: list[object] = []
    window.export_following_requested.connect(emitted.append)
    chosen = tmp_path / "list"
    monkeypatch.setattr(
        "kusamushiri.gui.QFileDialog.getSaveFileName",
        lambda *args: (str(chosen), "CSV (*.csv)"),
    )
    window.export_following_button.click()

    assert len(emitted) == 1
    assert emitted[0].username == "alice"
    assert emitted[0].output_path == tmp_path / "list.csv"
    assert not window.export_following_button.isEnabled()

    monkeypatch.setattr("kusamushiri.gui.QMessageBox.information", lambda *args: None)
    window.on_following_export_finished(str(tmp_path / "list.csv"), 3)
    assert window.export_following_button.isEnabled()
    assert "3 件" in window.status_label.text()


def test_export_following_cancelled_dialog_emits_nothing(window, qtbot, monkeypatch) -> None:
    window.on_login_checked(True, "alice")
    emitted: list[object] = []
    window.export_following_requested.connect(emitted.append)
    monkeypatch.setattr("kusamushiri.gui.QFileDialog.getSaveFileName", lambda *args: ("", ""))

    window.export_following_button.click()

    assert emitted == []
    assert window.export_following_button.isEnabled()


def test_collect_request_carries_keywords_and_they_persist(window: XDeleterWindow, qtbot) -> None:
    emitted_requests: list[object] = []
    window._commit_typed_account(load_settings=False)
    window.username_input.setText("test_user")
    window.on_login_checked(True, "test_user")
    window.collect_requested.connect(emitted_requests.append)
    window.include_keywords_input.setText("懸賞、キャンペーン")
    window.exclude_keywords_input.setText(" 大事 ")

    window._handle_collect()

    assert len(emitted_requests) == 1
    request = emitted_requests[0]
    assert request.include_keywords == ("懸賞", "キャンペーン")
    assert request.exclude_keywords == ("大事",)
    assert "含む: 懸賞, キャンペーン" in window.filter_summary.text()
    assert "除外: 大事" in window.filter_summary.text()

    window.include_keywords_input.clear()
    window.exclude_keywords_input.clear()
    window._load_account_settings(window._current_account)
    assert window.include_keywords_input.text() == "懸賞、キャンペーン"
    assert window.exclude_keywords_input.text() == "大事"


def test_export_posts_saves_only_checked_rows(window, qtbot, monkeypatch, tmp_path) -> None:
    assert not window.export_posts_button.isEnabled()
    window.display_posts([_sample(), _sample(id="2", url="https://x.com/user/status/2", text="second")])
    assert window.export_posts_button.isEnabled()
    item = window.table.item(0, 0)
    assert item is not None
    item.setCheckState(Qt.CheckState.Unchecked)

    chosen = tmp_path / "backup"
    monkeypatch.setattr(
        "kusamushiri.gui.QFileDialog.getSaveFileName",
        lambda *args: (str(chosen), "JSON (*.json)"),
    )
    monkeypatch.setattr("kusamushiri.gui.QMessageBox.information", lambda *args: None)
    window.export_posts_button.click()

    saved = tmp_path / "backup.csv"
    assert saved.exists()
    text = saved.read_text(encoding="utf-8-sig")
    assert "second" in text
    assert "hello" not in text
    assert "1 件" in window.status_label.text()


def test_export_posts_cancelled_dialog_writes_nothing(window, qtbot, monkeypatch, tmp_path) -> None:
    window.display_posts([_sample()])
    monkeypatch.setattr("kusamushiri.gui.QFileDialog.getSaveFileName", lambda *args: ("", ""))

    window.export_posts_button.click()

    assert list(tmp_path.glob("*.csv")) == []


def _write_archive(folder: Path) -> None:
    data = folder / "data"
    data.mkdir(parents=True)
    tweets = [
        {"tweet": {"id_str": "10", "created_at": "Wed Oct 10 20:19:24 +0000 2018", "full_text": "old post",
                   "favorite_count": "2", "retweet_count": "0"}},
        {"tweet": {"id_str": "11", "created_at": "Thu Oct 11 20:19:24 +0000 2018", "full_text": "RT @bob: hi",
                   "favorite_count": "0", "retweet_count": "0"}},
        {"tweet": {"id_str": "12", "created_at": "Fri Oct 12 20:19:24 +0000 2018", "full_text": "keep me",
                   "favorite_count": "0", "retweet_count": "0"}},
    ]
    (data / "tweets.js").write_text("window.YTD.tweets.part0 = " + json.dumps(tweets), encoding="utf-8")


def test_import_archive_filters_and_skips_reposts(window, qtbot, monkeypatch, tmp_path) -> None:
    _write_archive(tmp_path)
    window.username_input.setText("alice")
    window.exclude_keywords_input.setText("keep")
    window.min_replies_input.setValue(5)
    monkeypatch.setattr(
        "kusamushiri.gui.QFileDialog.getOpenFileName",
        lambda *args: (str(tmp_path / "data" / "tweets.js"), ""),
    )
    assert window.import_archive_button.isEnabled()

    window.import_archive_button.click()

    assert window.table.rowCount() == 1
    url_item = window.table.item(0, 2)
    assert url_item is not None
    assert url_item.text() == "https://x.com/alice/status/10"
    assert "リポスト 1 件" in window.status_label.text()


def test_import_archive_reports_unreadable_archive(window, qtbot, monkeypatch, tmp_path) -> None:
    window.username_input.setText("alice")
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    monkeypatch.setattr("kusamushiri.gui.QFileDialog.getOpenFileName", lambda *args: (str(broken), ""))
    warnings: list[object] = []
    monkeypatch.setattr("kusamushiri.gui.QMessageBox.warning", lambda *args: warnings.append(args))

    window.import_archive_button.click()

    assert len(warnings) == 1
    assert window.table.rowCount() == 0
def test_likes_option_collects_from_profile_and_disables_mode(window: XDeleterWindow, qtbot) -> None:
    emitted_requests: list[object] = []
    window._commit_typed_account(load_settings=False)
    window.username_input.setText("test_user")
    window.on_login_checked(True, "test_user")
    window.collect_requested.connect(emitted_requests.append)
    window._set_combo_current_data(window.mode_combo, "search")
    window._set_combo_current_data(window.post_kind_combo, "likes")

    assert not window.mode_combo.isEnabled()
    window._handle_collect()

    assert len(emitted_requests) == 1
    request = emitted_requests[0]
    assert isinstance(request, CollectRequest)
    assert (request.post_kind_filter, request.search_mode) == ("likes", "profile")
    assert "種別: いいね" in window.filter_summary.text()

    window._set_combo_current_data(window.post_kind_combo, "posts")
    assert window.mode_combo.isEnabled()


def test_liked_posts_show_kind_and_unlike_summary(
    window: XDeleterWindow,
    qtbot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    questions: list[str] = []
    emitted_requests: list[ExecuteActionsRequest] = []
    window.username_input.setText("user")
    window.on_login_checked(True, "user")
    window.display_posts([
        _sample(url="https://x.com/someone/status/42", author_username="someone", kind="like"),
        _sample(id="43", url="https://x.com/other/status/43", author_username="other", kind="like"),
    ])
    window.delete_requested.connect(emitted_requests.append)

    def answer(_parent, _title, text, *args, **kwargs):
        questions.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.question", answer)

    window._handle_delete()

    assert window.table.item(0, 4).text() == "いいね済み"
    assert len(questions) == 1
    assert "いいね取り消し 2 件" in questions[0]
    assert "ポスト削除" not in questions[0]
    assert emitted_requests[0].targets == [
        PostActionTarget(url="https://x.com/someone/status/42", kind="like"),
        PostActionTarget(url="https://x.com/other/status/43", kind="like"),
    ]

    remove_successful_rows(
        window.table,
        [PostActionResult(emitted_requests[0].targets[0], "いいね取り消し", True, None)],
    )
    assert window.table.rowCount() == 1
def _follows() -> list[FollowRecord]:
    return [
        FollowRecord("mutual", "Mutual", "https://x.com/mutual", True),
        FollowRecord("one_way", "One way", "https://x.com/one_way", False),
        FollowRecord("quiet", "Quiet", "https://x.com/quiet", False),
    ]


def _open_follow_dialog(window: XDeleterWindow) -> FollowListDialog:
    window.on_login_checked(True, "alice")
    window.on_following_collected("alice", _follows(), False)
    dialog = window.follow_dialog
    assert dialog is not None
    return dialog


def _usernames(records: list[FollowRecord]) -> list[str]:
    return [record.username for record in records]


def test_manage_following_requires_login_and_emits_username(window, qtbot) -> None:
    assert not window.manage_following_button.isEnabled()
    window.on_login_checked(True, "alice")
    emitted: list[str] = []
    window.collect_following_requested.connect(emitted.append)

    window.manage_following_button.click()

    assert emitted == ["alice"]
    assert not window.manage_following_button.isEnabled()


def test_follow_dialog_hides_mutuals_and_starts_unchecked(window, qtbot) -> None:
    dialog = _open_follow_dialog(window)
    try:
        assert dialog.selected_records() == []
        dialog.toggle_all()
        assert _usernames(dialog.selected_records()) == ["one_way", "quiet"]

        dialog.hide_mutual_checkbox.setChecked(False)
        dialog.toggle_all()
        assert len(dialog.selected_records()) == 3
        dialog.hide_mutual_checkbox.setChecked(True)
        assert _usernames(dialog.selected_records()) == ["one_way", "quiet"]
    finally:
        dialog.close()


def test_unfollow_confirmation_emits_only_checked_rows(window, qtbot, monkeypatch) -> None:
    dialog = _open_follow_dialog(window)
    emitted: list[UnfollowRequest] = []
    window.unfollow_requested.connect(emitted.append)
    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.question", lambda *a, **kw: answers.pop(0))
    try:
        dialog.toggle_all()
        quiet_row = next(row for row in range(dialog.table.rowCount()) if dialog._record(row).username == "quiet")
        dialog.table.item(quiet_row, 0).setCheckState(Qt.CheckState.Unchecked)
        dialog.unfollow_button.click()
        assert emitted == []

        dialog.unfollow_button.click()
        assert len(emitted) == 1
        assert _usernames(emitted[0].targets) == ["one_way"]
        assert emitted[0].interval_seconds == 10.0
        assert not dialog.unfollow_button.isEnabled()
    finally:
        dialog.close()


def test_unfollow_rechecks_account_match(window, qtbot, monkeypatch) -> None:
    dialog = _open_follow_dialog(window)
    window._last_logged_in_username = "someone_else"
    emitted: list[UnfollowRequest] = []
    window.unfollow_requested.connect(emitted.append)
    titles: list[str] = []

    def answer(_parent, title, *_args, **_kwargs):
        titles.append(title)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.question", answer)
    try:
        dialog.toggle_all()
        dialog.unfollow_button.click()
        assert emitted == []
        assert titles == ["アカウント不一致"]
    finally:
        dialog.close()


def test_unfollow_done_removes_successes_and_offers_retry(window, qtbot, monkeypatch) -> None:
    dialog = _open_follow_dialog(window)
    records = _follows()
    window._requested_unfollow_count = 2
    retried: list[list[UnfollowResult]] = []
    monkeypatch.setattr(window, "_retry_failed_unfollows", retried.append)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.exec", lambda box: 0)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.clickedButton", lambda box: box.buttons()[0])
    try:
        window.on_unfollow_done([
            UnfollowResult(records[1], True),
            UnfollowResult(records[2], False, "rate limited"),
        ])
        remaining = {dialog._record(row).username for row in range(dialog.table.rowCount())}
        assert remaining == {"mutual", "quiet"}
        assert "1 / 2" in window.status_label.text()
        assert [_usernames([result.target for result in batch]) for batch in retried] == [["quiet"]]
    finally:
        dialog.close()


def test_retry_failed_unfollows_uses_dialog_interval(window, qtbot) -> None:
    dialog = _open_follow_dialog(window)
    emitted: list[UnfollowRequest] = []
    window.unfollow_requested.connect(emitted.append)
    dialog.interval_input.setValue(30)
    try:
        window._retry_failed_unfollows([UnfollowResult(_follows()[1], False, "failed")])
        assert len(emitted) == 1
        assert emitted[0].interval_seconds == 30
        assert _usernames(emitted[0].targets) == ["one_way"]
    finally:
        dialog.close()


def test_follow_dialog_save_writes_checked_rows(window, qtbot, monkeypatch, tmp_path) -> None:
    dialog = _open_follow_dialog(window)
    monkeypatch.setattr(
        "kusamushiri.gui_follows.QFileDialog.getSaveFileName", lambda *args: (str(tmp_path / "picked"), "")
    )
    monkeypatch.setattr("kusamushiri.gui_follows.QMessageBox.information", lambda *args: None)
    try:
        dialog.toggle_all()
        dialog.save_button.click()
        text = (tmp_path / "picked.csv").read_text(encoding="utf-8-sig")
        assert "one_way" in text and "quiet" in text and "mutual" not in text
    finally:
        dialog.close()
