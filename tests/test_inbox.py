"""The inbox: an orphan branch that two machines can both write to.

Everything here runs against real repositories, including a real bare remote,
because what is being tested is what Git actually does with a branch that has
no history. A fake would only prove that the fake agrees with the code.
"""

import datetime as dt
import subprocess

import pytest

from mabolo import git, inbox, write
from mabolo.errors import MaboloError
from mabolo.proposal import Proposal

#: The real clock, because expiry is measured against it. A fixed date here
#: would put every proposal in these tests months past its expiry date the
#: moment the calendar moved on, and the suite would fail for a reason that has
#: nothing to do with what it is testing.
NOW = dt.datetime.now().astimezone().replace(microsecond=0)

ENTRY = """---
type: reference
title: t
description: d
mabolo:
  area: infra
---

text
"""


def made(quote="remember that beacon runs the nightly backup", *, target="infra/beacon",
         action="write", filed_at=NOW, source="extract") -> Proposal:
    stamp = filed_at
    if isinstance(filed_at, dt.datetime):
        stamp = filed_at.isoformat(timespec="seconds")
    return Proposal(
        action=action, target=target, quote=quote, entry=ENTRY, source=source, filed_at=stamp
    )


def clone(tmp_path, remote, name="second"):
    where = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(remote), str(where)], check=True)
    return where


def on_branch(root, branch=inbox.BRANCH) -> list[str]:
    """The file names on the branch, straight out of Git, without a checkout."""
    tip = git.rev(root, f"refs/heads/{branch}")
    return sorted(git.run(root, "ls-tree", "--name-only", tip).stdout.split()) if tip else []


def commits(root, branch=inbox.BRANCH) -> int:
    return int(git.run(root, "rev-list", "--count", branch).stdout.strip())


def parents(root, branch=inbox.BRANCH) -> list[str]:
    return git.run(root, "rev-list", "--parents", "-n", "1", branch).stdout.split()[1:]


# The branch itself


def test_the_inbox_branch_is_one_parentless_commit(git_vault):
    """Three filings, one commit, no parent. A history of what somebody was
    thinking and then threw away is not what the vault is for."""
    for word in ("first", "second", "third"):
        inbox.file(git_vault.root, made(quote=f"remember the {word} thing about beacon"))
    assert len(on_branch(git_vault.root)) == 3
    assert commits(git_vault.root) == 1
    assert parents(git_vault.root) == []


def test_filing_leaves_main_alone(git_vault):
    """The proposal never reaches the branch the person has checked out, and
    never reaches the directory they have open."""
    before = git_vault.git("rev-parse", "HEAD").stdout.strip()
    result = inbox.file(git_vault.root, made())
    assert result.outcome == write.LOCAL
    assert git_vault.git("rev-parse", "HEAD").stdout.strip() == before
    assert not git_vault.git("status", "--porcelain").stdout.strip()
    assert list(git_vault.root.glob("*.json")) == []


def test_a_merge_in_progress_does_not_block_the_inbox(git_vault):
    """A write refuses here, because it commits on top of HEAD. The inbox has
    nothing to collide with: it touches neither HEAD nor the working tree."""
    (git_vault.root / ".git" / "MERGE_HEAD").write_text("x", encoding="utf-8")
    result = inbox.file(git_vault.root, made())
    assert result.outcome == write.LOCAL
    assert on_branch(git_vault.root) == [made().filename]


def test_a_vault_without_git_cannot_hold_an_inbox(vault):
    """Said in our own words, before anything is built. A proposal in a folder
    with no repository has nowhere to wait and no way to reach a second machine."""
    with pytest.raises(MaboloError) as caught:
        inbox.file(vault.root, made())
    assert "Git repository" in str(caught.value)


# Two machines


def test_the_union_keeps_what_the_other_machine_filed(git_vault, remote, tmp_path):
    theirs = made(quote="remember that atlas is the build host", target="infra/atlas")
    mine = made()
    inbox.file(clone(tmp_path, remote), theirs, remote="origin")

    result = inbox.file(git_vault.root, mine, remote="origin")
    assert result.outcome == write.WRITTEN
    assert on_branch(git_vault.root) == sorted([mine.filename, theirs.filename])
    assert on_branch(remote) == sorted([mine.filename, theirs.filename])


def test_a_proposal_on_both_sides_keeps_the_earlier_filing(git_vault, remote, tmp_path):
    """Both directions, so that a merge which always prefers one side is red as
    well. The same id means the same sentence, so the two files differ only in
    when they were filed, and the first filing is the one that happened."""
    early, late = NOW - dt.timedelta(days=3), NOW
    x, y = "remember that beacon reboots on sunday", "remember that atlas has two disks"

    inbox.file(git_vault.root, made(quote=x, filed_at=late))
    inbox.file(git_vault.root, made(quote=y, filed_at=early))
    other = clone(tmp_path, remote)
    inbox.file(other, made(quote=x, filed_at=early), remote="origin")
    inbox.file(other, made(quote=y, filed_at=late), remote="origin")

    inbox.sync(git_vault.root, remote="origin")
    filed = {p.quote: p.filed_at for p in inbox.pending(git_vault.root)}
    assert filed[x] == early.isoformat(timespec="seconds")
    assert filed[y] == early.isoformat(timespec="seconds")


def test_a_refused_push_is_rebuilt_against_the_remote_and_goes_through(
    git_vault, remote, tmp_path, monkeypatch
):
    """The race the lease exists for: somebody files between our fetch and our
    push. The second attempt is not the same commit sent again, it is the union
    rebuilt against where the remote has actually got to."""
    other = clone(tmp_path, remote)
    theirs = made(quote="remember that atlas is the build host", target="infra/atlas")
    mine = made()
    real, attempts = git.push, []

    def racing(root, *args, **rest):
        if root == git_vault.root:
            attempts.append(1)
            if len(attempts) == 1:
                inbox.file(other, theirs, remote="origin")
                return "rejected"
        return real(root, *args, **rest)

    monkeypatch.setattr(git, "push", racing)
    result = inbox.file(git_vault.root, mine, remote="origin")
    assert len(attempts) == 2
    assert result.outcome == write.WRITTEN
    assert on_branch(remote) == sorted([mine.filename, theirs.filename])


def test_a_second_refusal_reports_conflict(git_vault, remote, monkeypatch):
    """A remote that keeps moving is answered with a conflict rather than with a
    third round. Nothing local moved, so there is nothing to undo."""
    attempts = []

    def refusing(*args, **rest):
        attempts.append(1)
        return "rejected"

    monkeypatch.setattr(git, "push", refusing)
    result = inbox.file(git_vault.root, made(), remote="origin")
    assert len(attempts) == 2
    assert result.outcome == write.CONFLICT
    assert on_branch(git_vault.root) == []
    assert on_branch(remote) == []


def test_an_offline_filing_goes_out_with_the_next_one(git_vault, remote, tmp_path):
    """Refusing to remember something because a laptop is on a train would be
    the worse bargain. The next operation with a reachable remote carries it."""
    first, second = made(), made(quote="remember that atlas is the build host")
    away = tmp_path / "gone.git"
    remote.rename(away)

    filed = inbox.file(git_vault.root, first, remote="origin")
    assert filed.outcome == write.OFFLINE
    assert "only here" in filed.message

    away.rename(remote)
    again = inbox.file(git_vault.root, second, remote="origin")
    assert again.outcome == write.WRITTEN
    assert on_branch(remote) == sorted([first.filename, second.filename])


# Reading what is there


def put(root, name: str, data: bytes) -> None:
    """Put a file on the branch the way another machine would: through plumbing.

    The commit this builds has a parent, which the next rebuild drops. What the
    test needs is the tree, and building it by hand is the only way to get a
    file onto the branch that this module would never write itself.
    """
    tip = git.rev(root, f"refs/heads/{inbox.BRANCH}")
    commit = git.build_commit(root, tip, {name: data}, "from somewhere else")
    git.update_ref(root, f"refs/heads/{inbox.BRANCH}", commit, tip)


def test_a_file_that_is_not_json_is_named_and_the_rest_still_lists(git_vault):
    mine = made()
    inbox.file(git_vault.root, mine)
    put(git_vault.root, "deadbeef.json", b"{ this was half written")

    listing = inbox.read(git_vault.root)
    assert [p.id for p in listing.proposals] == [mine.id]
    assert len(listing.unreadable) == 1
    assert listing.unreadable[0].startswith("deadbeef.json: ")


def test_a_file_that_will_not_read_is_kept_on_the_branch(git_vault):
    """Deleting what cannot be parsed throws away the only copy of somebody's
    suggestion, and the rebuild is where a file would quietly disappear."""
    put(git_vault.root, "deadbeef.json", b"{ this was half written")
    inbox.file(git_vault.root, made(), now=NOW + dt.timedelta(days=90))
    assert "deadbeef.json" in on_branch(git_vault.root)


def test_a_proposal_whose_name_does_not_match_its_content_is_refused(git_vault):
    """The id is the file's name and a claim about what is inside it. A file
    somebody renamed would otherwise be approved under an id a person read next
    to a different sentence."""
    put(git_vault.root, "00000000.json", made().to_json().encode("utf-8"))

    listing = inbox.read(git_vault.root)
    assert listing.proposals == []
    assert listing.unreadable[0].startswith("00000000.json: ")


def test_pending_lists_the_newest_first(git_vault):
    days = (9, 1, 5)
    for day in days:
        inbox.file(git_vault.root, made(quote=f"remember thing {day} about beacon",
                                        filed_at=NOW - dt.timedelta(days=day)))
    listed = [p.filed_at for p in inbox.pending(git_vault.root)]
    assert listed == [
        (NOW - dt.timedelta(days=day)).isoformat(timespec="seconds") for day in (1, 5, 9)
    ]


# Filing twice, forgetting, expiring


def test_filing_the_same_proposal_twice_says_it_is_already_there(git_vault):
    """Not an error and not a filing either. The id is derived from the
    sentence, so an extraction pass run twice over the same transcript must not
    turn one thought into two things to read."""
    first = inbox.file(git_vault.root, made(filed_at=""), now=NOW)
    again = inbox.file(git_vault.root, made(filed_at=""), now=NOW + dt.timedelta(days=2))

    assert first.outcome == write.LOCAL
    assert again.outcome == inbox.ALREADY
    assert again.id == made().id
    assert [p.filed_at for p in inbox.pending(git_vault.root)] == [
        NOW.isoformat(timespec="seconds")
    ]


def test_forget_removes_only_what_it_was_given(git_vault):
    kept, gone = made(), made(quote="remember that atlas is the build host")
    inbox.file(git_vault.root, kept)
    inbox.file(git_vault.root, gone)

    result = inbox.forget(git_vault.root, [gone.id])
    assert result.ids == (gone.id,)
    assert on_branch(git_vault.root) == [kept.filename]


def test_expiry_drops_the_stale_and_keeps_the_rest(git_vault):
    """Dropped on a rebuild, never in a pass of its own: a tidy that runs by
    itself is a write nobody asked for, at a moment nobody chose."""
    stale = made(quote="remember something nobody looked at", filed_at=NOW - dt.timedelta(days=40))
    fresh = made(quote="remember that atlas is the build host", filed_at=NOW - dt.timedelta(days=2))
    third = made(filed_at=NOW)
    inbox.file(git_vault.root, stale, now=NOW)
    result = inbox.file(git_vault.root, fresh, now=NOW)
    inbox.file(git_vault.root, third, now=NOW)

    assert result.expired == (stale.id,)
    assert on_branch(git_vault.root) == sorted([fresh.filename, third.filename])


# The ledger, which is where a decision lives


def ledger(root, *lines: str) -> None:
    text = ""
    for line in lines:
        text = inbox.append(text, line)
    (root / ".mabolo").mkdir(parents=True, exist_ok=True)
    (root / inbox.LEDGER).write_text(text, encoding="utf-8")


def test_a_rendered_decision_parses_back_with_its_answer(git_vault):
    yes = made()
    no = made(quote="remember that atlas is the build host", target="infra/atlas")
    ledger(
        git_vault.root,
        inbox.decision_line(yes, approved=True, by="human:alex", at=dt.date(2026, 3, 1)),
        inbox.decision_line(no, approved=False, by="human:alex", at=dt.date(2026, 3, 2)),
        "- not a decision at all, somebody typed this",
    )

    found = {one.id: one for one in inbox.decisions(git_vault.root)}
    assert len(found) == 2
    assert found[yes.id].approved and found[yes.id].target == "infra/beacon"
    assert not found[no.id].approved
    assert inbox.decided(git_vault.root, no.id).at == "2026-03-02"
    assert inbox.decided(git_vault.root, "0" * 8) is None


def test_the_ledger_keeps_the_last_answer_for_an_id(git_vault):
    """Appended newest last, so the last word counts. Reading the first line
    would let one old refusal outlive every decision after it."""
    one = made()
    ledger(
        git_vault.root,
        inbox.decision_line(one, approved=False, by="human:alex", at=dt.date(2026, 3, 1)),
        inbox.decision_line(one, approved=True, by="human:alex", at=dt.date(2026, 6, 2)),
    )
    assert inbox.decided(git_vault.root, one.id).approved


def test_a_decision_never_carries_the_quote(git_vault):
    """A refusal is often precisely "I do not want this remembered", and a line
    that repeats the sentence in order to record that it was refused has
    remembered it, in the vault, for as long as the vault exists."""
    one = made(quote="remember that the beacon key is in the drawer under the sink")
    line = inbox.decision_line(one, approved=False, by="human:alex", at=dt.date(2026, 3, 1))
    for word in ("beacon key", "drawer", "sink", one.quote):
        assert word not in line


def test_an_inbox_commit_says_that_mabolo_made_it(git_vault):
    """A stray branch in somebody's vault should not leave them guessing where
    it came from, and doctor tells Mabolo's commits from everybody else's by
    the trailer alone."""
    one = made()
    inbox.file(git_vault.root, one)
    trailer = git_vault.git(
        "log", inbox.BRANCH, "--format=%(trailers:key=Mabolo,valueonly)"
    ).stdout.strip()
    assert trailer.startswith("propose by extract")
    inbox.forget(git_vault.root, [one.id])
    trailer = git_vault.git(
        "log", inbox.BRANCH, "--format=%(trailers:key=Mabolo,valueonly)"
    ).stdout.strip().splitlines()[0]
    assert trailer.startswith("unfile by ")
