"""Saying so when Mabolo itself is not working: old code, and hooks that gave up."""

import io
import json
import os

from mabolo import doctor, health, server
from mabolo.cli import main
from mabolo.server import Settings, build

from test_server import call


def package(tmp_path):
    where = tmp_path / "pkg"
    where.mkdir()
    (where / "one.py").write_text("x = 1\n", encoding="utf-8")
    (where / "two.py").write_text("y = 2\n", encoding="utf-8")
    return where


def test_the_fingerprint_moves_when_the_code_on_disk_does(tmp_path):
    where = package(tmp_path)
    before = health.code_fingerprint(where)
    assert health.code_fingerprint(where) == before
    target = where / "two.py"
    target.write_text("y = 3\n", encoding="utf-8")
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert health.code_fingerprint(where) != before


def test_old_code_is_named_and_current_code_is_not(tmp_path):
    where = package(tmp_path)
    loaded = health.code_fingerprint(where)
    assert health.stale_note(loaded, where) == ""
    (where / "three.py").write_text("z = 3\n", encoding="utf-8")
    assert "Reconnect" in health.stale_note(loaded, where)


def test_a_server_on_old_code_says_so_in_every_answer(git_vault, tmp_path, monkeypatch):
    built = build(git_vault, Settings(actor="human:alex", cwd=tmp_path))
    assert health.STALE_NOTE not in call(built, "mabolo_search", {"query": "anything"})
    monkeypatch.setattr(server, "LOADED", "not-the-code-on-disk")
    said = call(built, "mabolo_search", {"query": "anything"})
    assert said.endswith(health.STALE_NOTE)
    refused = call(built, "mabolo_read", {"names": []})
    assert refused.endswith(health.STALE_NOTE)


def test_a_failure_is_kept_until_it_is_reported():
    health.record("prompt hook", "TimeoutError: slow")
    health.record("prompt hook", "OSError: gone")
    assert [item["error"] for item in health.pending()] == ["TimeoutError: slow", "OSError: gone"]
    assert "gave up 2 times" in health.summary(health.pending())
    assert "last: OSError: gone" in health.summary(health.pending())
    assert len(health.take()) == 2
    assert health.pending() == []


def test_the_record_does_not_grow_without_end():
    for number in range(health.KEEP + 20):
        health.record("file hook", f"failure {number}")
    kept = health.pending()
    assert len(kept) == health.KEEP
    assert kept[-1]["error"] == f"failure {health.KEEP + 19}"


def test_a_hook_that_gave_up_is_reported_by_the_next_session_start(tmp_path, capsys, monkeypatch):
    def broken(args):
        raise RuntimeError("the index is on fire")

    monkeypatch.setattr("mabolo.cli._prompt_payload", broken)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert main(["hook", "prompt"]) == 0
    capsys.readouterr()
    assert health.pending()

    config = str(tmp_path / "c.toml")
    main(["--config", config, "init", "--vault", str(tmp_path / "v"), "--yes", "--no-git", "--no-clients"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    main(["--config", config, "hook", "session-start"])
    text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert text.startswith("Mabolo had trouble since the last session: the prompt hook gave up once")
    assert "RuntimeError: the index is on fire" in text
    assert health.pending() == []


def test_doctor_shows_a_failure_without_using_it_up():
    health.record("session start", "_OutOfTime: no payload within 5 seconds")
    check = doctor._hooks_failed_check()
    assert not check.clean
    assert "session start gave up once" in check.findings[0].message
    assert health.pending()
    health.take()
    assert doctor._hooks_failed_check().clean
