"""Catching up before a session reads: forward when it is safe, a sentence when not."""

import subprocess

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
