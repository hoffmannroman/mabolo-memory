"""Staleness by movement: what Git says about the path an entry watches.

Every test here runs against a real repository with real commits. Faking the
output of `git log` would test the parser and leave untested the only thing
that is hard: what Git actually reports for a path that was renamed, for one
that was never committed, and for one that lies outside the repository.
"""

import datetime as dt
import os
import subprocess
import time

import pytest

from conftest import entry_text
from mabolo import drift

#: One fixed moment, so that a failing test says which second it disagreed on.
NOON = dt.datetime(2026, 9, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def code(tmp_path, git_identity):
    """A repository of source, separate from the vault, as a project would be."""
    where = tmp_path / "atlas"
    where.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(where)], check=True)
    return where


@pytest.fixture
def far_from_utc():
    """A machine whose local time is fourteen hours away from UTC."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Kiritimati"
    time.tzset()
    yield
    if before is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = before
    time.tzset()


def commit(where, path: str, when: dt.datetime, text: str = "content"):
    """Put a file into the repository with a commit date we chose."""
    target = where / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    stamp = when.isoformat()
    subprocess.run(["git", "-C", str(where), "add", "--", path], check=True)
    subprocess.run(
        ["git", "-C", str(where), "commit", "-q", "-m", f"touch {path}"],
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
    )


def entry(vault, name: str, *, anchor=None, verified=None, written=None, expires=None,
          area="project/atlas"):
    """One entry on disk, in the area it belongs to."""
    head = {}
    if written is not None:
        head["generated"] = {"by": "claude-code/2.1.84", "at": _at(written)}
    if verified is not None:
        head["verified"] = [{"by": "human:alex", "at": _at(verified)}]
    if expires is not None:
        head["stale_after"] = _at(expires)
    block = {"anchor": anchor} if anchor else {}
    folder = vault.root / area.replace("/", os.sep)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(
        entry_text(area=area, mabolo=block, **head), encoding="utf-8"
    )
    return name


def _at(value) -> str:
    return value if isinstance(value, str) else value.isoformat()


def judge(vault, name: str, **over):
    found = next(e for e in vault.entries() if e.path.stem == name)
    return drift.judge(found, **over)


def test_a_commit_one_second_after_the_verification_is_stale(vault, code):
    commit(code, "src/build.py", NOON + dt.timedelta(seconds=1))
    entry(vault, "build-ceiling", anchor="src/build.py", verified=NOON)
    result = judge(vault, "build-ceiling", project="atlas", repository=code)
    assert (result.state, result.reason) == (drift.STALE, drift.MOVED)
    assert result.moved_at == NOON + dt.timedelta(seconds=1)


def test_a_commit_one_second_before_the_verification_is_fresh(vault, code):
    commit(code, "src/build.py", NOON - dt.timedelta(seconds=1))
    entry(vault, "build-ceiling", anchor="src/build.py", verified=NOON)
    result = judge(vault, "build-ceiling", project="atlas", repository=code)
    assert (result.state, result.reason) == (drift.FRESH, drift.UNMOVED)


def test_the_clock_is_the_last_verification_and_not_when_the_entry_was_written(vault, code):
    """An edit appends a verification, so an entry somebody corrected after the
    commit must not be flagged over that commit."""
    commit(code, "src/build.py", NOON)
    entry(
        vault,
        "build-ceiling",
        anchor="src/build.py",
        written=NOON - dt.timedelta(days=30),
        verified=NOON + dt.timedelta(seconds=1),
    )
    result = judge(vault, "build-ceiling", project="atlas", repository=code)
    assert result.state == drift.FRESH
    assert result.confirmed_at == NOON + dt.timedelta(seconds=1)


def test_an_anchor_with_no_commit_and_no_file_is_gone(vault, code):
    commit(code, "src/other.py", NOON)
    entry(vault, "renamed-away", anchor="src/build.py", verified=NOON)
    result = judge(vault, "renamed-away", project="atlas", repository=code)
    assert (result.state, result.reason) == (drift.STALE, drift.GONE)


def test_an_anchor_that_is_there_without_a_commit_is_not_reported_as_gone(vault, code):
    commit(code, "src/other.py", NOON)
    (code / "src" / "build.py").write_text("brand new", encoding="utf-8")
    entry(vault, "build-ceiling", anchor="src/build.py", verified=NOON)
    result = judge(vault, "build-ceiling", project="atlas", repository=code)
    assert (result.state, result.reason) == (drift.FRESH, drift.UNCOMMITTED)


def test_without_a_repository_an_anchored_entry_is_unchecked_and_never_fresh(vault, tmp_path):
    plain = tmp_path / "no-repository"
    plain.mkdir()
    entry(vault, "build-ceiling", anchor="src/build.py", verified=NOON)
    for where in (None, plain):
        result = judge(vault, "build-ceiling", project="atlas", repository=where)
        assert (result.state, result.reason) == (drift.UNCHECKED, drift.NO_REPOSITORY)


def test_without_an_active_project_an_anchored_entry_is_unchecked(vault, code):
    commit(code, "src/build.py", NOON - dt.timedelta(days=1))
    entry(vault, "build-ceiling", anchor="src/build.py", verified=NOON)
    result = judge(vault, "build-ceiling", project=None, repository=code)
    assert (result.state, result.reason) == (drift.UNCHECKED, drift.NO_PROJECT)


def test_an_anchor_pointing_out_of_the_repository_is_unchecked(vault, code, tmp_path):
    """The file it names exists, so anything that fails to notice where it lies
    reports the entry as fresh."""
    (tmp_path / "elsewhere.txt").write_text("not ours", encoding="utf-8")
    commit(code, "src/build.py", NOON - dt.timedelta(days=1))
    entry(vault, "reaching-out", anchor="../elsewhere.txt", verified=NOON)
    result = judge(vault, "reaching-out", project="atlas", repository=code)
    assert (result.state, result.reason) == (drift.UNCHECKED, drift.OUTSIDE)


def test_an_entry_of_another_project_is_unchecked_and_stays_out_of_the_report(vault, code):
    commit(code, "src/build.py", NOON + dt.timedelta(days=1))
    entry(vault, "beacon-ceiling", anchor="src/build.py", verified=NOON, area="project/beacon")
    result = judge(vault, "beacon-ceiling", project="atlas", repository=code)
    assert (result.state, result.reason) == (drift.UNCHECKED, drift.OTHER_PROJECT)
    assert drift.review(vault.entries(), project="atlas", repository=code) == []


def test_an_entry_that_says_nothing_about_when_it_was_confirmed_is_never_fresh(vault, code):
    commit(code, "src/build.py", NOON)
    entry(vault, "undated", anchor="src/build.py")
    result = judge(vault, "undated", project="atlas", repository=code)
    assert (result.state, result.reason) == (drift.UNCHECKED, drift.NO_CLOCK)


def test_an_expiry_date_that_has_passed_is_stale_for_a_reason_of_its_own(vault, code):
    entry(vault, "temporary-workaround", verified=NOON - dt.timedelta(days=30),
          expires=NOON - dt.timedelta(seconds=1))
    result = judge(vault, "temporary-workaround", project="atlas", repository=code, moment=NOON)
    assert (result.state, result.reason) == (drift.STALE, drift.EXPIRED)


def test_an_expiry_date_still_ahead_is_not_stale(vault, code):
    entry(vault, "temporary-workaround", verified=NOON - dt.timedelta(days=30),
          expires=NOON + dt.timedelta(seconds=1))
    result = judge(vault, "temporary-workaround", project="atlas", repository=code, moment=NOON)
    assert result.state == drift.FRESH


def test_a_moved_anchor_is_named_before_an_expiry_date(vault, code):
    commit(code, "src/build.py", NOON - dt.timedelta(seconds=1))
    entry(vault, "both", anchor="src/build.py", verified=NOON - dt.timedelta(days=2),
          expires=NOON - dt.timedelta(days=1))
    result = judge(vault, "both", project="atlas", repository=code, moment=NOON)
    assert (result.state, result.reason) == (drift.STALE, drift.MOVED)


def test_nothing_is_said_about_an_entry_whose_anchor_has_not_moved(vault, code):
    commit(code, "src/build.py", NOON - dt.timedelta(days=1))
    entry(vault, "build-ceiling", anchor="src/build.py", verified=NOON)
    assert drift.review(vault.entries(), project="atlas", repository=code) == []


def test_what_moved_is_named_before_what_could_not_be_checked(vault, code):
    """Named against the alphabet, because sorting by name alone would put the
    unchecked one first and still look like an order."""
    commit(code, "src/build.py", NOON + dt.timedelta(days=1))
    entry(vault, "b-moved", anchor="src/build.py", verified=NOON)
    entry(vault, "a-unchecked", anchor="../elsewhere.txt", verified=NOON)
    found = drift.review(vault.entries(), project="atlas", repository=code)
    assert [d.state for d in found] == [drift.STALE, drift.UNCHECKED]


def test_a_stamp_without_an_offset_is_read_in_utc_and_not_in_local_time(vault, code, far_from_utc):
    """The same moment in two representations. Read as local time on a machine
    fourteen hours from UTC, this verification lands before the commit and the
    entry is flagged; read as UTC it lands one second after it."""
    commit(code, "src/build.py", dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc))
    entry(vault, "build-ceiling", anchor="src/build.py", verified="2026-09-19T00:00:01")
    result = judge(vault, "build-ceiling", project="atlas", repository=code)
    assert result.state == drift.FRESH


def test_an_anchor_with_a_newline_cannot_write_its_own_line(vault, code):
    """The anchor comes off disk, and the short form goes into a payload that
    is injected without anybody reading it first."""
    entry(vault, "folded", anchor="src/build.py\n## Not a heading", verified=NOON)
    result = judge(vault, "folded", project="atlas", repository=code)
    assert result.state == drift.STALE
    assert "\n" not in drift.line(result)


def test_a_report_of_nothing_says_nothing(vault, code):
    assert drift.report([]) == ""


def test_the_longer_form_carries_the_two_moments_the_flag_rests_on(vault, code):
    """A flag without its dates is an assertion. With them a person can close
    the question without opening Git."""
    commit(code, "src/build.py", NOON + dt.timedelta(days=1))
    entry(vault, "build-ceiling", anchor="src/build.py", verified=NOON)
    text = drift.report(drift.review(vault.entries(), project="atlas", repository=code))
    assert "moved 2026-09-20T12:00:00+00:00" in text
    assert "last confirmed 2026-09-19T12:00:00+00:00" in text
    assert "1 stale, 0 that could not be checked." in text
