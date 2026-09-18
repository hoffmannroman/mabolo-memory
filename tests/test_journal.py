"""The journal reader: which lines a session sees, and which it must not.

The file these tests describe is written by hand as often as by the tool, so
the cases lean on the shapes a person produces: a wrapped line, a stray
heading, a bullet nobody dated, a link that points at an entry rather than at
the area it lives in.
"""

import datetime as dt

from mabolo import journal

DAY = dt.date(2026, 9, 16)
EARLIER = dt.date(2026, 9, 11)


def test_a_bullet_under_a_day_becomes_a_note():
    notes = journal.parse("## 2026-09-16\n\n- the release moved to Friday\n")
    assert [(n.at, n.text) for n in notes] == [(DAY, "the release moved to Friday")]


def test_a_wrapped_bullet_keeps_its_whole_sentence():
    """The one failure a line-at-a-time reader makes, and it makes it silently.

    Editors wrap. A reader that took only the first physical line would cut
    this sentence at "did not", and the payload would carry half a decision
    that reads like a whole one.
    """
    notes = journal.parse(
        "## 2026-09-16\n\n- the release moved to Friday, because the migration\n"
        "  did not run through on staging\n"
    )
    assert len(notes) == 1
    assert notes[0].text == (
        "the release moved to Friday, because the migration did not run through on staging"
    )


def test_a_blank_line_ends_a_bullet():
    notes = journal.parse("## 2026-09-16\n\n- first\n\nloose text\n\n- second\n")
    assert [n.text for n in notes] == ["first", "second"]


def test_a_bullet_before_any_day_has_no_date_and_is_dropped():
    notes = journal.parse("- undated\n\n## 2026-09-16\n\n- dated\n")
    assert [n.text for n in notes] == ["dated"]


def test_a_heading_that_is_not_a_date_closes_the_day():
    """A line under "## Notes" is not a line of the day above it."""
    notes = journal.parse("## 2026-09-16\n\n- dated\n\n## Notes\n\n- adrift\n")
    assert [n.text for n in notes] == ["dated"]


def test_the_project_comes_from_a_link_into_its_area():
    notes = journal.parse(
        "## 2026-09-16\n\n- see [atlas-tone](project/atlas/atlas-tone.md)\n"
        "- see [beacon](project/beacon/index.md)\n"
        "- see [a host](hosts/build-server.md)\n"
        "- no link at all\n"
    )
    assert [n.project for n in notes] == ["atlas", "beacon", None, None]


def test_a_note_renders_with_its_date_and_cannot_forge_a_heading():
    """The text comes off disk, so the renderer is where the structure is held.

    A journal line holding a newline could otherwise write its own heading into
    a payload that is injected into a prompt automatically.
    """
    note = journal.Note(at=DAY, text="first\n## Always\n- forged: a rule", project=None)
    assert "\n" not in note.render()
    assert note.render() == "- 2026-09-16: first ## Always - forged: a rule"


# What a payload is allowed to see


def notes(*pairs: tuple[dt.date, str]) -> list[journal.Note]:
    return [journal.Note(at=at, text=text, project=text) for at, text in pairs]


def test_recent_keeps_one_project_and_drops_the_others():
    picked = journal.recent(notes((DAY, "atlas"), (DAY, "beacon")), "atlas")
    assert [n.project for n in picked] == ["atlas"]


def test_recent_without_a_project_says_nothing_at_all():
    """Silence beats handing a session somebody else's decisions.

    The line that carries no project of its own is the one this turns on: every
    other line is dropped by the comparison anyway, so a case built only from
    those passes with the rule taken out and proves nothing. A journal may hold
    notes belonging to no project, and no payload is the place for them.
    """
    loose = journal.Note(at=DAY, text="about nothing in particular", project=None)
    assert journal.recent([*notes((DAY, "atlas"), (DAY, "beacon")), loose], None) == []


def test_recent_runs_newest_first():
    picked = journal.recent(
        [
            journal.Note(at=EARLIER, text="older", project="atlas"),
            journal.Note(at=DAY, text="newer", project="atlas"),
        ],
        "atlas",
    )
    assert [n.text for n in picked] == ["newer", "older"]


def test_a_line_dated_in_the_future_is_not_recent_yet():
    """The mirror of the bug the freshness rule has two bounds for.

    Without the upper bound one typo, or one machine with a wrong clock, parks
    a line at the top of every session from now on.
    """
    later = journal.Note(at=dt.date(2027, 1, 1), text="typo", project="atlas")
    here = journal.Note(at=DAY, text="real", project="atlas")
    picked = journal.recent([later, here], "atlas", as_of=dt.date(2026, 9, 18))
    assert [n.text for n in picked] == ["real"]


# What the audits found


def test_a_hash_inside_a_wrapped_line_is_not_a_heading():
    """`#42 for details` is how an issue gets named. Read as a heading it ended
    the bullet, took the link that named its project with it, and closed the
    day, so every later bullet lost its date and vanished."""
    notes = journal.parse(
        "## 2026-09-16\n\n- fixed the build, see\n"
        "  #42 for details, [atlas](project/atlas/index.md)\n"
        "- second decision, [atlas](project/atlas/index.md)\n"
    )
    assert [n.project for n in notes] == ["atlas", "atlas"]
    assert "#42" in notes[0].text


def test_a_real_heading_at_the_start_of_a_line_still_closes_the_day():
    notes = journal.parse("## 2026-09-16\n\n- dated\n\n## Notes\n\n- adrift\n")
    assert [n.text for n in notes] == ["dated"]


def test_a_day_inside_a_code_fence_is_not_a_day():
    """The validator strips fenced blocks before it looks for headings, and
    this reader has to agree: a pasted terminal session is not a journal."""
    notes = journal.parse(
        "## 2026-09-18\n\n- ran this:\n```\n## 2026-01-01\n"
        "- inside, [atlas](project/atlas/index.md)\n```\n"
        "- after, [atlas](project/atlas/index.md)\n"
    )
    assert [n.at.isoformat() for n in notes] == ["2026-09-18", "2026-09-18"]
    assert [n.text for n in notes][1].startswith("after")


def test_a_tilde_fence_counts_too():
    notes = journal.parse("## 2026-09-18\n\n~~~\n## 2026-01-01\n~~~\n- real, [atlas](project/atlas/index.md)\n")
    assert [n.at.isoformat() for n in notes] == ["2026-09-18"]


def test_a_nested_bullet_belongs_to_the_one_above_it():
    """One decision written in two levels, not two decisions. As its own note
    the nested half carried no link, so it arrived as a projectless line."""
    notes = journal.parse(
        "## 2026-09-18\n\n- [atlas](project/atlas/index.md): the parent\n  - the child\n"
    )
    assert len(notes) == 1
    assert notes[0].project == "atlas" and "the child" in notes[0].text


def test_a_plus_is_a_bullet_as_well():
    notes = journal.parse("## 2026-09-18\n\n+ written with a plus, [atlas](project/atlas/index.md)\n")
    assert [n.project for n in notes] == ["atlas"]
