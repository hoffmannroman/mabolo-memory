"""Renaming an area. What moves, what is readdressed, and what is left alone."""

import pytest

from conftest import entry_text
from mabolo import frontmatter, move, write
from mabolo.errors import MaboloError
from mabolo.vault import Vault


def area(vault, name: str, entries: dict[str, str]) -> None:
    folder = vault.area_dir(name)
    folder.mkdir(parents=True, exist_ok=True)
    for stem, text in entries.items():
        (folder / f"{stem}.md").write_text(text, encoding="utf-8")
    vault.rebuild_indexes()


def atlas(vault) -> None:
    """A project area shaped like a real one: an overview and two entries."""
    area(vault, "project/atlas", {
        "atlas": entry_text(area="project/atlas", title="atlas",
                            description="The atlas platform", body="One overview."),
        "atlas-rollout": entry_text(area="project/atlas", body="Read [atlas](atlas.md) first."),
        "feedback-small-steps": entry_text(area="project/atlas", body="Smallest step that works."),
    })


def carry_out(vault, old="project/atlas", new="project/beacon"):
    made = move.plan(vault, old, new)
    result = move.carry_out(vault, made, actor="human:alex")
    assert result.ok, result.message
    return made


def test_an_area_takes_its_entries_and_says_where_they_live_now(vault):
    """The whole point: a project renamed on disk finds its memory again."""
    atlas(vault)

    carry_out(vault)

    assert not (vault.root / "project" / "atlas").exists()
    moved = vault.root / "project" / "beacon"
    assert sorted(p.name for p in moved.glob("*.md")) == [
        "atlas-rollout.md", "beacon.md", "feedback-small-steps.md", "index.md",
    ]
    for entry in vault.entries():
        if entry.path and entry.path.parent == moved:
            assert entry.mabolo.area == "project/beacon", entry.path


def test_the_entry_named_after_the_area_is_renamed_and_the_others_are_not(vault):
    """That name *is* the area's name, so it moves with it. Every other name is
    an identity that links, the inbox and the eval baseline are keyed by."""
    atlas(vault)

    carry_out(vault)

    moved = vault.root / "project" / "beacon"
    assert (moved / "beacon.md").is_file()
    assert not (moved / "atlas.md").exists()
    assert (moved / "atlas-rollout.md").is_file(), "a name that merely starts with it stays"
    assert frontmatter.read(moved / "beacon.md").meta["title"] == "beacon"


def test_a_link_into_the_moved_folder_is_readdressed(vault):
    """A link is an address. An address that moved is a link that has to move,
    and this is the half that would otherwise turn up weeks later as a dead
    link nobody can date."""
    atlas(vault)
    area(vault, "project/harbour", {
        "harbour": entry_text(area="project/harbour", title="harbour",
                              body="It replaced [atlas](../atlas/atlas.md)."),
    })

    made = carry_out(vault)

    text = (vault.root / "project" / "harbour" / "harbour.md").read_text(encoding="utf-8")
    assert "(../beacon/beacon.md)" in text, text
    assert "project/harbour/harbour.md" in made.relinked


def test_a_link_between_two_moved_entries_still_points_at_its_neighbour(vault):
    """The folder moved underneath them, and one of them was renamed inside it."""
    atlas(vault)

    carry_out(vault)

    text = (vault.root / "project" / "beacon" / "atlas-rollout.md").read_text(encoding="utf-8")
    assert "(beacon.md)" in text, text


def test_the_journal_is_reported_and_never_rewritten(vault):
    """The journal is the one file written in the order things happened. What
    it says was true on the day it was written: the project really was called
    that then. Rewriting it would be forging a record to match today."""
    atlas(vault)
    log = vault.log_file
    log.write_text("# Log\n\n## 2026-09-19\n\n- did a thing for [atlas](project/atlas/atlas.md)\n",
                   encoding="utf-8")
    before = log.read_bytes()

    made = carry_out(vault)

    assert log.read_bytes() == before
    assert any("log.md" in where for where in made.left)
    assert all("log.md" not in change.path for change in made.changes)


def test_a_sentence_elsewhere_that_names_the_project_is_left_for_a_person(vault):
    """Whether a mention became wrong is a judgement about the sentence. "The
    reservation product was folded into atlas in September" is still true;
    "atlas is only a working title" is not. A rename that guessed would be
    wrong in one of those two directions every time."""
    atlas(vault)
    area(vault, "project/harbour", {
        "harbour": entry_text(area="project/harbour", title="harbour",
                              description="Folded into atlas in September",
                              body="It was folded into atlas, and that is that."),
    })

    made = carry_out(vault)

    text = (vault.root / "project" / "harbour" / "harbour.md").read_text(encoding="utf-8")
    assert "folded into atlas" in text
    assert "project/harbour/harbour.md" in made.left


def test_a_moved_entry_that_still_says_the_old_name_is_reported_too(vault):
    """The gap the first version of this report had.

    An entry that travels with the area is the likeliest of all to still name
    it, and the overview is both the likeliest and the most read. Reported
    under the name it will have, so the line can be acted on afterwards.
    """
    atlas(vault)

    made = carry_out(vault)

    assert "project/beacon/beacon.md" in made.left, made.left
    text = (vault.root / "project" / "beacon" / "beacon.md").read_text(encoding="utf-8")
    assert "The atlas platform" in text, "reported, not rewritten"


def test_the_index_of_the_emptied_folder_goes_and_the_new_one_arrives(vault):
    atlas(vault)

    carry_out(vault)

    assert not (vault.root / "project" / "atlas" / "index.md").exists()
    fresh = (vault.root / "project" / "beacon" / "index.md").read_text(encoding="utf-8")
    assert "[beacon](beacon.md)" in fresh, fresh
    above = (vault.root / "project" / "index.md").read_text(encoding="utf-8")
    assert "atlas" not in above, above


def test_a_move_is_one_commit_with_a_kind_of_its_own(git_vault):
    """One commit, because an area half moved is a vault that neither name
    finds. Its own kind, so the history can say which commits changed what the
    memory knows and which only renamed a folder."""
    atlas(git_vault)
    git_vault.git("add", "-A")
    git_vault.git("commit", "-q", "-m", "the area, by hand")

    carry_out(git_vault)

    message = git_vault.git("log", "-1", "--format=%B").stdout
    assert "Mabolo: move by human:alex" in message, message
    assert git_vault.git("status", "--porcelain").stdout.strip() == ""
    touched = git_vault.git("show", "--name-status", "--format=", "HEAD").stdout
    assert "project/beacon/beacon.md" in touched and "project/atlas/atlas.md" in touched


def test_the_moved_vault_still_validates(vault):
    atlas(vault)

    carry_out(vault)

    report = vault.validate()
    assert report.ok, report.render(vault.root)


@pytest.mark.parametrize("old,new,why", [
    ("persona", "project/beacon", "not a project area"),
    ("project/atlas", "hosts", "not a project area"),
    ("project/atlas", "project/atlas", "the same area"),
    ("project/nothing", "project/beacon", "there is no"),
])
def test_a_move_that_cannot_be_right_is_refused_before_anything_is_written(vault, old, new, why):
    atlas(vault)
    with pytest.raises(MaboloError, match=why):
        move.plan(vault, old, new)


def test_merging_two_areas_is_a_different_decision_and_is_refused(vault):
    """Renaming into an occupied folder is not a rename. It reads like one
    right up to the point where two entries share a name."""
    atlas(vault)
    area(vault, "project/beacon", {"beacon": entry_text(area="project/beacon", title="beacon")})

    with pytest.raises(MaboloError, match="already exists"):
        move.plan(vault, "project/atlas", "project/beacon")
