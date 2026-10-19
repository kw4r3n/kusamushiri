import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Locator, Page, Request, Response
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from kusamushiri.browser import BASE_X_URL, NAVIGATION_TIMEOUT_MS, find_first_visible_locator, is_rate_limit_response
from kusamushiri.http_failures import ACCOUNT_CODES, KNOWN_CODES, HttpFailure, http_failure
from kusamushiri.i18n import tr
from kusamushiri.logger import logger
from kusamushiri.models import FailureKind, PostActionResult, PostActionTarget, PostKind
from kusamushiri.network_diagnostics import NetworkDiagnostics
from kusamushiri.parsing import USERNAME_PATTERN, extract_post_id
from kusamushiri.rate_limits import classify_rate_limit
from kusamushiri.retry import parse_retry_after
from kusamushiri.view_cache import PendingView

ACTION_STATE_TIMEOUT_MS = 10_000
UNREPOST_CONFIRM_SELECTORS = (
    '[data-testid="unretweetConfirm"]',
    '[role="menuitem"]:has-text("Undo repost")',
    '[role="menuitem"]:has-text("Undo Retweet")',
    '[role="menuitem"]:has-text("リポストを取り消す")',
    '[role="menuitem"]:has-text("リツイートを取り消す")',
    'button:has-text("Undo repost")',
    'button:has-text("Undo Retweet")',
    'button:has-text("リポストを取り消す")',
    'button:has-text("リツイートを取り消す")',
)
DELETE_MENU_ITEM_SELECTORS = (
    '[data-testid="Dropdown"] [role="menuitem"][data-testid="delete"]:visible',
    '[data-testid="Dropdown"] [role="menuitem"]:has-text("Delete"):visible',
    # 「リストに追加/削除」「리스트에 추가/삭제」も同じ語を含むため、リスト項目は除外する。
    '[data-testid="Dropdown"] [role="menuitem"]:has-text("削除"):not(:has-text("リスト")):visible',
    '[data-testid="Dropdown"] [role="menuitem"]:has-text("삭제"):not(:has-text("리스트")):not(:has-text("목록")):visible',
)
# CSS above is only a coarse prefilter. All selected candidates also
# pass this full-item text filter (descendant spans included), except the
# existing explicit delete test id. A substring is never a label fallback.
DELETE_MENU_ITEM_LABELS = (None, "Delete", "削除", "삭제")


def delete_menu_item_locators(page: Page) -> tuple[Locator, ...]:
    return tuple(
        page.locator(selector) if label is None else page.locator(selector).filter(
            has_text=re.compile(r"^\s*" + re.escape(label) + r"\s*$"),
        )
        for selector, label in zip(DELETE_MENU_ITEM_SELECTORS, DELETE_MENU_ITEM_LABELS, strict=True)
    )


def delete_menu_items(page: Page) -> Locator:
    candidates = delete_menu_item_locators(page)
    union = candidates[0]
    for candidate in candidates[1:]:
        union = union.or_(candidate)
    return union


CONFIRMATION_SHEET_CONFIRM_SELECTOR = '[data-testid="confirmationSheetConfirm"]'
DELETE_POST_MUTATION = "DeleteTweet"
UNDO_REPOST_MUTATION = "DeleteRetweet"
UNLIKE_MUTATION = "UnfavoriteTweet"
# Unfollowing is a REST call (POST /i/api/1.1/friendships/destroy.json), not GraphQL.
UNFOLLOW_MUTATION = "friendships/destroy.json"
PROFILE_COLUMN_SELECTOR = '[data-testid="primaryColumn"]'
# The profile header button is "<numeric user id>-unfollow" / "-follow". Recommendation
# cards (UserCell) in the same column carry those test ids for other accounts.
UNFOLLOW_BUTTON_SELECTOR = f'{PROFILE_COLUMN_SELECTOR} [data-testid$="-unfollow"]:not([data-testid="UserCell"] *)'
FOLLOW_BUTTON_SELECTOR = f'{PROFILE_COLUMN_SELECTOR} [data-testid$="-follow"]:not([data-testid="UserCell"] *)'
# Unfollowed once the header shows a visible "-follow" button and no "-unfollow" one.
UNFOLLOW_DONE_SCRIPT = """({follow, unfollow}) => {
    if (document.querySelector(unfollow)) return false;
    const button = document.querySelector(follow);
    return button !== null && button.getClientRects().length > 0;
}"""

LocatorFinder = Callable[[Page | Locator, tuple[str, ...]], Locator | None]


# Only descendants owned by a top-level post may identify or operate it.
# An anchor's own role="link" is fine; a containing link is a quote boundary.
OWNED_NODE_XPATH = (
    "count(ancestor::article) = 1 and "
    "not(ancestor::blockquote or ancestor::*[@data-testid='quoteTweet' or "
    "@data-testid='tweetText' or @role='link'])"
)
POST_ARTICLE_SELECTOR = "xpath=//article[@data-testid='tweet'][not(ancestor::article)]"
# The post's own timestamp anchor is its permalink (/<author>/status/<id>).
OWNED_TIMESTAMP_LINK_SELECTOR = f"xpath=.//a[.//time][{OWNED_NODE_XPATH}]"

# A toggle has flipped when the owned "undo" control (e.g. "unretweet") is gone
# and the owned "do" control (e.g. "retweet") is visible.
TOGGLE_OFF_DONE_SCRIPT = """({article, owned, undoId, doId}) => {
    if (!article || !article.isConnected) return false;
    const controls = id => document.evaluate(
        `.//*[@data-testid='${id}'][${owned}]`, article, null,
        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    if (controls(undoId).snapshotLength !== 0) return false;
    const done = controls(doId);
    for (let i = 0; i < done.snapshotLength; i++) {
        const button = done.snapshotItem(i);
        if (button.getClientRects().length &&
            getComputedStyle(button).visibility !== 'hidden') return true;
    }
    return false;
}"""


def action_label_for(kind: PostKind) -> str:
    if kind == "repost":
        return tr("リポスト解除")
    if kind == "like":
        return tr("いいね取り消し")
    return tr("ポスト削除")


def _own_control(test_id: str) -> str:
    return f"xpath=.//*[@data-testid='{test_id}'][{OWNED_NODE_XPATH}]"


def _is_mutation_request(request: Request, mutation_name: str) -> bool:
    try:
        parsed = urlparse(request.url)
        if parsed.scheme != "https" or request.method != "POST" or parsed.hostname not in {"x.com", "www.x.com", "twitter.com"}:
            return False
        if mutation_name == UNFOLLOW_MUTATION:
            return parsed.path.rstrip("/") == "/i/api/1.1/friendships/destroy.json"
        return re.fullmatch(r"/i/api/graphql/[A-Za-z0-9_-]+/" + re.escape(mutation_name), parsed.path.rstrip("/")) is not None
    except Exception:
        return False


def _valid_error_envelope(body: object) -> bool:
    if not isinstance(body, dict):
        return False
    if "errors" not in body:
        return True
    errors = body["errors"]
    return isinstance(errors, list) and all(
        isinstance(item, dict) and isinstance(item.get("code"), (int, str, type(None)))
        for item in errors
    )


def _completed_response(request: Request) -> Response | None:
    # response()/json() can otherwise wait on an unfinished download after cancellation.
    # X must have completed the response before it can count as a durable receipt.
    if isinstance(request, Request) and request.timing.get("responseEnd", -1) < 0:
        return None
    return request.response()


def _mutation_error(requests: list[Request], mutation_name: str) -> str | None:
    """Xの操作APIが明示的に失敗を返した場合、そのエラーメッセージを返す。"""
    for request in requests:
        response = _completed_response(request)
        if response is None:
            return tr("{mutation} の通信に失敗しました: {detail}", mutation=mutation_name, detail=request.failure or tr("応答なし"))
        if response.status >= 400:
            return tr("{mutation} がエラーを返しました (HTTP {status})。", mutation=mutation_name, status=response.status)
        if not 200 <= response.status < 300 or not _is_mutation_request(response.request, mutation_name):
            return tr("{mutation} の応答を確認できませんでした。結果不明です。", mutation=mutation_name)
        try:
            body = response.json()
        except Exception:
            logger.debug("Unreadable %s response; completion is unknown.", mutation_name)
            return tr("{mutation} の応答を確認できませんでした。結果不明です。", mutation=mutation_name)
        if not _valid_error_envelope(body):
            return tr("{mutation} の応答を確認できませんでした。結果不明です。", mutation=mutation_name)
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            first = errors[0] if isinstance(errors, list) else errors
            detail = first.get("message") if isinstance(first, dict) else first
            return tr("{mutation} がエラーを返しました: {detail}", mutation=mutation_name, detail=detail)
    return None


def _wait_for_mutation_result(
    requests: list[Request],
    mutation_name: str,
    wait_for_success: Callable[[], object],
) -> str | None:
    """Return the mutation error, or None once the DOM confirms success.

    Local pages that never send the mutation keep the DOM-only behavior.
    """
    try:
        wait_for_success()
    except PlaywrightTimeoutError:
        error_message = _mutation_error(requests, mutation_name)
        if error_message is not None:
            return error_message
        raise
    return _mutation_error(requests, mutation_name)


def _target_article(page: Page, post_url: str) -> Locator:
    post_id = extract_post_id(post_url)
    parsed = urlparse(post_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc.lower() not in {"x.com", "www.x.com"}
        or not re.fullmatch(r"[0-9]+", post_id)
        or not re.fullmatch(r"/[A-Za-z0-9_]+/status/[0-9]+(?:/[^?#]*)?", parsed.path)
    ):
        raise ValueError(tr("操作対象のポストIDをURLから確認できませんでした。"))

    href_path = "@href"
    for delimiter in ("?", "#"):
        href_path = f"substring-before(concat({href_path}, '{delimiter}'), '{delimiter}')"
    # Parse the supported URL forms as whole path segments. Neither a foreign
    # host nor a query/fragment containing a status URL proves post identity.
    identities = []
    for prefix in ("/", "https://x.com/", "http://x.com/", "https://www.x.com/", "http://www.x.com/"):
        path = f"substring-after({href_path}, '{prefix}')"
        author = f"substring-before({path}, '/')"
        status = f"substring-after({path}, '/')"
        identity = f"substring-before(concat(substring-after({status}, 'status/'), '/'), '/')"
        identities.append(
            f"(starts-with({href_path}, '{prefix}') and string-length({author}) > 0 "
            f"and translate({author}, 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_', '') = '' "
            f"and starts-with({status}, 'status/') and {identity} = '{post_id}')"
        )
    same_identity = " or ".join(identities)
    owned_links = f".//a[.//time][{OWNED_NODE_XPATH}]"
    # Repeated permalinks are fine, conflicting identities are not. Preserve
    # locator strictness so duplicates and later DOM changes cannot pick a peer.
    return page.locator(
        f"xpath=//article[@data-testid='tweet'][not(ancestor::article)]"
        f"[{owned_links}[{same_identity}]][not({owned_links}[not({same_identity})])]"
    )


@dataclass
class ActionEvidence:
    submitted: bool = False
    transient_before_submission: bool = False
    blocked: bool = False
    rate_blocked: bool = False
    rate_limit_pending: Callable[[], bool] = lambda: False
    cancelled: bool = False
    should_cancel: Callable[[], bool] = lambda: False
    diagnostics: NetworkDiagnostics | None = None
    reuse_view: PendingView | None = None
    on_target_loaded: Callable[[Locator], None] = lambda _: None
    view_reuse_invalid: bool = False
    identity_check: Callable[[], bool] = lambda: True
    identity_failed: bool = False
    control_failed: bool = False
    prepare_control: Callable[[Locator, str], Callable[[], None]] = lambda locator, _: locator.click

    def phase(self, phase: str) -> None:
        if self.diagnostics is not None:
            self.diagnostics.phase(phase)

    def can_submit(self) -> bool:
        self.cancelled = self.should_cancel()
        self.rate_blocked = self.rate_blocked or self.rate_limit_pending()
        if not (self.blocked or self.cancelled or self.rate_blocked):
            try:
                self.identity_failed = self.identity_failed or self.identity_check() is not True
            except Exception:
                self.identity_failed = True
            self.blocked = self.blocked or self.identity_failed
        return not (self.blocked or self.cancelled or self.rate_blocked)

    def prepared_click(self, locator: Locator, stage: str, *, mutation: bool = False) -> Callable[[], None]:
        def failed() -> NoReturn:
            self.control_failed = self.blocked = True
            raise TargetPostUnavailableError(tr("操作対象のメニューまたは確認ボタンを一意に照合できないため停止しました。")) from None

        try:
            click = self.prepare_control(locator, stage)
        except Exception:
            failed()

        def invoke() -> None:
            if not self.can_submit():
                raise TargetPostUnavailableError(tr("送信前に中断しました。待機またはログイン確認が必要です。"))
            if mutation:
                # Only a positively admitted invocation can have submitted anything.
                # Errors after entering click() remain uncertain and must stay held.
                self.submitted = True
            try:
                click()
            except Exception:
                failed()

        return invoke


class TargetPostUnavailableError(Exception):
    """The post page never showed the target post; the message says why, for the user."""


class CachedTargetChangedError(TargetPostUnavailableError):
    """A positively unsent target can no longer be revalidated without a reload."""


def _open_target_article(page: Page, post_url: str, *, diagnostics: NetworkDiagnostics | None = None,
                         reuse_view: PendingView | None = None) -> Locator:
    """Open the post page and wait for its own article."""
    article = _target_article(page, post_url)
    if reuse_view is not None:
        if not reuse_view.validate(page, post_url, article):
            raise CachedTargetChangedError(tr("保存した対象表示を確認できないため停止します。再読込や再送はしません。"))
        if diagnostics is not None:
            diagnostics.view_reused()
        return article
    rate_limited: list[Response] = []

    def record_rate_limit(response: Response) -> None:
        if is_rate_limit_response(response.url, response.status):
            rate_limited.append(response)

    page.on("response", record_rate_limit)
    try:
        if diagnostics is not None:
            diagnostics.navigation()
        navigation = page.goto(post_url, timeout=NAVIGATION_TIMEOUT_MS)
        if navigation is not None and navigation.status in {401, 403, 404, 410}:
            raise TargetPostUnavailableError(f"HTTP {navigation.status}: target unavailable")
        article.wait_for(state="visible", timeout=NAVIGATION_TIMEOUT_MS)
    except PlaywrightTimeoutError as error:
        # X stops showing posts after many actions in a short time; say so instead of dumping the locator.
        if rate_limited:
            raise TargetPostUnavailableError(
                tr(
                    "X の利用制限 (HTTP {status}) でポストを表示できませんでした。しばらく待ってから再試行してください。",
                    status=rate_limited[0].status,
                )
            ) from error
        raise TargetPostUnavailableError(
            tr("ポストが表示されませんでした。削除済み・非公開か、X の利用制限の可能性があります。時間をおいて再試行してください。")
        ) from error
    finally:
        page.remove_listener("response", record_rate_limit)
    return article


def execute_post_action(
    page: Page,
    target: PostActionTarget,
    find_locator: LocatorFinder = find_first_visible_locator,
    *,
    should_cancel: Callable[[], bool] = lambda: False,
    rate_limit_pending: Callable[[], bool] = lambda: False,
    rate_limit_blocking: Callable[[Response], bool] = lambda _: True,
    isolated_dependency_failed: Callable[[], None] = lambda: None,
    isolation_pending: Callable[[], bool] = lambda: False,
    diagnostics: NetworkDiagnostics | None = None,
    reuse_view: PendingView | None = None,
    on_target_loaded: Callable[[Locator], None] = lambda _: None,
    identity_check: Callable[[], bool] = lambda: True,
    prepare_control: Callable[[Locator, str], Callable[[], None]] = lambda locator, _: locator.click,
) -> PostActionResult:
    action_label = action_label_for(target.kind)
    mutation = {"post": DELETE_POST_MUTATION, "repost": UNDO_REPOST_MUTATION, "like": UNLIKE_MUTATION}[target.kind]
    requests: list[Request] = []
    responses: list[Response] = []
    classify_http = diagnostics.http_failure_for if diagnostics is not None else http_failure
    evidence = ActionEvidence(should_cancel=should_cancel, rate_limit_pending=rate_limit_pending, diagnostics=diagnostics,
                              reuse_view=reuse_view, on_target_loaded=on_target_loaded, identity_check=identity_check,
                              prepare_control=prepare_control)

    def record_request(request: Request) -> None:
        if _is_mutation_request(request, mutation):
            requests.append(request)
            if diagnostics is not None:
                diagnostics.request(request)

    def record_response(response: Response) -> None:
        if diagnostics is not None:
            diagnostics.response(response)
        host = urlparse(response.url).hostname or ""
        if host in {"x.com", "twitter.com"} or host.endswith((".x.com", ".twitter.com")):
            responses.append(response)
            rate_blocks = response.status in {420, 429} and rate_limit_blocking(response)
            if response.status in {401, 403} or rate_blocks:
                evidence.blocked = True
            if rate_blocks:
                evidence.rate_blocked = True

    page.on("request", record_request)
    page.on("response", record_response)
    try:
        action = {"post": delete_post, "repost": undo_repost, "like": unlike_post}[target.kind]
        if evidence.can_submit():
            success, error_message = action(page, target.url, find_locator, _evidence=evidence)
        else:
            evidence.phase("submission_blocked")
            success, error_message = False, tr("送信前に中断しました。待機またはログイン確認が必要です。")
        if success and evidence.submitted and len(requests) != 1:
            # DOM changes alone can be optimistic. The batch requires a receipt
            # for the actual mutation before recording a confirmed success.
            success = False
        if (not success and not evidence.submitted and not requests and not evidence.cancelled and not evidence.blocked
                and (isolation_pending() or any(response.status in {420, 429} and not rate_limit_blocking(response)
                                               for response in responses))):
            # No proof that the quarantined endpoint is dispensable for this DOM.
            # Restore the global signal and stop; retain this positively unsent item.
            isolated_dependency_failed()
            evidence.rate_blocked = evidence.rate_blocked or rate_limit_pending()
        kind: FailureKind = "permanent"
        retry_after = None
        mutation_rate_refused = False
        graphql_code88 = False
        failure_evidence = None
        observed_http = [failure for response in responses if (failure := classify_http(response)) is not None]
        if observed_http:
            failure_evidence = max(observed_http, key=lambda item: (item.auth_confirmed, {"mutation": 3, "target": 2, "ancillary": 1, "unknown": 0}[item.role]))
        if not success:
            if evidence.submitted or requests:
                # An absent response, 5xx, or unconfirmed 2xx may follow a committed
                # deletion. Only an explicit refusal proves it safe to repeat.
                kind = "ambiguous"
                for request in requests if len(requests) == 1 else []:
                    response = _completed_response(request)
                    if response is None:
                        break
                    if response.status in {420, 429}:
                        mutation_rate_refused = True
                        kind = "retryable"
                        retry_after = parse_retry_after(response.headers.get("retry-after"))
                    elif response.status in {401, 403}:
                        failure_evidence = classify_http(response)
                        kind = "auth" if failure_evidence is not None and failure_evidence.auth_confirmed else "blocked"
                    elif 400 <= response.status < 500:
                        kind = "permanent"
                    elif 200 <= response.status < 300 and _is_mutation_request(response.request, mutation):
                        try:
                            body = response.json()
                        except Exception:
                            continue
                        if not _valid_error_envelope(body):
                            continue
                        errors = body.get("errors") if isinstance(body, dict) else None
                        if errors:
                            codes = {item.get("code") for item in errors if isinstance(item, dict)} if isinstance(errors, list) else set()
                            if codes == {88}:
                                mutation_rate_refused = True
                                graphql_code88 = True
                                kind = "retryable"
                                retry_after = parse_retry_after(response.headers.get("retry-after"))
                            elif codes & {32, 89, 99, 135, 215, 220, 326}:
                                kind = "auth"
                                failure_evidence = HttpFailure(response.status, "mutation", mutation,
                                    "account_code" if codes & ACCOUNT_CODES else "auth_code", "error_code",
                                    tuple(sorted(code for code in KNOWN_CODES if code in codes)))
                            else:
                                kind = "permanent"
            else:
                statuses = {response.status for response in responses
                            if response.status not in {420, 429} or rate_limit_blocking(response)}
                if statuses & {401, 403}:
                    kind = "auth" if failure_evidence is not None and failure_evidence.auth_confirmed else "blocked"
                elif evidence.cancelled:
                    kind = "retryable"
                elif statuses & {404, 410}:
                    kind = "permanent"
                elif statuses & {420, 429, 500, 502, 503, 504} or evidence.transient_before_submission:
                    kind = "retryable"
                    retry_after = max((parse_retry_after(r.headers.get("retry-after")) or 0.0
                                       for r in responses if r.status in {420, 429, 503}), default=0.0) or None
                if evidence.control_failed:
                    kind = "blocked"
                elif evidence.identity_failed:
                    kind = "blocked"
                    error_message = tr("操作前のsession・対象投稿・アカウント照合を確認できないため、送信せず停止しました。")
            if kind == "ambiguous":
                error_message = tr("結果不明のため再試行しません。X で確認してください。") + " " + (error_message or "")
            if kind == "blocked" and not (evidence.identity_failed or evidence.control_failed):
                error_message = tr("HTTP {status} の通信拒否で停止しました。認証原因は未確認です。diagnostics --summary で通信先と理由を確認してください。",
                                   status=failure_evidence.status if failure_evidence is not None else 403)
        limited = [response for response in responses if response.status in {420, 429} and rate_limit_blocking(response)]
        rate_scope, rate_endpoint = None, None
        if limited:
            priority = {"ancillary": 0, "unknown": 1, "target": 2, "mutation": 3}
            rate_scope, rate_endpoint = max((classify_rate_limit(response.url) for response in limited),
                                           key=lambda pair: priority[pair[0]])
        if mutation_rate_refused:
            rate_scope, rate_endpoint = "mutation", mutation
        deferred = not success and not evidence.submitted and not requests and (evidence.rate_blocked or evidence.view_reuse_invalid) and kind not in {"auth", "ambiguous", "blocked"} and not any(response.status in {404, 410} for response in responses)
        if deferred:
            kind = "retryable"
            if not evidence.view_reuse_invalid:
                error_message = tr("利用制限を検知したため送信せず保留しました。削除失敗には数えません。")
        result = PostActionResult(target, action_label, success, error_message, kind, retry_after,
                                  deferred=deferred, rate_limit_scope=rate_scope, rate_limit_endpoint=rate_endpoint,
                                  view_reuse_invalid=evidence.view_reuse_invalid,
                                  http_failure=failure_evidence if not success else None)
        if diagnostics is not None:
            diagnostics.outcome(result, requests, submitted=evidence.submitted, graphql_code88=graphql_code88)
        return result
    finally:
        page.remove_listener("request", record_request)
        page.remove_listener("response", record_response)


def undo_repost(
    page: Page,
    post_url: str,
    find_locator: LocatorFinder = find_first_visible_locator,
    *,
    _evidence: ActionEvidence | None = None,
) -> tuple[bool, str | None]:
    """指定されたポストのリポストを解除する"""
    return _toggle_off_post(
        page,
        post_url,
        find_locator,
        evidence=_evidence,
        action_name="undo repost",
        undo_id="unretweet",
        do_id="retweet",
        mutation=UNDO_REPOST_MUTATION,
        button_missing=tr("リポスト解除ボタンが見つかりませんでした。"),
        confirm=(UNREPOST_CONFIRM_SELECTORS, tr("リポスト解除の確認ボタンが見つかりませんでした。")),
    )


def unlike_post(
    page: Page,
    post_url: str,
    find_locator: LocatorFinder = find_first_visible_locator,
    *,
    _evidence: ActionEvidence | None = None,
) -> tuple[bool, str | None]:
    """指定されたポストのいいねを取り消す"""
    # Unliking has no confirmation step; the first click sends the mutation.
    return _toggle_off_post(
        page,
        post_url,
        find_locator,
        evidence=_evidence,
        action_name="unlike",
        undo_id="unlike",
        do_id="like",
        mutation=UNLIKE_MUTATION,
        button_missing=tr("いいね取り消しボタンが見つかりませんでした。"),
    )


def _toggle_off_post(
    page: Page,
    post_url: str,
    find_locator: LocatorFinder,
    *,
    evidence: ActionEvidence | None = None,
    action_name: str,
    undo_id: str,
    do_id: str,
    mutation: str,
    button_missing: str,
    confirm: tuple[tuple[str, ...], str] | None = None,
) -> tuple[bool, str | None]:
    """Click the post's own `undo_id` control (and its confirmation) until it turns back into `do_id`."""
    logger.info("Initiating %s for: %s", action_name, post_url)
    mutation_requests: list[Request] = []

    def record_mutation(request: Request) -> None:
        if _is_mutation_request(request, mutation):
            mutation_requests.append(request)

    listening = False
    try:
        main_tweet_article = _open_target_article(page, post_url, diagnostics=evidence.diagnostics if evidence else None)
        if evidence is not None:
            evidence.phase("menu")
        undo_button = find_locator(main_tweet_article, (_own_control(undo_id),))
        if undo_button is None:
            logger.warning("%s URL=%s", button_missing, post_url)
            return False, button_missing

        article_element = main_tweet_article.element_handle()
        if confirm is None:
            page.on("request", record_mutation)
            listening = True
            if evidence is not None:
                if not evidence.can_submit():
                    evidence.phase("submission_blocked")
                    return False, tr("送信前に中断しました。待機またはログイン確認が必要です。")
                evidence.phase("mutation_submit")
            (evidence.prepared_click(undo_button, "unlike", mutation=True) if evidence else undo_button.click)()
        else:
            if evidence is not None:
                evidence.phase("confirmation")
            confirm_selectors, confirm_missing = confirm
            (evidence.prepared_click(undo_button, "toggle_menu") if evidence else undo_button.click)()
            page.wait_for_selector(", ".join(confirm_selectors), state="visible", timeout=ACTION_STATE_TIMEOUT_MS)
            confirm_button = find_locator(page, confirm_selectors)
            if confirm_button is None:
                logger.warning("%s URL=%s", confirm_missing, post_url)
                return False, confirm_missing
            page.on("request", record_mutation)
            listening = True
            if evidence is not None:
                if not evidence.can_submit():
                    evidence.phase("submission_blocked")
                    return False, tr("送信前に中断しました。待機またはログイン確認が必要です。")
                evidence.phase("mutation_submit")
            (evidence.prepared_click(confirm_button, "toggle_confirm", mutation=True) if evidence else confirm_button.click)()

        if evidence is not None:
            evidence.phase("mutation_wait")
        error_message = _wait_for_mutation_result(
            mutation_requests,
            mutation,
            lambda: page.wait_for_function(
                TOGGLE_OFF_DONE_SCRIPT,
                arg={"article": article_element, "owned": OWNED_NODE_XPATH, "undoId": undo_id, "doId": do_id},
                timeout=ACTION_STATE_TIMEOUT_MS,
            ),
        )
        if error_message is not None:
            logger.warning("%s URL=%s", error_message, post_url)
            return False, error_message
        logger.info("Completed %s: %s", action_name, post_url)
        return True, None
    except TargetPostUnavailableError as error:
        if evidence is not None:
            evidence.transient_before_submission = isinstance(error.__cause__, PlaywrightTimeoutError)
        logger.warning("%s URL=%s", error, post_url)
        return False, str(error)
    except Exception as error:
        if evidence is not None:
            evidence.transient_before_submission = not evidence.submitted and isinstance(error, PlaywrightTimeoutError)
        logger.exception("Exception during %s for %s.", action_name, post_url)
        return False, f"{type(error).__name__}: {error}"
    finally:
        if listening:
            page.remove_listener("request", record_mutation)


def delete_post(
    page: Page,
    post_url: str,
    find_locator: LocatorFinder = find_first_visible_locator,
    *,
    _evidence: ActionEvidence | None = None,
) -> tuple[bool, str | None]:
    """指定されたポストのURLに直接アクセスし、削除操作を行う"""
    logger.info("Initiating delete for: %s", post_url)
    stage = tr("対象ポストの読み込み")
    mutation_requests: list[Request] = []

    def record_mutation(request: Request) -> None:
        if _is_mutation_request(request, DELETE_POST_MUTATION):
            mutation_requests.append(request)

    listening = False
    try:
        main_tweet_article = _open_target_article(page, post_url, diagnostics=_evidence.diagnostics if _evidence else None,
                                                   reuse_view=_evidence.reuse_view if _evidence else None)
        if _evidence is not None:
            _evidence.on_target_loaded(main_tweet_article)
            if not _evidence.can_submit():
                _evidence.phase("submission_blocked")
                return False, tr("送信前に中断しました。待機またはログイン確認が必要です。")
        if _evidence is not None:
            _evidence.phase("menu")
        stage = tr("「…」メニューボタンの表示待ち")
        main_tweet_article.locator(_own_control('caret')).wait_for(
            state="visible", timeout=ACTION_STATE_TIMEOUT_MS
        )
        menu_btn = find_locator(
            main_tweet_article,
            (_own_control('caret'),),
        )

        if menu_btn is None:
            message = tr("削除メニューが見つかりませんでした。")
            logger.warning("%s URL=%s", message, post_url)
            return False, message

        article_element = main_tweet_article.element_handle()
        if _evidence is not None and not _evidence.can_submit():
            _evidence.phase("submission_blocked")
            return False, tr("送信前に中断しました。待機またはログイン確認が必要です。")
        (_evidence.prepared_click(menu_btn, "caret") if _evidence is not None else menu_btn.click)()
        stage = tr("削除メニュー項目の表示待ち")
        # The dropdown container can appear before its menu items are rendered.
        # Wait for a positively identified delete action, never a generic menu item.
        delete_candidates = delete_menu_items(page)
        try:
            # .first is readiness only. The complete union is checked below and
            # again by the verified control scope, never truncated for identity.
            delete_candidates.first.wait_for(state="visible", timeout=ACTION_STATE_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            message = tr("削除メニュー項目が見つかりませんでした。自分のポストではない可能性があります。")
            logger.warning("%s URL=%s", message, post_url)
            return False, message

        count = delete_candidates.count()
        if count != 1:
            message = tr("操作対象のメニューまたは確認ボタンを一意に照合できないため停止しました。")
            if _evidence is not None:
                _evidence.control_failed = _evidence.blocked = True
                if _evidence.diagnostics is not None:
                    _evidence.diagnostics.verification_stop("delete_menu", "control_count")
            logger.warning("%s URL=%s", message, post_url)
            return False, message
        delete_menu_item = delete_candidates

        if _evidence is not None and not _evidence.can_submit():
            _evidence.phase("submission_blocked")
            return False, tr("送信前に中断しました。待機またはログイン確認が必要です。")

        stage = tr("削除確認画面の表示")
        (_evidence.prepared_click(delete_menu_item, "delete_menu") if _evidence is not None else delete_menu_item.click)()
        if _evidence is not None:
            _evidence.phase("confirmation")
        page.wait_for_selector(
            CONFIRMATION_SHEET_CONFIRM_SELECTOR,
            state="visible",
            timeout=ACTION_STATE_TIMEOUT_MS,
        )

        confirm_btn = find_locator(
            page,
            (CONFIRMATION_SHEET_CONFIRM_SELECTOR,),
        )
        if confirm_btn is not None:
            stage = tr("削除確定後の完了確認（結果不明。再試行前にXで確認してください）")
            page.on("request", record_mutation)
            listening = True
            click_confirm = _evidence.prepared_click(confirm_btn, "confirm", mutation=True) if _evidence is not None else confirm_btn.click
            if _evidence is not None:
                if not _evidence.can_submit():
                    _evidence.phase("submission_blocked")
                    return False, tr("送信前に中断しました。待機またはログイン確認が必要です。")
                _evidence.phase("mutation_submit")
            click_confirm()
            if _evidence is not None:
                _evidence.phase("mutation_wait")
            error_message = _wait_for_mutation_result(
                mutation_requests,
                DELETE_POST_MUTATION,
                lambda: page.wait_for_function(
                    "article => article !== null && !article.isConnected",
                    arg=article_element,
                    timeout=ACTION_STATE_TIMEOUT_MS,
                ),
            )
            if error_message is not None:
                logger.warning("%s URL=%s", error_message, post_url)
                return False, error_message
            logger.info("Successfully deleted post: %s", post_url)
            return True, None

        message = tr("削除確認ボタンが見つかりませんでした。")
        logger.warning("%s URL=%s", message, post_url)
        return False, message

    except TargetPostUnavailableError as error:
        if _evidence is not None:
            _evidence.transient_before_submission = isinstance(error.__cause__, PlaywrightTimeoutError)
            _evidence.view_reuse_invalid = isinstance(error, CachedTargetChangedError)
        logger.warning("%s URL=%s", error, post_url)
        return False, str(error)
    except Exception as error:
        if _evidence is not None:
            _evidence.transient_before_submission = not _evidence.submitted and isinstance(error, PlaywrightTimeoutError)
        logger.exception("Exception during deletion of post %s at %s.", post_url, stage)
        return False, f"{stage}: {type(error).__name__}: {error}"
    finally:
        if listening:
            page.remove_listener("request", record_mutation)


def unfollow_account(
    page: Page,
    username: str,
    find_locator: LocatorFinder = find_first_visible_locator,
) -> tuple[bool, str | None]:
    """Open ``x.com/<username>`` and unfollow it from the profile header."""
    logger.info("Initiating unfollow for: @%s", username)
    if not USERNAME_PATTERN.fullmatch(username):
        return False, tr("ユーザー名は1〜15文字の英数字またはアンダースコアで指定してください。")
    stage = tr("プロフィールの読み込み")
    mutation_requests: list[Request] = []

    def record_mutation(request: Request) -> None:
        if _is_mutation_request(request, UNFOLLOW_MUTATION):
            mutation_requests.append(request)

    def fail(message: str) -> tuple[bool, str]:
        logger.warning("%s @%s", message, username)
        return False, message

    button_missing = tr("フォロー解除ボタンが見つかりませんでした。")
    listening = False
    try:
        page.goto(urljoin(BASE_X_URL, username), timeout=NAVIGATION_TIMEOUT_MS)
        page.locator(PROFILE_COLUMN_SELECTOR).first.wait_for(state="visible", timeout=NAVIGATION_TIMEOUT_MS)
        stage = tr("フォロー解除ボタンの表示待ち")
        try:
            page.wait_for_selector(
                f"{UNFOLLOW_BUTTON_SELECTOR}, {FOLLOW_BUTTON_SELECTOR}",
                state="visible",
                timeout=ACTION_STATE_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            return fail(button_missing)
        # Exactly one header button; more would mean the page is not the profile we expect.
        if page.locator(UNFOLLOW_BUTTON_SELECTOR).count() != 1:
            not_following = page.locator(FOLLOW_BUTTON_SELECTOR).count() > 0
            return fail(tr("このアカウントをフォローしていません。") if not_following else button_missing)
        unfollow_button = find_locator(page, (UNFOLLOW_BUTTON_SELECTOR,))
        if unfollow_button is None:
            return fail(button_missing)

        unfollow_button.click()
        stage = tr("フォロー解除確認画面の表示")
        page.wait_for_selector(CONFIRMATION_SHEET_CONFIRM_SELECTOR, state="visible", timeout=ACTION_STATE_TIMEOUT_MS)
        confirm_button = find_locator(page, (CONFIRMATION_SHEET_CONFIRM_SELECTOR,))
        if confirm_button is None:
            return fail(tr("フォロー解除の確認ボタンが見つかりませんでした。"))

        stage = tr("フォロー解除後の完了確認（結果不明。再試行前にXで確認してください）")
        page.on("request", record_mutation)
        listening = True
        confirm_button.click()
        error_message = _wait_for_mutation_result(
            mutation_requests,
            UNFOLLOW_MUTATION,
            lambda: page.wait_for_function(
                UNFOLLOW_DONE_SCRIPT,
                arg={"follow": FOLLOW_BUTTON_SELECTOR, "unfollow": UNFOLLOW_BUTTON_SELECTOR},
                timeout=ACTION_STATE_TIMEOUT_MS,
            ),
        )
        if error_message is not None:
            return fail(error_message)
        logger.info("Successfully unfollowed: @%s", username)
        return True, None
    except Exception as error:
        logger.exception("Exception during unfollow of @%s at %s.", username, stage)
        return False, f"{stage}: {type(error).__name__}: {error}"
    finally:
        if listening:
            page.remove_listener("request", record_mutation)
