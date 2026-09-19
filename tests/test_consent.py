"""The prompt notes, and what they are allowed to prove."""

import datetime as dt
import json
import os

from mabolo import consent


def at(hours: float = 0.0) -> dt.datetime:
    return dt.datetime(2026, 9, 19, 12, 0, tzinfo=dt.timezone.utc) + dt.timedelta(hours=hours)


def test_a_sentence_that_was_typed_is_verified(tmp_path):
    consent.record("remember that releases are cut from main", cwd=tmp_path, at=at(),
                   directory=tmp_path / "p")
    given = consent.check("releases are cut from main", cwd=tmp_path, now=at(1),
                          directory=tmp_path / "p")
    assert given.verified
    assert given.at == at()


def test_a_sentence_nobody_typed_is_not(tmp_path):
    consent.record("remember that releases are cut from main", cwd=tmp_path, at=at(),
                   directory=tmp_path / "p")
    given = consent.check("releases are cut from a tag", cwd=tmp_path, now=at(1),
                          directory=tmp_path / "p")
    assert not given.verified
    assert "no prompt" in given.reason


def test_capitalisation_and_spacing_do_not_decide(tmp_path):
    """Capitals on both sides, so that a comparison which only lowered the
    quote cannot pass this. It did once."""
    consent.record("Remember: Releases Are\nCut From Main", cwd=tmp_path, at=at(),
                   directory=tmp_path / "p")
    given = consent.check("releases  are cut from   main", cwd=tmp_path, now=at(1),
                          directory=tmp_path / "p")
    assert given.verified


def test_a_quote_under_the_floor_is_never_verified(tmp_path):
    consent.record("yes please do", cwd=tmp_path, at=at(), directory=tmp_path / "p")
    short = "yes please"
    assert len(short) == consent.MIN_QUOTE_CHARS - 2
    assert not consent.check(short, cwd=tmp_path, now=at(), directory=tmp_path / "p").verified
    # One character over the floor, out of the same prompt, is enough.
    longer = "yes please d"
    assert len(longer) == consent.MIN_QUOTE_CHARS
    assert consent.check(longer, cwd=tmp_path, now=at(), directory=tmp_path / "p").verified


def test_the_window_is_a_boundary_not_a_mood(tmp_path):
    consent.record("remember that the pilot runs in one region", cwd=tmp_path, at=at(),
                   directory=tmp_path / "p")
    quote = "the pilot runs in one region"
    inside = at(consent.WINDOW_HOURS) - dt.timedelta(seconds=1)
    outside = at(consent.WINDOW_HOURS) + dt.timedelta(seconds=1)
    assert consent.check(quote, cwd=tmp_path, now=inside, directory=tmp_path / "p").verified
    assert not consent.check(quote, cwd=tmp_path, now=outside, directory=tmp_path / "p").verified


def test_a_prompt_from_another_project_does_not_count(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    consent.record("remember that the pilot runs in one region", cwd=other, at=at(),
                   directory=tmp_path / "p")
    quote = "the pilot runs in one region"
    assert not consent.check(quote, cwd=tmp_path, now=at(1), directory=tmp_path / "p").verified
    assert consent.check(quote, cwd=other, now=at(1), directory=tmp_path / "p").verified


def test_a_named_session_beats_the_window_and_the_directory(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    consent.record("remember that the pilot runs in one region", cwd=other, at=at(),
                   session="abc", directory=tmp_path / "p")
    quote = "the pilot runs in one region"
    late = at(consent.WINDOW_HOURS + 5)
    assert consent.check(quote, cwd=tmp_path, session="abc", now=late,
                         directory=tmp_path / "p").verified
    # Another session's notes are not this session's evidence.
    assert not consent.check(quote, cwd=other, session="other", now=at(1),
                             directory=tmp_path / "p").verified


def test_a_secret_is_redacted_before_it_is_written(tmp_path):
    consent.record("use token=sk-abcdefghijklmnop for the call", cwd=tmp_path, at=at(),
                   directory=tmp_path / "p")
    written = (tmp_path / "p").glob("*.jsonl")
    text = "".join(path.read_text(encoding="utf-8") for path in written)
    assert "sk-abcdefghijklmnop" not in text
    assert "***" in text


def test_old_notes_are_forgotten(tmp_path):
    where = tmp_path / "p"
    consent.record("remember the thing that happened", cwd=tmp_path, at=at(), session="s",
                   directory=where)
    note = where / f"{consent.session_id('s')}.jsonl"
    # The age is the file's, so the test sets it rather than waiting a week.
    # Without it this would pass or fail depending on the day it is run on.
    stamp = at().timestamp()
    os.utime(note, (stamp, stamp))
    day = dt.timedelta(days=1).total_seconds() / 3600
    assert consent.forget_old(where, now=at(day * consent.RETENTION_DAYS - 1)) == 0
    assert note.exists()
    assert consent.forget_old(where, now=at(day * consent.RETENTION_DAYS + 1)) == 1
    assert not note.exists()


def test_a_long_session_does_not_grow_without_end(tmp_path):
    where = tmp_path / "p"
    for number in range(consent.MAX_LINES + 20):
        consent.record(f"sentence number {number}", cwd=tmp_path, at=at(), session="s",
                       directory=where)
    lines = (where / f"{consent.session_id('s')}.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == consent.MAX_LINES
    assert json.loads(lines[-1])["text"].endswith(str(consent.MAX_LINES + 19))


def test_a_prompt_that_cannot_be_written_does_not_raise(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    assert consent.record("something", cwd=tmp_path, directory=blocked / "p") is None


def test_a_broken_note_is_skipped_not_fatal(tmp_path):
    where = tmp_path / "p"
    consent.record("remember that the pilot runs in one region", cwd=tmp_path, at=at(),
                   session="s", directory=where)
    with (where / f"{consent.session_id('s')}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
    assert consent.check("the pilot runs in one region", cwd=tmp_path, now=at(1),
                         directory=where).verified
