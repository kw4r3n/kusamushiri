# Verified single-post delete

Deleting exactly one of your own posts runs through an extra identity check before anything is clicked. The examples use the placeholder URL `https://x.com/author/status/123`.

## What it does

- **GUI, current view** – enter the post URL in the target field and press *Delete this one displayed post…*. No collection or table is needed. The dialog shows the URL and one item, defaults to No, and nothing is sent before Yes. The expected viewer is the account the GUI confirmed at login, and it must also match the URL author. The post must already be displayed in the browser tab. The tool does not navigate, reload or reopen it, and stops if the post is not there.
- **GUI, table** – a table selection of one post uses the same checks.
- **CLI** – `delete` with a list of one `post` item uses the same checks. `--expected-viewer NAME` sets the expected account, and the default is the URL author. A value that differs from the URL author is rejected before the browser starts. The CLI uses its existing owned navigation, opens the post, then verifies the loaded page. It does not attach to a running GUI session. `--dry-run` still opens no browser and no journal.
- **Target check** – read-only. It checks that the displayed post and the account match the request.
- **Menu diagnostic** – never deletes. An already open menu is read with zero clicks. If no menu is open, it clicks the post's caret once and then reads the menu.

## Guards

Immediately before each click, the same browser session, command thread, page, main frame, article, target post and viewer are checked again. The sequence is fixed:

1. the caret of the owned article,
2. one visible `Dropdown`,
3. one exact delete item in that menu,
4. one confirmation control.

The chosen element, its root and its visible dialog ancestors are pinned. The click goes to that element handle, not to a re-resolved selector. Any duplicate, nested, foreign or replaced control, or any change of dialog, stops the action. A prepared click and a scope are single-use. There is no automatic retry or cooldown, so `max_retries` and `max_cooldowns` are forced to 0. The soft budget is 30 seconds. It is not a hard process limit. Cancel, rate limits and holds fail closed.

Results go through the existing `ActionJournal`. `ambiguous` (unknown outcome), `inflight`, `success` and `blocked` entries are never replayed automatically. The GUI retry only offers `retryable` failures. Do not work around an ambiguous result by rerunning or deleting the record. Check the real state on X first.

## Delete item labels

The explicit delete test id is always accepted. Otherwise a menu item matches only when its complete text is `Delete`, `削除` or `삭제`. A prefix, a suffix, or a direct exact text with extra child text does not match. Selection, the verified path and the diagnostic share one candidate builder, and a count other than one stops before any selection.

## Diagnostic output

The result is a closed, bounded structure: fixed enum values, counts, booleans and at most 48 rows, with `truncated` and `stable` flags. It contains no labels, post text, links, HTML, account names or authentication data. The GUI shows details that can be copied.

## Limits

- The checks are DOM identity checks. They do not prove minimal traffic or permission to delete, and no live verification is claimed.
- Do not use the original browser tab while a check or action runs.
- The soft budget and the final click are not atomic.
