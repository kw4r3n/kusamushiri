import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from kusamushiri import cli
from kusamushiri.browser import BrowserManager
from kusamushiri.deleted_posts import DeletedPostStore
from kusamushiri.exporting import write_post_list
from kusamushiri.follows import FollowRecord, write_follow_list
from kusamushiri.last_posts import LastPostResult, LastPostStore
from kusamushiri.models import PostActionResult, PostActionTarget, PostRecord


def make_post(post_id: str, kind: str = "post") -> PostRecord:
    return PostRecord(
        id=post_id,
        url=f"https://x.com/alice/status/{post_id}",
        author_username="alice",
        text="=1+1 hello",
        date="2024-01-02",
        likes=3,
        replies=0,
        has_media=False,
        is_reply=False,
        kind=kind,  # type: ignore[arg-type]
    )


class FakeCore:
    """Stands in for XDeleterCore so no browser starts."""

    instances: list["FakeCore"] = []

    def __init__(self, *, headless: bool = False) -> None:
        self.headless = headless
        self.logged_in = True
        self.cancelled = False
        self.collection_limit_reached = False
        self.started_profile: str | None = None
        self.stopped = False
        self.actions: list[PostActionTarget] = []
        self.failing_urls: set[str] = set()
        self.unfollowed: list[str] = []
        FakeCore.instances.append(self)

    def start_browser(self, user_data_dir=None, account_name=None) -> None:
        self.started_profile = account_name

    def go_to_home(self) -> None:
        pass

    def is_logged_in(self) -> bool:
        return self.logged_in

    def get_logged_in_username(self) -> str | None:
        return "alice"

    def stop_browser(self) -> None:
        self.stopped = True

    def request_cancel(self) -> None:
        self.cancelled = True

    def is_cancel_requested(self) -> bool:
        return self.cancelled

    def wait_for_cancel(self, seconds: float) -> bool:
        return self.cancelled

    def search_and_collect_posts(self, request, on_progress=None) -> list[PostRecord]:
        self.request = request
        return [make_post("1"), make_post("2", "repost")]

    def execute_post_action(self, target: PostActionTarget) -> PostActionResult:
        self.actions.append(target)
        success = target.url not in self.failing_urls
        return PostActionResult(target, "delete", success, None if success else "boom")

    def unfollow_account(self, username: str) -> tuple[bool, str | None]:
        self.unfollowed.append(username)
        return True, None

    def fetch_last_post(self, username: str) -> LastPostResult:
        return LastPostResult(username, "2026-10-01T00:00:00+00:00", "2026-09-30T00:00:00+00:00")


@pytest.fixture
def fake_core(monkeypatch, tmp_path) -> type[FakeCore]:
    FakeCore.instances = []
    monkeypatch.setattr(cli, "XDeleterCore", FakeCore)
    monkeypatch.setattr(cli, "LOGIN_POLL_SECONDS", 0)
    monkeypatch.setattr(cli.DeletedPostStore, "for_account", lambda _name: DeletedPostStore(tmp_path / "deleted.json"))
    monkeypatch.setattr(cli.LastPostStore, "for_account", lambda _name: LastPostStore(tmp_path / "last.json"))
    return FakeCore


def parse(*argv: str):
    return cli.build_parser().parse_args(argv)


def test_collect_request_forces_profile_mode_for_likes() -> None:
    args = parse("collect", "-o", "out.json", "--kind", "likes", "--mode", "search", "--include", "a, b,a")

    request = cli.build_collect_request(args, "@alice")

    assert request.username == "alice"
    assert request.search_mode == "profile"
    assert request.include_keywords == ("a", "b")
    request.validate()


def test_output_must_be_csv_or_json(capsys) -> None:
    with pytest.raises(SystemExit):
        parse("collect", "-o", "out.txt")
    assert "csv or .json" in capsys.readouterr().err


@pytest.mark.parametrize("suffix", [".json", ".csv"])
def test_post_file_round_trips_into_targets(tmp_path, suffix) -> None:
    path = tmp_path / f"posts{suffix}"
    write_post_list(path, [make_post("1"), make_post("2", "like")])

    targets = cli.post_targets_from_rows(cli.read_rows(path))

    assert targets == [
        PostActionTarget("https://x.com/alice/status/1", "post"),
        PostActionTarget("https://x.com/alice/status/2", "like"),
    ]


def test_post_rows_reject_unknown_kind() -> None:
    with pytest.raises(cli.CliError, match="row 1"):
        cli.post_targets_from_rows([{"url": "https://x.com/a/status/1", "kind": "quote"}])


@pytest.mark.parametrize("suffix", [".json", ".csv"])
def test_follow_file_round_trips_into_records(tmp_path, suffix) -> None:
    path = tmp_path / f"follows{suffix}"
    write_follow_list(path, [FollowRecord("bob", "Bob", "https://x.com/bob", True)])

    records = cli.follow_records_from_rows(cli.read_rows(path))

    assert records == [FollowRecord("bob", "Bob", "https://x.com/bob", True)]


def test_collect_writes_posts_and_passes_headless(fake_core, tmp_path) -> None:
    output = tmp_path / "posts.json"

    code = cli.main(["collect", "--headless", "--profile", "work", "--since", "2024-01-01", "-o", str(output)])

    assert code == 0
    core = fake_core.instances[0]
    assert core.headless is True
    assert core.started_profile == "work"
    assert core.request.username == "alice"  # falls back to the logged-in account
    assert core.stopped
    assert [row["id"] for row in json.loads(output.read_text(encoding="utf-8"))] == ["1", "2"]


def test_commands_fail_when_not_logged_in(fake_core, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "LOGIN_SETTLE_SECONDS", 0)
    monkeypatch.setattr(FakeCore, "is_logged_in", lambda self: False)

    code = cli.main(["collect", "--profile", "work", "-o", str(tmp_path / "posts.json")])

    assert code == cli.EXIT_FAILURE
    assert "login --profile work" in capsys.readouterr().err
    assert fake_core.instances[0].stopped
    assert not (tmp_path / "posts.json").exists()


def test_login_is_always_headed(fake_core) -> None:
    assert cli.main(["login", "--timeout", "0"]) == 0
    assert fake_core.instances[0].headless is False


def test_login_timeout_reports_the_timeout(fake_core, monkeypatch, capsys) -> None:
    monkeypatch.setattr(FakeCore, "is_logged_in", lambda self: False)

    assert cli.main(["login", "--timeout", "0"]) == cli.EXIT_FAILURE
    assert "0 秒以内" in capsys.readouterr().err


def test_unexpected_errors_print_one_line(fake_core, tmp_path, monkeypatch, capsys) -> None:
    def fail(self, request, on_progress=None):
        raise RuntimeError("page closed")

    monkeypatch.setattr(FakeCore, "search_and_collect_posts", fail)

    assert cli.main(["collect", "-o", str(tmp_path / "posts.json")]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert "page closed" in err
    assert "Traceback" not in err
    assert fake_core.instances[0].stopped


def test_delete_dry_run_never_starts_browser(fake_core, tmp_path, capsys) -> None:
    path = tmp_path / "posts.json"
    write_post_list(path, [make_post("1")])

    assert cli.main(["delete", str(path), "--dry-run"]) == 0

    assert fake_core.instances == []
    assert "https://x.com/alice/status/1" in capsys.readouterr().out


def test_delete_requires_confirmation_without_tty(fake_core, tmp_path, monkeypatch) -> None:
    path = tmp_path / "posts.json"
    write_post_list(path, [make_post("1")])
    monkeypatch.setattr("sys.stdin", Mock(isatty=lambda: False))

    assert cli.main(["delete", str(path)]) == cli.EXIT_USAGE
    assert fake_core.instances == []


def test_delete_records_deleted_posts_and_writes_failures(fake_core, tmp_path, monkeypatch) -> None:
    path = tmp_path / "posts.csv"
    failed = tmp_path / "failed.json"
    write_post_list(path, [make_post("1"), make_post("2"), make_post("3", "repost")])
    real_init = FakeCore.__init__

    def init(self, *, headless=False):
        real_init(self, headless=headless)
        self.failing_urls = {"https://x.com/alice/status/2"}

    monkeypatch.setattr(FakeCore, "__init__", init)

    code = cli.main(["delete", str(path), "--yes", "--interval", "0", "--failed-output", str(failed)])

    assert code == cli.EXIT_FAILURE
    assert len(fake_core.instances[0].actions) == 3
    # Only successfully deleted original posts are remembered for archive imports.
    assert DeletedPostStore(tmp_path / "deleted.json").load() == {"1"}
    assert [row["id"] for row in json.loads(failed.read_text(encoding="utf-8"))] == ["2"]


def test_delete_stops_when_cancelled_during_interval(fake_core, tmp_path, monkeypatch) -> None:
    path = tmp_path / "posts.json"
    write_post_list(path, [make_post("1"), make_post("2")])

    def wait_then_cancel(self, seconds):
        self.cancelled = True
        return True

    monkeypatch.setattr(FakeCore, "wait_for_cancel", wait_then_cancel)

    code = cli.main(["delete", str(path), "--yes"])

    assert code == cli.EXIT_INTERRUPTED
    assert len(fake_core.instances[0].actions) == 1


def test_unfollow_skips_mutual_follows(fake_core, tmp_path) -> None:
    path = tmp_path / "follows.json"
    write_follow_list(
        path,
        [FollowRecord("bob", "Bob", "https://x.com/bob", True), FollowRecord("carol", "", "https://x.com/carol", False)],
    )

    assert cli.main(["unfollow", str(path), "--skip-mutual", "--yes", "--interval", "0"]) == 0
    assert fake_core.instances[0].unfollowed == ["carol"]


def test_last_posts_records_and_writes_dates(fake_core, tmp_path) -> None:
    path = tmp_path / "follows.json"
    output = tmp_path / "dated.csv"
    write_follow_list(path, [FollowRecord("Bob", "Bob", "https://x.com/bob", False)])

    assert cli.main(["last-posts", str(path), "--interval", "0", "-o", str(output)]) == 0

    assert LastPostStore(tmp_path / "last.json").load()["bob"].last_post_at == "2026-09-30T00:00:00+00:00"
    assert "2026-09-30T00:00:00+00:00" in output.read_text(encoding="utf-8-sig")


def test_archive_selects_posts_without_browser(fake_core, tmp_path) -> None:
    tweets = tmp_path / "tweets.js"
    entries = [
        {"tweet": {"id_str": str(post_id), "created_at": created, "full_text": text, "favorite_count": "0"}}
        for post_id, created, text in [
            (1, "Wed Oct 10 20:19:24 +0000 2018", "old keep"),
            (2, "Wed Oct 10 20:19:24 +0000 2019", "skip me"),
            (3, "Wed Oct 10 20:19:24 +0000 2020", "new keep"),
        ]
    ]
    tweets.write_text(f"window.YTD.tweets.part0 = {json.dumps(entries)}", encoding="utf-8")
    DeletedPostStore(tmp_path / "deleted.json").record(["3"])
    output = tmp_path / "selected.json"

    code = cli.main(["archive", str(tweets), "--user", "alice", "--include", "keep", "--oldest-first", "-o", str(output)])

    assert code == 0
    assert fake_core.instances == []
    assert [row["id"] for row in json.loads(output.read_text(encoding="utf-8"))] == ["1"]


def test_browser_manager_launches_headless(monkeypatch, tmp_path: Path) -> None:
    starter = Mock()
    context = starter.start.return_value.chromium.launch_persistent_context.return_value
    context.pages = [Mock()]
    monkeypatch.setattr("kusamushiri.browser.sync_playwright", lambda: starter)

    BrowserManager(headless=True).start(tmp_path)

    launch = starter.start.return_value.chromium.launch_persistent_context
    assert launch.call_args.kwargs["headless"] is True


class SignInCore(FakeCore):
    """Signed out until a window opens, as on a fresh profile."""

    signed_in = False

    def is_logged_in(self) -> bool:
        if not self.headless:
            SignInCore.signed_in = True
        return SignInCore.signed_in


def write_recipe(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kusamushiri.toml"
    path.write_text('profile = "work"\noutput_dir = "lists"\n' + text, encoding="utf-8")
    return path


def interactive(monkeypatch, answers: list[str] | None = None, on_ask=None) -> list[str]:
    prompts: list[str] = []
    replies = iter(answers or [])

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        if on_ask is not None:
            on_ask()
        return next(replies)

    monkeypatch.setattr("sys.stdin", Mock(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", ask)
    return prompts


def test_run_deletes_only_rows_left_in_the_edited_list(fake_core, tmp_path, monkeypatch) -> None:
    recipe = write_recipe(tmp_path, "[collect]\nkind = 'all'\n[delete]\ninterval = 0\n")

    def drop_repost_row() -> None:
        (list_path,) = (tmp_path / "lists").glob("posts-*.csv")
        lines = list_path.read_text(encoding="utf-8-sig").splitlines()
        list_path.write_text("\n".join(line for line in lines if ",repost," not in line) + "\n", encoding="utf-8-sig")

    prompts = interactive(monkeypatch, ["y"], on_ask=drop_repost_row)

    assert cli.main(["run", str(recipe)]) == 0

    core = fake_core.instances[0]
    assert core.started_profile == "work"
    assert core.actions == [PostActionTarget("https://x.com/alice/status/1", "post")]
    assert len(prompts) == 1
    assert DeletedPostStore(tmp_path / "deleted.json").load() == {"1"}


def test_run_without_delete_table_only_writes_the_list(fake_core, tmp_path, monkeypatch) -> None:
    prompts = interactive(monkeypatch)

    assert cli.main(["run", str(write_recipe(tmp_path, ""))]) == 0

    assert prompts == []
    assert fake_core.instances[0].actions == []
    assert len(list((tmp_path / "lists").glob("posts-*.csv"))) == 1


def test_run_with_confirm_false_needs_no_terminal(fake_core, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", Mock(isatty=lambda: False))
    recipe = write_recipe(tmp_path, "headless = true\n[delete]\ninterval = 0\nconfirm = false\n")

    assert cli.main(["run", str(recipe)]) == 0

    assert fake_core.instances[0].headless is True
    assert len(fake_core.instances[0].actions) == 2


def test_run_refuses_confirmation_without_terminal(fake_core, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", Mock(isatty=lambda: False))

    assert cli.main(["run", str(write_recipe(tmp_path, "[delete]\n"))]) == cli.EXIT_USAGE
    assert fake_core.instances == []


def test_run_answering_no_deletes_nothing(fake_core, tmp_path, monkeypatch) -> None:
    interactive(monkeypatch, [""])

    assert cli.main(["run", str(write_recipe(tmp_path, "[delete]\n"))]) == 0
    assert fake_core.instances[0].actions == []


def test_run_signs_in_with_a_window_before_running_headless(fake_core, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "XDeleterCore", SignInCore)
    monkeypatch.setattr(cli, "LOGIN_SETTLE_SECONDS", 0)
    SignInCore.signed_in = False
    interactive(monkeypatch)

    assert cli.main(["run", str(write_recipe(tmp_path, "headless = true\n"))]) == 0

    assert [core.headless for core in fake_core.instances] == [True, False, True]
    assert all(core.stopped for core in fake_core.instances)


def test_run_reports_invalid_recipe(fake_core, tmp_path, capsys) -> None:
    assert cli.main(["run", str(write_recipe(tmp_path, "[collect]\nexlude = []\n"))]) == cli.EXIT_USAGE
    assert "collect.exlude" in capsys.readouterr().err


def test_no_arguments_without_terminal_prints_help(fake_core, monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", Mock(isatty=lambda: False))

    assert cli.main([]) == cli.EXIT_USAGE
    assert "usage: kusamushiri-cli" in capsys.readouterr().err


def test_no_arguments_reuses_the_saved_recipe(fake_core, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_recipe(tmp_path, "")
    # Pick the saved recipe (default), run it, then the closing "press Enter" prompt.
    prompts = interactive(monkeypatch, ["", "y", ""])

    assert cli.main([]) == 0

    assert len(prompts) == 3
    assert len(list((tmp_path / "lists").glob("posts-*.csv"))) == 1


def test_run_from_archive_opens_the_browser_only_to_delete(fake_core, tmp_path, monkeypatch) -> None:
    entries = [{"tweet": {"id_str": "7", "created_at": "Wed Oct 10 20:19:24 +0000 2018", "full_text": "old"}}]
    (tmp_path / "tweets.js").write_text(f"window.YTD.tweets.part0 = {json.dumps(entries)}", encoding="utf-8")
    interactive(monkeypatch, ["y"])

    assert cli.main(["run", str(write_recipe(tmp_path, 'archive = "tweets.js"\n[delete]\ninterval = 0\n'))]) == 0

    assert len(fake_core.instances) == 1
    assert [target.url for target in fake_core.instances[0].actions] == ["https://x.com/i/status/7"]


@pytest.mark.parametrize(
    ("flags", "expected"),
    [([], ["3"]), (["--order", "newest"], ["3"]), (["--order", "oldest"], ["1"]), (["--oldest-first"], ["1"])],
)
def test_archive_order_picks_newest_or_oldest(fake_core, tmp_path, flags: list[str], expected: list[str]) -> None:
    tweets = tmp_path / "tweets.js"
    entries = [
        {"tweet": {"id_str": str(post_id), "created_at": created, "full_text": "text", "favorite_count": "0"}}
        for post_id, created in [(1, "Wed Oct 10 20:19:24 +0000 2018"), (3, "Wed Oct 10 20:19:24 +0000 2020")]
    ]
    tweets.write_text(f"window.YTD.tweets.part0 = {json.dumps(entries)}", encoding="utf-8")
    output = tmp_path / "selected.json"

    code = cli.main(["archive", str(tweets), "--user", "alice", "--max-posts", "1", *flags, "-o", str(output)])

    assert code == 0
    assert [row["id"] for row in json.loads(output.read_text(encoding="utf-8"))] == expected
