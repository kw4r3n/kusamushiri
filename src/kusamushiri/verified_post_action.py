"""One post on one owned live session; DOM identity, no auth-state access.

Used only within the existing journalled GUI/CLI action path. This does not grant
permission to execute an action, prove minimal traffic, or replace action receipts.
"""

from collections.abc import Callable
from typing import Any, NoReturn
from urllib.parse import urlsplit

from kusamushiri.actions import (
    CONFIRMATION_SHEET_CONFIRM_SELECTOR,
    _own_control,
    _target_article,
    delete_menu_items,
)
from kusamushiri.active_target_session import ActiveTargetSession, TargetReadStopError
from kusamushiri.models import PostActionTarget
from kusamushiri.target_read_probe import TargetReadRequest, probe_current_target
from kusamushiri.view_cache import OVERLAYS, PendingView


class VerifiedSinglePost:
    def __init__(
        self, core: Any, target: PostActionTarget, expected_viewer: str, *,
        current_view_only: bool, worker_thread_id: int | None, is_current: Callable[[], bool],
    ):
        self.core, self.target = core, target
        self.request = TargetReadRequest(target.url, expected_viewer)
        self.request.validate()
        if (
            target.kind != "post" or type(current_view_only) is not bool
            or urlsplit(target.url).path.split("/")[1].casefold() != expected_viewer.casefold()
            or core._browser.post_actions_active or getattr(core, "_verified_post_action", None) is not None
        ):
            raise TargetReadStopError("single_post_identity_unestablished")
        self.current_view_only = current_view_only
        self.session = ActiveTargetSession(core, target.url, worker_thread_id, is_current, _action_owner=self)
        self.session.check(target_required=False)
        self.frame = self.session.page.main_frame
        self.article: Any = None
        self.loaded = False
        self.used = False
        self.control_stage = 0
        self.menu: Any = None

    def check(self, *, target_required: bool) -> None:
        self.session.check(target_required=target_required)
        if self.session.page.main_frame is not self.frame or self.frame.page is not self.session.page:
            raise TargetReadStopError("session_changed")

    def viewer_matches(self) -> bool:
        self.check(target_required=False)
        logged_in = self.core.is_logged_in()
        self.check(target_required=False)
        viewer = self.core.get_logged_in_username()
        self.check(target_required=False)
        return logged_in is True and type(viewer) is str and viewer.casefold() == self.request.expected_viewer.casefold()

    def prepare(self) -> None:
        if not self.viewer_matches():
            raise TargetReadStopError("account_unmatched")
        if self.current_view_only:
            self.check(target_required=True)
            self.loaded = True
            if not self.can_submit():
                raise TargetReadStopError("single_post_identity_unestablished")
            locator = _target_article(self.session.page, self.target.url)
            pending = PendingView.capture(self.session.page, self.target.url, locator)
            self.check(target_required=True)
            if pending is None or not self.same_article(pending.article):
                raise TargetReadStopError("target_unmatched")
            self.core._pending_view = pending

    def same_article(self, current: Any) -> bool:
        if current is None:
            return False
        if self.article is None:
            self.article = current
        return self.session.page.evaluate(
            "refs => refs.old === refs.current && refs.old.isConnected",
            {"old": self.article, "current": current},
        ) is True

    def begin(self, target: PostActionTarget) -> bool:
        if self.used or target != self.target:
            self.core.request_cancel()
            return False
        self.used = True
        return True

    def target_loaded(self, locator: Any) -> None:
        self.loaded = True
        if self.article is None:
            self.article = locator.element_handle()

    def can_submit(self) -> bool:
        try:
            self.check(target_required=self.loaded)
            if not self.loaded:
                return self.viewer_matches()
            result = probe_current_target(
                self.core, self.request, worker_thread_id=self.session.worker_thread_id, _session=self.session,
            )
            if not result.matched:
                raise TargetReadStopError("single_post_identity_unestablished")
            locator = _target_article(self.session.page, self.target.url)
            matched = self.same_article(locator.element_handle())
            self.check(target_required=True)
            if not matched:
                raise TargetReadStopError("target_unmatched")
            return True
        except Exception:
            self.core.request_cancel()
            return False

    def prepare_control(self, locator: Any, stage: str) -> Callable[[], None]:
        """Pin the selected control and its visible dialog ancestor chain."""
        reported = False

        def reject(reason: str) -> NoReturn:
            nonlocal reported
            if not reported:
                reported = True
                self.core._browser.diagnostics.verification_stop(stage, reason)
            raise TargetReadStopError("control_unmatched")

        def prepare() -> Callable[[], None]:
            page = self.session.page
            stages = ("caret", "delete_menu", "confirm")
            if self.control_stage >= len(stages) or stage != stages[self.control_stage]:
                reject("wrong_control_stage")
            if not self.can_submit():
                reject("identity_unestablished")
            element = locator.element_handle()
            if element is None:
                reject("control_changed")
            dropdown = '[data-testid="Dropdown"]:visible'
            dialogs = '[role="dialog"]:visible, [role="alertdialog"]:visible'
            if stage == "caret":
                if page.locator(OVERLAYS).count() != 0:
                    reject("overlay_present")
                selected = _target_article(page, self.target.url).locator(_own_control("caret"))
                root = self.article
            elif stage == "delete_menu":
                if page.locator(dropdown).count() != 1:
                    reject("menu_count")
                self.menu = page.locator(dropdown).element_handle()
                selected, root = delete_menu_items(page), self.menu
            else:
                selected = page.locator(CONFIRMATION_SHEET_CONFIRM_SELECTOR + ":visible")
                root = element
            # Dialog wrappers may be nested. Every visible dialog must belong to
            # this control's ancestor chain, and the exact chain must survive
            # until the pinned ElementHandle is clicked.
            pinned_dialogs = page.locator(dialogs).element_handles()

            def validate_control() -> None:
                if selected.count() != 1:
                    reject("control_count")
                visible_menu = page.locator(dropdown)
                if stage == "caret" and page.locator(OVERLAYS).count() != 0:
                    reject("overlay_present")
                if stage == "delete_menu" and visible_menu.count() != 1:
                    reject("menu_count")
                if stage == "confirm" and visible_menu.count() > 1:
                    reject("menu_count")
                if stage == "confirm" and visible_menu.count() and not page.evaluate(
                    "refs => refs.old === refs.current", {"old": self.menu, "current": visible_menu.element_handle()},
                ):
                    reject("menu_changed")
                current_dialogs = page.locator(dialogs).element_handles()
                if not page.evaluate(
                    """refs => refs.old.length === refs.current.length &&
                        refs.old.every((n, i) => n === refs.current[i] && n.isConnected &&
                            n.contains(refs.owner))""",
                    {"old": pinned_dialogs, "current": current_dialogs,
                     "owner": root if stage == "delete_menu" else element},
                ):
                    reject("dialog_ownership_changed")
                if not page.evaluate(
                    """refs => {
                        const visible = n => n && n.isConnected && n.getClientRects().length > 0 &&
                            getComputedStyle(n).visibility !== 'hidden';
                        if (!visible(refs.control) || !visible(refs.root) || refs.control !== refs.current ||
                            !refs.root.contains(refs.control)) return false;
                        if (refs.stage === 'caret') return refs.control.closest('article') === refs.root &&
                            !refs.control.closest('blockquote,[data-testid="quoteTweet"],[data-testid="tweetText"]');
                        if (refs.stage === 'delete_menu') return refs.control.closest('[data-testid="Dropdown"]') === refs.root;
                        return true;
                    }""",
                    {"control": element, "current": selected.element_handle(), "root": root, "stage": stage},
                ):
                    reject("control_changed")

            validate_control()
            used = False

            def click_once() -> None:
                nonlocal used
                try:
                    if used:
                        reject("control_reused")
                    if not self.can_submit():
                        reject("identity_unestablished")
                    validate_control()
                    used = True
                    self.control_stage += 1
                    element.click()
                except Exception:
                    if not reported:
                        reject("read_failed")
                    raise

            return click_once

        try:
            return prepare()
        except Exception:
            if not reported:
                reject("read_failed")
            raise
