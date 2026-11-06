"""The transaction: what reaches the vault, and what is refused before it does."""

import subprocess

import pytest

from mabolo import __version__, git, write
from mabolo.errors import MaboloError

ENTRY = b"""---
type: reference
title: t
description: d
status: stable
mabolo:
  area: infra
---

text
"""


CHANGED = ENTRY.replace(b"text", b"other text")


def change(path: str = "infra/thing.md", data: bytes | None = ENTRY, expect: str | None = None):
    return write.Change(path=path, data=data, expect=expect)


def head(vault) -> str:
    return vault.git("rev-parse", "HEAD").stdout.strip()


def messages(vault) -> list[str]:
    return vault.git("log", "--format=%s").stdout.strip().splitlines()


def clone(tmp_path, remote, name="second"):
    where = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(remote), str(where)], check=True)
    return where


def test_a_write_commits_pushes_and_leaves_the_tree_clean(git_vault, remote):
    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex",
                         remote="origin", branch="main")
    assert result.outcome == write.WRITTEN
    assert (git_vault.root / "infra/thing.md").read_bytes() == ENTRY
    assert result.revisions["infra/thing.md"]
    assert not git_vault.git("status", "--porcelain").stdout.strip()
    assert git.rev(git_vault.root, "refs/remotes/origin/main") == head(git_vault)


def test_without_a_remote_the_change_is_still_made(git_vault):
    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    assert result.outcome == write.LOCAL
    assert result.commit
    assert messages(git_vault)[0] == "write a thing"


def test_a_stale_revision_writes_nothing(git_vault):
    write.apply(git_vault.root, [change()], "first", actor="human:alex")
    before = head(git_vault)
    result = write.apply(
        git_vault.root, [change(data=b"other", expect="0" * 12)], "second", actor="human:alex"
    )
    assert result.outcome == write.STALE
    assert "read it again" in result.message.lower()
    assert head(git_vault) == before
    assert (git_vault.root / "infra/thing.md").read_bytes() == ENTRY


def test_an_entry_that_is_already_there_is_not_overwritten(git_vault):
    write.apply(git_vault.root, [change()], "first", actor="human:alex")
    result = write.apply(git_vault.root, [change(data=b"other")], "again", actor="human:alex")
    assert result.outcome == write.STALE
    assert "already exists" in result.message


def test_a_remote_that_moved_refuses_the_push_and_changes_nothing(
    git_vault, remote, tmp_path, monkeypatch
):
    other = clone(tmp_path, remote)
    (other / "infra" / "elsewhere.md").write_bytes(ENTRY)
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "from another machine"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q"], check=True)

    # The remote moves between the fetch and the push, which is the race the
    # lease exists for. A fetch that reports success without fetching is how
    # that race is reproduced without two processes.
    monkeypatch.setattr(git, "fetch", lambda *a, **k: True)
    before = head(git_vault)
    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex",
                         remote="origin", branch="main")
    assert result.outcome == write.CONFLICT
    assert head(git_vault) == before
    assert not (git_vault.root / "infra/thing.md").exists()


def test_a_clone_that_is_behind_is_brought_forward_first(git_vault, remote, tmp_path):
    other = clone(tmp_path, remote)
    (other / "infra" / "elsewhere.md").write_bytes(ENTRY)
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "from another machine"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q"], check=True)

    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex",
                         remote="origin", branch="main")
    assert result.outcome == write.WRITTEN
    assert (git_vault.root / "infra/elsewhere.md").exists()
    assert messages(git_vault)[:2] == ["write a thing", "from another machine"]


def test_a_hand_made_change_becomes_its_own_commit(git_vault):
    (git_vault.root / "infra" / "by-hand.md").write_bytes(ENTRY)
    write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    assert messages(git_vault)[:2] == ["write a thing", "edit outside mabolo by human:alex"]
    touched = git_vault.git("show", "--name-only", "--format=", "HEAD").stdout.split()
    assert touched == ["infra/thing.md"]


def test_a_merge_in_progress_blocks_everything(git_vault):
    (git_vault.root / ".git" / "MERGE_HEAD").write_text("x", encoding="utf-8")
    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    assert result.outcome == write.BLOCKED
    assert "merge" in result.message
    assert not (git_vault.root / "infra/thing.md").exists()


def test_a_detached_head_blocks(git_vault):
    git_vault.git("checkout", "--detach", "--quiet", "HEAD")
    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    assert result.outcome == write.BLOCKED
    assert "detached" in result.message


def test_writing_what_is_already_there_is_not_a_commit(git_vault):
    first = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    before = head(git_vault)
    again = write.apply(
        git_vault.root, [change(expect=first.revisions["infra/thing.md"])], "again",
        actor="human:alex",
    )
    assert again.outcome == write.NOTHING
    assert head(git_vault) == before


def test_the_persons_own_staged_work_is_not_swept_up(git_vault):
    (git_vault.root / "infra" / "mine.md").write_bytes(ENTRY)
    git_vault.git("add", "infra/mine.md")
    write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    touched = git_vault.git("show", "--name-only", "--format=", "HEAD").stdout.split()
    assert touched == ["infra/thing.md"]


def test_an_unreachable_remote_keeps_the_change_here(git_vault, tmp_path):
    git_vault.git_set_remote(str(tmp_path / "nowhere.git"))
    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex",
                         remote="origin", branch="main")
    assert result.outcome == write.OFFLINE
    assert result.commit
    assert "only here" in result.message
    assert (git_vault.root / "infra/thing.md").exists()


def test_forgetting_removes_the_file_in_a_commit(git_vault):
    first = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    result = write.apply(
        git_vault.root,
        [change(data=None, expect=first.revisions["infra/thing.md"])],
        "forget a thing",
        actor="human:alex",
    )
    assert result.outcome == write.LOCAL
    assert not (git_vault.root / "infra/thing.md").exists()
    assert not git_vault.git("status", "--porcelain").stdout.strip()


def test_forgetting_what_is_not_there_says_so(git_vault):
    result = write.apply(
        git_vault.root, [change(data=None, expect="0" * 12)], "forget", actor="human:alex"
    )
    assert result.outcome == write.STALE
    assert "is gone" in result.message


def test_a_path_out_of_the_vault_is_refused(git_vault):
    """In our own words, before anything is read or written. Git refuses such a
    path as well, at the end of a mutation that has already taken the lock and
    committed whatever it found lying around."""
    for path in ("../escape.md", "/etc/escape.md", "infra/../../escape.md"):
        with pytest.raises(MaboloError) as caught:
            write.apply(git_vault.root, [change(path=path)], "x", actor="human:alex")
        assert "not a path inside the vault" in str(caught.value)


def test_a_remote_that_goes_away_between_fetch_and_push_keeps_the_change_here(
    git_vault, remote, monkeypatch
):
    """The fetch answered and the push did not. That is not a conflict: nobody
    said no, nobody was there. The change stays here and says so."""
    monkeypatch.setattr(git, "fetch", lambda *a, **k: True)
    subprocess.run(["rm", "-rf", str(remote)], check=True)
    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex",
                         remote="origin", branch="main")
    assert result.outcome == write.OFFLINE
    assert "only here" in result.message
    assert (git_vault.root / "infra/thing.md").exists()


def test_a_vault_without_git_still_writes(vault):
    result = write.apply(vault.root, [change()], "write a thing", actor="human:alex")
    assert result.outcome == write.LOCAL
    assert result.commit is None
    assert (vault.root / "infra/thing.md").exists()


def test_the_lock_is_held_by_one_writer_at_a_time(git_vault):
    with write.lock(git_vault.root):
        with pytest.raises(MaboloError) as caught:
            with write.lock(git_vault.root, seconds=0.1):
                pass
    assert "another write" in str(caught.value)


def test_nothing_to_write_is_not_an_error(git_vault):
    assert write.apply(git_vault.root, [], "x", actor="human:alex").outcome == write.NOTHING


# Every commit Mabolo makes says so, in a trailer, because it commits with the
# person's own Git identity and the message is the only evidence it controls.


def trailers(vault) -> list[str]:
    return vault.git(
        "log", "--format=%(trailers:key=Mabolo,valueonly,separator=%x2C)"
    ).stdout.strip().splitlines()


def test_every_commit_says_it_was_mabolo(git_vault):
    first = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    write.apply(
        git_vault.root,
        [change(data=None, expect=first.revisions["infra/thing.md"])],
        "forget a thing",
        actor="human:alex",
        kind="forget",
    )
    assert trailers(git_vault)[:2] == [
        f"forget by human:alex, mabolo-memory/{__version__}",
        f"write by human:alex, mabolo-memory/{__version__}",
    ]


def test_a_hand_made_change_is_a_kind_of_its_own(git_vault):
    """Mabolo committed it, Mabolo did not make it, and doctor has to be able
    to tell those apart without reading prose."""
    (git_vault.root / "infra" / "by-hand.md").write_bytes(ENTRY)
    write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    assert trailers(git_vault)[:2] == [
        f"write by human:alex, mabolo-memory/{__version__}",
        f"foreign by human:alex, mabolo-memory/{__version__}",
    ]


def test_the_trailer_stands_alone_in_the_message(git_vault):
    """Git reads trailers out of the last paragraph, and a paragraph that also
    holds prose is one a future Git may decline to parse."""
    write.apply(git_vault.root, [change()], "write a thing\n\nbecause somebody said so",
                actor="human:alex")
    body = git_vault.git("log", "-1", "--format=%B").stdout.strip()
    assert body.splitlines()[-1].startswith("Mabolo: write by")
    assert body.splitlines()[-2] == ""


def test_a_kind_nobody_defined_is_refused(git_vault):
    with pytest.raises(MaboloError):
        write.apply(git_vault.root, [change()], "x", actor="human:alex", kind="whatever")
    assert not (git_vault.root / "infra/thing.md").exists()


def test_a_file_that_is_staged_and_edited_since_blocks_the_write(git_vault):
    """Git holds two versions of it, and committing the one on disk throws away
    the one the person staged on purpose. It is the same class as a merge in
    progress: somebody is building a commit by hand."""
    path = git_vault.root / "infra" / "mine.md"
    path.write_bytes(ENTRY)
    git_vault.git("add", "infra/mine.md")
    path.write_bytes(CHANGED)
    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    assert result.outcome == write.BLOCKED
    assert "staged and has been edited since" in result.message
    assert not (git_vault.root / "infra/thing.md").exists()
    assert path.read_bytes() == CHANGED, "and neither version was touched"


def test_a_file_that_is_only_staged_is_carried_along(git_vault):
    """Staging is not a hiding place. A file Mabolo looked away from would
    never reach the other machines and would be overwritten by the next write
    of that path."""
    (git_vault.root / "infra" / "mine.md").write_bytes(ENTRY)
    git_vault.git("add", "infra/mine.md")
    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    assert result.ok
    assert trailers(git_vault)[1].startswith("foreign by")
    assert not git_vault.git("status", "--porcelain").stdout.strip()


def test_a_rename_by_hand_does_not_leave_the_old_file_behind(git_vault):
    """Skipping the path a rename came from committed the new file and kept the
    old one, so both stood in the tree and the old one came back from the dead
    on the next machine to pull."""
    (git_vault.root / "infra" / "old.md").write_bytes(ENTRY)
    git_vault.git("add", "infra/old.md")
    git_vault.git("commit", "-qm", "the file before the rename")
    git_vault.git("mv", "infra/old.md", "infra/new.md")
    write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    tree = git_vault.git("ls-tree", "-r", "--name-only", "HEAD").stdout.split()
    assert "infra/new.md" in tree
    assert "infra/old.md" not in tree
    assert not git_vault.git("status", "--porcelain").stdout.strip()


def test_the_revision_is_checked_after_the_clone_has_caught_up(git_vault, remote, tmp_path):
    """The order is the guarantee. Asking before the fast forward compares the
    caller's revision against a file that is already out of date, so the entry
    somebody else edited is overwritten without anybody being told."""
    first = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex",
                        remote="origin", branch="main")
    other = clone(tmp_path, remote)
    (other / "infra" / "thing.md").write_bytes(CHANGED)
    subprocess.run(["git", "-C", str(other), "commit", "-qam", "edited on another machine"],
                   check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q"], check=True)

    # This clone has not seen that edit and still holds the first revision.
    result = write.apply(
        git_vault.root,
        [change(data=ENTRY.replace(b"text", b"a third version"),
                expect=first.revisions["infra/thing.md"])],
        "write over it",
        actor="human:alex",
        remote="origin",
        branch="main",
    )
    assert result.outcome == write.STALE
    assert (git_vault.root / "infra/thing.md").read_bytes() == CHANGED, "their edit stands"


def test_a_commit_that_is_safe_says_so_when_the_files_cannot_be_written(git_vault):
    """The commit goes first on purpose. A failure after it leaves a repository
    that is right about itself, and the message names the commit and the one
    command that puts the files back; the caller must not hear "refused" about
    a change that is committed and pushed."""
    write.apply(git_vault.root, [change()], "write a thing", actor="human:alex")
    before = head(git_vault)
    area = git_vault.root / "infra"
    area.chmod(0o555)
    try:
        with pytest.raises(MaboloError) as caught:
            write.apply(
                git_vault.root,
                [change(data=CHANGED, expect=write.current_revision(git_vault.root,
                                                                    "infra/thing.md"))],
                "write again",
                actor="human:alex",
            )
    finally:
        area.chmod(0o755)
    said = str(caught.value)
    assert "is committed" in said and "recover files" in said
    assert head(git_vault) != before, "the commit stands, which is what the message promises"


def test_a_vault_on_another_branch_is_not_written_to(git_vault, remote):
    """The commit is built on whatever is checked out and pushed to the branch
    that syncs. A scratch branch in the vault would have published its commits
    into the shared one, left the local branch behind its own push, and shown
    the person a raw Git error instead of any of that."""
    git_vault.git("checkout", "-q", "-b", "scratch")
    (git_vault.root / "infra" / "scratch.md").write_bytes(ENTRY)
    git_vault.git("add", "infra/scratch.md")
    git_vault.git("commit", "-qm", "scratch work nobody pushed")
    before = git_vault.git("rev-parse", "refs/remotes/origin/main").stdout.strip()

    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex",
                         remote="origin", branch="main")
    assert result.outcome == write.BLOCKED
    assert "scratch" in result.message and "main" in result.message
    assert not (git_vault.root / "infra/thing.md").exists()
    git_vault.git("fetch", "-q", "origin", "main")
    assert git_vault.git("rev-parse", "refs/remotes/origin/main").stdout.strip() == before


def test_a_remote_that_is_still_empty_is_pushed_to(git_vault, tmp_path):
    """A bare repository with no branch yet answers a fetch with a failure, and
    reading that as unreachable made a brand new remote offline for good: every
    write said "only here" while the backup the README recommends stayed
    empty."""
    bare = tmp_path / "fresh.git"
    subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main", str(bare)], check=True)
    git_vault.git_set_remote(str(bare))
    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex",
                         remote="origin", branch="main")
    assert result.outcome == write.WRITTEN
    there = subprocess.run(["git", "-C", str(bare), "rev-parse", "main"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert there == head(git_vault)


def test_the_index_is_derived_after_the_vault_moved_forward(git_vault, remote, tmp_path):
    """What a write makes wrong elsewhere is worked out under the lock, after
    the catch-up the write does itself. Worked out before, it described the
    vault as it was: the index another machine had just extended was either
    refused as stale or, when the move landed in between, committed without
    that machine's entry."""
    from mabolo.vault import Vault

    other = clone(tmp_path, remote)
    (other / "infra" / "elsewhere.md").write_bytes(ENTRY)
    Vault(other).rebuild_indexes()
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "from another machine"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q"], check=True)

    result = write.apply(git_vault.root, [change()], "write a thing", actor="human:alex",
                         remote="origin", branch="main", derive=git_vault.index_changes)
    assert result.outcome == write.WRITTEN, result.message
    index = (git_vault.root / "infra" / "index.md").read_text(encoding="utf-8")
    assert "elsewhere.md" in index and "thing.md" in index


def test_an_interrupted_git_command_blocks_a_write(git_vault):
    """index.lock left behind means a tree half way between two commits, and a
    write would commit the newer half as a hand edit nobody made."""
    (git.repository_dir(git_vault.root) / "index.lock").write_text("", encoding="utf-8")
    result = write.apply(git_vault.root, [change()], "x", actor="human:alex")
    assert result.outcome == write.BLOCKED
    assert "index.lock" in result.message
    assert not (git_vault.root / "infra" / "thing.md").exists()


def _alive(pid: int) -> bool:
    """Running, as opposed to gone or a zombie nobody has reaped yet."""
    import os

    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_timed_out_git_takes_what_it_started_with_it(tmp_path, monkeypatch):
    """The ssh under a hung fetch outlived git's timeout and collected, one per
    retry, on a machine that retries every minute."""
    import os
    import time

    monkeypatch.setattr(git, "_in_foreground_of_a_terminal", lambda: False)
    marker = tmp_path / "child.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        git._execute(
            ["sh", "-c", f"sleep 30 & echo $! > {marker}; wait"],
            env=dict(os.environ), timeout=2, check=False, text=True,
        )
    child = int(marker.read_text().strip())
    for _ in range(100):
        if not _alive(child):
            break
        time.sleep(0.02)
    else:
        os.kill(child, 9)
        pytest.fail("the child git started was still running after the timeout")


def test_ending_a_timed_out_git_has_a_bound_of_its_own(monkeypatch):
    """Something that left the group but kept a pipe open held the wait after
    the kill for as long as it lived."""
    import os
    import shutil
    import time

    if shutil.which("setsid") is None:
        pytest.skip("needs setsid")
    monkeypatch.setattr(git, "_in_foreground_of_a_terminal", lambda: False)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        git._execute(["sh", "-c", "setsid sleep 6 & sleep 30"], env=dict(os.environ),
                     timeout=0.5, check=False, text=True)
    assert time.monotonic() - started < 4


@pytest.mark.parametrize("at_a_terminal", [False, True])
def test_git_gets_a_group_of_its_own_only_away_from_a_terminal(monkeypatch, at_a_terminal):
    """At a terminal ssh may ask for a passphrase there, and a process outside
    the foreground group is stopped the moment it tries."""
    import os

    seen = []
    real = git.subprocess.Popen

    def popen(argv, **kw):
        seen.append(kw)
        return real(argv, **kw)

    monkeypatch.setattr(git, "_in_foreground_of_a_terminal", lambda: at_a_terminal)
    monkeypatch.setattr(git.subprocess, "Popen", popen)
    git._execute(["true"], env=dict(os.environ), timeout=5, check=True, text=True)
    assert ("process_group" in seen[0]) is (not at_a_terminal)


def test_git_never_asks_for_a_passphrase_through_a_window():
    assert git._environment()["SSH_ASKPASS_REQUIRE"] == "never"
    assert git._environment()["GIT_TERMINAL_PROMPT"] == "0"


def test_a_fast_forward_holds_the_signals_that_would_cut_it_short(git_vault, monkeypatch):
    """A checkout killed half way leaves the newer half of the files on top of
    the older commit. The hook's own deadline, a stopped service, Ctrl-C: none
    of them lands until it is done."""
    import signal

    seen = []
    real = git.run

    def run(root, *args, **kw):
        if args[:1] == ("merge",):
            seen.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
        return real(root, *args, **kw)

    monkeypatch.setattr(git, "run", run)
    git.fast_forward(git_vault.root, "HEAD")
    assert seen and {signal.SIGALRM, signal.SIGTERM, signal.SIGINT, signal.SIGHUP} <= seen[0]
    assert signal.SIGALRM not in signal.pthread_sigmask(signal.SIG_BLOCK, [])


def test_a_write_cut_short_between_branch_and_files_is_finished_by_the_next(git_vault, monkeypatch):
    """The branch moves first, then the files. Stopped in between, the next
    write took the older files for a hand edit and reverted the entry."""
    real = write._materialise
    calls = []

    def interrupted(root, todo):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real(root, todo)

    monkeypatch.setattr(write, "_materialise", interrupted)
    with pytest.raises(KeyboardInterrupt):
        write.apply(git_vault.root, [change()], "first", actor="human:alex")
    result = write.apply(git_vault.root, [change("infra/other.md")], "second", actor="human:alex")
    assert result.ok, result.message
    assert messages(git_vault)[:2] == ["second", "first"]
    assert (git_vault.root / "infra" / "thing.md").read_bytes() == ENTRY
    assert "infra/thing.md" in git_vault.git("ls-tree", "-r", "--name-only", "HEAD").stdout
    assert git.unfinished_write(git_vault.root) is None


def test_a_branch_that_did_not_move_leaves_nothing_to_finish(git_vault, monkeypatch):
    from mabolo.errors import MaboloError

    def refused(*args, **kw):
        raise MaboloError("git update-ref failed: cannot lock ref")

    monkeypatch.setattr(git, "update_ref", refused)
    with pytest.raises(MaboloError):
        write.apply(git_vault.root, [change()], "x", actor="human:alex")
    assert git.unfinished_write(git_vault.root) is None


def test_a_fast_forward_cut_short_blocks_the_next_write(git_vault, remote, tmp_path):
    """Git moves HEAD last and lets go of index.lock before it does; killed in
    between, only our own mark says the tree is ahead of its commit."""
    other = clone(tmp_path, remote)
    (other / "infra" / "elsewhere.md").write_bytes(ENTRY)
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "other"], check=True)
    target = subprocess.run(["git", "-C", str(other), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    (git.repository_dir(git_vault.root) / git.FAST_FORWARD_MARKER).write_text(target)
    result = write.apply(git_vault.root, [change()], "x", actor="human:alex")
    assert result.outcome == write.BLOCKED
    assert "fast forward that was cut short" in result.message


def test_a_mark_left_by_a_fast_forward_that_finished_is_cleared(git_vault):
    marker = git.repository_dir(git_vault.root) / git.FAST_FORWARD_MARKER
    marker.write_text(head(git_vault))
    assert write.apply(git_vault.root, [change()], "x", actor="human:alex").ok
    assert not marker.exists()


def test_a_fast_forward_that_ends_by_itself_leaves_no_mark(git_vault, remote, tmp_path):
    other = clone(tmp_path, remote)
    (other / "infra" / "elsewhere.md").write_bytes(ENTRY)
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "other"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q"], check=True)
    git_vault.git("fetch", "-q", "origin")
    assert git.fast_forward(git_vault.root, "origin/main")
    assert not (git.repository_dir(git_vault.root) / git.FAST_FORWARD_MARKER).exists()


def test_a_repair_with_nothing_given_is_derived_entirely_under_the_lock(git_vault):
    """reindex hands no changes of its own: every index it writes is worked out
    after the vault caught up."""
    from mabolo.vault import Vault

    vault = Vault(git_vault.root)
    (git_vault.root / "infra" / "made-in-an-editor.md").write_bytes(ENTRY)
    git_vault.git("add", "-A")
    git_vault.git("commit", "-qm", "by hand")
    asked = []

    def derive(changes):
        asked.append(list(changes))
        return vault.stale_indexes()

    result = write.apply(git_vault.root, [], "rebuild", actor="human:alex", derive=derive)
    assert result.ok and asked == [[]]
    assert "made-in-an-editor.md" in (git_vault.root / "infra" / "index.md").read_text()
    assert write.apply(git_vault.root, [], "again", actor="human:alex",
                       derive=lambda _: vault.stale_indexes()).outcome == write.NOTHING
