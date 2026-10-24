"""The record of what went wrong with Mabolo itself, which outlives the notice."""

import io
import json

from mabolo import health, reports
from mabolo.cli import main
from mabolo.server import Settings, build

from test_server import call


def test_repeats_of_one_failure_are_one_problem_seen_often():
    first = reports.add(reports.HOOK, "file hook", "_OutOfTime: no payload within 2 seconds", kind="_OutOfTime")
    again = reports.add(reports.HOOK, "file hook", "_OutOfTime: no payload within 3 seconds", kind="_OutOfTime")
    other = reports.add(reports.HOOK, "file hook", "OSError: gone", kind="OSError")
    assert first == again != other
    waiting = reports.open_problems()
    assert [problem.id for problem in waiting] == [first, other]
    assert waiting[0].since == 2


def test_two_accounts_from_sessions_are_two_problems():
    one = reports.add(reports.SESSION, "proj", "the start block left out the rules")
    two = reports.add(reports.SESSION, "proj", "a search found nothing it should have")
    assert one != two


def test_a_settled_problem_stays_on_record_and_reopens_when_it_happens_again():
    found = reports.add(reports.TOOL, "mabolo_read tool", "OSError: disk", kind="OSError")
    assert reports.settle(found, reports.FIXED, commit="abc")
    assert reports.open_problems() == []
    assert reports.find(found).status["commit"] == "abc"
    reports.add(reports.TOOL, "mabolo_read tool", "OSError: disk again", kind="OSError")
    reopened = reports.open_problems()
    assert [problem.id for problem in reopened] == [found]
    assert reopened[0].since == 1, "only what happened after the fix counts"


def test_an_unknown_problem_or_status_is_not_settled():
    found = reports.add(reports.SESSION, "proj", "something")
    assert not reports.settle("00000000", reports.FIXED)
    assert not reports.settle(found, "solved")


def test_the_file_is_trimmed_without_losing_the_newest(monkeypatch):
    monkeypatch.setattr(reports, "KEEP", 5)
    for number in range(12):
        reports.add(reports.SESSION, "proj", f"report {number}")
    lines = reports.reports_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 10
    assert json.loads(lines[-1])["message"] == "report 11"


def test_a_broken_line_is_skipped_rather_than_fatal():
    reports.add(reports.SESSION, "proj", "first")
    with reports.reports_path().open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    reports.add(reports.SESSION, "proj", "second")
    assert len(reports.open_problems()) == 2


def test_a_hook_failure_outlives_the_notice_that_reported_it():
    """The evidence used to be gone once the session start had said it."""
    health.record("file hook", "_OutOfTime: no payload within 2 seconds")
    health.take()
    assert health.pending() == []
    kept = reports.open_problems()
    assert len(kept) == 1
    assert kept[0].source == reports.HOOK
    assert kept[0].where == "file hook"


def test_a_tool_that_broke_is_recorded_and_a_refusal_is_not(git_vault, tmp_path, monkeypatch):
    built = build(git_vault, Settings(actor="human:alex", cwd=tmp_path))
    assert call(built, "mabolo_read", {"names": ["nothing-like-this"]})
    assert reports.problems() == [], "a refusal is the gate working, not a fault"

    def broken(*args, **kwargs):
        raise OSError("the disk went away")

    monkeypatch.setattr(git_vault, "entries", broken)
    said = call(built, "mabolo_search", {"query": "anything"})
    assert said.startswith("refused: the disk went away")
    kept = reports.open_problems()
    assert [(problem.source, problem.where) for problem in kept] == [(reports.TOOL, "mabolo_search tool")]
    assert "the mabolo_search tool gave up once" in health.summary(health.pending())


def test_an_unexpected_exception_is_recorded_and_still_raised(git_vault, tmp_path, monkeypatch):
    built = build(git_vault, Settings(actor="human:alex", cwd=tmp_path))

    def broken(*args, **kwargs):
        raise KeyError("no such field")

    monkeypatch.setattr(git_vault, "entries", broken)
    # Raised on, so the client still sees an error and not a sentence that
    # reads like an answer.
    assert "Error executing tool" in call(built, "mabolo_search", {"query": "anything"})
    assert reports.open_problems()[0].last["message"].startswith("KeyError")


def test_a_session_can_report_a_fault_even_in_read_mode(git_vault, tmp_path):
    reading = build(git_vault, Settings(actor="human:alex", cwd=tmp_path / "proj", read_only=True))
    said = call(reading, "mabolo_report", {"what": "the map left out the rules", "expected": "the rules"})
    assert said.startswith("reported as ")
    kept = reports.open_problems()[0]
    assert kept.source == reports.SESSION
    assert kept.where == "proj"
    assert kept.last["message"] == "the map left out the rules\nexpected: the rules"
    assert not (git_vault.root / ".mabolo" / "reports.jsonl").exists(), "nothing goes into the vault"


def test_an_empty_report_is_refused(git_vault, tmp_path):
    built = build(git_vault, Settings(actor="human:alex", cwd=tmp_path))
    assert call(built, "mabolo_report", {"what": "  "}).startswith("refused:")
    assert reports.problems() == []


def test_the_command_lists_open_problems_and_closes_one(capsys):
    found = reports.add(reports.SESSION, "proj", "the start block left out the rules")
    assert main(["reports"]) == 0
    out = capsys.readouterr().out
    assert found in out and "the start block left out the rules" in out
    assert main(["reports", "--close", found, "--note", "not a fault"]) == 0
    capsys.readouterr()
    assert main(["reports"]) == 0
    assert capsys.readouterr().out.startswith("nothing is waiting")
    assert main(["reports", "--all"]) == 0
    assert "closed" in capsys.readouterr().out
    assert main(["reports", "--close", "00000000"]) != 0


def test_the_session_start_points_at_the_record(tmp_path, capsys, monkeypatch):
    health.record("prompt hook", "RuntimeError: on fire")
    config = str(tmp_path / "c.toml")
    main(["--config", config, "init", "--vault", str(tmp_path / "v"), "--yes", "--no-git", "--no-clients"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    main(["--config", config, "hook", "session-start"])
    text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "`mabolo reports` keeps it" in text
