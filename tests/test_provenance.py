"""Provenance: what an entry rests on, and where that evidence has a hole in it.

The cases lean on the shapes a real vault produces rather than on tidy ones: a
quote that wrapped in somebody's editor, a source whose footnote was deleted
with the sentence that cited it, a file that was renamed, a vault that is not a
Git repository at all. Those are the states `mabolo why` exists to describe, and
a report that only handles the tidy shape describes nothing worth checking.
"""

import dataclasses

import pytest

from conftest import entry_text
from mabolo import frontmatter, provenance
from mabolo.errors import MaboloError


def write(vault, name="beacon-ci", area="infra", body="text", **meta):
    """One entry on disk, the way a person would have typed it."""
    target = vault.root / area / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(entry_text(area=area, body=body, **meta), encoding="utf-8")
    return target


def commit(vault, message, *paths):
    vault.git("add", "--", *[str(p) for p in paths])
    vault.git("commit", "-qm", message)


def headings(text: str) -> list[str]:
    """The lines a reader takes as structure: every one that is not indented."""
    return [line for line in text.splitlines() if line and not line.startswith(" ")]


# Where the entry is


def test_the_file_is_named_the_way_it_is_spelled_inside_the_vault(vault):
    path = write(vault)
    found = provenance.of(vault, "beacon-ci")
    assert found.where == "infra/beacon-ci.md"
    assert found.path == path


def test_the_revision_is_the_one_a_writer_would_have_to_hand_back(vault):
    """`why` and `mabolo_edit` have to agree about which version was read.

    A report that quotes a sentence from one version and names the revision of
    another is how a person approves a diff against something they never saw.
    """
    path = write(vault)
    assert provenance.of(vault, "beacon-ci").revision == frontmatter.revision(path.read_bytes())


# Who produced it and who approved it


def test_generation_and_approval_come_back_in_the_order_they_happened(vault):
    """Not in the order the frontmatter lists them.

    `generated` is written above `verified` in every entry, and the two are
    written by two different machines. Here the approval is the older of the
    two, which is what an entry re-generated from a source somebody had already
    signed off looks like.
    """
    write(
        vault,
        generated={"by": "beacon/1.0", "at": "2026-08-14T12:00:00+00:00"},
        verified=[{"by": "human:alex", "at": "2026-08-14T10:00:00+00:00"}],
    )
    found = provenance.of(vault, "beacon-ci")
    assert [(a.kind, a.by) for a in found.acts] == [
        ("verified", "human:alex"),
        ("generated", "beacon/1.0"),
    ]


def test_a_timestamp_without_an_offset_is_still_ordered_against_one_that_has_one(vault):
    """The two halves of a provenance are written on two machines.

    One of them writes an offset and the other does not, and an order that
    cannot compare the two either raises or silently keeps the file's order.
    Read as UTC, the approval at 14:00+03:00 is the earlier moment.
    """
    write(
        vault,
        generated={"by": "beacon/1.0", "at": "2026-08-14T12:00:00"},
        verified=[{"by": "human:alex", "at": "2026-08-14T14:00:00+03:00"}],
    )
    assert [a.kind for a in provenance.of(vault, "beacon-ci").acts] == ["verified", "generated"]


def test_an_entry_that_names_nobody_says_so_instead_of_showing_an_empty_block(vault):
    """An entry with no provenance is the finding, so it gets a sentence.

    An empty block under a heading reads like a rendering fault and invites the
    reader to assume the report failed rather than that the entry is bare.
    """
    write(vault)
    found = provenance.of(vault, "beacon-ci")
    assert found.acts == ()
    assert "nobody is named" in found.render()


# What it rests on


def test_a_source_and_the_footnote_that_cites_it_come_back_with_the_sentence(vault):
    write(
        vault,
        body="Eight workers is the ceiling.[^s1]\n\n[^s1]: the runner dies above eight\n",
        sources=[{"id": "s1", "resource": "session://2026-08-14"}],
    )
    quote = provenance.of(vault, "beacon-ci").quotes[0]
    assert (quote.id, quote.status) == ("s1", provenance.QUOTED)
    assert quote.resource == "session://2026-08-14"
    assert quote.text == "the runner dies above eight"


def test_a_source_no_footnote_cites_is_reported_as_uncited(vault):
    """The frontmatter kept a source whose sentence was edited out of the prose."""
    write(
        vault,
        body="Eight workers is the ceiling.\n",
        sources=[{"id": "s1", "resource": "session://2026-08-14"}],
    )
    quotes = provenance.of(vault, "beacon-ci").quotes
    assert [(q.id, q.status, q.text) for q in quotes] == [("s1", provenance.UNCITED, None)]


def test_a_footnote_no_source_backs_is_reported_as_unbacked(vault):
    """The other half of the same failure, and it has to be just as visible.

    A footnote with no source is a sentence in quotation marks that says where
    it came from and is backed by nothing. Dropping it from the report because
    the frontmatter does not mention it would hide exactly the claim whose
    evidence went missing.
    """
    write(
        vault,
        body="Eight workers is the ceiling.[^s9]\n\n[^s9]: the runner dies above eight\n",
        sources=[{"id": "s1", "resource": "session://2026-08-14"}],
    )
    quotes = provenance.of(vault, "beacon-ci").quotes
    assert [(q.id, q.status) for q in quotes] == [
        ("s1", provenance.UNCITED),
        ("s9", provenance.UNBACKED),
    ]
    assert quotes[1].resource is None
    assert quotes[1].text == "the runner dies above eight"


def test_a_source_with_no_id_is_reported_as_one_nothing_can_cite(vault):
    """The importer writes these: it knows the file, it has no sentence.

    Calling it uncited would blame a person for a footnote they could never
    have written, because a footnote can only cite an id and this source has
    none.
    """
    write(vault, sources=[{"resource": "file://imported/notes.md"}])
    quotes = provenance.of(vault, "beacon-ci").quotes
    assert [(q.id, q.status) for q in quotes] == [(None, provenance.UNQUOTED)]


def test_a_footnote_that_wraps_over_two_lines_keeps_its_whole_sentence(vault):
    """Editors wrap, and half a quote reads exactly like a whole one.

    Cut at the wrap, this quote ends at "job", which is a sentence that says
    something the person never said.
    """
    write(
        vault,
        body=(
            "Eight workers is the ceiling.[^s1]\n\n"
            "[^s1]: the runner dies every time we bump the job\n"
            "  count, and nobody sees why\n"
        ),
        sources=[{"id": "s1", "resource": "session://2026-08-14"}],
    )
    quote = provenance.of(vault, "beacon-ci").quotes[0]
    assert quote.text == "the runner dies every time we bump the job count, and nobody sees why"


def test_a_resource_holding_a_newline_cannot_add_a_line_to_the_report(vault):
    """The report is read as structure, and its values come off disk.

    A resource is a string in somebody's frontmatter. Printed as it stands, it
    writes its own section heading and its own history line into a report a
    person is about to believe.
    """
    write(
        vault,
        sources=[{"id": "s1", "resource": "session://x\nhistory\n  0000000  2026-01-01  added  none"}],
    )
    text = provenance.of(vault, "beacon-ci").render()
    assert headings(text) == ["beacon-ci", "who and when", "evidence", "anchor", "history"]
    assert any("session://x history 0000000" in line for line in text.splitlines())


# What Git says


def test_every_commit_that_touched_the_file_is_read_with_its_id_date_and_subject(git_vault):
    path = write(git_vault)
    commit(git_vault, "Add the ceiling on workers", path)
    path.write_text(entry_text(body="more"), encoding="utf-8")
    commit(git_vault, "Say why eight is the ceiling", path)

    found = provenance.of(git_vault, "beacon-ci")
    assert [c.subject for c in found.commits] == [
        "Say why eight is the ceiling",
        "Add the ceiling on workers",
    ]
    assert all(c.revision and c.at for c in found.commits)
    assert found.history_note is None


def test_a_commit_that_touched_only_another_entry_is_not_in_this_history(git_vault):
    """A history that lists the whole vault answers a question nobody asked."""
    mine = write(git_vault, "beacon-ci")
    commit(git_vault, "Add the ceiling on workers", mine)
    other = write(git_vault, "atlas-release")
    commit(git_vault, "Add the release rule", other)

    found = provenance.of(git_vault, "beacon-ci")
    assert [c.subject for c in found.commits] == ["Add the ceiling on workers"]


def test_a_commit_says_whether_it_added_changed_or_renamed_the_file(git_vault):
    """"Changed" and "added" are different answers to "where did this come from"."""
    path = write(git_vault)
    commit(git_vault, "Add the ceiling on workers", path)
    path.write_text(entry_text(body="more"), encoding="utf-8")
    commit(git_vault, "Say why eight is the ceiling", path)

    found = provenance.of(git_vault, "beacon-ci")
    assert [c.change for c in found.commits] == [provenance.CHANGED, provenance.ADDED]


def test_a_renamed_entry_keeps_the_commits_from_before_the_rename(git_vault):
    """Without this the history of a renamed entry starts at the rename.

    An entry that was renamed then looks like it was invented that day, which
    is the one reading a provenance report must never produce.
    """
    old = write(git_vault, "beacon-ci")
    commit(git_vault, "Add the ceiling on workers", old)
    new = old.with_name("beacon-workers.md")
    git_vault.git("mv", str(old), str(new))
    git_vault.git("commit", "-qm", "Rename it after what it is about")

    found = provenance.of(git_vault, "beacon-workers")
    assert [c.subject for c in found.commits] == [
        "Rename it after what it is about",
        "Add the ceiling on workers",
    ]
    assert found.commits[0].change == provenance.RENAMED
    assert found.commits[0].previous == "infra/beacon-ci.md"


def test_a_vault_that_is_no_git_repository_gets_a_sentence_instead_of_a_failure(vault):
    """A vault is a folder of Markdown. Git is how it syncs, not what it is."""
    write(vault)
    found = provenance.of(vault, "beacon-ci")
    assert found.commits == ()
    assert "not a Git repository" in found.history_note


def test_a_file_in_no_commit_yet_says_so_rather_than_showing_an_empty_history(git_vault):
    """The state every entry passes through, between being written and committed."""
    write(git_vault)
    found = provenance.of(git_vault, "beacon-ci")
    assert found.commits == ()
    assert "in no commit yet" in found.history_note


# Finding the entry at all


def test_an_alias_finds_the_entry_it_was_renamed_from(vault):
    """A rename keeps the old name as an alias, and `why` is what follows a rename."""
    write(vault, mabolo={"aliases": ["ci-ceiling"]})
    assert provenance.of(vault, "ci-ceiling").name == "beacon-ci"


def test_a_name_that_is_in_two_files_is_refused_rather_than_guessed_at(vault):
    """Two files of one name is a finding of its own, and picking one hides it."""
    write(vault, "beacon-ci", area="infra")
    write(vault, "beacon-ci", area="persona")
    with pytest.raises(MaboloError) as raised:
        provenance.of(vault, "beacon-ci")
    assert "infra/beacon-ci.md" in str(raised.value)
    assert "persona/beacon-ci.md" in str(raised.value)


def test_an_unknown_name_is_refused_by_name(vault):
    write(vault)
    with pytest.raises(MaboloError) as raised:
        provenance.of(vault, "no-such-entry")
    assert "no-such-entry" in str(raised.value)


# The anchor, which somebody else answers


def test_the_anchor_note_is_the_callers_to_fill_in_and_says_so_until_it_is(vault):
    """Drift detection is not this module's, so the report must not imply it ran.

    An anchor printed on its own reads like a clean bill of health. The blank
    is named instead, and a caller that has the answer replaces the sentence.
    """
    write(vault, mabolo={"anchor": ".ci/build.yml"})
    found = provenance.of(vault, "beacon-ci")
    assert found.anchor == ".ci/build.yml"
    assert found.anchor_note is None
    assert "was not checked" in found.render()

    answered = dataclasses.replace(found, anchor_note="it still points at a file that is there")
    assert "it still points at a file that is there" in answered.render()


# The report as a whole


def test_the_report_answers_all_four_questions_in_one_read(vault):
    """Four blocks, always, in this order, whether or not each one has an answer."""
    write(vault)
    assert headings(provenance.of(vault, "beacon-ci").render()) == [
        "beacon-ci",
        "who and when",
        "evidence",
        "anchor",
        "history",
    ]


def test_a_quote_the_file_already_spelled_in_quotes_is_not_quoted_twice(vault):
    """`why` shows the sentence a person typed, and the one place a reader
    checks the evidence must not look like it garbled it. Mabolo writes the
    footnote quoted, so wrapping it again printed a doubled pair."""
    write(
        vault,
        sources=[{"id": "q1", "resource": "session://2026-09-19"}],
        body='Releases are cut from `main`. [^q1]\n\n[^q1]: "releases are cut from main only"\n',
    )
    printed = provenance.of(vault, "beacon-ci").render()
    assert '"releases are cut from main only"' in printed
    assert '""' not in printed
