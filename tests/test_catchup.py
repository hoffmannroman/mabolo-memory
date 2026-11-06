"""Catching up before a session reads: forward when it is safe, a sentence when not."""

import os
import subprocess
import time

from mabolo import catchup, git, write

ENTRY = """---
type: reference
title: t
description: d
status: stable
mabolo:
  area: infra
---

{body}
"""


def head(root) -> str:
    return git.rev(root, "HEAD")


def run(where, *args):
    subprocess.run(["git", "-C", str(where), *args], check=True, capture_output=True)


def other_machine(tmp_path, remote, name="other"):
    """A second clone of the vault, the way a second machine holds it."""
    where = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(remote), str(where)], check=True)
    return where


def commit(where, path="infra/thing.md", body="text", push=True):
    target = where / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ENTRY.format(body=body), encoding="utf-8")
    run(where, "add", path)
    run(where, "commit", "-q", "-m", f"write {path}")
    if push:
        run(where, "push", "-q", "origin", "main")


def test_without_a_remote_there_is_nothing_to_catch_up_with(git_vault):
    done = catchup.catch_up(git_vault.root, None, "main")
    assert done.state == catchup.NO_REMOTE
    assert done.settled and done.note() == ""


def test_a_vault_in_step_with_its_remote_stays_where_it_is(git_vault, remote):
    before = head(git_vault.root)
    done = catchup.catch_up(git_vault.root, "origin", "main")
    assert done.state == catchup.CURRENT
    assert head(git_vault.root) == before


def test_what_another_machine_wrote_arrives_before_the_session_reads(tmp_path, git_vault, remote):
    other = other_machine(tmp_path, remote)
    commit(other, "infra/one.md")
    commit(other, "infra/two.md")
    done = catchup.catch_up(git_vault.root, "origin", "main")
    assert done.state == catchup.MOVED and done.commits == 2
    assert head(git_vault.root) == head(other)
    assert (git_vault.root / "infra" / "two.md").exists()
    assert done.note() == ""


def test_an_unreachable_remote_leaves_the_vault_and_says_so(tmp_path, git_vault, remote):
    other = other_machine(tmp_path, remote)
    commit(other)
    remote.rename(tmp_path / "gone.git")
    before = head(git_vault.root)
    done = catchup.catch_up(git_vault.root, "origin", "main")
    assert done.state == catchup.OFFLINE
    assert head(git_vault.root) == before
    assert "could not be reached" in done.note()


def test_a_remote_that_hangs_costs_the_fetch_and_not_the_session(git_vault, remote):
    # An ssh command that never answers: the fetch has to give up on its own.
    git_vault.git("config", "core.sshCommand", "sh -c 'sleep 5' --")
    git_vault.git("remote", "set-url", "origin", "ssh://nowhere.invalid/vault.git")
    done = catchup.catch_up(git_vault.root, "origin", "main", seconds=0.3)
    assert done.state == catchup.OFFLINE
    assert "0.3 seconds" in done.message


def test_commits_that_were_never_pushed_are_left_alone(git_vault, remote):
    commit(git_vault.root, push=False)
    before = head(git_vault.root)
    done = catchup.catch_up(git_vault.root, "origin", "main")
    assert done.state == catchup.AHEAD and done.commits == 1
    assert head(git_vault.root) == before
    assert "have not reached origin" in done.note()


def test_two_histories_that_parted_are_never_merged(tmp_path, git_vault, remote):
    other = other_machine(tmp_path, remote)
    commit(other, "infra/there.md")
    commit(git_vault.root, "infra/here.md", push=False)
    before = head(git_vault.root)
    done = catchup.catch_up(git_vault.root, "origin", "main")
    assert done.state == catchup.DIVERGED
    assert head(git_vault.root) == before
    assert not (git_vault.root / "infra" / "there.md").exists()


def test_a_file_edited_by_hand_is_never_overwritten(tmp_path, git_vault, remote):
    commit(git_vault.root, "infra/thing.md", body="first")
    other = other_machine(tmp_path, remote)
    commit(other, "infra/thing.md", body="from the other machine")
    (git_vault.root / "infra" / "thing.md").write_text(ENTRY.format(body="by hand"), encoding="utf-8")
    done = catchup.catch_up(git_vault.root, "origin", "main")
    assert done.state == catchup.BLOCKED
    assert "by hand" in (git_vault.root / "infra" / "thing.md").read_text(encoding="utf-8")


def test_a_write_in_progress_is_not_raced(tmp_path, git_vault, remote):
    other = other_machine(tmp_path, remote)
    commit(other)
    before = head(git_vault.root)
    with write.lock(git_vault.root):
        done = catchup.catch_up(git_vault.root, "origin", "main", lock_seconds=0.05)
    assert done.state == catchup.BLOCKED
    assert head(git_vault.root) == before


def test_a_vault_on_another_branch_is_not_moved(tmp_path, git_vault, remote):
    other = other_machine(tmp_path, remote)
    commit(other)
    git_vault.git("switch", "-q", "-c", "scratch")
    done = catchup.catch_up(git_vault.root, "origin", "main")
    assert done.state == catchup.BLOCKED
    assert "scratch" in done.message


def test_a_machine_that_never_caught_up_is_due():
    assert catchup.due()


def test_a_catch_up_a_moment_ago_is_not_due_yet():
    catchup.mark()
    assert not catchup.due()


def test_a_catch_up_older_than_a_minute_is_due_again():
    catchup.mark()
    old = time.time() - catchup.STALE_SECONDS - 1
    os.utime(catchup._stamp(), (old, old))
    assert catchup.due()


def test_a_stamp_from_the_future_does_not_hold_the_vault_still():
    """After the clock was set back, the stamp lies ahead: the vault must not
    stand still until the clock catches up with it."""
    catchup.mark()
    ahead = time.time() + 3600
    os.utime(catchup._stamp(), (ahead, ahead))
    assert catchup.due()


def test_a_failed_catch_up_still_counts_as_tried(tmp_path, git_vault, remote):
    """An unreachable remote is asked again a minute later, not on every prompt."""
    remote.rename(tmp_path / "gone.git")
    assert catchup.due()
    assert catchup.catch_up(git_vault.root, "origin", "main").state == catchup.OFFLINE
    assert not catchup.due()


def test_a_stamp_a_little_ahead_is_not_due():
    """A state directory on a network share stamps with the server's clock. A
    server a second ahead must not make every prompt start a catch-up."""
    catchup.mark()
    ahead = time.time() + 5
    os.utime(catchup._stamp(), (ahead, ahead))
    assert not catchup.due()


def test_a_claim_takes_the_catch_up_once():
    assert catchup.claim()
    assert not catchup.claim(), "claimed a moment ago, so not due"


def test_a_claim_in_progress_elsewhere_is_left_to_it():
    """Two sessions prompting at the same instant: the one holding the claim
    lock decides, the other starts nothing."""
    import fcntl

    where = catchup._stamp().parent
    where.mkdir(parents=True, exist_ok=True)
    with open(where / catchup.CLAIM_FILE, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert not catchup.claim()
    assert catchup.claim()


def test_a_stamp_that_cannot_be_written_claims_nothing(tmp_path):
    """Otherwise every prompt found the catch-up due and started one."""
    where = catchup._stamp().parent
    where.mkdir(parents=True, exist_ok=True)
    where.chmod(0o500)
    try:
        assert not catchup.mark()
        assert not catchup.claim()
    finally:
        where.chmod(0o700)


def test_the_background_fetch_ends_before_a_write_stops_waiting():
    """The catch-up holds the write lock while it fetches. A slow remote may
    cost the catch-up, never a write the person asked for."""
    assert catchup.BACKGROUND_SECONDS + 2 <= write.LOCK_SECONDS


def test_an_interrupted_git_command_stops_the_catch_up(tmp_path, git_vault, remote):
    """A checkout killed half way leaves index.lock and part of the newer files.
    Moving on from there would build on a tree nobody can vouch for."""
    other = other_machine(tmp_path, remote)
    commit(other)
    (git.repository_dir(git_vault.root) / "index.lock").write_text("", encoding="utf-8")
    before = head(git_vault.root)
    done = catchup.catch_up(git_vault.root, "origin", "main")
    assert done.state == catchup.BLOCKED
    assert "index.lock" in done.note()
    assert head(git_vault.root) == before


def test_a_lock_that_cannot_be_made_is_a_sentence_not_a_crash(git_vault, remote, monkeypatch):
    """The caller is a hook that must start the session whatever happens here."""
    from contextlib import contextmanager

    @contextmanager
    def broken(root, seconds):
        raise OSError(28, "No space left on device")
        yield

    monkeypatch.setattr(write, "lock", broken)
    done = catchup.catch_up(git_vault.root, "origin", "main")
    assert done.state == catchup.BLOCKED
    assert "No space left" in done.note()
