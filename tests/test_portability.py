"""What a second machine, a second file system or a second language changes.

Everything here was found by reading the code against macOS, and none of it is
about macOS in the end: a localised Git, a file system that folds case and a
Git older than the flags in use are all things a Linux machine can have too.
"""

import os
import sqlite3
import subprocess

import pytest

from mabolo import consent, git, index, write
from mabolo.errors import MaboloError


def test_a_refused_push_is_told_from_an_absent_one_by_asking_the_remote(git_vault, remote,
                                                                        monkeypatch):
    """Git says which one happened in the user's own language. Reading the
    words made a German refusal look unreachable, and the caller then moved the
    local branch past a remote that had just said no."""
    real = git.run

    def localised(root, *args, **kwargs):
        if args and args[0] == "push":
            return subprocess.CompletedProcess(
                args=list(args), returncode=1, stdout="",
                stderr="Aktualisierungen wurden zurueckgewiesen",
            )
        return real(root, *args, **kwargs)

    monkeypatch.setattr(git, "run", localised)
    assert git.push(git_vault.root, "origin", "main", "HEAD", None) == "rejected"


def test_a_remote_nobody_can_reach_is_not_a_refusal(git_vault, tmp_path):
    git_vault.git_set_remote(str(tmp_path / "nowhere.git"))
    assert git.push(git_vault.root, "origin", "main", "HEAD", None) == "unreachable"


def test_every_git_call_runs_in_a_language_this_code_can_read(git_vault, monkeypatch):
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    result = git.run(git_vault.root, "status", check=False)
    assert "On branch" in result.stdout


def test_no_stray_environment_can_redirect_a_blob(git_vault, tmp_path, monkeypatch):
    """`hash_object` built its own environment and so missed the scrubbing every
    other call gets. One call out of all of them could write into a repository
    nobody named."""
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(["git", "init", "-q", str(elsewhere)], check=True)
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(elsewhere / ".git" / "objects"))
    blob = git.hash_object(git_vault.root, b"some bytes")
    assert git.run(git_vault.root, "cat-file", "-e", blob, check=False).returncode == 0


def test_a_new_repository_lands_on_the_branch_it_was_asked_for(tmp_path, git_identity,
                                                                monkeypatch):
    """Not on the one Git happens to default to. The flag that says so arrived
    in Git 2.28 and the Git on a stock macOS can be older, so the branch is set
    afterwards instead. Setting the default to something else is what makes
    that visible on any Git, including this one."""
    from mabolo.vault import Vault

    with open(os.environ["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8") as handle:
        handle.write("[init]\n\tdefaultBranch = trunk\n")
    made = Vault(tmp_path / "fresh")
    made.initialise()
    made.git_initialise(branch="main")
    assert git.current_branch(made.root) == "main"
    assert made.git("log", "-1", "--format=%s").stdout.strip()


def test_a_session_file_is_readable_and_still_a_name(tmp_path):
    """The readable part is what a person sees in the directory; the eight
    characters after it are what make it a name. Folding the case alone was the
    first answer to a case insensitive file system, and it was the wrong one:
    it made two different ids one file everywhere instead of nowhere."""
    assert consent.session_id("abc-1").startswith("abc-1-")
    assert consent.session_id("ABC-1") != consent.session_id("abc-1")


def test_two_sessions_never_share_one_file(tmp_path):
    """Folding alone collided three ways: `team/a` and `team?a` became one name,
    two ids differing past the sixty-fourth character were cut to one stem, and
    everything unprintable became `unnamed`. One shared file means one session's
    sentences authorise the other's writes."""
    pairs = [("team/a", "team?a"), ("A" * 64 + "x", "A" * 64 + "y"), ("???", "---")]
    for one, other in pairs:
        assert consent.session_id(one) != consent.session_id(other), (one, other)


def test_the_same_directory_written_two_ways_is_the_same_directory(tmp_path):
    """Half proven. On this file system `samefile` and comparing resolved text
    agree, so what this holds is only that a link does not break the match. The
    half that matters, two spellings differing in case on a file system that
    folds it, cannot be reproduced here and is stated in the module rather than
    claimed by a green test."""
    here = tmp_path / "work"
    here.mkdir()
    link = tmp_path / "also-work"
    link.symlink_to(here)
    consent.record("remember that the pilot runs in one region", cwd=link, session="s",
                   directory=tmp_path / "p")
    given = consent.check("the pilot runs in one region", cwd=here, session="s",
                          directory=tmp_path / "p")
    assert given.verified


def test_a_sqlite_without_fts5_says_so_in_a_sentence(monkeypatch):
    """The hook swallows everything, so the raw error would show up as a memory
    that knows nothing rather than as a tool that cannot search."""
    class Broken(sqlite3.Connection):
        def executescript(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(index.sqlite3, "connect", lambda *a, **k: Broken(":memory:"))
    with pytest.raises(MaboloError) as caught:
        index.Index([])
    assert "FTS5" in str(caught.value)


def test_a_project_area_with_capitals_is_named_with_its_rename(vault):
    """A folder with a capital is the same folder as its lower case twin where
    case folds, so two areas become one and one of them disappears on a clone.
    The finding says which rename fixes it rather than only that something is
    wrong."""
    from conftest import entry_text

    folder = vault.root / "project" / "Atlas"
    folder.mkdir(parents=True)
    (folder / "a-thing.md").write_text(
        entry_text(area="project/Atlas", description="d"), encoding="utf-8"
    )
    report = vault.validate()
    said = [p.message for p in report.problems if p.code == "mabolo.area.capitals"]
    assert said and "project/atlas" in said[0]


def test_a_repository_whose_name_carries_capitals_still_finds_its_project():
    from mabolo import context

    assert context.project_for("Atlas", {"project/atlas"}) == "atlas"
