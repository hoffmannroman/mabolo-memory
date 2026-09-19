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
