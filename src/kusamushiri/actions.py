import re
from collections.abc import Callable
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page, Request
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from kusamushiri.browser import NAVIGATION_TIMEOUT_MS, find_first_visible_locator
from kusamushiri.i18n import tr
from kusamushiri.logger import logger
from kusamushiri.models import PostActionResult, PostActionTarget, PostKind
from kusamushiri.parsing import extract_post_id

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
DELETE_CONFIRM_SELECTOR = '[data-testid="confirmationSheetConfirm"]'
DELETE_POST_MUTATION = "DeleteTweet"
UNDO_REPOST_MUTATION = "DeleteRetweet"
UNLIKE_MUTATION = "UnfavoriteTweet"

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
    logger.info("Initiating undo repost for: %s", post_url)
    mutation_requests: list[Request] = []

    def record_mutation(request: Request) -> None:
        if _is_mutation_request(request, UNDO_REPOST_MUTATION):
            mutation_requests.append(request)

    listening = False
    try:
        main_tweet_article = _target_article(page, post_url)
        page.goto(post_url, timeout=NAVIGATION_TIMEOUT_MS)

        main_tweet_article.wait_for(state="visible", timeout=NAVIGATION_TIMEOUT_MS)
        unrepost_button = find_locator(
            main_tweet_article,
            (_own_control('unretweet'),),
        )
        if unrepost_button is None:
            message = tr("リポスト解除ボタンが見つかりませんでした。")
            logger.warning("%s URL=%s", message, post_url)
            return False, message

        article_element = main_tweet_article.element_handle()
        unrepost_button.click()
        page.wait_for_selector(
            ", ".join(UNREPOST_CONFIRM_SELECTORS),
            state="visible",
            timeout=ACTION_STATE_TIMEOUT_MS,
        )

        confirm_button = find_locator(page, UNREPOST_CONFIRM_SELECTORS)
        if confirm_button is None:
            message = tr("リポスト解除の確認ボタンが見つかりませんでした。")
            logger.warning("%s URL=%s", message, post_url)
            return False, message

        page.on("request", record_mutation)
        listening = True
        confirm_button.click()
        error_message = _wait_for_mutation_result(
            mutation_requests,
            UNDO_REPOST_MUTATION,
            lambda: page.wait_for_function(
                TOGGLE_OFF_DONE_SCRIPT,
                arg={"article": article_element, "owned": OWNED_NODE_XPATH, "undoId": "unretweet", "doId": "retweet"},
                timeout=ACTION_STATE_TIMEOUT_MS,
            ),
        )
        if error_message is not None:
            logger.warning("%s URL=%s", error_message, post_url)
            return False, error_message
        logger.info("Successfully removed repost: %s", post_url)
        return True, None
    except Exception as error:
        logger.exception("Exception during undo repost for %s.", post_url)
        return False, f"{type(error).__name__}: {error}"
    finally:
        if listening:
            page.remove_listener("request", record_mutation)


def unlike_post(
    page: Page,
    post_url: str,
    find_locator: LocatorFinder = find_first_visible_locator,
) -> tuple[bool, str | None]:
    """指定されたポストのいいねを取り消す"""
    logger.info("Initiating unlike for: %s", post_url)
    mutation_requests: list[Request] = []

    def record_mutation(request: Request) -> None:
        if _is_mutation_request(request, UNLIKE_MUTATION):
            mutation_requests.append(request)

    listening = False
    try:
        main_tweet_article = _target_article(page, post_url)
        page.goto(post_url, timeout=NAVIGATION_TIMEOUT_MS)

        main_tweet_article.wait_for(state="visible", timeout=NAVIGATION_TIMEOUT_MS)
        unlike_button = find_locator(main_tweet_article, (_own_control("unlike"),))
        if unlike_button is None:
            message = tr("いいね取り消しボタンが見つかりませんでした。")
            logger.warning("%s URL=%s", message, post_url)
            return False, message

        article_element = main_tweet_article.element_handle()
        # Unliking has no confirmation step; the first click sends the mutation.
        page.on("request", record_mutation)
        listening = True
        unlike_button.click()
        error_message = _wait_for_mutation_result(
            mutation_requests,
            UNLIKE_MUTATION,
            lambda: page.wait_for_function(
                TOGGLE_OFF_DONE_SCRIPT,
                arg={"article": article_element, "owned": OWNED_NODE_XPATH, "undoId": "unlike", "doId": "like"},
                timeout=ACTION_STATE_TIMEOUT_MS,
            ),
        )
        if error_message is not None:
            logger.warning("%s URL=%s", error_message, post_url)
            return False, error_message
        logger.info("Successfully removed like: %s", post_url)
        return True, None
    except Exception as error:
        logger.exception("Exception during unlike for %s.", post_url)
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
            DELETE_CONFIRM_SELECTOR,
            state="visible",
            timeout=ACTION_STATE_TIMEOUT_MS,
        )

        confirm_btn = find_locator(
            page,
            (DELETE_CONFIRM_SELECTOR,),
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
