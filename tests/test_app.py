from __future__ import annotations

import queue
import threading
from collections.abc import Callable

import pytest

from kusamushiri.app import DeletePostsCommand, XDeleterWorker
from kusamushiri.follows import (
    ExportFollowingRequest,
    FollowCollectionResult,
    FollowRecord,
    LastPostEntry,
    UnfollowRequest,
    UnfollowResult,
)
from kusamushiri.last_posts import FetchLastPostsRequest, LastPostResult
from kusamushiri.models import CollectRequest, ExecuteActionsRequest, PostActionResult, PostActionTarget, PostRecord


class FakeCore:
    rate_limit_status: int | None = None
    on_rate_limited: Callable[[], None] | None = None

    def __init__(self) -> None:
        self.page: object | None = object()
        self.calls: list[str] = []
        self.account_name: str | None = None
        self.is_logged_in_result = True
        self.username_result = "tester"
        self.collected_posts = [
            PostRecord(
                id="1",
                url="https://x.com/tester/status/1",
                author_username="tester",
                text="hello",
                date="2026-01-01",
                likes=1,
                replies=0,
                has_media=False,
                is_reply=False,
                kind="post",
            )
        ]
        self.action_result = PostActionResult(
            target=PostActionTarget(url="https://x.com/tester/status/1", kind="post"),
            action_label="ポスト削除",
            success=True,
            error_message=None,
        )
        self.cancel_requested = False
        self.collection_limit_reached = False
        self.executed_targets: list[PostActionTarget] = []
        self.fail_on: str | None = None
        self.cancel_hook: Callable[[], None] = lambda: None

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def clear_cancel(self) -> None:
        self.cancel_requested = False
        self.rate_limit_status = None

    def _raise_if_configured(self, method_name: str) -> None:
        if self.fail_on == method_name:
            raise RuntimeError(f"{method_name} failed")

    def start_browser(self, account_name: str | None = None) -> None:
        self._raise_if_configured("start_browser")
        self.calls.append("start_browser")
        self.account_name = account_name

    def go_to_home(self) -> None:
        self._raise_if_configured("go_to_home")
        self.calls.append("go_to_home")

    def is_logged_in(self) -> bool:
        self._raise_if_configured("is_logged_in")
        self.calls.append("is_logged_in")
        return self.is_logged_in_result

    def get_logged_in_username(self) -> str | None:
        self._raise_if_configured("get_logged_in_username")
        self.calls.append("get_logged_in_username")
        return self.username_result

    def search_and_collect_posts(self, request: CollectRequest, on_progress=None) -> list[PostRecord]:
        self._raise_if_configured("search_and_collect_posts")
        self.calls.append(f"search_and_collect_posts:{request.username}")
        if on_progress is not None:
            on_progress(1, 1, 100)
        return self.collected_posts

    def collect_following(self, username: str, on_progress=None) -> FollowCollectionResult:
        self._raise_if_configured("collect_following")
        self.calls.append(f"collect_following:{username}")
        if on_progress is not None:
            on_progress(1)
        if self.fail_on == "cancel_during_following":
            self.cancel_hook()
        return FollowCollectionResult(records=[FollowRecord("alice", "Alice", "https://x.com/alice", True)])

    def execute_post_action(self, target: PostActionTarget) -> PostActionResult:
        self._raise_if_configured("execute_post_action")
        self.calls.append(f"execute_post_action:{target.url}")
        self.executed_targets.append(target)
        return self.action_result

    def unfollow_account(self, username: str) -> tuple[bool, str | None]:
        self._raise_if_configured("unfollow_account")
        self.calls.append(f"unfollow_account:{username}")
        if username == "rate_limited":
            return False, "friendships/destroy.json がエラーを返しました: Rate limit"
        if username == "http429":
            self.rate_limit_status = 429
            self.request_cancel()
            if self.on_rate_limited is not None:
                self.on_rate_limited()
            return False, "HTTP 429"
        return True, None

    def fetch_last_post(self, username: str) -> LastPostResult:
        self._raise_if_configured("fetch_last_post")
        self.calls.append(f"fetch_last_post:{username}")
        if username == "broken":
            return LastPostResult(username, "2026-10-01T00:00:00+00:00", error_message="timeline did not load")
        return LastPostResult(username, "2026-10-01T00:00:00+00:00", last_post_at="2026-09-30T00:00:00+00:00")

    def stop_browser(self) -> None:
        self.calls.append("stop_browser")


def build_collect_request() -> CollectRequest:
    return CollectRequest(
        username="tester",
        max_posts=10,
        media_filter="all",
        is_reply=False,
        min_likes=0,
        min_replies=0,
        search_mode="search",
    )


def build_delete_request() -> ExecuteActionsRequest:
    return ExecuteActionsRequest(
        targets=[PostActionTarget(url="https://x.com/tester/status/1", kind="post")],
        interval_seconds=0,
    )


def wait_until(qtbot: object, predicate: Callable[[], bool]) -> None:
    qtbot.waitUntil(predicate, timeout=1_000)


def test_worker_dispatches_start_browser_when_enqueued(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    browser_ready_count = 0

    def record_browser_ready() -> None:
        nonlocal browser_ready_count
        browser_ready_count += 1

    worker.events.browser_ready.connect(record_browser_ready)

    worker.enqueue_start_browser(account_name="work")

    wait_until(qtbot, lambda: browser_ready_count == 1)
    assert core.calls[:2] == ["start_browser", "go_to_home"]
    assert core.account_name == "work"
    worker.shutdown()


def test_worker_dispatches_check_login_when_enqueued(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    login_events: list[tuple[bool, str]] = []
    worker.events.login_checked.connect(lambda logged_in, username: login_events.append((logged_in, username)))

    worker.enqueue_check_login()

    wait_until(qtbot, lambda: login_events == [(True, "tester")])
    assert core.calls[:2] == ["is_logged_in", "get_logged_in_username"]
    worker.shutdown()


def test_worker_dispatches_collect_posts_when_enqueued(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    collected_events: list[list[PostRecord]] = []
    worker.events.posts_collected.connect(collected_events.append)

    worker.enqueue_collect_posts(build_collect_request())

    wait_until(qtbot, lambda: collected_events == [core.collected_posts])
    assert core.calls == ["search_and_collect_posts:tester"]
    worker.shutdown()


def test_worker_dispatches_delete_posts_when_enqueued(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    completed_events: list[list[PostActionResult]] = []
    worker.events.delete_completed.connect(completed_events.append)

    worker.enqueue_delete_posts(build_delete_request())

    wait_until(qtbot, lambda: completed_events == [[core.action_result]])
    assert core.calls == ["execute_post_action:https://x.com/tester/status/1"]
    assert core.executed_targets == [PostActionTarget(url="https://x.com/tester/status/1", kind="post")]
    worker.shutdown()


def test_worker_exports_following_to_file(qtbot: object, tmp_path) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    finished: list[tuple[str, int]] = []
    progress: list[int] = []
    worker.events.following_progress.connect(progress.append)
    worker.events.following_export_finished.connect(lambda path, count: finished.append((path, count)))
    output_path = tmp_path / "following.csv"

    entry = LastPostEntry("2026-10-01T09:30:00+00:00", "2026-09-30T08:00:00+00:00")

    worker.enqueue_export_following(
        ExportFollowingRequest(username="tester", output_path=output_path, last_posts={"alice": entry})
    )

    wait_until(qtbot, lambda: finished == [(str(output_path), 1)])
    assert core.calls == ["collect_following:tester"]
    assert progress == [1]
    text = output_path.read_text(encoding="utf-8-sig")
    assert "alice" in text and "2026-09-30T08:00:00+00:00,2026-10-01T09:30:00+00:00" in text
    worker.shutdown()


def test_worker_does_not_write_following_file_when_cancelled(qtbot: object, tmp_path) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    core.fail_on = "cancel_during_following"
    core.cancel_hook = worker.cancel_current_operation
    finished: list[tuple[str, int]] = []
    worker.events.following_export_finished.connect(lambda path, count: finished.append((path, count)))
    output_path = tmp_path / "following.csv"

    worker.enqueue_export_following(ExportFollowingRequest(username="tester", output_path=output_path))

    wait_until(qtbot, lambda: finished == [("", 1)])
    assert not output_path.exists()
    worker.shutdown()


def test_worker_emits_status_and_error_when_command_fails(qtbot: object) -> None:
    core = FakeCore()
    core.fail_on = "search_and_collect_posts"
    worker = XDeleterWorker(core=core)
    statuses: list[str] = []
    errors: list[str] = []
    worker.events.status_changed.connect(statuses.append)
    worker.events.error_occurred.connect(errors.append)

    worker.enqueue_collect_posts(build_collect_request())

    wait_until(qtbot, lambda: errors == ["search_and_collect_posts failed"])
    assert statuses == ["エラーが発生しました: search_and_collect_posts failed"]
    worker.shutdown()


def test_worker_shutdown_stops_thread_deterministically(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)

    worker.shutdown()

    assert worker._thread.is_alive() is False
    assert worker._thread.daemon is False
    assert core.calls == ["stop_browser"]


def test_worker_cancels_delete_batch_during_interval(qtbot: object) -> None:
    core = FakeCore()
    core.page = object()
    worker = XDeleterWorker(core=core)
    completed_events: list[list[PostActionResult]] = []
    worker.events.delete_progress.connect(lambda *args: worker.cancel_current_operation())
    worker.events.delete_completed.connect(completed_events.append)
    request = ExecuteActionsRequest(
        targets=[
            PostActionTarget(url="https://x.com/user/status/1", kind="post"),
            PostActionTarget(url="https://x.com/user/status/2", kind="post"),
        ],
        interval_seconds=10,
    )

    worker.enqueue_delete_posts(request)
    wait_until(qtbot, lambda: len(completed_events) == 1)
    worker.shutdown()

    assert len(completed_events[0]) <= 1


def test_worker_stop_command_closes_browser(qtbot: object) -> None:
    core = FakeCore()
    core.page = object()
    worker = XDeleterWorker(core=core)
    stopped = 0

    def record_stopped() -> None:
        nonlocal stopped
        stopped += 1

    worker.events.browser_stopped.connect(record_stopped)
    worker.enqueue_stop_browser()
    wait_until(qtbot, lambda: stopped == 1)
    worker.shutdown()

    assert "stop_browser" in core.calls


@pytest.mark.parametrize("cancellation", ["cancel", "stop", "shutdown"])
def test_worker_cancellation_after_dequeue_skips_old_commands(
    qtbot: object, monkeypatch: pytest.MonkeyPatch, cancellation: str
) -> None:
    dequeued = threading.Event()
    resume = threading.Event()
    cancelled = threading.Event()

    class PausedQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            item = super().get(block=block, timeout=timeout)
            command = getattr(item, "command", item)
            if block and isinstance(command, DeletePostsCommand) and not dequeued.is_set():
                dequeued.set()
                assert resume.wait(3), "dequeued command was never released"
            return item

    commands = PausedQueue()
    monkeypatch.setattr("kusamushiri.app.queue.Queue", lambda: commands)
    core = FakeCore()
    request_cancel = core.request_cancel

    def record_cancellation() -> None:
        request_cancel()
        cancelled.set()

    monkeypatch.setattr(core, "request_cancel", record_cancellation)
    worker = XDeleterWorker(core=core)
    completed: list[list[PostActionResult]] = []
    errors: list[str] = []
    worker.events.delete_completed.connect(completed.append)
    worker.events.error_occurred.connect(errors.append)
    shutdown_thread = None
    future_request = ExecuteActionsRequest(
        targets=[PostActionTarget(url="https://x.com/tester/status/3", kind="post")],
        interval_seconds=0,
    )

    try:
        worker.enqueue_delete_posts(build_delete_request())
        assert dequeued.wait(3), "worker never dequeued the command"
        worker.enqueue_delete_posts(build_delete_request())
        if cancellation == "shutdown":
            shutdown_thread = threading.Thread(target=worker.shutdown)
            shutdown_thread.start()
        elif cancellation == "stop":
            worker.enqueue_stop_browser()
        else:
            worker.cancel_current_operation()
        assert cancelled.wait(3), "cancellation did not reach the core"
        if cancellation != "shutdown":
            worker.enqueue_delete_posts(future_request)
        resume.set()

        if shutdown_thread is not None:
            shutdown_thread.join(timeout=3)
            assert not shutdown_thread.is_alive(), "shutdown failed to join the worker"
            assert not worker._thread.is_alive()
            assert core.executed_targets == []
        else:
            wait_until(qtbot, lambda: bool(completed) and future_request.targets[0] in core.executed_targets)
            assert core.executed_targets == list(future_request.targets)
            assert len(completed) == 1
            assert not core.cancel_requested
            if cancellation == "stop":
                assert core.calls[0] == "stop_browser"
        assert errors == []
    finally:
        resume.set()
        if shutdown_thread is not None:
            shutdown_thread.join(timeout=3)
        worker.shutdown()


def test_worker_active_batch_cancellation_survives_future_submission(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    resume = threading.Event()
    cancelled = threading.Event()
    core = FakeCore()
    execute = core.execute_post_action

    def paused_execute(target: PostActionTarget) -> PostActionResult:
        if not started.is_set():
            started.set()
            assert resume.wait(3), "active action was never released"
        return execute(target)

    monkeypatch.setattr(core, "execute_post_action", paused_execute)
    worker = XDeleterWorker(core=core)
    completed: list[list[PostActionResult]] = []
    worker.events.delete_completed.connect(completed.append)
    request = ExecuteActionsRequest(
        targets=[
            PostActionTarget(url="https://x.com/tester/status/1", kind="post"),
            PostActionTarget(url="https://x.com/tester/status/2", kind="post"),
        ],
        interval_seconds=10,
    )
    future_request = ExecuteActionsRequest(
        targets=[PostActionTarget(url="https://x.com/tester/status/3", kind="post")],
        interval_seconds=0,
    )

    def cancel() -> None:
        worker.cancel_current_operation()
        cancelled.set()

    cancellation_thread = threading.Thread(target=cancel)
    try:
        worker.enqueue_delete_posts(request)
        assert started.wait(3), "delete batch never started"
        cancellation_thread.start()
        assert cancelled.wait(3), "cancellation blocked on the active browser action"
        worker.enqueue_delete_posts(future_request)
        assert worker._cancel_event.is_set()
        assert core.cancel_requested
        resume.set()
        wait_until(qtbot, lambda: len(completed) == 2)
        assert core.executed_targets == [request.targets[0], future_request.targets[0]]
        assert [len(results) for results in completed] == [1, 1]
        assert not core.cancel_requested
    finally:
        resume.set()
        if cancellation_thread.ident is not None:
            cancellation_thread.join(timeout=3)
        worker.shutdown()


def _follow(username: str, follows_you: bool = False) -> FollowRecord:
    return FollowRecord(username, username.title(), f"https://x.com/{username}", follows_you)


def test_worker_collects_following_for_review(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    collected: list[tuple[str, list[FollowRecord], bool]] = []
    worker.events.following_collected.connect(lambda name, records, limit: collected.append((name, records, limit)))

    worker.enqueue_collect_following("tester")

    wait_until(qtbot, lambda: len(collected) == 1)
    assert collected[0] == ("tester", [FollowRecord("alice", "Alice", "https://x.com/alice", True)], False)
    assert core.calls == ["collect_following:tester"]
    worker.shutdown()


def test_worker_unfollows_each_target_and_reports_results(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    progress: list[tuple[str, int, int]] = []
    completed: list[list[UnfollowResult]] = []
    worker.events.unfollow_progress.connect(lambda name, current, total: progress.append((name, current, total)))
    worker.events.unfollow_completed.connect(completed.append)
    targets = [_follow("alice"), _follow("rate_limited")]

    worker.enqueue_unfollow(UnfollowRequest(targets=targets, interval_seconds=0))

    wait_until(qtbot, lambda: len(completed) == 1)
    assert core.calls == ["unfollow_account:alice", "unfollow_account:rate_limited"]
    assert progress == [("alice", 1, 2), ("rate_limited", 2, 2)]
    assert [(result.target.username, result.success) for result in completed[0]] == [
        ("alice", True),
        ("rate_limited", False),
    ]
    assert "Rate limit" in (completed[0][1].error_message or "")
    worker.shutdown()


def test_worker_cancels_unfollow_batch_during_interval(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    completed: list[list[UnfollowResult]] = []
    worker.events.unfollow_progress.connect(lambda *args: worker.cancel_current_operation())
    worker.events.unfollow_completed.connect(completed.append)

    worker.enqueue_unfollow(UnfollowRequest(targets=[_follow("alice"), _follow("bob")], interval_seconds=30))

    wait_until(qtbot, lambda: len(completed) == 1)
    worker.shutdown()
    assert len(completed[0]) <= 1
    assert "unfollow_account:bob" not in core.calls


def test_worker_stops_unfollow_batch_when_rate_limited(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    completed: list[list[UnfollowResult]] = []
    errors: list[str] = []
    worker.events.unfollow_completed.connect(completed.append)
    worker.events.rate_limited.connect(errors.append)

    # The interval wait must wake up on the rate limit instead of sleeping it out.
    worker.enqueue_unfollow(UnfollowRequest(targets=[_follow("http429"), _follow("bob")], interval_seconds=30))

    wait_until(qtbot, lambda: len(completed) == 1 and len(errors) == 1)
    worker.shutdown()
    assert [result.target.username for result in completed[0]] == ["http429"]
    assert "unfollow_account:bob" not in core.calls
    assert "HTTP 429" in errors[0]


def test_unfollow_request_rejects_empty_targets_and_negative_interval() -> None:
    with pytest.raises(ValueError):
        UnfollowRequest(targets=[]).validate()
    with pytest.raises(ValueError):
        UnfollowRequest(targets=[_follow("alice")], interval_seconds=-1).validate()


def test_worker_fetches_last_posts_for_each_target(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    progress: list[tuple[str, int, int]] = []
    completed: list[list[LastPostResult]] = []
    worker.events.last_post_progress.connect(lambda name, current, total: progress.append((name, current, total)))
    worker.events.last_posts_completed.connect(completed.append)

    worker.enqueue_fetch_last_posts(
        FetchLastPostsRequest(targets=[_follow("alice"), _follow("broken")], interval_seconds=0)
    )

    wait_until(qtbot, lambda: len(completed) == 1)
    assert core.calls == ["fetch_last_post:alice", "fetch_last_post:broken"]
    assert progress == [("alice", 1, 2), ("broken", 2, 2)]
    assert [(result.username, result.success, result.last_post_at) for result in completed[0]] == [
        ("alice", True, "2026-09-30T00:00:00+00:00"),
        ("broken", False, None),
    ]
    worker.shutdown()


def test_worker_cancels_last_post_batch_during_interval(qtbot: object) -> None:
    core = FakeCore()
    worker = XDeleterWorker(core=core)
    completed: list[list[LastPostResult]] = []
    worker.events.last_post_progress.connect(lambda *args: worker.cancel_current_operation())
    worker.events.last_posts_completed.connect(completed.append)

    worker.enqueue_fetch_last_posts(
        FetchLastPostsRequest(targets=[_follow("alice"), _follow("bob")], interval_seconds=30)
    )

    wait_until(qtbot, lambda: len(completed) == 1)
    worker.shutdown()
    assert len(completed[0]) <= 1
    assert "fetch_last_post:bob" not in core.calls


def test_fetch_last_posts_request_rejects_empty_targets_and_negative_interval() -> None:
    with pytest.raises(ValueError):
        FetchLastPostsRequest(targets=[]).validate()
    with pytest.raises(ValueError):
        FetchLastPostsRequest(targets=[_follow("alice")], interval_seconds=-1).validate()
