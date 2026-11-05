"""The step a person takes, which is the only way a proposal becomes an entry."""

import pytest

from mabolo import decide, frontmatter, inbox, write
from mabolo.errors import MaboloError
from mabolo.proposal import Proposal

ENTRY = """---
type: reference
title: Deploy from main only
description: Releases are cut from main, never from a tag
status: stable
mabolo:
  area: infra
---

Releases are cut from `main`.
"""


def suggested(action="write", target="infra/deploy-from-main", **over):
    fields = {
        "action": action,
        "target": target,
        "quote": "please remember that releases are cut from main only",
        "entry": ENTRY if action == "write" else "",
        "source": "process:extract",
        "filed_at": "2026-09-19T10:00:00+03:00",
    }
    fields.update(over)
    return Proposal(**fields)


def ledger(vault) -> str:
    path = vault.root / inbox.LEDGER
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_a_yes_writes_the_entry_and_the_answer_in_one_commit(git_vault):
    """Two commits would leave a window in which the vault holds a claim that
    nothing accounts for, and the ledger is what says who approved it."""
    one = suggested()
    inbox.file(git_vault.root, one)
    answer = decide.answer(git_vault, one, approved=True, by="human:alex")
    assert answer.ok and answer.where == "infra/deploy-from-main.md"
    assert (git_vault.root / "infra" / "deploy-from-main.md").exists()
    assert f"{one.id} " in ledger(git_vault)
    touched = git_vault.git("show", "--name-only", "--format=", "HEAD").stdout.split()
    # The two the claim is about, plus the indexes the new entry belongs in:
    # derived files travel with the entry, or the commit before them holds a
    # vault whose index has forgotten it.
    assert sorted(touched) == [
        ".mabolo/decided.md",
        "index.md",
        "infra/deploy-from-main.md",
        "infra/index.md",
    ]


def test_a_yes_records_who_approved_it_on_the_entry(git_vault):
    one = suggested()
    inbox.file(git_vault.root, one)
    decide.answer(git_vault, one, approved=True, by="human:alex")
    document = frontmatter.read(git_vault.root / "infra" / "deploy-from-main.md")
    assert document.meta["verified"][-1]["by"] == "human:alex"


def test_a_no_writes_the_answer_and_nothing_else(git_vault):
    """The id stays on file so the same sentence is not suggested again next
    week. The sentence itself never is: a refusal is often precisely "I do not
    want this remembered"."""
    one = suggested()
    inbox.file(git_vault.root, one)
    answer = decide.answer(git_vault, one, approved=False, by="human:alex")
    assert answer.ok and answer.where == ""
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()
    assert f"{one.id} " in ledger(git_vault)
    assert one.quote not in ledger(git_vault)


def test_an_answered_proposal_leaves_the_inbox(git_vault):
    one = suggested()
    inbox.file(git_vault.root, one)
    assert [p.id for p in inbox.pending(git_vault.root)] == [one.id]
    decide.answer(git_vault, one, approved=False, by="human:alex")
    assert inbox.pending(git_vault.root) == []
    assert inbox.decided(git_vault.root, one.id).approved is False


def test_the_validator_runs_again_at_approval(git_vault):
    """It ran when the proposal was filed and the vault has moved since. An
    entry that was valid against last week's areas may not be valid now."""
    one = suggested(entry=ENTRY.replace("area: infra", "area: nowhere"))
    inbox.file(git_vault.root, one)
    with pytest.raises(MaboloError):
        decide.answer(git_vault, one, approved=True, by="human:alex")
    assert not list(git_vault.root.glob("nowhere/*"))


def test_an_edit_whose_passage_moved_is_refused_rather_than_applied(git_vault):
    git_vault.root.joinpath("infra", "deploy-from-main.md").write_text(ENTRY, encoding="utf-8")
    one = suggested(action="edit", target="deploy-from-main", old="Releases are cut",
                    new="Nothing is cut", entry="")
    inbox.file(git_vault.root, one)
    # Somebody rewrote that sentence in the meantime.
    git_vault.root.joinpath("infra", "deploy-from-main.md").write_text(
        ENTRY.replace("Releases are cut from `main`.", "It all comes from main."), encoding="utf-8"
    )
    with pytest.raises(MaboloError) as caught:
        decide.answer(git_vault, one, approved=True, by="human:alex")
    assert "0 times" in str(caught.value)


def test_an_edit_that_still_fits_is_applied(git_vault):
    git_vault.root.joinpath("infra", "deploy-from-main.md").write_text(ENTRY, encoding="utf-8")
    one = suggested(action="edit", target="deploy-from-main", old="Releases are cut",
                    new="Nothing is cut", entry="")
    inbox.file(git_vault.root, one)
    answer = decide.answer(git_vault, one, approved=True, by="human:alex")
    assert answer.ok
    assert "Nothing is cut" in (git_vault.root / "infra" / "deploy-from-main.md").read_text()


def test_a_forget_removes_the_entry_it_names(git_vault):
    git_vault.root.joinpath("infra", "deploy-from-main.md").write_text(ENTRY, encoding="utf-8")
    one = suggested(action="forget", target="deploy-from-main", entry="")
    inbox.file(git_vault.root, one)
    assert decide.answer(git_vault, one, approved=True, by="human:alex").ok
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()


def test_an_entry_the_vault_does_not_have_is_named(git_vault):
    one = suggested(action="forget", target="never-existed", entry="")
    inbox.file(git_vault.root, one)
    with pytest.raises(MaboloError) as caught:
        decide.answer(git_vault, one, approved=True, by="human:alex")
    assert "never-existed" in str(caught.value)


def test_an_answer_carries_the_kind_of_commit_it_is(git_vault):
    one = suggested()
    inbox.file(git_vault.root, one)
    decide.answer(git_vault, one, approved=True, by="human:alex")
    trailer = git_vault.git("log", "-1", "--format=%(trailers:key=Mabolo,valueonly)").stdout
    assert trailer.strip().startswith("approve by human:alex")
    assert "approve" in write.KINDS and "reject" in write.KINDS


def clone_of(tmp_path, remote, name="behind"):
    import subprocess

    from mabolo.vault import Vault
    where = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(remote), str(where)], check=True)
    return Vault(where)


def fresh(**over):
    import datetime as dt
    return suggested(filed_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"), **over)


def test_a_clone_that_is_behind_cannot_answer_again_what_was_answered(git_vault, remote, tmp_path):
    """Refused on one machine, then approved on another whose copy of the inbox
    still listed it: the entry was written and the ledger held both answers."""
    one = fresh()
    inbox.file(git_vault.root, one, remote="origin")
    behind = clone_of(tmp_path, remote)
    inbox.sync(behind.root, remote="origin")
    decide.answer(git_vault, one, approved=False, by="human:alex", remote="origin", branch="main")

    with pytest.raises(MaboloError, match="answered already"):
        decide.answer(behind, one, approved=True, by="human:alex", remote="origin", branch="main")
    assert not (behind.root / "infra" / "deploy-from-main.md").exists()


def test_a_cleanup_that_fails_after_the_answer_is_not_reported_as_unanswered(
    git_vault, monkeypatch
):
    one = fresh()
    inbox.file(git_vault.root, one)

    def broken(*args, **kwargs):
        raise MaboloError("git fetch did not finish within 20 seconds")

    monkeypatch.setattr(inbox, "forget", broken)
    answered = decide.answer(git_vault, one, approved=True, by="human:alex")
    assert answered.ok
    assert "still listed" in answered.line()
    assert (git_vault.root / "infra" / "deploy-from-main.md").exists()
