"""Every machine's record on one branch of the vault, and every machine reading all of them."""

import io
import subprocess

import pytest

from mabolo import cli, inbox, reports
from mabolo.cli import main
from mabolo.vault import Vault


@pytest.fixture
def machines(tmp_path, git_identity, monkeypatch):
    """Two clones of one vault, each with a state directory and a name of its own."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main", str(origin)], check=True)
    first = Vault(tmp_path / "one")
    first.initialise()
    first.git_initialise()
    first.git_set_remote(str(origin))
    first.git("push", "-q", "origin", "main")
    second = tmp_path / "two"
    subprocess.run(["git", "clone", "-q", str(origin), str(second)], check=True)

    def on(name):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"state-{name}"))
        monkeypatch.setattr(reports, "host", lambda: name)
        return first.root if name == "alpha" else second

    return on


def test_a_problem_from_one_machine_is_seen_and_settled_on_another(machines):
    root = machines("alpha")
    found = reports.add(reports.HOOK, "file hook", "_OutOfTime: no payload", kind="_OutOfTime")
    assert reports.sync(root, "origin") == [], "nobody else has published yet"

    root = machines("beta")
    assert reports.open_problems() == [], "beta's own record is empty"
    elsewhere = reports.sync(root, "origin")
    waiting = reports.open_problems(elsewhere)
    assert [problem.id for problem in waiting] == [found]
    assert "on alpha" in reports.line(waiting[0])
    assert not reports.settle(found, reports.FIXED), "not known without the other machine's record"
    assert reports.settle(found, reports.FIXED, commit="abc", elsewhere=elsewhere)
    reports.sync(root, "origin")

    root = machines("alpha")
    assert reports.open_problems(reports.sync(root, "origin")) == []
    reports.add(reports.HOOK, "file hook", "_OutOfTime: again", kind="_OutOfTime")
    assert [problem.id for problem in reports.open_problems(reports.sync(root, "origin"))] == [found], \
        "it happened again after the fix, so it is open again"


def test_each_machine_only_ever_replaces_its_own_file(machines):
    root = machines("alpha")
    reports.add(reports.SESSION, "proj", "from alpha")
    reports.sync(root, "origin")
    root = machines("beta")
    reports.add(reports.SESSION, "proj", "from beta")
    reports.sync(root, "origin")
    there = inbox._files(root, subprocess.run(
        ["git", "-C", str(root), "rev-parse", "origin/reports"], capture_output=True, text=True, check=True
    ).stdout.strip())
    assert sorted(there) == ["alpha.jsonl", "beta.jsonl"]
    assert "from alpha" in there["alpha.jsonl"].decode() and "from alpha" not in there["beta.jsonl"].decode()


def test_a_machine_that_published_meanwhile_is_not_overwritten(machines, monkeypatch):
    root = machines("alpha")
    reports.add(reports.SESSION, "proj", "from alpha")
    reports.sync(root, "origin")

    root = machines("beta")
    reports.add(reports.SESSION, "proj", "from beta")
    real = inbox._fetch_branch
    calls = []

    def stale(*args):
        # The first look happens before alpha's push arrived.
        calls.append(args)
        return (True, None) if len(calls) == 1 else real(*args)

    monkeypatch.setattr(inbox, "_fetch_branch", stale)
    elsewhere = reports.sync(root, "origin")
    assert len(calls) == 2, "the refused push was tried again"
    assert [item["message"] for item in elsewhere] == ["from alpha"]


def test_a_vault_without_a_remote_reads_only_this_machine(git_vault):
    reports.add(reports.SESSION, "proj", "here")
    assert reports.sync(git_vault.root, None) == []
    assert reports.sync(git_vault.root, "origin") == [], "named, but not configured"
    assert len(reports.open_problems()) == 1


def test_what_was_published_is_not_published_again(machines):
    root = machines("alpha")
    assert not reports.needs_sync(), "nothing recorded, nothing to publish"
    reports.add(reports.SESSION, "proj", "one")
    assert reports.needs_sync()
    reports.sync(root, "origin")
    assert not reports.needs_sync()
    reports.add(reports.SESSION, "proj", "two")
    assert reports.needs_sync()


def test_lines_from_several_machines_are_read_in_the_order_they_were_written():
    early = {"id": "a", "at": "2026-10-24T10:00:00+03:00", "source": "hook", "where": "w", "message": "m"}
    status = {"id": "a", "at": "2026-10-24T08:30:00+00:00", "status": "fixed"}
    later = {"id": "a", "at": "2026-10-24T12:00:00+03:00", "source": "hook", "where": "w", "message": "m"}
    # 08:30 UTC is 11:30 at +03:00: after the first, before the second.
    assert reports.open_problems([later, status, early])[0].since == 1


def test_the_session_start_publishes_in_the_background_only_when_needed(tmp_path, capsys, monkeypatch):
    started = []
    real = cli.subprocess.Popen

    def popen(argv, **kw):
        if argv[-2:] == ["reports", "--sync"]:
            started.append(argv)
            return None
        return real(argv, **kw)

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    config = str(tmp_path / "c.toml")
    main(["--config", config, "init", "--vault", str(tmp_path / "v"), "--yes", "--no-git", "--no-clients"])
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    main(["--config", config, "hook", "session-start"])
    assert started == []

    reports.add(reports.SESSION, "proj", "something")
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    main(["--config", config, "hook", "session-start"])
    assert len(started) == 1
    assert started[0][-2:] == ["reports", "--sync"]
    assert config in started[0]


def test_the_quiet_sync_says_nothing_and_never_fails(tmp_path, capsys, monkeypatch):
    def broken(*args):
        raise RuntimeError("the remote is on fire")

    monkeypatch.setattr(reports, "sync", broken)
    config = str(tmp_path / "c.toml")
    main(["--config", config, "init", "--vault", str(tmp_path / "v"), "--yes", "--no-git", "--no-clients"])
    capsys.readouterr()
    assert main(["--config", config, "reports", "--sync"]) == 0
    assert capsys.readouterr().out == ""


def test_the_listing_includes_the_other_machines(tmp_path, machines, capsys):
    root = machines("alpha")
    found = reports.add(reports.SESSION, "proj", "from alpha")
    reports.sync(root, "origin")
    machines("beta")
    config = tmp_path / "c.toml"
    config.write_text(f'vault = "{root.parent / "two"}"\nactor = "human:alex"\n'
                      f'[remote]\nurl = "{root.parent / "origin.git"}"\n', encoding="utf-8")
    assert main(["--config", str(config), "reports"]) == 0
    assert found in capsys.readouterr().out
    assert main(["--config", str(config), "reports", "--local"]) == 0
    assert capsys.readouterr().out.startswith("nothing is waiting")
