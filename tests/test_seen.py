"""What a session has already been shown, and why it is not the vault's business."""

import datetime as dt

from mabolo import consent, seen


def at(hours: float = 0.0) -> dt.datetime:
    return dt.datetime(2026, 9, 19, 12, 0, tzinfo=dt.timezone.utc) + dt.timedelta(hours=hours)


def test_what_was_shown_comes_back(tmp_path):
    seen.remember(["a-rule@1234"], session="s", at=at(), directory=tmp_path)
    assert seen.already("s", directory=tmp_path) == {"a-rule@1234"}


def test_a_corrected_rule_is_a_different_thing(tmp_path):
    """The key carries the revision. Keyed by name alone, a rule that was fixed
    would stay silent for the rest of the session that saw the old one."""
    seen.remember([seen.key("a-rule", "1111")], session="s", at=at(), directory=tmp_path)
    assert seen.key("a-rule", "2222") not in seen.already("s", directory=tmp_path)


def test_without_a_session_nothing_is_remembered(tmp_path):
    """Falling back to the directory would let two sessions in one project
    silence each other, and a rule that never arrived is the failure this
    project is against. Repetition is the visible cost, silence the hidden."""
    assert seen.remember(["a-rule@1"], session=None, at=at(), directory=tmp_path) is None
    assert seen.already(None, directory=tmp_path) == set()


def test_two_sessions_do_not_share_what_they_saw(tmp_path):
    seen.remember(["a-rule@1"], session="one", at=at(), directory=tmp_path)
    assert seen.already("two", directory=tmp_path) == set()


def test_the_notes_do_not_grow_without_end(tmp_path):
    for number in range(consent.MAX_LINES + 10):
        seen.remember([f"rule-{number}@1"], session="s", at=at(), directory=tmp_path)
    kept = seen.already("s", directory=tmp_path)
    assert len(kept) == consent.MAX_LINES
    assert f"rule-{consent.MAX_LINES + 9}@1" in kept, "the newest survive, not the oldest"
    assert "rule-0@1" not in kept


def test_a_note_that_cannot_be_written_is_not_an_error(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    assert seen.remember(["a@1"], session="s", directory=blocked / "deeper") is None


def test_a_broken_line_is_skipped_rather_than_fatal(tmp_path):
    seen.remember(["a-rule@1"], session="s", at=at(), directory=tmp_path)
    with (tmp_path / "s.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    assert seen.already("s", directory=tmp_path) == {"a-rule@1"}
