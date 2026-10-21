"""Explicit zero delays and optional jitter; no browser or network."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_retry import FakeCore, target

from kusamushiri import cli
from kusamushiri.core import XDeleterCore
from kusamushiri.models import PostActionResult, RetryPolicy
from kusamushiri.progress import ActionJournal
from kusamushiri.recipe import format_recipe, load_recipe
from kusamushiri.retry import run_post_actions


def zero_policy(**overrides):
    return replace(RetryPolicy(initial_delay_seconds=0, cooldown_delay_seconds=0,
                               batch_pause_seconds=0, rate_limit_interval_seconds=0), **overrides)


def test_zero_disables_fixed_batch_and_post_limit_delays(monkeypatch):
    core = FakeCore(monkeypatch)
    core.isolated_rate_limit_seen = True
    results = run_post_actions(core, [target(i) for i in range(30)], 0, zero_policy(batch_size=1))
    assert len(results) == 30 and all(r.success for r in results) and core.waits == []


@pytest.mark.parametrize("after", [None, 0, 5])
def test_zero_cooldown_stops_without_server_deadline(monkeypatch, after):
    core = FakeCore(monkeypatch)
    def action(item):
        if len(core.calls) == 1:
            core.rate_limit_status = 429
            core.rate_limit_retry_after = after
            return PostActionResult(item, "delete", False, "limit", deferred=True, retry_after_seconds=after)
        return PostActionResult(item, "delete", True, None)
    core.behavior = action
    results = run_post_actions(core, [target(1), target(2)], 0, zero_policy())
    assert [seconds for _, seconds in core.waits] == ([5] if after else [])
    if after:
        assert len(results) == 2 and all(r.success for r in results)
    else:
        assert len(core.calls) == 1 and core.cancelled


@pytest.mark.parametrize("after", [None, 3])
def test_zero_backoff_stops_or_obeys_server_retry(monkeypatch, after):
    core = FakeCore(monkeypatch)
    core.behavior = lambda item: (
        PostActionResult(item, "delete", False, "retry", failure_kind="retryable", retry_after_seconds=after)
        if len(core.calls) == 1 else PostActionResult(item, "delete", True, None))
    run_post_actions(core, [target()], 0, zero_policy())
    assert len(core.calls) == (2 if after else 1)
    assert [seconds for _, seconds in core.waits] == ([3] if after else [])


def test_jitter_adds_to_interval_and_respects_post_limit_floor(monkeypatch):
    core = FakeCore(monkeypatch)
    jitter = Mock(return_value=2)
    monkeypatch.setattr("kusamushiri.retry.random.uniform", jitter)
    run_post_actions(core, [target(1), target(2)], 3, zero_policy(interval_jitter_seconds=4))
    assert core.waits == [(1, 5)]
    jitter.assert_called_with(0, 4)
    core = FakeCore(monkeypatch)
    core.isolated_rate_limit_seen = True
    run_post_actions(core, [target(1), target(2)], 0, zero_policy(rate_limit_interval_seconds=7, interval_jitter_seconds=4))
    assert core.waits == [(1, 7)]


def test_cancel_and_resume_keep_server_deadline(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    def action(item):
        core.rate_limit_status = 429
        core.rate_limit_retry_until = core.clock + 60
        return PostActionResult(item, "delete", True, None)
    core.behavior = action
    core.cancel_on_wait = True
    run_post_actions(core, [target(1), target(2)], 0, zero_policy(), journal=journal)
    assert journal.cooldown_until == 1060 and journal.entries["post:1"] == "success"
    core.cancelled = core.cancel_on_wait = False
    core.rate_limit_status = None
    core.behavior = lambda item: PostActionResult(item, "delete", True, None)
    run_post_actions(core, [target(1), target(2)], 0, zero_policy(), journal=journal)
    assert [seconds for _, seconds in core.waits] == [60, 55]
    assert core.calls == [target(1), target(2)]


@pytest.mark.parametrize("headers, expected", [({}, 1000), ({"retry-after": "4"}, 1004),
                                               ({"x-rate-limit-reset": "1009"}, 1009)])
def test_core_persists_only_server_delay_when_fallback_zero(monkeypatch, headers, expected):
    monkeypatch.setattr("kusamushiri.core.time.time", lambda: 1000)
    core = XDeleterCore()
    core.prepare_post_action_policy(zero_policy())
    response = Mock(status=429, headers=headers)
    response.url = "https://x.com/unclassified"
    response.request.method = "GET"
    core._action_journal = Mock()
    core._browser.ancillary_isolation.quarantine = Mock(return_value=False)
    core._stop_on_rate_limit(response)
    core._action_journal.persist_isolated_deadline.assert_called_once_with(expected)
    assert core._browser.ancillary_isolation.quarantine.called is bool(headers)


def test_zero_recipe_and_cli_values_round_trip(tmp_path):
    path = tmp_path / "trial.toml"
    path.write_text('[delete]\ninterval=0\nretry_delay=0\nretry_max_delay=0\nretry_max_wait=0\n'
                    'batch_pause=0\ncooldown_delay=0\nrate_limit_interval=0\ninterval_jitter=0\n')
    recipe = load_recipe(path)
    assert recipe.delete is not None and recipe.delete.interval == 0
    configured = recipe.delete.retry_policy
    assert configured.max_wait_seconds == configured.max_delay_seconds == configured.initial_delay_seconds == 0
    path.write_text(format_recipe(recipe))
    assert load_recipe(path).delete == recipe.delete
    args = cli.build_parser().parse_args(["resume", "fixture.csv", "--interval", "0", "--retry-delay", "0",
                                         "--cooldown-delay", "0", "--batch-pause", "0", "--interval-jitter", "0"])
    parsed = cli._retry_policy_from_args(args)
    assert parsed.initial_delay_seconds == parsed.cooldown_delay_seconds == parsed.batch_pause_seconds == 0
    assert cli._needs_action_context(parsed)


def test_zero_wait_cap_stops_without_shortening_server_deadline(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    core.rate_limit_status = 429
    core.rate_limit_retry_until = 1060
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    run_post_actions(core, [target()], 0, zero_policy(max_wait_seconds=0), journal=journal)
    assert core.calls == core.waits == [] and journal.cooldown_until == 1060


def test_zero_local_cap_disables_backoff_but_not_server_wait():
    configured = RetryPolicy(max_delay_seconds=0)
    configured.validate()
    assert configured.delay(1) == configured.cooldown_delay(1) == 0
    assert configured.delay(1, 10) == configured.cooldown_delay(1, 10) == 10


def test_run_zero_override_does_not_replace_with_recipe_default(monkeypatch, tmp_path):
    path = tmp_path / "trial.toml"
    path.write_text('[delete]\ninterval=8\ninterval_jitter=2\ncooldown_delay=120\n')
    captured = []
    monkeypatch.setattr(cli, "run_recipe", lambda recipe, **kwargs: captured.append(recipe) or 0)
    assert cli.main(["run", str(path), "--interval", "0", "--interval-jitter", "0", "--cooldown-delay", "0"]) == 0
    assert captured[0].delete.interval == captured[0].delete.retry_policy.interval_jitter_seconds == 0
    assert captured[0].delete.retry_policy.cooldown_delay_seconds == 0
    assert 'interval=8' in path.read_text()


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf")])
def test_jitter_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        RetryPolicy(interval_jitter_seconds=value).validate()
