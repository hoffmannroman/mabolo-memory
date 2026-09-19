"""What doctor finds, what it stays quiet about, and what it refuses to claim.

The last of those is the point of the file. Almost every test here has a second
half asserting silence on a healthy vault, because a doctor that reports
something on every run is one nobody reads, and a check that cannot run has its
own tests, because counting it as clean is the failure the command exists
against.
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from conftest import entry_text

from mabolo import consent, doctor, git, write
from mabolo.config import Config

BROKEN = "---\ntype: [unclosed\n---\n\nx\n"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    """The disposable notes and the write lock live here and nowhere real."""
    where = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(where))
    return where


def messages(report, group):
    return [f.message for f in report.findings if f.group == group]


def check(report, name):
    return next(c for c in report.checks if c.name == name)


def commit(vault, message, *, kind=None, path="infra/thing.md", body="text", actor="human:alex"):
    """One commit in a test repository, with or without Mabolo's trailer."""
    target = vault.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    vault.git("add", "--", path)
    vault.git("commit", "-q", "-m", write.signed(message, kind, actor) if kind else message)


def configuration(tmp_path, root, name="config.toml"):
    path = tmp_path / name
    Config(vault=root, actor="human:alex").save(path)
    return path


def write_entry(vault, where, **meta):
    target = vault.root / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(entry_text(**meta), encoding="utf-8")
    return target


# Configuration and wiring.


def test_a_configuration_anybody_can_read_is_a_finding(tmp_path, vault):
    path = configuration(tmp_path, vault.root)
    assert not [m for m in messages(doctor.examine(config_path=path), doctor.CONFIGURATION) if "mode" in m]
    path.chmod(0o644)
    found = messages(doctor.examine(config_path=path), doctor.CONFIGURATION)
    assert any("mode 0644" in m for m in found)


def test_a_configuration_that_is_not_there_is_a_finding(tmp_path, vault):
    found = messages(doctor.examine(config_path=tmp_path / "nothing.toml"), doctor.CONFIGURATION)
    assert any("there is no configuration" in m for m in found)


def test_a_vault_that_is_not_there_is_a_finding(tmp_path):
    path = configuration(tmp_path, tmp_path / "gone")
    found = messages(doctor.examine(config_path=path), doctor.CONFIGURATION)
    assert any("is not there" in m for m in found)


def test_git_missing_is_a_finding(vault, monkeypatch):
    monkeypatch.setattr(git, "available", lambda: False)
    found = messages(doctor.examine(root=vault.root), doctor.CONFIGURATION)
    assert any("git is not installed" in m for m in found)


def test_a_missing_git_identity_is_a_finding(git_vault, tmp_path, monkeypatch):
    assert check(doctor.examine(root=git_vault.root), "config.identity").clean
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    (Path(os.environ["HOME"]) / ".gitconfig").unlink()
    found = messages(doctor.examine(root=git_vault.root), doctor.CONFIGURATION)
    assert any("user.email" in m for m in found)


def test_a_wiring_file_that_is_gone_is_a_finding(tmp_path, vault):
    report = doctor.examine(root=vault.root, clients=[tmp_path / "client.json"])
    assert any("is gone" in m for m in messages(report, doctor.CONFIGURATION))


def test_a_wiring_that_names_no_mabolo_command_is_a_finding(tmp_path, vault):
    wiring = tmp_path / "client.json"
    wiring.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    report = doctor.examine(root=vault.root, clients=[wiring])
    assert any("names no mabolo command" in m for m in messages(report, doctor.CONFIGURATION))


def test_a_wiring_that_points_at_another_executable_is_a_finding(tmp_path, vault):
    here = tmp_path / "here" / "mabolo"
    other = tmp_path / "elsewhere" / "mabolo"
    for one in (here, other):
        one.parent.mkdir(parents=True)
        one.write_text("#!/bin/sh\n", encoding="utf-8")
    wiring = tmp_path / "client.json"
    wiring.write_text(json.dumps({"mcpServers": {"mabolo": {"command": str(here)}}}), encoding="utf-8")
    quiet = doctor.examine(root=vault.root, clients=[wiring], executable=str(here))
    assert check(quiet, "wiring.executable").clean
    wiring.write_text(json.dumps({"mcpServers": {"mabolo": {"command": str(other)}}}), encoding="utf-8")
    loud = doctor.examine(root=vault.root, clients=[wiring], executable=str(here))
    assert any("points at" in m for m in messages(loud, doctor.CONFIGURATION))


def test_wiring_nobody_supplied_is_not_run_rather_than_clean(vault):
    report = doctor.examine(root=vault.root)
    assert [c.name for c in report.not_run if c.name.startswith("wiring.")] == [
        "wiring.present",
        "wiring.executable",
    ]


# The repository.


def test_a_merge_in_progress_is_a_finding(git_vault):
    assert check(doctor.examine(root=git_vault.root), "repository.operation").clean
    (git_vault.root / ".git" / "MERGE_HEAD").write_text("x", encoding="utf-8")
    found = messages(doctor.examine(root=git_vault.root), doctor.REPOSITORY)
    assert any("a merge is running" in m for m in found)


def test_a_detached_head_is_a_finding(git_vault):
    assert check(doctor.examine(root=git_vault.root), "repository.head").clean
    git_vault.git("checkout", "--detach", "--quiet", "HEAD")
    found = messages(doctor.examine(root=git_vault.root), doctor.REPOSITORY)
    assert any("HEAD is detached" in m for m in found)


def test_a_lock_held_longer_than_the_timeout_is_a_finding(git_vault):
    with write.lock(git_vault.root):
        path = write._lock_path(git_vault.root)
        stamp = time.time() - write.LOCK_SECONDS - 60
        os.utime(path, (stamp, stamp))
        report = doctor.examine(root=git_vault.root)
    assert any("held the lock" in m for m in messages(report, doctor.REPOSITORY))


def test_a_lock_file_nobody_holds_is_not_a_finding(git_vault):
    """`write.lock` never removes the file, so its age alone means nothing, and
    a check on the file lying there would fire on every vault ever written to."""
    with write.lock(git_vault.root):
        pass
    path = write._lock_path(git_vault.root)
    stamp = time.time() - write.LOCK_SECONDS - 60
    os.utime(path, (stamp, stamp))
    assert check(doctor.examine(root=git_vault.root), "repository.lock").clean


def test_a_vault_that_is_no_repository_leaves_the_repository_checks_not_run(vault):
    report = doctor.examine(root=vault.root)
    names = [c.name for c in report.not_run if c.group == doctor.REPOSITORY]
    assert names == ["repository.operation", "repository.head", "repository.lock"]
    assert all("not a Git repository" in c.reason for c in report.not_run if c.name in names)


# The remote.


def test_a_vault_in_step_with_its_remote_says_nothing(git_vault, remote):
    report = doctor.examine(root=git_vault.root)
    assert messages(report, doctor.REMOTE) == []
    assert [c.name for c in report.clean if c.group == doctor.REMOTE] == [
        "remote.reachable",
        "remote.unpushed",
        "remote.behind",
    ]


def test_a_vault_without_a_remote_registers_no_remote_check(git_vault):
    report = doctor.examine(root=git_vault.root)
    assert [c.name for c in report.checks if c.group == doctor.REMOTE] == []


def test_a_remote_that_cannot_be_reached_is_a_finding_and_the_rest_is_not_run(git_vault, remote):
    shutil.rmtree(remote)
    report = doctor.examine(root=git_vault.root)
    assert any("could not be reached" in m for m in messages(report, doctor.REMOTE))
    assert [c.name for c in report.not_run if c.group == doctor.REMOTE] == [
        "remote.unpushed",
        "remote.behind",
    ]
    assert [c.name for c in report.clean if c.group == doctor.REMOTE] == []


def test_commits_that_are_not_on_the_remote_are_a_finding(git_vault, remote):
    commit(git_vault, "one more")
    found = messages(doctor.examine(root=git_vault.root), doctor.REMOTE)
    assert any("1 commits on main are not on origin" in m for m in found)


def test_a_remote_ahead_of_this_clone_is_a_finding(git_vault, remote, tmp_path):
    other = tmp_path / "second"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    (other / "elsewhere.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "from another machine"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q"], check=True)
    git_vault.git("fetch", "--quiet", "origin")
    found = messages(doctor.examine(root=git_vault.root), doctor.REMOTE)
    assert any("ahead of this clone" in m for m in found)


def test_a_remote_tip_this_clone_never_fetched_is_a_finding_and_the_count_is_not_run(
    git_vault, remote, tmp_path
):
    other = tmp_path / "second"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    (other / "elsewhere.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-qm", "from another machine"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q"], check=True)
    report = doctor.examine(root=git_vault.root)
    assert any("never seen" in m for m in messages(report, doctor.REMOTE))
    assert [c.name for c in report.not_run if c.group == doctor.REMOTE] == ["remote.unpushed"]


# The history.


def test_a_repository_without_an_adopt_commit_says_so_once(git_vault):
    commit(git_vault, "one")
    commit(git_vault, "two", path="infra/other.md")
    report = doctor.examine(root=git_vault.root)
    assert messages(report, doctor.HISTORY) == [
        "no adopt commit, so there is no point where Mabolo's own history starts"
    ]
    assert [c.name for c in report.not_run if c.group == doctor.HISTORY] == [
        "history.origin",
        "history.paths",
    ]


def test_commits_before_the_boundary_are_one_line(git_vault):
    commit(git_vault, "an older change", path="infra/old.md")
    commit(git_vault, "adopt", kind="adopt", path="infra/a.md")
    found = messages(doctor.examine(root=git_vault.root), doctor.HISTORY)
    assert found == ["2 commits before Mabolo, not classified"]


def test_a_commit_after_the_boundary_without_a_trailer_is_a_finding(git_vault):
    commit(git_vault, "adopt", kind="adopt", path="infra/a.md")
    commit(git_vault, "written by mabolo", kind="write", path="infra/b.md")
    assert not [
        m
        for m in messages(doctor.examine(root=git_vault.root), doctor.HISTORY)
        if "no Mabolo trailer" in m
    ]
    commit(git_vault, "from somewhere else", path="infra/c.md")
    found = messages(doctor.examine(root=git_vault.root), doctor.HISTORY)
    assert any("carries no Mabolo trailer" in m for m in found)


def test_a_merge_after_the_boundary_is_a_finding(git_vault):
    commit(git_vault, "adopt", kind="adopt", path="infra/a.md")
    git_vault.git("checkout", "--quiet", "-b", "side")
    commit(git_vault, "on the side", kind="write", path="infra/s.md")
    git_vault.git("checkout", "--quiet", "main")
    commit(git_vault, "on the trunk", kind="write", path="infra/m.md")
    git_vault.git(
        "merge", "--quiet", "--no-ff", "-m", write.signed("merge", "write", "human:alex"), "side"
    )
    found = messages(doctor.examine(root=git_vault.root), doctor.HISTORY)
    assert any("is a merge, and Mabolo never merges" in m for m in found)


def test_a_mabolo_commit_touching_a_path_mabolo_never_writes_is_a_finding(git_vault):
    commit(git_vault, "adopt", kind="adopt", path="infra/a.md")
    commit(git_vault, "an entry", kind="write", path="infra/b.md")
    assert not [
        m
        for m in messages(doctor.examine(root=git_vault.root), doctor.HISTORY)
        if "never writes" in m
    ]
    commit(git_vault, "something else", kind="write", path="infra/notes.txt")
    found = messages(doctor.examine(root=git_vault.root), doctor.HISTORY)
    assert any("infra/notes.txt, which Mabolo never writes" in m for m in found)


# The content.


def test_a_file_that_does_not_validate_is_a_finding_with_its_errors(vault):
    (vault.root / "infra" / "broken.md").write_text(BROKEN, encoding="utf-8")
    report = doctor.examine(root=vault.root)
    named = [f for f in report.findings if f.group == doctor.CONTENT and "broken.md" in f.message]
    assert named and named[0].detail


def test_warnings_are_one_count_and_not_one_line_each(vault):
    for name in ("one", "two", "three"):
        write_entry(vault, f"infra/{name}.md", type="whatever")
    found = [m for m in messages(doctor.examine(root=vault.root), doctor.CONTENT) if "warnings" in m]
    assert len(found) == 1


def test_two_names_that_fold_to_one_are_a_finding(vault):
    write_entry(vault, "infra/beacon.md")
    assert not [
        m for m in messages(doctor.examine(root=vault.root), doctor.CONTENT) if "fold to" in m
    ]
    write_entry(vault, "infra/atlas.md", mabolo={"aliases": ["Beacon"]})
    found = messages(doctor.examine(root=vault.root), doctor.CONTENT)
    assert any("fold to 'beacon'" in m for m in found)


def test_an_entry_missing_from_its_area_index_is_a_finding(vault):
    write_entry(vault, "infra/thing.md")
    found = messages(doctor.examine(root=vault.root), doctor.CONTENT)
    assert any("missing from its area's index.md" in m for m in found)
    vault.rebuild_indexes()
    assert check(doctor.examine(root=vault.root), "content.index").clean


def test_anchors_without_a_repository_are_not_run_rather_than_clean(vault):
    write_entry(vault, "project/atlas/thing.md", area="project/atlas", mabolo={"anchor": "src/app.py"})
    anchors = check(doctor.examine(root=vault.root), "content.anchors")
    assert not anchors.ran and not anchors.clean
    assert "no project and repository were named" in anchors.reason


def test_an_anchor_that_moved_is_a_finding(vault, tmp_path, git_identity):
    where = tmp_path / "atlas"
    (where / "src").mkdir(parents=True)
    (where / "src" / "app.py").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(where)], check=True)
    subprocess.run(["git", "-C", str(where), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(where), "commit", "-qm", "first"], check=True)

    def look(when):
        write_entry(
            vault,
            "project/atlas/thing.md",
            area="project/atlas",
            mabolo={"anchor": "src/app.py"},
            generated={"by": "mabolo/0.1.0", "at": when},
        )
        return doctor.examine(root=vault.root, project="atlas", repository=where)

    assert check(look("2099-01-01T00:00:00+00:00"), "content.anchors").clean
    found = messages(look("2020-01-01T00:00:00+00:00"), doctor.CONTENT)
    assert any(drift_line in found for drift_line in [f"thing: {m}" for m in ("the file it watches moved",)])


# What is waiting.


def test_proposals_waiting_are_one_line_with_the_oldest_and_the_expired(vault):
    now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
    waiting = [
        doctor.Pending("p-old", now - dt.timedelta(days=40)),
        doctor.Pending("p-new", now - dt.timedelta(days=2)),
    ]
    report = doctor.examine(root=vault.root, pending=waiting, now=now)
    assert messages(report, doctor.WAITING) == [
        "2 proposals waiting, the oldest p-old from 2026-08-10, 1 past 30 days"
    ]


def test_an_inbox_nobody_supplied_is_not_run_rather_than_clean(vault):
    """An empty list and nobody having looked are different answers, and this is
    the easiest place in the project to collapse them into one."""
    nobody = check(doctor.examine(root=vault.root), "waiting.inbox")
    assert not nobody.ran and not nobody.clean
    assert check(doctor.examine(root=vault.root, pending=[]), "waiting.inbox").clean


def test_notes_past_their_retention_are_a_finding(vault):
    where = consent.prompt_dir()
    where.mkdir(parents=True, exist_ok=True)
    note = where / "one.jsonl"
    note.write_text("{}\n", encoding="utf-8")
    assert check(doctor.examine(root=vault.root), "waiting.notes").clean
    stamp = time.time() - (consent.RETENTION_DAYS + 1) * 86400
    os.utime(note, (stamp, stamp))
    found = messages(doctor.examine(root=vault.root), doctor.WAITING)
    assert any(f"past the {consent.RETENTION_DAYS} day promise" in m for m in found)


# The report itself.


def test_the_closing_line_counts_findings_clean_and_not_run():
    report = doctor.Report(
        [
            doctor.Check("a", doctor.REPOSITORY, findings=(doctor.Finding(doctor.REPOSITORY, "x"),)),
            doctor.Check("b", doctor.CONTENT),
            doctor.Check("c", doctor.REMOTE, reason="nobody was there"),
        ]
    )
    assert report.render().splitlines() == [
        "repository: x",
        "not run: c, nobody was there",
        "1 findings, 1 checks clean, 1 not run",
    ]


def test_a_check_that_did_not_run_makes_the_report_not_ok():
    findings_only = doctor.Report([doctor.Check("a", doctor.CONTENT)])
    assert findings_only.ok
    assert not doctor.Report([doctor.Check("a", doctor.CONTENT, reason="nobody looked")]).ok


def test_the_groups_come_in_the_order_the_report_promises(git_vault, remote, tmp_path):
    path = configuration(tmp_path, git_vault.root)
    path.chmod(0o644)
    (git_vault.root / ".git" / "MERGE_HEAD").write_text("x", encoding="utf-8")
    shutil.rmtree(remote)
    (git_vault.root / "infra" / "broken.md").write_text(BROKEN, encoding="utf-8")
    now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
    report = doctor.examine(
        config_path=path,
        pending=[doctor.Pending("p", now - dt.timedelta(days=40))],
        now=now,
    )
    groups = [f.group for f in report.findings]
    assert set(groups) == set(doctor.GROUPS)
    assert groups == sorted(groups, key=doctor.GROUPS.index)


def test_a_vault_with_nothing_wrong_reports_only_the_closing_line(git_vault, tmp_path):
    git_vault.git(
        "commit", "--quiet", "--amend", "-m", write.signed("adopt the vault", "adopt", "human:alex")
    )
    path = configuration(tmp_path, git_vault.root)
    report = doctor.examine(
        config_path=path,
        clients=[],
        executable=str(tmp_path / "mabolo"),
        pending=[],
        project="atlas",
        repository=git_vault.root,
    )
    assert report.findings == []
    assert report.not_run == []
    assert report.ok
    assert report.render() == f"0 findings, {len(report.clean)} checks clean, 0 not run"


def test_a_wired_client_whose_hooks_have_never_run_is_not_reported_as_fine(tmp_path, git_vault):
    """A plugin whose hooks are never discovered starts the MCP server and runs
    none of them, with no error anywhere. The only thing that can speak to it is
    whether a note has ever arrived, and "nothing seen yet" is not "it works"."""
    wiring_file = tmp_path / "settings.json"
    wiring_file.write_text('{"hooks": {"SessionStart": [{"hooks": [{"command": "mabolo hook '
                           'session-start"}]}]}}', encoding="utf-8")
    report = doctor.examine(
        config_path=configuration(tmp_path, git_vault.root),
        clients=[wiring_file],
        executable="mabolo",
        pending=[],
    )
    waiting = [c for c in report.not_run if c.name == "wiring.seen"]
    assert waiting and "no prompt note has ever arrived" in waiting[0].reason


def test_a_hook_that_has_run_clears_it(tmp_path, git_vault, monkeypatch):
    from mabolo import consent

    wiring_file = tmp_path / "settings.json"
    wiring_file.write_text('{"hooks": {"SessionStart": [{"hooks": [{"command": "mabolo hook '
                           'session-start"}]}]}}', encoding="utf-8")
    consent.record("remember that the pilot runs in one region", cwd=tmp_path, session="s")
    report = doctor.examine(
        config_path=configuration(tmp_path, git_vault.root),
        clients=[wiring_file],
        executable="mabolo",
        pending=[],
    )
    assert [c.name for c in report.not_run if c.name == "wiring.seen"] == []
    assert "wiring.seen" in [c.name for c in report.clean]
