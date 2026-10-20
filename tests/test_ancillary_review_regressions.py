"""Independent adversarial probes; in-memory doubles only, no browser/account/network."""
import json
import logging
from unittest.mock import Mock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from test_ancillary_isolation import BADGES, FLEETS, Route, emit_ancillary
from test_ancillary_isolation import isolated as isolated_fixture
from test_retry import ActionLocator, target

from kusamushiri.ancillary_isolation import isolatable_endpoint
from kusamushiri.browser import BrowserManager
from kusamushiri.models import RetryPolicy
from kusamushiri.progress import ActionJournal
from kusamushiri.retry import run_post_actions

isolated = isolated_fixture  # Reuse the in-memory context with a temporary durable journal.


@pytest.mark.parametrize('query', [
    'only_spaces=true&only_spaces=',
    'only_spaces=&only_spaces=true',
    'only_spaces=true&only_spaces',
    'only_spaces=true&cursor=x',
    'cursor=x&only_spaces=true',
    'only_spaces=true&only_spaces=true',
])
def test_blank_duplicate_query_is_not_known_spaces_only(query):
    assert isolatable_endpoint('https://x.com/i/api/fleets/v1/fleetline?' + query, 'GET') is None


def test_malformed_url_is_classified_conservatively_without_callback_failure():
    assert isolatable_endpoint('https://[x.com/i/api/graphql/hash/ViewerBadgeCounts', 'GET') is None


def test_quarantined_dependency_failure_in_later_item_stops_before_reloading(isolated, monkeypatch, tmp_path):
    core, page, context, clock, events = isolated
    navigations, waits = [], []

    def navigate(url, **kwargs):
        navigations.append(url)
        if url.endswith('/1'):
            emit_ancillary(isolated, BADGES, after='999')
        else:
            # The ancillary request is locally blocked; no second server 429 is emitted.
            context.handler(Route(BADGES))

    def missing_target(self, **kwargs):
        if navigations[-1].endswith('/2'):
            raise PlaywrightTimeoutError('synthetic missing required view')

    def wait(seconds):
        waits.append(seconds)
        clock.tick(seconds)
        return core.is_cancel_requested()

    page.goto = navigate
    monkeypatch.setattr(ActionLocator, 'wait_for', missing_target)
    journal = ActionJournal(tmp_path / 'action-progress.json', 'synthetic')
    run_post_actions(core, [target(1), target(2)], 0,
                     RetryPolicy(ancillary_rate_limit_mode='isolate-known', max_retries=2),
                     journal=journal, wait_for_cancel=wait)
    assert navigations.count(target(2).url) == 1, {'navigations': navigations, 'waits': waits}
    assert core._browser.ancillary_isolation.failed
    assert journal.cooldown_until >= clock.wall()


def test_offline_control_failure_still_sets_cancel_and_global_signal(isolated):
    core, page, context, clock, events = isolated
    with core.post_action_retry_scope():
        emit_ancillary(isolated, BADGES, after='999')
        context.set_offline = Mock(side_effect=RuntimeError('synthetic offline control failure'))
        with pytest.raises(RuntimeError, match='offline control failure'):
            context.handler(Route(BADGES, fail_abort=True))
        assert core.is_cancel_requested(), 'offline exception skipped global stop callback'
        assert core.rate_limit_status == 429


def test_missing_json_receipt_is_not_a_confirmed_success(isolated):
    core, page, context, clock, events = isolated
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated, BADGES)
    page.response.json.side_effect = ValueError('synthetic unreadable response')
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert not result.success and result.failure_kind == 'ambiguous'


@pytest.mark.parametrize('mode', ['global', 'isolate-known'])
def test_isolated_deadline_survives_restart_before_browser_launch(isolated, tmp_path, monkeypatch, mode):
    core, page, context, clock, events = isolated
    profile = tmp_path / 'chromium-profile-synthetic'
    journal = ActionJournal(profile / 'action-progress.json', 'synthetic')
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated, BADGES, after='999')
    run_post_actions(core, [target()], 0, RetryPolicy(ancillary_rate_limit_mode='isolate-known'), journal=journal)
    manager = BrowserManager()
    manager.block_service_workers = mode == 'isolate-known'
    launch = Mock(side_effect=AssertionError('browser launch prohibited'))
    monkeypatch.setattr('kusamushiri.browser.sync_playwright', launch)
    with pytest.raises(RuntimeError):
        manager.start(user_data_dir=profile)
    launch.assert_not_called()
    assert json.loads(journal.path.read_text())['cooldown_until'] == pytest.approx(clock.wall() + 999)


@pytest.mark.parametrize('first', ['known', 'mutation'])
def test_global_mutation_precedence_in_both_event_orders(isolated, first):
    core, page, context, clock, events = isolated
    with core.post_action_retry_scope():
        response = Mock(url='https://x.com/i/api/graphql/fixture/DeleteTweet', status=429,
                        headers={'retry-after': '1001'}, request=Mock(method='POST'))
        if first == 'mutation':
            core._browser._watch_response(response)
        emit_ancillary(isolated, BADGES, after='999')
        if first == 'known':
            core._browser._watch_response(response)
        assert core.rate_limit_scope == 'mutation' and core.rate_limit_status == 429
        assert core.rate_limit_retry_until == pytest.approx(clock.wall() + 1001)
        result = core.execute_post_action(target())
        assert result.deferred and not result.success
        assert not any('confirmationSheetConfirm' in selector for selector in page.clicks)


def test_duplicate_rows_do_not_repeat_after_isolated_429(isolated, tmp_path):
    core, page, context, clock, events = isolated
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated, BADGES, after='999')
    journal = ActionJournal(tmp_path / 'action-progress.json', 'synthetic')
    results = run_post_actions(core, [target(), target()], 0,
                               RetryPolicy(ancillary_rate_limit_mode='isolate-known'),
                               journal=journal, wait_for_cancel=lambda seconds: False)
    assert all(result.success for result in results)
    assert [result.attempts for result in results] == [1, 0]
    assert sum('confirmationSheetConfirm' in selector for selector in page.clicks) == 1


def test_deadline_is_durable_before_the_next_item(isolated, tmp_path):
    core, page, context, clock, events = isolated
    journal = ActionJournal(tmp_path / 'action-progress.json', 'synthetic')
    durable_at_second_item = []
    execute = core.execute_post_action

    def perform(item):
        if item == target(2):
            durable_at_second_item.append(json.loads(journal.path.read_text())['cooldown_until'])
        return execute(item)

    page.goto = lambda *args, **kwargs: emit_ancillary(isolated, BADGES, after='999') if args[0].endswith('/1') else None
    core.execute_post_action = perform
    run_post_actions(core, [target(1), target(2)], 0,
                     RetryPolicy(ancillary_rate_limit_mode='isolate-known'), journal=journal,
                     wait_for_cancel=lambda seconds: False)
    assert len(durable_at_second_item) == 1 and durable_at_second_item[0] > clock.wall() + 998
    assert journal.cooldown_until > clock.wall()


def test_unknown_response_metadata_retains_global_gate(isolated):
    core, page, context, clock, events = isolated
    response = Mock(url=BADGES, status=429, headers={'retry-after': '999'})
    response.request = None
    with core.post_action_retry_scope():
        core._browser._watch_response(response)
        result = core.execute_post_action(target())
        assert result.deferred and not result.success
        assert core.rate_limit_status == 429


def test_diagnostic_unknown_response_has_no_invented_origin(isolated):
    from test_network_diagnostics import Request, Response, by_event
    core, page, context, clock, events = isolated
    response = Response(Request(clock, BADGES), after='999')
    with core.post_action_retry_scope():
        core._browser._watch_response(response)
    event = by_event(events, 'response')[0]
    assert event['request'] is None and event['origin_operation'] is None
    assert event['attribution'] == 'unmatched'


def test_new_quarantine_messages_and_diagnostics_do_not_leak_url_or_secret(isolated, caplog):
    core, page, context, clock, events = isolated
    canary = 'PRIVATE_REVIEW_CANARY'
    url = FLEETS + '&auth=' + canary
    with core.post_action_retry_scope():
        emit_ancillary(isolated, url, after='999')
        context.handler(Route(url))
    text = '\n'.join(record.getMessage() for record in caplog.records)
    assert canary not in text and url not in text
    assert canary not in json.dumps(events) and url not in json.dumps(events)


def test_later_missing_control_stops_without_retry_and_keeps_item_pending(isolated, tmp_path):
    core, page, context, clock, events = isolated
    navigations, waits = [], []

    def navigate(url, **kwargs):
        navigations.append(url)
        if url.endswith('/1'):
            emit_ancillary(isolated, BADGES, after='999')
        else:
            context.handler(Route(BADGES))

    core._find_first_visible_locator = lambda owner, selectors: owner.locator(selectors[0]) if navigations[-1].endswith('/1') else None
    page.goto = navigate
    journal = ActionJournal(tmp_path / 'action-progress.json', 'synthetic')
    results = run_post_actions(core, [target(1), target(2)], 0,
                              RetryPolicy(ancillary_rate_limit_mode='isolate-known'), journal=journal,
                              wait_for_cancel=lambda seconds: waits.append(seconds) or False)
    assert len(results) == 1 and results[0].success
    assert navigations.count(target(2).url) == 1 and waits == [5.0]
    assert core.is_cancel_requested() and core._browser.ancillary_isolation.failed
    assert journal.entries[journal.key(target(2))] == 'pending'
    assert sum('confirmationSheetConfirm' in selector for selector in page.clicks) == 1


def test_deadline_is_saved_before_the_same_items_irreversible_click(isolated, tmp_path):
    core, page, context, clock, events = isolated
    journal = ActionJournal(tmp_path / 'action-progress.json', 'synthetic')
    observed = []

    def navigate(*args, **kwargs):
        emit_ancillary(isolated, BADGES, after='999')
        saved = json.loads(journal.path.read_text())
        observed.append(saved['cooldown_until'])
        assert saved['entries'][journal.key(target())] == 'inflight'
        assert not any('confirmationSheetConfirm' in selector for selector in page.clicks)

    page.goto = navigate
    result = run_post_actions(core, [target()], 0, RetryPolicy(ancillary_rate_limit_mode='isolate-known'), journal=journal)
    assert result[0].success and observed[0] > clock.wall() + 998


def test_later_longer_deadline_is_immediately_durable(isolated, tmp_path):
    core, page, context, clock, events = isolated
    journal = ActionJournal(tmp_path / 'extension.json', 'synthetic')
    with journal:
        journal.arm_isolation_storage()
        core._browser.ancillary_isolation.persist_deadline = journal.persist_isolated_deadline
        with core.post_action_retry_scope():
            emit_ancillary(isolated, BADGES, after='999')
            clock.tick(1)
            emit_ancillary(isolated, BADGES, after='2000')
            assert json.loads(journal.path.read_text())['cooldown_until'] == clock.wall() + 2000


def test_abort_offline_and_close_failure_cannot_resume_actions_or_leak_errors(isolated, caplog):
    core, page, context, clock, events = isolated
    canary = 'PRIVATE_CONTROL_FAILURE_CANARY'
    with core.post_action_retry_scope():
        emit_ancillary(isolated, BADGES, after='999')
        context.set_offline = Mock(side_effect=RuntimeError(canary))
        context.close = Mock(side_effect=RuntimeError(canary))
        with pytest.raises(RuntimeError, match='offline control failure') as captured:
            context.handler(Route(BADGES, fail_abort=True))
        assert core.is_cancel_requested() and core.rate_limit_status == 429
        assert core.rate_limit_retry_until >= clock.wall() + 999
        assert not core._browser.network_suspended  # Offline control never succeeded.
        core.clear_cancel()
        with pytest.raises(RuntimeError, match='actions are disabled'):
            core.execute_post_action(target(2))
        with pytest.raises(RuntimeError, match='network control failed'):
            core._browser.resume_action_network()
        assert canary not in str(captured.value)
    assert canary not in '\n'.join(record.getMessage() for record in caplog.records)
    context.close.assert_called_once()


@pytest.mark.parametrize('failure', ['write', 'directory_sync'])
def test_deadline_storage_failure_retains_durable_lock_and_blocks_restart(isolated, tmp_path, monkeypatch, caplog, failure):
    core, page, context, clock, events = isolated
    canary = 'PRIVATE_STORAGE_FAILURE_CANARY'
    profile = tmp_path / 'chromium-profile-synthetic'
    journal = ActionJournal(profile / 'action-progress.json', 'synthetic')
    method = journal._write if failure == 'write' else journal._sync_directory

    def fault_after_observation():
        if journal.cooldown_until:
            raise OSError(canary)
        return method()

    monkeypatch.setattr(journal, '_write' if failure == 'write' else '_sync_directory', fault_after_observation)
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated, BADGES, after='999')
    with pytest.raises(RuntimeError, match='action deadline') as captured:
        run_post_actions(core, [target(), target(2)], 0,
                         RetryPolicy(ancillary_rate_limit_mode='isolate-known'), journal=journal)
    assert core.is_cancel_requested() and core.rate_limit_status == 429
    assert core._browser.network_suspended and journal._lock_path.exists()
    assert not any('confirmationSheetConfirm' in selector for selector in page.clicks)
    launch = Mock(side_effect=AssertionError('no browser launch'))
    monkeypatch.setattr('kusamushiri.browser.sync_playwright', launch)
    with pytest.raises(RuntimeError, match='lock exists'):
        BrowserManager().start(user_data_dir=profile)
    launch.assert_not_called()
    assert canary not in str(captured.value)
    assert canary not in '\n'.join(record.getMessage() for record in caplog.records)


def test_unavailable_storage_before_scope_starts_no_mutation(isolated, tmp_path, monkeypatch, caplog):
    core, page, context, clock, events = isolated
    journal = ActionJournal(tmp_path / 'preflight.json', 'synthetic')
    monkeypatch.setattr(journal, '_sync_directory', Mock(side_effect=OSError('PRIVATE_PREFLIGHT_CANARY')))
    with pytest.raises(RuntimeError, match='Action batch stopped') as captured:
        run_post_actions(core, [target()], 0, RetryPolicy(ancillary_rate_limit_mode='isolate-known'), journal=journal)
    assert core.is_cancel_requested() and not page.clicks and journal._lock_path.exists()
    assert 'PRIVATE_PREFLIGHT_CANARY' not in str(captured.value)


def test_without_durable_journal_opt_in_reverts_to_global_gate(isolated):
    core, page, context, clock, events = isolated
    core._browser.ancillary_isolation.persist_deadline = None
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated, BADGES)
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert result.deferred and not result.success and core.rate_limit_status == 429
    assert not core.isolated_rate_limit_seen


@pytest.mark.parametrize('mode', ['global', 'isolate-known'])
@pytest.mark.parametrize('body', ['unreadable', None, [], 'invalid', 42])
def test_unverifiable_body_is_ambiguous_and_never_retried(isolated, tmp_path, caplog, mode, body):
    core, page, context, clock, events = isolated
    canary = 'PRIVATE_RECEIPT_DECODE_CANARY'
    caplog.set_level(logging.DEBUG, logger='kusamushiri')
    if mode == 'isolate-known':
        page.goto = lambda *args, **kwargs: emit_ancillary(isolated, BADGES)
    if body == 'unreadable':
        page.response.json.side_effect = ValueError(canary)
    else:
        page.response.json.return_value = body
    journal = ActionJournal(tmp_path / 'receipt.json', 'synthetic')
    results = run_post_actions(core, [target(), target()], 0,
                               RetryPolicy(ancillary_rate_limit_mode=mode), journal=journal)
    assert len(results) == 1 and results[0].failure_kind == 'ambiguous' and not results[0].success
    assert journal.previous_result(target()).failure_kind == 'ambiguous'
    assert sum('confirmationSheetConfirm' in selector for selector in page.clicks) == 1
    assert canary not in '\n'.join(record.getMessage() for record in caplog.records)


def test_malformed_routing_input_remains_unknown_and_does_not_raise(isolated):
    core, page, context, clock, events = isolated
    url = 'https://[x.com/i/api/graphql/hash/ViewerBadgeCounts'
    assert not context.matcher(url)
    route = Route(url)
    context.handler(route)
    assert route.continued and not route.aborted


def test_quarantine_during_a_pause_keeps_its_deadline_on_disk(isolated, tmp_path):
    core, page, context, clock, events = isolated
    journal = ActionJournal(tmp_path / 'action-progress.json', 'synthetic')
    durable_at_second_item = []
    second_item_times = []
    execute = core.execute_post_action
    emitted = []

    def perform(item):
        if item == target(2):
            durable_at_second_item.append(json.loads(journal.path.read_text())['cooldown_until'])
            second_item_times.append(clock.wall())
        return execute(item)

    def wait(seconds):
        if not emitted:
            # An ancillary 429 dispatched while the proactive pause is waiting.
            emitted.append(True)
            emit_ancillary(isolated, BADGES, after='999')
        clock.tick(seconds)
        return core.is_cancel_requested()

    page.goto = lambda *args, **kwargs: None
    core.execute_post_action = perform
    run_post_actions(core, [target(1), target(2)], 0,
                     RetryPolicy(ancillary_rate_limit_mode='isolate-known', batch_size=1, batch_pause_seconds=30.0),
                     journal=journal, wait_for_cancel=wait)
    assert emitted and core._browser.ancillary_isolation.deadlines
    assert len(durable_at_second_item) == 1
    deadline = core._browser.ancillary_isolation.deadline
    assert second_item_times[0] >= deadline  # extension is fully waited before resuming
    assert durable_at_second_item[0] == 0 or durable_at_second_item[0] >= deadline
