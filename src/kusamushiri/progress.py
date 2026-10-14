"""Crash-safe action receipts. Uncertain submissions require manual verification."""

import contextlib
import json
import math
import os
import tempfile
from pathlib import Path
from types import TracebackType
from typing import cast

from kusamushiri.i18n import tr
from kusamushiri.models import FailureKind, PostActionResult, PostActionTarget
from kusamushiri.parsing import extract_post_id

STATES = {"inflight", "success", "retryable", "permanent", "auth", "ambiguous", "pending", "blocked"}


class ActionJournal:
    def __init__(self, path: Path, scope: str) -> None:
        self.path = path
        self.scope = scope
        self.entries: dict[str, str] = {}
        self.cooldown_until = 0.0
        self.cooldown_reason = ""
        self.completed_since_pause = 0
        self._lock_path = path.with_name(path.name + ".lock")
        self._retain_lock = False

    @staticmethod
    def key(target: PostActionTarget) -> str:
        post_id = extract_post_id(target.url)
        if not post_id.isascii() or not post_id.isdecimal() or target.kind not in {"post", "repost", "like"}:
            raise ValueError("Cannot record an action without a numeric post ID and known kind")
        return f"{target.kind}:{post_id}"

    def __enter__(self) -> "ActionJournal":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RuntimeError(
                "Action journal is locked; another job may be using it. No action was started."
            ) from error
        os.close(fd)
        try:
            self._load()
        except BaseException:
            self._lock_path.unlink()
            raise
        return self

    def __exit__(self, typ: type[BaseException] | None, value: BaseException | None, tb: TracebackType | None) -> None:
        if not self._retain_lock:
            self._lock_path.unlink()

    def _sync_directory(self) -> None:
        if os.name == "posix":
            fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def arm_isolation_storage(self) -> None:
        """Make the already-held lock durable before allowing scoped continuation."""
        try:
            fd = os.open(self._lock_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self._sync_directory()
        except BaseException:
            self._retain_lock = True
            raise

    def persist_isolated_deadline(self, deadline: float) -> None:
        try:
            self.defer_until(deadline, tr("周辺通信の休止期限を保持"))
        except BaseException:
            # The durable pre-existing lock blocks restart even if a new deadline
            # cannot be saved. It requires manual review, never automatic unlink.
            self._retain_lock = True
            raise

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("scope") != self.scope:
            raise ValueError("Action journal version/profile mismatch; no action was started")
        entries = data.get("entries")
        deadline = data.get("cooldown_until", 0)
        if (
            not isinstance(entries, dict)
            or not all(isinstance(k, str) and isinstance(v, str) and v in STATES for k, v in entries.items())
            or isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
            or deadline < 0
        ):
            raise ValueError("Invalid action journal; no action was started")
        completed = data.get("completed_since_pause", 0)
        if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
            raise ValueError("Invalid completion counter; no action was started")
        self.completed_since_pause = completed
        self.entries = entries
        reason = data.get("cooldown_reason", "")
        if not isinstance(reason, str) or len(reason) > 512:
            raise ValueError("Invalid cooldown reason; no action was started")
        self.cooldown_reason = reason
        self.cooldown_until = float(deadline)

    def previous_result(self, target: PostActionTarget) -> PostActionResult | None:
        from kusamushiri.actions import action_label_for

        state = self.entries.get(self.key(target))
        if state is None or state in {"retryable", "pending"}:
            return None
        if state == "success":
            return PostActionResult(target, action_label_for(target.kind), True, None, attempts=0)
        kind = "ambiguous" if state == "inflight" else state
        return PostActionResult(
            target,
            action_label_for(target.kind),
            False,
            tr("記録された {state} のため再実行しません。X と実行記録を確認してください。", state=kind),
            failure_kind=cast(FailureKind, kind),
            attempts=0,
        )

    def record(self, target: PostActionTarget, state: str, *, count_completion: bool = True) -> None:
        if state not in STATES:
            raise ValueError("Invalid action state")
        if count_completion and state == "success" and self.entries.get(self.key(target)) != "success":
            self.completed_since_pause += 1
        self.entries[self.key(target)] = state
        self._write()

    def reset_batch_count(self) -> None:
        self.completed_since_pause = 0
        self._write()

    def defer_until(self, deadline: float, reason: str = "") -> None:
        if reason:
            self.cooldown_reason = reason
        elif not deadline:
            self.cooldown_reason = ""
        self.cooldown_until = max(self.cooldown_until, deadline) if deadline else 0.0
        self._write()

    def _write(self) -> None:
        payload = {
            "version": 1,
            "scope": self.scope,
            "entries": self.entries,
            "cooldown_until": self.cooldown_until,
            "cooldown_reason": self.cooldown_reason,
            "completed_since_pause": self.completed_since_pause,
        }
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            self._sync_directory()
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
