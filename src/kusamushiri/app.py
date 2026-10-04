import queue
import sys
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from kusamushiri.actions import action_label_for
from kusamushiri.core import XDeleterCore
from kusamushiri.follows import ExportFollowingRequest, UnfollowRequest, UnfollowResult, write_follow_list
from kusamushiri.gui import XDeleterWindow
from kusamushiri.i18n import set_language, tr
from kusamushiri.logger import logger
from kusamushiri.models import CollectRequest, ExecuteActionsRequest, PostActionResult
from kusamushiri.paths import configure_frozen_browser_path, migrate_legacy_app_data
from kusamushiri.settings import AccountSettingsManager, migrate_legacy_settings


def apply_dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#11151c"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e3e9f3"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#151b24"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#191f29"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#191f29"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#e3e9f3"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e3e9f3"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#222b38"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e3e9f3"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link, QColor("#8296ff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#455ba5"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#718096"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#718096"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor("#718096"))

    palette.setColor(QPalette.ColorRole.Light, QColor("#6b7a91"))
    palette.setColor(QPalette.ColorRole.Mid, QColor("#435065"))
    app.setPalette(palette)


@dataclass(frozen=True, slots=True)
class StartBrowserCommand:
    action: str = "start_browser"
    account_name: str | None = None


@dataclass(frozen=True, slots=True)
class StopBrowserCommand:
    action: str = "stop_browser"


@dataclass(frozen=True, slots=True)
class CheckLoginCommand:
    action: str = "check_login"


@dataclass(frozen=True, slots=True)
class CollectPostsCommand:
    request: CollectRequest
    action: str = "collect_posts"


@dataclass(frozen=True, slots=True)
class DeletePostsCommand:
    request: ExecuteActionsRequest
    action: str = "delete_posts"


@dataclass(frozen=True, slots=True)
class ExportFollowingCommand:
    request: ExportFollowingRequest
    action: str = "export_following"


@dataclass(frozen=True, slots=True)
class CollectFollowingCommand:
    username: str
    action: str = "collect_following"


@dataclass(frozen=True, slots=True)
class UnfollowCommand:
    request: UnfollowRequest
    action: str = "unfollow"


WorkerCommand = (
    StartBrowserCommand
    | StopBrowserCommand
    | CheckLoginCommand
    | CollectPostsCommand
    | DeletePostsCommand
    | ExportFollowingCommand
    | CollectFollowingCommand
    | UnfollowCommand
)


@dataclass(frozen=True, slots=True)
class _QueuedCommand:
    command: WorkerCommand
    generation: int


class XDeleterWorkerEvents(QObject):
    status_changed = Signal(str)
    browser_ready = Signal()
    browser_stopped = Signal()
    login_checked = Signal(bool, str)
    posts_collected = Signal(list)
    collection_progress = Signal(int, int, int)
    delete_progress = Signal(str, str, int, int)
    delete_completed = Signal(list)
    following_progress = Signal(int)
    following_export_finished = Signal(str, int)  # empty path: nothing was written
    following_collected = Signal(str, list, bool)  # username, records, limit reached
    unfollow_progress = Signal(str, int, int)
    unfollow_completed = Signal(list)
    error_occurred = Signal(str)


class XDeleterWorker:
    def __init__(self, core: XDeleterCore | None = None) -> None:
        self.events = XDeleterWorkerEvents()
        self._core = core
        self._active_core = core
        self._cancel_event = threading.Event()
        self._command_lock = threading.RLock()
        self._generation = 0
        self._commands: queue.Queue[_QueuedCommand | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name="playwright-worker",
            daemon=False,
        )
        self._thread.start()

    def _handle_error(self, action: str, error: Exception) -> None:
        logger.exception("%s failed.", action)
        message = str(error) or tr("{action} に失敗しました。", action=action)
        self.events.status_changed.emit(tr("エラーが発生しました: {message}", message=message))
        self.events.error_occurred.emit(message)

    def _enqueue(self, command: WorkerCommand) -> None:
        with self._command_lock:
            self._commands.put(_QueuedCommand(command, self._generation))

    def enqueue_start_browser(self, account_name: str | None = None) -> None:
        self._enqueue(StartBrowserCommand(account_name=account_name))

    def enqueue_check_login(self) -> None:
        self._enqueue(CheckLoginCommand())

    def enqueue_stop_browser(self) -> None:
        with self._command_lock:
            self.cancel_current_operation()
            self._discard_pending_commands()
            self._enqueue(StopBrowserCommand())

    def enqueue_collect_posts(self, request: CollectRequest) -> None:
        self._enqueue(CollectPostsCommand(request=request))

    def enqueue_delete_posts(self, request: ExecuteActionsRequest) -> None:
        self._enqueue(DeletePostsCommand(request=request))

    def enqueue_export_following(self, request: ExportFollowingRequest) -> None:
        self._enqueue(ExportFollowingCommand(request=request))

    def enqueue_collect_following(self, username: str) -> None:
        self._enqueue(CollectFollowingCommand(username=username))

    def enqueue_unfollow(self, request: UnfollowRequest) -> None:
        self._enqueue(UnfollowCommand(request=request))

    def _discard_pending_commands(self) -> None:
        while True:
            try:
                self._commands.get_nowait()
            except queue.Empty:
                return

    def cancel_current_operation(self) -> None:
        with self._command_lock:
            self._generation += 1
            self._cancel_event.set()
            if self._active_core is not None:
                self._active_core.request_cancel()

    def _clear_cancel(self, core: XDeleterCore, queued: _QueuedCommand) -> bool:
        # Cancellation also invalidates commands already removed from the queue.
        # Check their generation and reset both Events atomically with cancellation.
        with self._command_lock:
            if queued.generation != self._generation:
                return False
            if not isinstance(queued.command, CheckLoginCommand):
                self._cancel_event.clear()
                core.clear_cancel()
            return True

    def _require_running_core(self, core: XDeleterCore) -> XDeleterCore:
        if core.page is None:
            raise RuntimeError(tr("先にブラウザを起動してログインしてください。"))
        return core

    def _run(self) -> None:
        logger.info("Starting Playwright backend thread.")
        core = self._core if self._core is not None else XDeleterCore()
        with self._command_lock:
            self._active_core = core
        while True:
            queued = self._commands.get()
            if queued is None:
                logger.info("Playwright backend thread received stop signal.")
                core.stop_browser()
                return

            command = queued.command
            try:
                if not isinstance(command, StopBrowserCommand) and not self._clear_cancel(core, queued):
                    continue

                if isinstance(command, StartBrowserCommand):
                    self.events.status_changed.emit(
                        tr("ブラウザを起動しています…（初回は Chromium のダウンロードに数分かかることがあります）")
                    )
                    core.start_browser(account_name=command.account_name)
                    core.go_to_home()
                    self.events.status_changed.emit(
                        tr("ブラウザを起動しました。ログイン後に確認を実行します。")
                    )
                    self.events.browser_ready.emit()
                    continue

                if isinstance(command, StopBrowserCommand):
                    core.stop_browser()
                    self.events.status_changed.emit(tr("ブラウザを停止しました。"))
                    self.events.browser_stopped.emit()
                    continue

                if isinstance(command, CheckLoginCommand):
                    active_core = self._require_running_core(core)
                    is_logged_in = active_core.is_logged_in()
                    current_username = active_core.get_logged_in_username() or ""
                    self.events.login_checked.emit(is_logged_in, current_username)
                    if is_logged_in:
                        if current_username:
                            self.events.status_changed.emit(
                                tr("ログインが確認されました。現在のログイン: @{username}", username=current_username)
                            )
                        else:
                            self.events.status_changed.emit(
                                tr("ログインが確認されました。ポストを収集できます。")
                            )
                    else:
                        self.events.status_changed.emit(
                            tr("ログインを確認できませんでした。X のホーム画面が開いた状態で再度確認してください。")
                        )
                    continue

                if isinstance(command, CollectPostsCommand):
                    posts = self._require_running_core(core).search_and_collect_posts(
                        command.request,
                        on_progress=lambda scanned, current, total: self.events.collection_progress.emit(
                            scanned, current, total
                        ),
                    )
                    self.events.posts_collected.emit(posts)
                    if self._cancel_event.is_set():
                        self.events.status_changed.emit(tr("収集処理を中断しました。"))
                    elif core.collection_limit_reached:
                        self.events.status_changed.emit(tr("収集の安全上限に到達したため走査を終了しました。"))
                    continue

                if isinstance(command, DeletePostsCommand):
                    command.request.validate()
                    if not command.request.targets:
                        self.events.delete_completed.emit([])
                        continue

                    active_core = self._require_running_core(core)
                    success_count = 0
                    total = len(command.request.targets)
                    interval_seconds = command.request.interval_seconds
                    results: list[PostActionResult] = []
                    for index, target in enumerate(command.request.targets, start=1):
                        if self._cancel_event.is_set():
                            break
                        action_label = action_label_for(target.kind)
                        self.events.delete_progress.emit(action_label, target.url, index, total)
                        result = active_core.execute_post_action(target)
                        results.append(result)
                        if result.success:
                            success_count += 1
                        if interval_seconds > 0 and index < total and self._cancel_event.wait(interval_seconds):
                            break
                    logger.info("Finished delete batch: %s/%s succeeded", success_count, total)
                    self.events.delete_completed.emit(results)
                    if self._cancel_event.is_set():
                        self.events.status_changed.emit(tr("削除/解除処理を中断しました。"))
                    continue

                if isinstance(command, ExportFollowingCommand):
                    command.request.validate()
                    result = self._require_running_core(core).collect_following(
                        command.request.username,
                        on_progress=self.events.following_progress.emit,
                    )
                    if self._cancel_event.is_set():
                        self.events.following_export_finished.emit("", len(result.records))
                        self.events.status_changed.emit(tr("フォローリストの取得を中断しました。ファイルは保存していません。"))
                        continue
                    write_follow_list(command.request.output_path, result.records)
                    self.events.following_export_finished.emit(
                        str(command.request.output_path), len(result.records)
                    )
                    if result.limit_reached:
                        self.events.status_changed.emit(
                            tr("取得の安全上限に到達したため、途中までのフォローリストを保存しました。")
                        )
                    continue

                if isinstance(command, CollectFollowingCommand):
                    result = self._require_running_core(core).collect_following(
                        command.username,
                        on_progress=self.events.following_progress.emit,
                    )
                    if self._cancel_event.is_set():
                        self.events.status_changed.emit(tr("フォローリストの取得を中断しました。"))
                    self.events.following_collected.emit(
                        command.username, result.records, result.limit_reached
                    )
                    continue

                if isinstance(command, UnfollowCommand):
                    command.request.validate()
                    active_core = self._require_running_core(core)
                    targets = command.request.targets
                    interval_seconds = command.request.interval_seconds
                    unfollow_results: list[UnfollowResult] = []
                    for index, target in enumerate(targets, start=1):
                        if self._cancel_event.is_set():
                            break
                        self.events.unfollow_progress.emit(target.username, index, len(targets))
                        success, error_message = active_core.unfollow_account(target.username)
                        unfollow_results.append(UnfollowResult(target, success, error_message))
                        if interval_seconds > 0 and index < len(targets) and self._cancel_event.wait(interval_seconds):
                            break
                    logger.info(
                        "Finished unfollow batch: %s/%s succeeded",
                        sum(1 for result in unfollow_results if result.success),
                        len(targets),
                    )
                    self.events.unfollow_completed.emit(unfollow_results)
                    if self._cancel_event.is_set():
                        self.events.status_changed.emit(tr("フォロー解除を中断しました。"))
                    continue

                logger.warning("Unknown worker command received: %s", command)
            except Exception as error:
                self._handle_error(getattr(command, "action", "worker command"), error)

    def shutdown(self) -> None:
        with self._command_lock:
            if not self._thread.is_alive():
                return
            self.cancel_current_operation()
            self._discard_pending_commands()
            self._commands.put(None)
        # The worker needs the command lock to reject a dequeued stale command.
        if self._thread.is_alive():
            self._thread.join()


def main() -> int:
    if migrate_legacy_app_data():
        logger.info("Moved profiles from the previous xposdeleter data directory.")
    configure_frozen_browser_path()
    logger.info("--- Starting Kusamushiri ---")
    app = QApplication(sys.argv)
    app.setOrganizationName("kusamushiri")
    app.setApplicationName("kusamushiri")
    if migrate_legacy_settings():
        logger.info("Copied settings from the previous xposdeleter configuration.")
    set_language(AccountSettingsManager().get_language())
    app.setApplicationDisplayName("Kusamushiri")
    apply_dark_palette(app)

    worker = XDeleterWorker()

    window = XDeleterWindow()
    window.start_browser_requested.connect(worker.enqueue_start_browser)
    window.stop_browser_requested.connect(worker.enqueue_stop_browser)
    window.check_login_requested.connect(worker.enqueue_check_login)
    window.collect_requested.connect(worker.enqueue_collect_posts)
    window.delete_requested.connect(worker.enqueue_delete_posts)
    window.export_following_requested.connect(worker.enqueue_export_following)
    window.collect_following_requested.connect(worker.enqueue_collect_following)
    window.unfollow_requested.connect(worker.enqueue_unfollow)

    worker.events.status_changed.connect(window.update_status)
    worker.events.browser_ready.connect(window.show_login_wait_dialog)
    worker.events.browser_stopped.connect(window.on_browser_stopped)
    worker.events.login_checked.connect(window.on_login_checked)
    worker.events.posts_collected.connect(window.display_posts)
    worker.events.collection_progress.connect(window.on_collection_progress)
    worker.events.delete_progress.connect(window.on_delete_progress)
    worker.events.delete_completed.connect(window.on_delete_done)
    worker.events.following_progress.connect(window.on_following_progress)
    worker.events.following_export_finished.connect(window.on_following_export_finished)
    worker.events.following_collected.connect(window.on_following_collected)
    worker.events.unfollow_progress.connect(window.on_unfollow_progress)
    worker.events.unfollow_completed.connect(window.on_unfollow_done)
    worker.events.error_occurred.connect(window.show_error)

    window.show()
    exit_code = app.exec()

    worker.shutdown()

    logger.info("--- Kusamushiri Stopped ---")
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
