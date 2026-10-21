"""Bounded post-action retries; no browser, network or credentials are managed here."""

import math
import random
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

from kusamushiri.active_target_session import ActionNotSentError
from kusamushiri.i18n import tr
from kusamushiri.logger import logger
from kusamushiri.models import PostActionResult, PostActionTarget, RetryPolicy
from kusamushiri.progress import ActionJournal


@dataclass
class _BatchState:
    results: list[PostActionResult] = field(default_factory=list)
    current: PostActionTarget | None = None
    current_result: PostActionResult | None = None
    current_attempt: int = 1
    index: int = 0
    executing: bool = False
    reason: str = "completed"
    cooldowns: int = 0
    scheduled_pauses: int = 0
    deferrals: int = 0
    retries: int = 0
    waited: float = 0.0


class ActionBatchError(RuntimeError):
    """Private partial results survive unexpected faults; never include raw errors."""

    def __init__(self, results: Sequence[PostActionResult], summary: dict[str, object]) -> None:
        super().__init__("Action batch stopped: action deadline or outcome requires review.")
        self.results = list(results)
        self.summary = summary


# Rejects epoch-millisecond or otherwise implausible values; waits remain bounded by max_wait.
MAX_RATE_LIMIT_RESET_SECONDS = 86400.0


def parse_retry_after(value: object, *, now: float | None = None) -> float | None:
    """Read HTTP delta-seconds or an HTTP-date, ignoring malformed/nonfinite values.

    Values are capped at MAX_RATE_LIMIT_RESET_SECONDS: they are persisted as cooldown deadlines,
    and an absurd header must not lock the profile for years.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        try:
            seconds = float(value)
        except ValueError:
            return None
        return min(seconds, MAX_RATE_LIMIT_RESET_SECONDS) if math.isfinite(seconds) else None
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        delay = deadline.timestamp() - (time.time() if now is None else now)
        return min(max(0.0, delay), MAX_RATE_LIMIT_RESET_SECONDS)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_rate_limit_reset(value: object, *, now: float | None = None) -> float | None:
    """Seconds until an x-rate-limit-reset epoch; past, malformed or out-of-range values are ignored."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not (value.isascii() and value.isdecimal()):
        return None
    delay = float(value) - (time.time() if now is None else now)
    return delay if 0.0 < delay <= MAX_RATE_LIMIT_RESET_SECONDS else None


def parse_rate_limit_remaining(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return int(value) if value.isascii() and value.isdecimal() else None


class ActionCore(Protocol):
    rate_limit_status: int | None

    def execute_post_action(self, target: PostActionTarget) -> PostActionResult: ...
    def is_cancel_requested(self) -> bool: ...
    def request_cancel(self) -> None: ...
    def wait_for_cancel(self, seconds: float) -> bool: ...


def _run_post_actions(
    core: ActionCore,
    targets: Sequence[PostActionTarget],
    interval_seconds: float,
    policy: RetryPolicy,
    *,
    journal: ActionJournal | None = None,
    on_progress: Callable[[PostActionTarget, int, int], None] = lambda *_: None,
    on_status: Callable[[str], None] = lambda _: None,
    on_result: Callable[[PostActionResult], None] = lambda _: None,
    wait_for_cancel: Callable[[float], bool] | None = None,
    known_successes: set[str] | None = None,
    _state: _BatchState,
) -> list[PostActionResult]:
    """Quiet bounded cooldowns are distinct from actual action retries.

    Unisolated X 429 remains a conservative global pacing signal. An unsent action is
    pending, not failed. Browser traffic stays paused on cancellation/exhaustion.
    """
    policy.validate()
    prepare = getattr(core, "prepare_post_action_policy", None)
    if prepare is not None:
        prepare(policy)
    results = _state.results
    seen: dict[str, PostActionResult] = {}
    waited = 0.0
    cooldowns = 0
    fallback_cooldowns = 0
    paced = False
    completed_since_pause = 0
    pending: PostActionResult | None = None
    pending_after_deadline = 0.0
    header_key: tuple[float, float, float] | None = None
    fallback_header_deadline = 0.0
    wait = wait_for_cancel or getattr(core, "wait_for_action_cancel", core.wait_for_cancel)

    def number(name: str) -> float:
        value = getattr(core, name, 0.0)
        return float(value) if isinstance(value, (int, float)) else 0.0

    def emit(message: str) -> None:
        logger.info("Post batch: %s", message)
        on_status(message)

    def pump() -> None:
        callback = getattr(core, "poll_action_events", None)
        if callback is not None:
            callback()

    def suspend(deadline: float, reason: str) -> None:
        if journal is not None:
            journal.defer_until(deadline, reason)
        callback = getattr(core, "suspend_action_network", None)
        if callback is not None:
            callback(deadline)

    def resume() -> None:
        callback = getattr(core, "resume_action_network", None)
        if callback is not None:
            callback()

    def limit_reason() -> str:
        role = getattr(core, "rate_limit_scope", None) or (pending.rate_limit_scope if pending else None) or "unknown"
        endpoint = (
            getattr(core, "rate_limit_endpoint", None)
            or (pending.rate_limit_endpoint if pending else None)
            or "unclassified"
        )
        reason = tr("X の利用制限（{scope} / {endpoint}）", scope=role, endpoint=endpoint)
        group = getattr(core, "rate_limit_group", "")
        if role == "unknown" and isinstance(group, str) and group:
            reason += f" [{group}; {getattr(core, 'rate_limit_resource', 'other')}; {getattr(core, 'rate_limit_family', 'other')}; {getattr(core, 'rate_limit_api_label', 'unrecognized')}]"
        return reason

    def preserve_limit(ordinal: int) -> tuple[float, str]:
        nonlocal header_key, fallback_header_deadline
        now = time.time()
        after = number("rate_limit_retry_after")
        until = number("rate_limit_retry_until")
        if after and not until:
            key = (number("rate_limit_generation"), number("rate_limit_seen_at"), after)
            if key != header_key:
                header_key = key
                fallback_header_deadline = (key[1] or now) + after
            until = fallback_header_deadline
        server_deadline = max(until, pending_after_deadline)
        deadline = server_deadline if policy.wait_limit_mode == "per-wait" and server_deadline > now else max(now + policy.cooldown_delay(ordinal), server_deadline)
        reason = limit_reason()
        suspend(deadline, reason)
        return deadline, reason

    def protected_until() -> float:
        return max(number("rate_limit_retry_until"), number("rate_limit_pace_until"),
                   journal.cooldown_until if journal is not None else 0.0)

    def pause(
        seconds: float, ordinal: int, limit: int, reason: str, *, proactive: bool = False, rate_floor: float = 0.0
    ) -> bool:
        nonlocal waited, paced
        started = time.time()
        scheduled = started + seconds
        deadline = max(scheduled, protected_until())
        generation = number("rate_limit_generation")
        while True:
            suspend(deadline, reason)
            # set_offline dispatches older queued responses; do not clear them as
            # though they were observed before the wait or ignore a newer header.
            observed = number("rate_limit_generation")
            if observed != generation:
                generation = observed
                server_deadline = number("rate_limit_retry_until")
                floor = server_deadline if policy.wait_limit_mode == "per-wait" and server_deadline > time.time() else number("rate_limit_seen_at") + (rate_floor or policy.cooldown_delay(1))
                deadline = max(deadline, server_deadline, floor)
            if core.rate_limit_status is not None:
                paced = True
                reason = limit_reason()
                deadline = max(deadline, number("rate_limit_retry_until"))
            deadline = max(deadline, protected_until())
            remaining = max(0.0, deadline - time.time())
            charge = not proactive or core.rate_limit_status is not None or protected_until() > scheduled
            if core.rate_limit_status is not None and policy.max_cooldowns == 0:
                _state.reason = "cooldown_limit"
                suspend(deadline, reason)
                emit(tr("通信休止の上限に達したため停止します。未送信の項目は未処理で保持します。"))
                core.request_cancel()
                return False
            required = waited + max(0.0, time.time() - started) + remaining if policy.wait_limit_mode == "total" else remaining
            if charge and required > policy.max_wait_seconds:
                _state.reason = "wait_limit" if policy.wait_limit_mode == "total" else "single_wait_limit"
                suspend(deadline, reason)
                emit(tr("1回の必要待機が設定上限を超えました。期限を保持して停止します。") if policy.wait_limit_mode == "per-wait" else tr("再試行の待機上限に達しました。処理を停止します。"))
                core.request_cancel()
                return False
            suspend(deadline, reason)
            deadline_text = datetime.fromtimestamp(deadline, UTC).isoformat(timespec="seconds")
            if ordinal and limit:
                emit(
                    tr(
                        "{reason}: {seconds:.1f} 秒休止（{current}/{limit}）。期限 {deadline}。通信を休止し、中止できます。",
                        reason=reason,
                        seconds=remaining,
                        current=ordinal,
                        limit=limit,
                        deadline=deadline_text,
                    )
                )
            else:
                emit(
                    tr(
                        "{reason}: {seconds:.1f} 秒休止。期限 {deadline}。通信を休止し、中止できます。",
                        reason=reason,
                        seconds=remaining,
                        deadline=deadline_text,
                    )
                )
            if core.is_cancel_requested() or (remaining > 0 and wait(remaining)) or core.is_cancel_requested():
                deadline = max(deadline, protected_until())
                suspend(deadline, reason)
                _state.reason = "cancelled"
                if charge:
                    _state.waited = waited + max(0.0, time.time() - started)
                core.request_cancel()
                return False
            pump()
            latest = protected_until()
            if number("rate_limit_generation") != generation or latest > deadline:
                deadline = max(deadline, latest)
                continue
            if time.time() < deadline:
                # A fake clock must advance with waits; production uses monotonic
                # cancellable waits and checks the persisted wall-clock deadline.
                continue
            if journal is not None:
                # No future server/predicted/persisted deadline may be erased on resume.
                latest = max(protected_until(), number("isolated_rate_limit_deadline"))
                if latest > time.time():
                    deadline = max(deadline, latest)
                    continue
                journal.defer_until(0.0)
            if core.rate_limit_status is None:
                resume()
                latest = protected_until()
                if latest > time.time() or core.rate_limit_status is not None:
                    deadline = max(deadline, latest)
                    continue
            if charge:
                waited += max(0.0, time.time() - started)
                _state.waited = waited
            return True

    def cool_down() -> bool:
        nonlocal cooldowns, fallback_cooldowns, pending, paced, pending_after_deadline, header_key
        cooldowns += 1
        server_directed = max(number("rate_limit_retry_until"), pending_after_deadline) > time.time() or number("rate_limit_retry_after") > 0
        bounded = policy.wait_limit_mode == "total" or not server_directed
        if bounded:
            fallback_cooldowns += 1
        ordinal = fallback_cooldowns if bounded else 1
        _state.cooldowns = cooldowns
        paced = True
        deadline, reason = preserve_limit(max(1, ordinal))
        if max(deadline, protected_until()) <= time.time():
            _state.reason = "cooldown_limit"
            emit(tr("有効な待機期限がなく、ローカル休止も無効なため停止します。自動再送しません。"))
            core.request_cancel()
            return False
        if policy.max_cooldowns == 0 or bounded and fallback_cooldowns > policy.max_cooldowns:
            _state.reason = "cooldown_limit"
            emit(tr("通信休止の上限に達したため停止します。未送信の項目は未処理で保持します。"))
            core.request_cancel()
            return False
        if not pause(
            max(0.0, deadline - time.time()),
            fallback_cooldowns if bounded else 0,
            policy.max_cooldowns if bounded else 0,
            reason,
            rate_floor=policy.cooldown_delay(max(1, ordinal)),
        ):
            return False
        if core.rate_limit_status is not None:
            clear = getattr(core, "clear_rate_limit", None)
            if clear is None:
                core.request_cancel()
                return False
            clear()
        pending = None
        pending_after_deadline = 0.0
        header_key = None
        resume()
        return not core.is_cancel_requested()

    @contextmanager
    def preserve_action_deadline():
        try:
            yield
        finally:
            deadline = max(protected_until(), number("isolated_rate_limit_deadline"))
            if deadline > time.time():
                # Observation-time storage handles abrupt exit; this cleanup also
                # suspends the context on orderly exits. No unperformed wait is charged.
                if journal is not None:
                    deadline = max(deadline, journal.cooldown_until)
                try:
                    suspend(deadline, journal.cooldown_reason if journal is not None and journal.cooldown_reason else tr("周辺通信の休止期限を保持"))
                except Exception:
                    core.request_cancel()
                    raise RuntimeError("Unable to retain the action deadline; further actions are stopped.") from None

    scope = getattr(core, "post_action_retry_scope", nullcontext)
    bind_journal = getattr(core, "post_action_journal_scope", lambda _: nullcontext())
    @contextmanager
    def persist_stop():
        try:
            yield
        except BaseException:
            if journal is not None and not journal._retain_lock:
                journal.record_stop("interrupted")
            raise
        else:
            if journal is not None and not journal._retain_lock:
                reason = _state.reason
                if reason == "completed" and len(results) < len(targets):
                    reason = "cancelled"
                journal.record_stop(reason)

    with journal if journal is not None else nullcontext(), bind_journal(journal), scope(), persist_stop(), preserve_action_deadline():
        if journal is not None:
            completed_since_pause = journal.completed_since_pause
            paced = journal.cooldown_until > 0
            if journal.cooldown_until > time.time():
                paced = True
                if not pause(
                    journal.cooldown_until - time.time(),
                    0,
                    policy.max_cooldowns,
                    journal.cooldown_reason or tr("前回の待機を再開"),
                ):
                    return results
                if policy.batch_size and policy.batch_pause_seconds > 0 and completed_since_pause >= policy.batch_size:
                    completed_since_pause = 0
                    journal.reset_batch_count()
        for index, target in enumerate(targets, 1):
            if core.is_cancel_requested():
                _state.reason = "cancelled"
                break
            _state.current, _state.index, _state.current_result = target, index, None
            key = ActionJournal.key(target)
            if journal is not None and known_successes is not None and key in known_successes:
                journal.record(target, "success", count_completion=False)
            previous = seen.get(key)
            if previous is None and journal is not None:
                previous = journal.previous_result(target)
            if previous is not None:
                result = replace(previous, attempts=0)
                emit(
                    tr("記録済みの項目をスキップ: {state}", state="success" if result.success else result.failure_kind)
                )
            else:
                pace = number("rate_limit_pace_until") - time.time()
                # Per-wait mode never sends the next target read before its known reset.
                # Legacy total mode retains its previous optional prediction behavior.
                eligible = policy.wait_limit_mode == "per-wait" or pace <= policy.max_wait_seconds - waited
                if pace > 0 and eligible and core.rate_limit_status is None and policy.max_cooldowns > 0:
                    paced = True
                    if not pause(pace, 0, 0, tr("利用枠の残りが少ないため先回り休止")):
                        break
                if policy.batch_size and policy.batch_pause_seconds > 0 and completed_since_pause >= policy.batch_size:
                    if not pause(policy.batch_pause_seconds, 0, 0, tr("完了件数による定期休止"), proactive=True):
                        break
                    _state.scheduled_pauses += 1
                    completed_since_pause = 0
                    if journal is not None:
                        journal.reset_batch_count()
                retries = 0
                last_failure: PostActionResult | None = None
                while True:
                    if core.is_cancel_requested():
                        if last_failure is not None:
                            results.append(last_failure)
                            on_result(last_failure)
                        return results
                    pump()
                    if core.rate_limit_status is not None or pending is not None:
                        if not cool_down():
                            if last_failure is not None:
                                results.append(last_failure)
                                on_result(last_failure)
                            return results
                        continue  # Check callbacks from re-enabling networking before navigating.
                    on_progress(target, index, len(targets))
                    if journal is not None:
                        journal.record(target, "inflight")
                    # A receipt from an earlier attempt cannot describe this execution.
                    _state.current_result = None
                    _state.current_attempt = retries + 1
                    _state.executing = True
                    result = replace(core.execute_post_action(target), attempts=retries + 1)
                    _state.executing = False
                    _state.current_result = result
                    paced = paced or bool(getattr(core, "isolated_rate_limit_seen", False))
                    if result.deferred:
                        _state.deferrals += 1
                        if result.view_reuse_invalid:
                            _state.reason = "view_changed"
                        # The action positively reports no irreversible click/request.
                        # It consumes cooldowns, never the deletion failure counter.
                        pending = result
                        pending_after_deadline = time.time() + (result.retry_after_seconds or 0.0)
                        if journal is not None:
                            journal.record(target, "retryable" if last_failure is not None else "pending")
                        emit(
                            result.error_message
                            or tr("利用制限を検知したため送信せず保留しました。削除失敗には数えません。")
                        )
                        continue
                    last_failure = None if result.success else result
                    if retries > 0:
                        _state.retries += 1
                    if journal is not None:
                        journal.record(target, "success" if result.success else result.failure_kind)
                    if (
                        not result.success
                        and result.failure_kind == "retryable"
                        and result.rate_limit_scope == "mutation"
                    ):
                        pending = result  # Includes a proven GraphQL code88 refusal on HTTP200.
                        pending_after_deadline = time.time() + (result.retry_after_seconds or 0.0)
                    if core.rate_limit_status is not None or pending is not None:
                        preserve_limit(cooldowns + 1)
                    if result.success or result.failure_kind != "retryable" or core.is_cancel_requested():
                        break
                    if retries >= policy.max_retries:
                        _state.reason = "retry_limit"
                        emit(tr("再試行上限に達しました。処理を停止します。"))
                        break
                    retries += 1
                    if core.rate_limit_status is None and pending is None and policy.delay(retries, result.retry_after_seconds) <= 0:
                        _state.reason = "retry_limit"
                        emit(tr("再試行の待機が無効で、サーバー指定もないため停止します。自動再送しません。"))
                        break
                    if (
                        core.rate_limit_status is None
                        and pending is None
                        and not pause(
                            policy.delay(retries, result.retry_after_seconds),
                            retries,
                            policy.max_retries,
                            result.action_label,
                        )
                    ):
                        break
            if result.success and result.attempts > 0:
                completed_since_pause += 1
            results.append(result)
            seen[key] = result
            on_result(result)
            if not result.success:
                if _state.reason == "completed":
                    _state.reason = "held_receipt" if result.attempts == 0 else result.failure_kind if result.failure_kind in {"auth", "ambiguous", "permanent", "blocked"} else "retry_limit"
                emit(result.error_message or result.failure_kind)
                core.request_cancel()
                break
            item_interval = interval_seconds + (random.uniform(0, policy.interval_jitter_seconds)
                                                if policy.interval_jitter_seconds > 0 and index < len(targets) else 0.0)
            effective_interval = max(item_interval, policy.rate_limit_interval_seconds) if paced else item_interval
            if (
                core.rate_limit_status is None
                and not core.is_cancel_requested()
                and index < len(targets)
                and effective_interval > 0
                and wait(effective_interval)
            ):
                _state.reason = "cancelled"
                core.request_cancel()
                break
        if core.rate_limit_status is not None or pending is not None:
            preserve_limit(cooldowns + 1)
    return results


def run_post_actions(
    core: ActionCore, targets: Sequence[PostActionTarget], interval_seconds: float, policy: RetryPolicy,
    *, journal: ActionJournal | None = None,
    on_progress: Callable[[PostActionTarget, int, int], None] = lambda *_: None,
    on_status: Callable[[str], None] = lambda _: None,
    on_result: Callable[[PostActionResult], None] = lambda _: None,
    wait_for_cancel: Callable[[float], bool] | None = None,
    known_successes: set[str] | None = None,
) -> list[PostActionResult]:
    policy.validate()
    state = _BatchState()

    def summary() -> dict[str, object]:
        failed = sum(not result.success for result in state.results)
        skipped = sum(result.success and result.attempts == 0 for result in state.results)
        held = sum(not result.success and result.attempts == 0 for result in state.results)
        return {"total": len(targets), "successes": len(state.results) - failed, "skipped": skipped,
                "failed": failed, "unprocessed": len(targets) - len(state.results), "deferrals": state.deferrals,
                "retries": state.retries, "cooldowns": state.cooldowns, "waited_seconds": round(state.waited, 3),
                "stop_reason": state.reason, "held_receipts": held}

    try:
        return _run_post_actions(core, targets, interval_seconds, policy, journal=journal, on_progress=on_progress,
                                 on_status=on_status, on_result=on_result, wait_for_cancel=wait_for_cancel,
                                 known_successes=known_successes, _state=state)
    except Exception as error:
        # The raised error stays generic; the local log keeps the cause (stale lock, wrong account, ...).
        logger.error("Action batch stopped by %s: %s", type(error).__name__, error)
        state.reason = "storage_error" if isinstance(error, (OSError, ValueError)) or (journal is not None and journal._retain_lock) else "unexpected_error"
        core.request_cancel()
        if isinstance(error, ActionNotSentError) and state.executing:
            # Nothing was sent: keep the item resumable instead of holding it as ambiguous.
            state.executing = False
            if journal is not None and state.current is not None:
                with suppress(Exception):
                    journal.record(state.current, "pending")
        if state.current is not None and len(state.results) < state.index:
            result = state.current_result
            if state.executing:
                result = PostActionResult(state.current, "unknown", False,
                                          tr("結果不明のため再試行しません。X で確認してください。"), "ambiguous",
                                          attempts=state.current_attempt)
                if state.current_attempt > 1:
                    state.retries += 1
            if result is not None and not result.deferred:
                state.results.append(result)
                with suppress(Exception):
                    on_result(result)
        raise ActionBatchError(state.results, summary()) from None
    finally:
        if state.reason == "completed" and len(state.results) < len(targets):
            state.reason = "cancelled"
        report = summary()
        reason_labels = {"completed": tr("完了"), "cancelled": tr("中止"), "cooldown_limit": tr("通信休止の上限"),
                         "wait_limit": tr("総待機の上限"), "single_wait_limit": tr("1回の待機上限"), "retry_limit": tr("安全な再試行の上限"), "auth": tr("認証・アカウント制限"),
                         "ambiguous": tr("結果不明"), "permanent": tr("恒久エラー"), "unexpected_error": tr("予期しないエラー"),
                         "storage_error": tr("実行記録の保存エラー"), "view_changed": tr("対象表示の変化"),
                         "blocked": tr("通信拒否（認証原因は未確認）"), "held_receipt": tr("保存済みの保留記録（今回の通信結果ではありません）")}
        try:
            callback = getattr(core, "record_action_batch_summary", None)
            if callback is not None:
                callback(report)
            on_status(tr("バッチ終了: 成功{successes}、未処理{unprocessed}、失敗{failed}。理由: {reason}。通信休止{cooldowns}回／{seconds:.1f}秒、定期休止{pauses}回。",
                         successes=report["successes"], unprocessed=report["unprocessed"], failed=report["failed"],
                         reason=reason_labels[state.reason], cooldowns=report["cooldowns"], seconds=report["waited_seconds"],
                         pauses=state.scheduled_pauses))
        except Exception:
            pass
