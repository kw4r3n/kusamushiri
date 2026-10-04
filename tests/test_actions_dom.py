"""Local Chromium regression tests for destructive X action target selection.

Every navigation is fulfilled from an in-memory document; these tests never contact
X.  They require Playwright Chromium (install it with ``uv run playwright install chromium``).
If Chromium is not installed, the browser fixture skips cleanly.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from kusamushiri import actions as x_deleter_actions
from kusamushiri.core import POST_ARTICLE_SELECTOR, XDeleterCore
from kusamushiri.models import CollectRequest, PostRecord

TARGET_ID = "123"
TARGET_URL = f"https://x.com/target/status/{TARGET_ID}"


@pytest.fixture
def local_page() -> Iterator[Callable[[str], tuple[Page, list[str]]]]:
    """Return pages whose requests are fulfilled only by local fixture HTML."""
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed; run `uv run playwright install chromium`")
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()

            def open_local_document(html: str) -> tuple[Page, list[str]]:
                navigations: list[str] = []

                def fulfill_locally(route) -> None:
                    navigations.append(route.request.url)
                    route.fulfill(content_type="text/html", body=html)

                page.route("**/*", fulfill_locally)
                return page, navigations

            yield open_local_document
        finally:
            browser.close()


def _article(post_id: str, *, extra_content: str = "") -> str:
    return f"""
        <article data-testid="tweet" data-post-id="{post_id}">
          <a href="/author/status/{post_id}"><time datetime="2026-09-10T00:00:00.000Z">primary</time></a>
          {extra_content}
          <button data-testid="caret" onclick="openDelete('{post_id}')">more</button>
          <button data-testid="unretweet" onclick="openUndo('{post_id}')">undo repost</button>
          <button data-testid="unlike" onclick="unlikePost('{post_id}')">undo like</button>
        </article>
    """


def _document(articles: list[str], *, leave_success_state_unchanged: bool = False) -> str:
    unchanged = "true" if leave_success_state_unchanged else "false"
    return f"""
        <!doctype html>
        <html><head><meta charset="utf-8"></head><body>
          <main>{"".join(articles)}</main>
          <div data-testid="Dropdown" hidden>
            <div role="menuitem" onclick="chooseDelete()">Delete</div>
          </div>
          <button data-testid="confirmationSheetConfirm" hidden onclick="confirmDelete()">Delete</button>
          <button data-testid="unretweetConfirm" hidden onclick="confirmUndo()">Undo repost</button>
          <script>
            window.actionLog = [];
            window.selectedPostId = null;
            window.leaveSuccessStateUnchanged = {unchanged};
            window.articleFor = (postId) => document.querySelector(`article[data-post-id="${{postId}}"]`);
            window.openDelete = (postId) => {{
              window.actionLog.push(`caret:${{postId}}`);
              window.selectedPostId = postId;
              document.querySelector('[data-testid="Dropdown"]').hidden = false;
            }};
            window.chooseDelete = () => {{
              window.actionLog.push(`delete-menu:${{window.selectedPostId}}`);
              document.querySelector('[data-testid="confirmationSheetConfirm"]').hidden = false;
            }};
            window.confirmDelete = () => {{
              window.actionLog.push(`delete-confirm:${{window.selectedPostId}}`);
              if (!window.leaveSuccessStateUnchanged) window.articleFor(window.selectedPostId).remove();
            }};
            window.openUndo = (postId) => {{
              window.actionLog.push(`unretweet:${{postId}}`);
              window.selectedPostId = postId;
              document.querySelector('[data-testid="unretweetConfirm"]').hidden = false;
            }};
            window.unlikePost = (postId) => {{
              window.actionLog.push(`unlike:${{postId}}`);
              if (!window.leaveSuccessStateUnchanged) {{
                window.articleFor(postId).querySelector('[data-testid="unlike"]').dataset.testid = 'like';
              }}
            }};
            window.confirmUndo = () => {{
              window.actionLog.push(`undo-confirm:${{window.selectedPostId}}`);
              if (!window.leaveSuccessStateUnchanged) {{
                const button = window.articleFor(window.selectedPostId).querySelector('[data-testid="unretweet"]');
                button.dataset.testid = 'retweet';
              }}
            }};
          </script>
        </body></html>
    """


ACTIONS = {
    "delete": x_deleter_actions.delete_post,
    "undo": x_deleter_actions.undo_repost,
    "unlike": x_deleter_actions.unlike_post,
}
EXPECTED_ACTION_LOGS = {
    "delete": [f"caret:{TARGET_ID}", f"delete-menu:{TARGET_ID}", f"delete-confirm:{TARGET_ID}"],
    "undo": [f"unretweet:{TARGET_ID}", f"undo-confirm:{TARGET_ID}"],
    "unlike": [f"unlike:{TARGET_ID}"],
}


def _run_action(page: Page, action_name: str) -> tuple[bool, str | None]:
    return ACTIONS[action_name](page, TARGET_URL)


def _action_log(page: Page) -> list[str]:
    return page.evaluate("window.actionLog")


def _collection_request() -> CollectRequest:
    return CollectRequest(
        username="target",
        max_posts=10,
        media_filter="all",
        is_reply=False,
        min_likes=0,
        min_replies=0,
        search_mode="profile",
        post_kind_filter="all",
    )


def _collect_local_articles(page: Page) -> list[PostRecord]:
    page.goto("https://x.com/target")
    core = XDeleterCore()
    collected: list[PostRecord] = []
    core._process_articles(
        articles=page.locator(POST_ARTICLE_SELECTOR).all(),
        request=_collection_request(),
        normalized_username="target",
        collected_posts=collected,
        seen_urls=set(),
    )
    return collected


def _collection_article(content: str) -> str:
    return f'<article data-testid="tweet">{content}</article>'


def _collection_permalink(post_url: str, date_text: str) -> str:
    return f'<a href="{post_url}"><time datetime="{date_text}">timestamp</time></a>'


def _collection_text(text: str) -> str:
    return f'<div data-testid="tweetText">{text}</div>'


def test_collection_uses_own_timestamp_text_and_date_when_quote_precedes_it(local_page) -> None:
    markup = _collection_article(
        """
        <blockquote data-testid="quoteTweet">
          <div data-testid="tweetText">
            <a href="/quoted/status/999"><time datetime="2026-09-01T00:00:00.000Z">quoted</time></a>
            quoted text
          </div>
        </blockquote>
        <div role="link">
          <a href="/role-quoted/status/998"><time datetime="2026-09-02T00:00:00.000Z">role quote</time></a>
        </div>
        """
        + _collection_permalink("/target/status/123", "2026-09-10T00:00:00.000Z")
        + _collection_text("own text")
    )
    page, _ = local_page(_document([markup]))

    posts = _collect_local_articles(page)

    assert [(post.url, post.date, post.text) for post in posts] == [
        ("https://x.com/target/status/123", "2026-09-10T00:00:00.000Z", "own text")
    ]


def test_collection_skips_quote_only_article_and_arbitrary_status_link(local_page) -> None:
    markup = _collection_article(
        """
        <blockquote>
          <a href="/quoted/status/999"><time datetime="2026-09-01T00:00:00.000Z">quoted</time></a>
          <div data-testid="tweetText">quoted text</div>
        </blockquote>
        <a href="/target/status/123">body status link without timestamp</a>
        """
    )
    page, _ = local_page(_document([markup]))

    assert _collect_local_articles(page) == []


def test_collection_skips_unsupported_own_timestamp_url(local_page) -> None:
    markup = _collection_article(
        _collection_permalink("https://example.com/target/status/123", "2026-09-10T00:00:00.000Z")
        + _collection_text("unsupported URL")
    )
    page, _ = local_page(_document([markup]))

    assert _collect_local_articles(page) == []


def test_collection_rejects_conflicting_own_timestamp_identities(local_page) -> None:
    markup = _collection_article(
        _collection_permalink("/target/status/123", "2026-09-10T00:00:00.000Z")
        + _collection_permalink("/target/status/456", "2026-09-11T00:00:00.000Z")
        + _collection_text("ambiguous text")
    )
    page, _ = local_page(_document([markup]))

    assert _collect_local_articles(page) == []


def test_collection_enumerates_only_top_level_articles(local_page) -> None:
    markup = _collection_article(
        """
        <article data-testid="tweet">
          <a href="/quoted/status/999"><time datetime="2026-09-01T00:00:00.000Z">nested</time></a>
          <div data-testid="tweetText">nested text</div>
        </article>
        """
        + _collection_permalink("/target/status/123", "2026-09-09T00:00:00.000Z")
        + _collection_text("top-level text")
    )
    page, _ = local_page(_document([markup]))

    posts = _collect_local_articles(page)

    assert [(post.url, post.text) for post in posts] == [
        ("https://x.com/target/status/123", "top-level text")
    ]


def test_collection_preserves_normal_and_repost_previews(local_page) -> None:
    normal = _collection_article(
        _collection_permalink("/target/status/123", "2026-09-10T00:00:00.000Z")
        + _collection_text("normal post")
    )
    repost = _collection_article(
        _collection_permalink("/original/status/456", "2026-09-11T00:00:00.000Z")
        + _collection_text("repost preview")
    )
    page, _ = local_page(_document([normal, repost]))

    posts = _collect_local_articles(page)

    assert [(post.url, post.text, post.kind) for post in posts] == [
        ("https://x.com/target/status/123", "normal post", "post"),
        ("https://x.com/original/status/456", "repost preview", "repost"),
    ]


@pytest.mark.parametrize("action_name", ["delete", "undo", "unlike"])
def test_action_targets_primary_timestamp_article_not_preceding_article(
    local_page,
    monkeypatch,
    action_name: str,
) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 100)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 100)
    page, navigations = local_page(_document([_article("999"), _article(TARGET_ID)]))

    success, error_message = _run_action(page, action_name)

    expected_log = EXPECTED_ACTION_LOGS[action_name]
    assert (success, error_message) == (True, None)
    assert _action_log(page) == expected_log
    assert page.locator('article[data-post-id="999"]').count() == 1
    if action_name == "delete":
        assert page.locator(f'article[data-post-id="{TARGET_ID}"]').count() == 0
    elif action_name == "undo":
        assert page.locator(f'article[data-post-id="{TARGET_ID}"] [data-testid="retweet"]').count() == 1
    else:
        assert page.locator(f'article[data-post-id="{TARGET_ID}"] [data-testid="like"]').count() == 1
    assert navigations == [TARGET_URL]


@pytest.mark.parametrize("action_name", ["delete", "undo", "unlike"])
@pytest.mark.parametrize(
    ("articles", "case_name"),
    [
        ([_article("999")], "missing-target"),
        ([_article("1234")], "id-prefix-collision"),
        (
            [_article("999", extra_content='<a href="/target/status/123">body link</a>')],
            "body-link-only",
        ),
        (
            [
                _article(
                    "999",
                    extra_content=(
                        '<blockquote><a href="/target/status/123"><time '
                        'datetime="2026-09-09T00:00:00.000Z">quoted timestamp</time></a></blockquote>'
                    ),
                )
            ],
            "quoted-timestamp-only",
        ),
        ([_article(TARGET_ID), _article(TARGET_ID)], "ambiguous-duplicate-targets"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_action_refuses_missing_or_non_primary_target_identity(
    local_page,
    monkeypatch,
    action_name: str,
    articles: list[str],
    case_name: str,
) -> None:
    del case_name  # Used as the parametrized test id.
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 100)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 100)
    page, navigations = local_page(_document(articles))

    success, error_message = _run_action(page, action_name)

    assert success is False
    assert error_message is not None
    assert _action_log(page) == []
    assert page.locator('article[data-testid="tweet"]').count() == len(articles)
    assert navigations == [TARGET_URL]


@pytest.mark.parametrize("action_name", ["delete", "undo", "unlike"])
def test_action_fails_when_target_success_state_does_not_change(local_page, monkeypatch, action_name: str) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 100)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 100)
    page, navigations = local_page(
        _document([_article("999"), _article(TARGET_ID)], leave_success_state_unchanged=True)
    )

    success, error_message = _run_action(page, action_name)

    expected_log = EXPECTED_ACTION_LOGS[action_name]
    assert success is False
    assert error_message is not None
    assert _action_log(page) == expected_log
    assert page.locator(f'article[data-post-id="{TARGET_ID}"]').count() == 1
    assert navigations == [TARGET_URL]


@pytest.mark.parametrize("action_name", ["delete", "undo", "unlike"])
def test_quote_before_own_timestamp_does_not_hide_target(local_page, monkeypatch, action_name: str) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 100)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 100)
    markup = _article(TARGET_ID).replace(
        '<a href=',
        '<blockquote><a href="/quoted/status/999"><time>quote first</time></a></blockquote><a href=',
        1,
    )
    page, _ = local_page(_document([markup]))

    assert _run_action(page, action_name) == (True, None)
    assert all(entry.endswith(f":{TARGET_ID}") for entry in _action_log(page))


@pytest.mark.parametrize("action_name", ["delete", "undo", "unlike"])
def test_conflicting_own_timestamp_after_target_refuses_action(local_page, monkeypatch, action_name: str) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 100)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 100)
    page, _ = local_page(_document([
        _article(TARGET_ID, extra_content='<a href="/other/status/999"><time>conflict</time></a>'),
    ]))

    success, error = _run_action(page, action_name)

    assert success is False
    assert error
    assert _action_log(page) == []


@pytest.mark.parametrize("label, has_test_id", [("Delete", False), ("Eliminar", True)])
def test_delete_waits_for_late_menu_item_and_accepts_explicit_delete_testid(
    local_page, monkeypatch, label: str, has_test_id: bool
) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 1000)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 1000)
    html = _document([_article(TARGET_ID)])
    html += f"""<script>
        document.querySelector('[role="menuitem"]').remove();
        document.body.insertAdjacentHTML('afterbegin',
          '<div data-testid="Dropdown" hidden><div role="menuitem" data-testid="delete">Delete</div></div>');
        window.openDelete = postId => {{
          window.actionLog.push(`caret:${{postId}}`);
          window.selectedPostId = postId;
          document.querySelectorAll('[data-testid="Dropdown"]')[1].hidden = false;
          setTimeout(() => {{
            const item = document.createElement('div');
            item.setAttribute('role', 'menuitem');
            {'item.setAttribute("data-testid", "delete");' if has_test_id else ''}
            item.textContent = {label!r};
            item.onclick = window.chooseDelete;
            document.querySelectorAll('[data-testid="Dropdown"]')[1].append(item);
          }}, 30);
        }};
    </script>"""
    page, _ = local_page(html)

    assert _run_action(page, "delete") == (True, None)
    assert _action_log(page) == [f"caret:{TARGET_ID}", f"delete-menu:{TARGET_ID}", f"delete-confirm:{TARGET_ID}"]


def test_delete_does_not_click_unidentified_menu_item(local_page, monkeypatch) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 100)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 100)
    html = _document([_article(TARGET_ID)]).replace(
        'role="menuitem" onclick="chooseDelete()">Delete',
        'role="menuitem" onclick="chooseDelete()">Share',
    )
    page, _ = local_page(html)

    success, error = _run_action(page, "delete")

    assert success is False
    assert error == "削除メニュー項目が見つかりませんでした。自分のポストではない可能性があります。"
    assert _action_log(page) == [f"caret:{TARGET_ID}"]


def test_delete_waits_for_late_caret(local_page, monkeypatch) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 1000)
    html = _document([_article(TARGET_ID)])
    html += '''<script>
      const caret = document.querySelector('[data-testid="caret"]');
      caret.hidden = true;
      setTimeout(() => caret.hidden = false, 100);
    </script>'''
    page, _ = local_page(html)
    assert _run_action(page, "delete") == (True, None)
    assert _action_log(page) == [f"caret:{TARGET_ID}", f"delete-menu:{TARGET_ID}", f"delete-confirm:{TARGET_ID}"]


def test_missing_caret_reports_stage_without_clicking(local_page, monkeypatch) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 100)
    html = _document([_article(TARGET_ID)]) + '<script>document.querySelector("[data-testid=caret]").remove()</script>'
    page, _ = local_page(html)
    success, error = _run_action(page, "delete")
    assert not success
    assert "メニューボタン" in error
    assert _action_log(page) == []


def test_delayed_search_results_are_collected_at_same_height(local_page, monkeypatch) -> None:
    from dataclasses import replace

    html = _document([_article(TARGET_ID)])
    html += '''<script>
      const article = document.querySelector('article');
      article.remove();
      document.querySelector('main').style.height = '1000px';
      setTimeout(() => document.querySelector('main').append(article), 600);
    </script>'''
    page, _ = local_page(html)
    core = XDeleterCore()
    core._browser.page = page
    monkeypatch.setattr(core, "_wait_for_timeline_update", lambda *args: None)
    posts = core.search_and_collect_posts(replace(_collection_request(), search_mode="search", max_posts=1))
    assert len(posts) == 1
    assert posts[0].id == TARGET_ID


def test_removing_permalink_does_not_prove_article_deletion(local_page, monkeypatch) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 100)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 100)
    html = _document([_article(TARGET_ID)], leave_success_state_unchanged=True)
    html += '<script>window.confirmDelete = () => document.querySelector("article a").remove();</script>'
    page, _ = local_page(html)

    success, error = _run_action(page, "delete")

    assert success is False
    assert error
    assert page.locator("article").count() == 1


@pytest.mark.parametrize("like_test_id", ["like", "unlike"])
def test_collection_reads_own_metrics_including_liked_posts(local_page, like_test_id):
    from dataclasses import replace

    html = _document([_article(TARGET_ID, extra_content=f'''
        <blockquote>
          <button data-testid="like" aria-label="999 Likes">quoted likes</button>
          <button data-testid="reply" aria-label="888 Replies">quoted replies</button>
        </blockquote>
        <button data-testid="{like_test_id}" aria-label="12 Likes">own likes</button>
        <button data-testid="reply" aria-label="3 Replies">own replies</button>
    ''')])
    page, _ = local_page(html)
    page.goto(TARGET_URL)
    core = XDeleterCore()
    collected = []
    core._process_articles(
        page.locator(POST_ARTICLE_SELECTOR).all(),
        replace(_collection_request(), min_likes=10, min_replies=2),
        "target", collected, set(),
    )

    assert len(collected) == 1
    assert (collected[0].likes, collected[0].replies) == (12, 3)


def test_collection_does_not_use_quoted_metrics_to_pass_filter(local_page):
    from dataclasses import replace

    html = _document([_article(TARGET_ID, extra_content='''
        <blockquote><button data-testid="like" aria-label="999 Likes">quoted</button></blockquote>
        <button data-testid="like" aria-label="2 Likes">own</button>
    ''')])
    page, _ = local_page(html)
    page.goto(TARGET_URL)
    core = XDeleterCore()
    collected = []
    core._process_articles(
        page.locator(POST_ARTICLE_SELECTOR).all(),
        replace(_collection_request(), min_likes=10),
        "target", collected, set(),
    )

    assert collected == []


def _collected_flags(page: Page) -> list[tuple[bool, bool]]:
    return [(post.has_media, post.is_reply) for post in _collect_local_articles(page)]


def test_collection_ignores_quoted_media_and_quoted_reply_context(local_page) -> None:
    markup = _collection_article(
        _collection_permalink("/target/status/123", "2026-09-10T00:00:00.000Z")
        + _collection_text("text only")
        + """
        <div role="link">
          <a href="/quoted/status/999"><time datetime="2026-09-01T00:00:00.000Z">quoted</time></a>
          <div>Replying to <a href="/someone">@someone</a></div>
          <a href="/quoted/status/999/photo/1" role="link"><div data-testid="tweetPhoto"></div></a>
          <div data-testid="videoPlayer"></div>
        </div>
        """
    )
    page, _ = local_page(_document([markup]))

    assert _collected_flags(page) == [(False, False)]


def test_collection_does_not_treat_body_text_as_reply_context(local_page) -> None:
    markup = _collection_article(
        _collection_permalink("/target/status/123", "2026-09-10T00:00:00.000Z")
        + _collection_text("Replying to everyone: 返信先: thanks")
    )
    page, _ = local_page(_document([markup]))

    assert _collected_flags(page) == [(False, False)]


def test_collection_detects_own_reply_context_and_linked_photo(local_page) -> None:
    markup = _collection_article(
        _collection_permalink("/target/status/123", "2026-09-10T00:00:00.000Z")
        + '<div>Replying to <div><a href="/someone" role="link">@someone</a></div></div>'
        + _collection_text("own reply")
        + '<a href="/target/status/123/photo/1" role="link"><div data-testid="tweetPhoto"></div></a>'
    )
    page, _ = local_page(_document([markup]))

    assert _collected_flags(page) == [(True, True)]


@pytest.mark.parametrize("label", ["リストに追加/削除", "리스트에 추가/삭제", "목록에 추가/삭제"])
def test_delete_does_not_click_lists_menu_item(local_page, monkeypatch, label: str) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 100)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 100)
    html = _document([_article(TARGET_ID)]).replace(
        'role="menuitem" onclick="chooseDelete()">Delete',
        f'role="menuitem" onclick="chooseDelete()"><span>{label}</span>',
    )
    page, _ = local_page(html)

    success, error = _run_action(page, "delete")

    assert success is False
    assert error == "削除メニュー項目が見つかりませんでした。自分のポストではない可能性があります。"
    assert _action_log(page) == [f"caret:{TARGET_ID}"]


@pytest.mark.parametrize("label", ["削除", "삭제"])
def test_delete_accepts_exact_localized_delete_item(local_page, monkeypatch, label: str) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 1000)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 1000)
    html = _document([_article(TARGET_ID)]).replace(
        '<div role="menuitem" onclick="chooseDelete()">Delete</div>',
        '<div role="menuitem">リストに追加/削除</div>'
        f'<div role="menuitem" onclick="chooseDelete()"><div><svg></svg></div><span>{label}</span></div>',
    )
    page, _ = local_page(html)

    assert _run_action(page, "delete") == (True, None)


def _document_with_mutation(mutation: str) -> str:
    """Confirm buttons send the X GraphQL mutation, then update the DOM as if successful."""
    html = _document([_article(TARGET_ID)])
    return html + f"""<script>
        const sendMutation = () => fetch('https://x.com/i/api/graphql/abc/{mutation}', {{method: 'POST'}})
          .catch(() => null);
        const originalDelete = window.confirmDelete;
        const originalUndo = window.confirmUndo;
        const originalUnlike = window.unlikePost;
        window.confirmDelete = async () => {{ await sendMutation(); originalDelete(); }};
        window.confirmUndo = async () => {{ await sendMutation(); originalUndo(); }};
        window.unlikePost = async (postId) => {{ await sendMutation(); originalUnlike(postId); }};
    </script>"""


@pytest.mark.parametrize(
    "action_name, mutation",
    [("delete", "DeleteTweet"), ("undo", "DeleteRetweet"), ("unlike", "UnfavoriteTweet")],
)
@pytest.mark.parametrize(
    "status, body",
    [
        (200, '{"errors": [{"message": "Rate limit exceeded"}]}'),
        (500, '{"data": {}}'),
    ],
)
def test_action_reports_failed_mutation_response_even_when_dom_updates(
    local_page, monkeypatch, action_name: str, mutation: str, status: int, body: str
) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 2000)
    page, _ = local_page(_document_with_mutation(mutation))
    page.route(
        f"**/{mutation}",
        lambda route: route.fulfill(status=status, content_type="application/json", body=body),
    )

    success, error = _run_action(page, action_name)

    assert success is False
    assert error is not None and mutation in error
    if status >= 400:
        assert str(status) in error
    else:
        assert "Rate limit exceeded" in error


@pytest.mark.parametrize(
    "action_name, mutation",
    [("delete", "DeleteTweet"), ("undo", "DeleteRetweet"), ("unlike", "UnfavoriteTweet")],
)
def test_action_accepts_successful_mutation_response(
    local_page, monkeypatch, action_name: str, mutation: str
) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 2000)
    page, _ = local_page(_document_with_mutation(mutation))
    page.route(
        f"**/{mutation}",
        lambda route: route.fulfill(status=200, content_type="application/json", body='{"data": {}}'),
    )

    assert _run_action(page, action_name) == (True, None)


def test_unlike_ignores_quoted_post_like_control(local_page, monkeypatch) -> None:
    monkeypatch.setattr(x_deleter_actions, "ACTION_STATE_TIMEOUT_MS", 100)
    monkeypatch.setattr(x_deleter_actions, "NAVIGATION_TIMEOUT_MS", 100)
    article = _article(TARGET_ID).replace(
        '<button data-testid="unlike" onclick="unlikePost(\'123\')">undo like</button>',
        '<blockquote><button data-testid="unlike" onclick="unlikePost(\'quoted\')">quoted</button></blockquote>',
    )
    assert "quoted" in article
    page, _ = local_page(_document([article]))

    success, error = _run_action(page, "unlike")

    assert success is False
    assert error
    assert _action_log(page) == []


def test_likes_collection_keeps_other_authors_posts_as_likes(local_page) -> None:
    from dataclasses import replace

    liked = _collection_article(
        _collection_permalink("/someone/status/456", "2026-09-11T00:00:00.000Z")
        + _collection_text("liked post")
        + '<button data-testid="unlike" aria-label="7 Likes">liked</button>'
    )
    own = _collection_article(
        _collection_permalink("/target/status/123", "2026-09-10T00:00:00.000Z")
        + _collection_text("own post I liked")
        + '<button data-testid="unlike" aria-label="9 Likes">liked</button>'
    )
    below_minimum = _collection_article(
        _collection_permalink("/other/status/789", "2026-09-09T00:00:00.000Z")
        + '<button data-testid="unlike" aria-label="2 Likes">liked</button>'
    )
    page, _ = local_page(_document([liked, own, below_minimum]))
    page.goto("https://x.com/target/likes")
    collected: list[PostRecord] = []
    XDeleterCore()._process_articles(
        articles=page.locator(POST_ARTICLE_SELECTOR).all(),
        request=replace(_collection_request(), post_kind_filter="likes", min_likes=5),
        normalized_username="target",
        collected_posts=collected,
        seen_urls=set(),
    )

    assert [(post.url, post.author_username, post.kind, post.likes) for post in collected] == [
        ("https://x.com/someone/status/456", "someone", "like", 7),
        ("https://x.com/target/status/123", "target", "like", 9),
    ]
