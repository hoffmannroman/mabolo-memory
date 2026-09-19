"""The way back, and the two places it refuses to decide for a person."""

import subprocess

import pytest

from mabolo import recover, write
from mabolo.errors import MaboloError

ENTRY = b"""---
type: reference
title: t
description: d
status: stable
mabolo:
  area: infra
---

first
"""

CHANGED = ENTRY.replace(b"first", b"second")


def written(vault, data=ENTRY, path="infra/thing.md", expect=None, kind="write"):
    return write.apply(
        vault.root,
        [write.Change(path=path, data=data, expect=expect)],
        f"write {path}",
        actor="human:alex",
        kind=kind,
    )


def head(vault) -> str:
    return vault.git("rev-parse", "HEAD").stdout.strip()


def test_reverting_a_write_takes_the_file_away_again(git_vault):
    first = written(git_vault)
    plan = recover.plan_revert(git_vault.root, first.commit)
    assert plan.lines == ("remove infra/thing.md",)
    result = recover.carry_out(git_vault.root, plan, actor="human:alex", kind="revert")
    assert result.ok
    assert not (git_vault.root / "infra" / "thing.md").exists()


def test_reverting_a_change_puts_the_old_text_back(git_vault):
    first = written(git_vault)
    second = written(git_vault, CHANGED, expect=first.revisions["infra/thing.md"])
    plan = recover.plan_revert(git_vault.root, second.commit)
    recover.carry_out(git_vault.root, plan, actor="human:alex", kind="revert")
    assert (git_vault.root / "infra" / "thing.md").read_bytes() == ENTRY


def test_the_way_back_says_that_mabolo_made_it(git_vault):
    """Otherwise the undoing shows up in doctor as a commit from nowhere, which
    is the report telling somebody to worry about the repair."""
    first = written(git_vault)
    plan = recover.plan_revert(git_vault.root, first.commit)
    recover.carry_out(git_vault.root, plan, actor="human:alex", kind="revert")
    trailer = git_vault.git("log", "-1", "--format=%(trailers:key=Mabolo,valueonly)").stdout
    assert trailer.strip().startswith("revert by human:alex")


def test_a_plan_is_printed_before_anything_happens(git_vault):
    first = written(git_vault)
    plan = recover.plan_revert(git_vault.root, first.commit)
    assert "revert" in plan.render() and "infra/thing.md" in plan.render()
    assert (git_vault.root / "infra" / "thing.md").exists(), "printing a plan changes nothing"


def test_reverting_what_was_already_reverted_does_nothing(git_vault):
    first = written(git_vault)
    plan = recover.plan_revert(git_vault.root, first.commit)
    recover.carry_out(git_vault.root, plan, actor="human:alex", kind="revert")
    again = recover.plan_revert(git_vault.root, first.commit)
    assert again.empty
    assert recover.carry_out(git_vault.root, again, actor="human:alex",
                             kind="revert").outcome == write.NOTHING


def test_a_merge_is_not_something_this_undoes(git_vault):
    """Mabolo never merges, so a commit with two parents is not one of its own,
    and which side of a merge to keep is a decision for a person and for Git."""
    written(git_vault)
    git_vault.git("checkout", "-q", "-b", "other", "HEAD~1")
    (git_vault.root / "infra" / "other.md").write_bytes(ENTRY)
    git_vault.git("add", "infra/other.md")
    git_vault.git("commit", "-qm", "on the other branch")
    git_vault.git("checkout", "-q", "main")
    git_vault.git("merge", "-q", "--no-ff", "-m", "a merge", "other")
    with pytest.raises(MaboloError) as caught:
        recover.plan_revert(git_vault.root, "HEAD")
    assert "never merges" in str(caught.value)


def test_a_commit_that_is_not_there_is_named(git_vault):
    with pytest.raises(MaboloError) as caught:
        recover.plan_revert(git_vault.root, "0" * 40)
    assert "not a commit" in str(caught.value)


def test_recovering_a_file_rewrites_it_from_the_last_commit(git_vault):
    """The one case the transaction leaves open: the commit went through and
    writing the file afterwards did not, so the file still holds the version
    before it. That version was committed once, which is what tells it from a
    hand edit."""
    first = written(git_vault)
    written(git_vault, CHANGED, expect=first.revisions["infra/thing.md"])
    (git_vault.root / "infra" / "thing.md").write_bytes(ENTRY)
    plan = recover.plan_files(git_vault.root, ["infra/thing.md"])
    assert plan.lines == ("restore infra/thing.md",)
    recover.carry_out(git_vault.root, plan, actor="human:alex", kind="recover")
    assert (git_vault.root / "infra" / "thing.md").read_bytes() == CHANGED


def test_a_hand_edit_is_refused_rather_than_thrown_away(git_vault):
    """Bytes that were never committed under this name are somebody's work. A
    repair that silently dropped them would be the damage rather than the cure."""
    written(git_vault)
    (git_vault.root / "infra" / "thing.md").write_bytes(ENTRY.replace(b"first", b"by hand"))
    plan = recover.plan_files(git_vault.root, ["infra/thing.md"])
    assert plan.empty
    assert "hand edit" in plan.note
    assert b"by hand" in (git_vault.root / "infra" / "thing.md").read_bytes()


def test_a_file_the_last_commit_does_not_have_is_named(git_vault):
    plan = recover.plan_files(git_vault.root, ["infra/never.md"])
    assert plan.empty and "nothing to put back" in plan.note


def test_a_file_that_is_already_right_is_left_alone(git_vault):
    written(git_vault)
    before = head(git_vault)
    plan = recover.plan_files(git_vault.root, ["infra/thing.md"])
    assert plan.empty and not plan.note
    assert head(git_vault) == before


def test_recovering_a_push_sends_what_the_remote_does_not_have(git_vault, remote):
    written(git_vault)
    git_vault.git("update-ref", "refs/remotes/origin/main", "HEAD~1", check=False)
    standing = recover.push(git_vault.root, remote="origin", branch="main")
    assert standing.pushed
    assert "pushed" in standing.render()


def test_a_push_the_remote_refuses_lists_both_sides_and_stops(git_vault, remote, tmp_path):
    """Merging or rebasing on somebody's behalf is a decision, and both sides
    are printed so the person making it can see what they are deciding."""
    other = tmp_path / "second"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    (other / "infra" / "elsewhere.md").write_bytes(ENTRY)
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "from another machine"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q"], check=True)
    written(git_vault)
    git_vault.git("update-ref", "refs/remotes/origin/main", "HEAD~1", check=False)

    before = head(git_vault)
    theirs = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "main"], capture_output=True, text=True, check=True
    ).stdout.strip()
    standing = recover.push(git_vault.root, remote="origin", branch="main")
    assert not standing.pushed
    assert standing.ahead and standing.behind
    assert "from another machine" in "\n".join(standing.behind)
    assert head(git_vault) == before, "nothing is merged, rebased or forced"
    after = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "main"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert after == theirs, "and the other machine's commit is still there"


def test_a_remote_nobody_can_reach_says_so(git_vault, tmp_path):
    git_vault.git_set_remote(str(tmp_path / "nowhere.git"))
    standing = recover.push(git_vault.root, remote="origin", branch="main")
    assert not standing.pushed and not standing.reachable
    assert "could not be reached" in standing.render()


def test_a_vault_without_a_remote_says_so(git_vault):
    standing = recover.push(git_vault.root, remote="origin", branch="main")
    assert not standing.reachable and "no remote" in standing.render()
