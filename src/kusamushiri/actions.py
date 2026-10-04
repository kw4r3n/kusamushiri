import re
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Locator, Page, Request
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from kusamushiri.browser import BASE_X_URL, NAVIGATION_TIMEOUT_MS, find_first_visible_locator
from kusamushiri.i18n import tr
from kusamushiri.logger import logger
from kusamushiri.models import PostActionResult, PostActionTarget, PostKind
from kusamushiri.parsing import USERNAME_PATTERN, extract_post_id

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
    return urlparse(request.url).path.rstrip("/").endswith(f"/{mutation_name}")


def _mutation_error(requests: list[Request], mutation_name: str) -> str | None:
    """Xの操作APIが明示的に失敗を返した場合、そのエラーメッセージを返す。"""
    for request in requests:
        response = request.response()
        if response is None:
            return tr("{mutation} の通信に失敗しました: {detail}", mutation=mutation_name, detail=request.failure or tr("応答なし"))
        if response.status >= 400:
            return tr("{mutation} がエラーを返しました (HTTP {status})。", mutation=mutation_name, status=response.status)
        try:
            body = response.json()
        except Exception as error:
            logger.debug("Failed to read %s response body: %s", mutation_name, error)
            continue
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


def execute_post_action(
    page: Page,
    target: PostActionTarget,
    find_locator: LocatorFinder = find_first_visible_locator,
) -> PostActionResult:
    action_label = action_label_for(target.kind)
    if target.kind == "repost":
        success, error_message = undo_repost(page, target.url, find_locator)
    elif target.kind == "like":
        success, error_message = unlike_post(page, target.url, find_locator)
    else:
        success, error_message = delete_post(page, target.url, find_locator)

    return PostActionResult(
        target=target,
        action_label=action_label,
        success=success,
        error_message=error_message,
    )


def undo_repost(
    page: Page,
    post_url: str,
    find_locator: LocatorFinder = find_first_visible_locator,
) -> tuple[bool, str | None]:
    """指定されたポストのリポストを解除する"""
    return _toggle_off_post(
        page,
        post_url,
        find_locator,
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
) -> tuple[bool, str | None]:
    """指定されたポストのいいねを取り消す"""
    # Unliking has no confirmation step; the first click sends the mutation.
    return _toggle_off_post(
        page,
        post_url,
        find_locator,
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
        main_tweet_article = _target_article(page, post_url)
        page.goto(post_url, timeout=NAVIGATION_TIMEOUT_MS)

        main_tweet_article.wait_for(state="visible", timeout=NAVIGATION_TIMEOUT_MS)
        undo_button = find_locator(main_tweet_article, (_own_control(undo_id),))
        if undo_button is None:
            logger.warning("%s URL=%s", button_missing, post_url)
            return False, button_missing

        article_element = main_tweet_article.element_handle()
        if confirm is None:
            page.on("request", record_mutation)
            listening = True
            undo_button.click()
        else:
            confirm_selectors, confirm_missing = confirm
            undo_button.click()
            page.wait_for_selector(", ".join(confirm_selectors), state="visible", timeout=ACTION_STATE_TIMEOUT_MS)
            confirm_button = find_locator(page, confirm_selectors)
            if confirm_button is None:
                logger.warning("%s URL=%s", confirm_missing, post_url)
                return False, confirm_missing
            page.on("request", record_mutation)
            listening = True
            confirm_button.click()

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
    except Exception as error:
        logger.exception("Exception during %s for %s.", action_name, post_url)
        return False, f"{type(error).__name__}: {error}"
    finally:
        if listening:
            page.remove_listener("request", record_mutation)


def delete_post(
    page: Page,
    post_url: str,
    find_locator: LocatorFinder = find_first_visible_locator,
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
        main_tweet_article = _target_article(page, post_url)
        page.goto(post_url, timeout=NAVIGATION_TIMEOUT_MS)

        main_tweet_article.wait_for(state="visible", timeout=NAVIGATION_TIMEOUT_MS)
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
        menu_btn.click()
        stage = tr("削除メニュー項目の表示待ち")
        # The dropdown container can appear before its menu items are rendered.
        # Wait for a positively identified delete action, never a generic menu item.
        try:
            page.wait_for_selector(
                ", ".join(DELETE_MENU_ITEM_SELECTORS),
                state="visible",
                timeout=ACTION_STATE_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            message = tr("削除メニュー項目が見つかりませんでした。自分のポストではない可能性があります。")
            logger.warning("%s URL=%s", message, post_url)
            return False, message

        delete_menu_item = find_locator(page, DELETE_MENU_ITEM_SELECTORS)

        if delete_menu_item is None:
            message = tr("削除メニュー項目が見つかりませんでした。自分のポストではない可能性があります。")
            logger.warning("%s URL=%s", message, post_url)
            return False, message

        stage = tr("削除確認画面の表示")
        delete_menu_item.click()
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
            confirm_btn.click()
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

    except Exception as error:
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
