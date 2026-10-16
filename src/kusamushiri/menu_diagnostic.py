"""Open the owned caret once and read menu metadata; never run a post action."""

import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from kusamushiri.actions import (
    DELETE_MENU_ITEM_SELECTORS,
    _own_control,
    _target_article,
    delete_menu_item_locators,
    delete_menu_items,
)
from kusamushiri.active_target_session import ActiveTargetSession, TargetReadStopError
from kusamushiri.models import PostActionTarget
from kusamushiri.target_read_probe import REASONS as READ_REASONS
from kusamushiri.target_read_probe import TargetReadRequest, probe_current_target
from kusamushiri.view_cache import OVERLAYS

REASONS = (READ_REASONS - {"matched"}) | {"captured", "control_unmatched", "identity_unestablished", "capacity_limit"}
STAGES = {"preflight", "caret", "menu", "capture", "complete"}
COUNTS = {"dropdown_total", "dropdown_visible", "menuitem_total", "union_visible", "dialog_visible"}
ROW_BOOLS = {"connected", "visible", "delete_testid", "inside_single_visible_dropdown", "aria_hidden_ancestor", "hidden_ancestor"}
ROW_COUNTS = {"dialog_ancestors", "dialog_descendants"}
ROW_ENUMS = {"tag": {"DIV", "BUTTON", "A", "SPAN", "OTHER"},
             "parent_role": {"menuitem", "menu", "button", "dialog", "alertdialog", "group", "OTHER"},
             "display": {"none", "contents", "OTHER"}, "visibility": {"visible", "hidden", "collapse", "OTHER"}}


def _integer(value: Any, minimum: int = 0, maximum: int = 10_000) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _snapshot_valid(value: Any) -> bool:
    if type(value) is not dict or set(value) != COUNTS | {"selector_counts", "truncated", "stable", "rows"}:
        return False
    if any(not _integer(value[k]) for k in COUNTS) or any(type(value[k]) is not bool for k in ("truncated", "stable")):
        return False
    if type(value["selector_counts"]) is not list or len(value["selector_counts"]) != len(DELETE_MENU_ITEM_SELECTORS):
        return False
    if any(not _integer(n) for n in value["selector_counts"]):
        return False
    rows = value["rows"]
    if type(rows) is not list or len(rows) > 48:
        return False
    keys = ROW_BOOLS | ROW_COUNTS | set(ROW_ENUMS) | {"dropdown_index", "selector_matches", "candidate_index", "contains_candidates"}
    for row in rows:
        if type(row) is not dict or set(row) != keys:
            return False
        if any(type(row[k]) is not bool for k in ROW_BOOLS) or any(not _integer(row[k], maximum=16) for k in ROW_COUNTS):
            return False
        if any(type(row[k]) is not str or row[k] not in allowed for k, allowed in ROW_ENUMS.items()):
            return False
        if not _integer(row["dropdown_index"], -1, 15) or not _integer(row["candidate_index"], -1, 15):
            return False
        if type(row["selector_matches"]) is not list or len(row["selector_matches"]) != 4 or any(type(v) is not bool for v in row["selector_matches"]):
            return False
        if type(row["contains_candidates"]) is not list or len(row["contains_candidates"]) > 16 or any(not _integer(v, maximum=15) for v in row["contains_candidates"]):
            return False
    return True


@dataclass(frozen=True, slots=True)
class MenuDiagnosticResult:
    reason: str
    stage: str = "preflight"
    caret_click_attempted: bool = False
    snapshot: dict | None = None
    inspection_mode: str = "caret_only"

    def safe_fields(self) -> dict:
        if (type(self.reason) is not str or self.reason not in REASONS
                or type(self.stage) is not str or self.stage not in STAGES
                or type(self.caret_click_attempted) is not bool
                or type(self.inspection_mode) is not str or self.inspection_mode not in {"caret_only", "existing_overlay_read_only"}
                or self.inspection_mode == "existing_overlay_read_only" and self.caret_click_attempted
                or (self.reason == "captured") != (self.snapshot is not None)
                or self.snapshot is not None and (not _snapshot_valid(self.snapshot) or len(json.dumps(self.snapshot)) > 32768)
                or self.reason == "captured" and (self.stage != "complete"
                    or self.inspection_mode == "caret_only" and not self.caret_click_attempted)):
            raise ValueError("invalid_menu_diagnostic")
        return {"reason": self.reason, "stage": self.stage, "caret_click_attempted": self.caret_click_attempted,
                "delete_requested": False, "journal_used": False, "inspection_mode": self.inspection_mode, "snapshot": self.snapshot}


class _MenuReadScope:
    """Diagnostic-only ownership checks; never an action or cached-action view."""

    def __init__(self, core: Any, request: TargetReadRequest, worker_thread_id: int | None,
                 is_current: Callable[[], bool]):
        self.core, self.request, self.is_current = core, request, is_current
        if urlsplit(request.url).path.split("/")[1].casefold() != request.expected_viewer.casefold():
            raise TargetReadStopError("account_unmatched")
        if getattr(core, "_verified_post_action", None) is not None:
            raise TargetReadStopError("session_changed")
        self.session = ActiveTargetSession(core, request.url, worker_thread_id, is_current)
        self.session.check(target_required=False)
        self.session.page.wait_for_timeout(0)
        self.session.check(target_required=True)
        self.frame = self.session.page.main_frame
        self.article = None
        self.check(target_required=True)

    def check(self, *, target_required: bool) -> None:
        self.session.check(target_required=target_required)
        page = self.session.page
        if (page.main_frame is not self.frame or self.frame.page is not page
                or getattr(self.core, "_verified_post_action", None) is not None):
            raise TargetReadStopError("session_changed")

    def verify(self) -> None:
        self.check(target_required=False)
        result = probe_current_target(self.core, self.request, worker_thread_id=self.session.worker_thread_id,
                                      _session=self.session)
        self.check(target_required=True)
        if not result.matched:
            raise TargetReadStopError(result.reason)
        current = _target_article(self.session.page, self.request.url).element_handle()
        self.check(target_required=True)
        if current is None:
            raise TargetReadStopError("target_unmatched")
        if self.article is None:
            self.article = current
        same = self.session.page.evaluate("refs => refs.old === refs.current && refs.old.isConnected",
                                          {"old": self.article, "current": current})
        self.check(target_required=True)
        if same is not True:
            raise TargetReadStopError("target_unmatched")

    def has_overlay(self) -> bool:
        self.check(target_required=True)
        count = self.session.page.locator(OVERLAYS + ', [role="alertdialog"]:visible').count()
        self.check(target_required=True)
        return count > 0

    def action_current(self) -> bool:
        # Keep the original diagnostic budget if the closed-menu branch opens a
        # separate, unchanged action-ownership scope. No cancel or rate reset.
        self.session.now()
        return self.is_current()


def _snapshot(scope: Any) -> dict:
    page = scope.session.page

    def read(call):
        scope.check(target_required=True)
        value = call()
        scope.check(target_required=True)
        return value

    def count(locator):
        value = read(locator.count)
        if not _integer(value):
            raise TargetReadStopError("capacity_limit")
        return value

    def handles(locator, total, limit):
        return [read(locator.nth(i).element_handle) for i in range(min(total, limit))]

    roots = page.locator('[data-testid="Dropdown"]')
    visible_roots = page.locator('[data-testid="Dropdown"]:visible')
    items = page.locator('[data-testid="Dropdown"] [role="menuitem"]')
    union = delete_menu_items(page)
    dialogs = page.locator('[role="dialog"]:visible, [role="alertdialog"]:visible')
    totals = {"dropdown_total": count(roots), "dropdown_visible": count(visible_roots), "menuitem_total": count(items),
              "union_visible": count(union), "dialog_visible": count(dialogs)}
    selectors = delete_menu_item_locators(page)
    selector_counts = [count(locator) for locator in selectors]
    root_handles = handles(roots, totals["dropdown_total"], 16)
    item_handles = handles(items, totals["menuitem_total"], 32)
    candidates = handles(union, totals["union_visible"], 16)
    dialog_handles = handles(dialogs, totals["dialog_visible"], 16)
    by_selector = [handles(locator, total, 32) for locator, total in zip(selectors, selector_counts, strict=True)]
    owner = read(visible_roots.element_handle) if totals["dropdown_visible"] == 1 else None
    sampled = candidates + item_handles
    visibility = [read(handle.is_visible) if handle is not None else False for handle in sampled]
    rows = read(lambda: page.evaluate("""refs => {
        const unique = [...new Set(refs.sampled)].filter(Boolean);
        const pick = (value, allowed) => allowed.includes(value) ? value : 'OTHER';
        return unique.map(node => {
            const style = getComputedStyle(node);
            const parent = node.parentElement && node.parentElement.closest('[role]');
            return {
                connected: node.isConnected, visible: refs.visibility[refs.sampled.indexOf(node)],
                aria_hidden_ancestor: node.closest('[aria-hidden="true"]') !== null,
                hidden_ancestor: node.closest('[hidden]') !== null,
                delete_testid: node.getAttribute('data-testid') === 'delete',
                inside_single_visible_dropdown: refs.owner !== null && node.closest('[data-testid="Dropdown"]') === refs.owner,
                dropdown_index: refs.roots.indexOf(node.closest('[data-testid="Dropdown"]')),
                candidate_index: refs.candidates.indexOf(node),
                selector_matches: refs.selectors.map(nodes => nodes.includes(node)),
                contains_candidates: refs.candidates.flatMap((child, i) => child !== node && node.contains(child) ? [i] : []),
                tag: pick(node.tagName, ['DIV','BUTTON','A','SPAN']),
                parent_role: pick(parent && parent.getAttribute('role'), ['menuitem','menu','button','dialog','alertdialog','group']),
                display: pick(style.display, ['none','contents']),
                visibility: pick(style.visibility, ['visible','hidden','collapse']),
                dialog_ancestors: refs.dialogs.filter(dialog => dialog.contains(node)).length,
                dialog_descendants: refs.dialogs.filter(dialog => node.contains(dialog)).length,
            };
        });
    }""", {"sampled": sampled, "visibility": visibility, "roots": root_handles, "candidates": candidates,
            "dialogs": dialog_handles, "selectors": by_selector, "owner": owner}))
    end = {"dropdown_total": count(roots), "dropdown_visible": count(visible_roots), "menuitem_total": count(items),
           "union_visible": count(union), "dialog_visible": count(dialogs)}
    end_selectors = [count(locator) for locator in selectors]
    previous = [root_handles, item_handles, candidates, dialog_handles, *by_selector]
    current = [handles(roots, end["dropdown_total"], 16), handles(items, end["menuitem_total"], 32),
               handles(union, end["union_visible"], 16), handles(dialogs, end["dialog_visible"], 16),
               *[handles(locator, total, 32) for locator, total in zip(selectors, end_selectors, strict=True)]]
    stable = end == totals and end_selectors == selector_counts and read(lambda: page.evaluate(
        "refs => refs.old.every((nodes,j) => nodes.length === refs.current[j].length && nodes.every((n,i) => n && n.isConnected && n === refs.current[j][i]))",
        {"old": previous, "current": current},
    )) is True
    return {**totals, "selector_counts": selector_counts,
            "truncated": totals["dropdown_total"] > 16 or totals["menuitem_total"] > 32 or totals["union_visible"] > 16
                         or totals["dialog_visible"] > 16 or any(n > 32 for n in selector_counts),
            "stable": stable, "rows": rows}


def diagnose_post_menu(core: Any, request: TargetReadRequest, *, worker_thread_id: int | None,
                       is_current: Callable[[], bool] = lambda: True) -> MenuDiagnosticResult:
    stage, attempted, mode = "preflight", False, "caret_only"
    try:
        if type(request) is not TargetReadRequest:
            return MenuDiagnosticResult("invalid_request")
        try:
            request.validate()
        except ValueError:
            return MenuDiagnosticResult("invalid_request")
        inspection = _MenuReadScope(core, request, worker_thread_id, is_current)
        if inspection.has_overlay():
            mode = "existing_overlay_read_only"
            inspection.verify()
            stage = "capture"
            snapshot = _snapshot(inspection)
            inspection.verify()
            result = MenuDiagnosticResult("captured", "complete", False, snapshot, mode)
            result.safe_fields()
            return result
        # The closed-menu click still uses the unchanged action-ownership scope.
        # The passive read above grants no permission to click or delete.
        with core.verified_single_post_scope(PostActionTarget(request.url, "post"), request.expected_viewer,
                                             current_view_only=True, worker_thread_id=worker_thread_id, is_current=inspection.action_current):
            # Core's context manager yields no value; use its exact current scope.
            scope = core._verified_post_action
            inspection.session.check(target_required=True)
            if scope.session.page is not inspection.session.page or scope.frame is not inspection.frame:
                raise TargetReadStopError("session_changed")
            stage = "caret"
            caret = _target_article(scope.session.page, request.url).locator(_own_control("caret"))
            click_caret = scope.prepare_control(caret, "caret")
            attempted = True
            click_caret()  # The only click in this module. No menu-item or confirmation path.
            stage = "menu"
            scope.check(target_required=True)
            # Zero is a useful diagnostic count; never retry the click.
            with suppress(PlaywrightTimeoutError):
                delete_menu_items(scope.session.page).first.wait_for(state="visible", timeout=10_000)
            scope.check(target_required=True)
            if not scope.can_submit():
                raise TargetReadStopError("identity_unestablished")
            stage = "capture"
            snapshot = _snapshot(scope)
            scope.check(target_required=True)
            if not scope.can_submit():
                raise TargetReadStopError("identity_unestablished")
            result = MenuDiagnosticResult("captured", "complete", attempted, snapshot)
            result.safe_fields()
            return result
    except TargetReadStopError as error:
        reason = str(error) if str(error) in REASONS else "read_failed"
    except Exception:
        reason = "read_failed"
    return MenuDiagnosticResult(reason, stage, attempted, inspection_mode=mode)
