import os
from pathlib import Path

import pytest

from mabolo import frontmatter
from mabolo.vault import Vault


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """No test may read or write the real configuration or the real vault."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("MABOLO_CONFIG", raising=False)
    monkeypatch.delenv("MABOLO_VAULT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    # `init` takes a new vault's language from the locale, so a test must not
    # start English on one machine and German on the next.
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def git_identity(tmp_path, monkeypatch):
    """A Git identity that exists only for this test."""
    text = "[user]\n\tname = Test\n\temail = test@example.invalid\n"
    config = tmp_path / "gitconfig"
    config.write_text(text, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # The same identity where an older Git looks for it. GIT_CONFIG_GLOBAL
    # arrived in 2.32, and a Git without it would read no identity at all here,
    # because HOME already points into this test's own directory.
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / ".gitconfig").write_text(text, encoding="utf-8")
    return config


@pytest.fixture
def vault(tmp_path):
    """An initialised, empty vault that no test shares with another."""
    made = Vault(tmp_path / "v")
    made.initialise()
    return made


def entry_text(area: str = "infra", body: str = "text", **meta) -> str:
    """A valid entry the way a person would type it, for tests about something else.

    Only well formed entries go through here. A test about a broken frontmatter
    writes the broken frontmatter out in full, because there the raw file is the
    thing being asserted about, and a helper that builds it through our own
    writer could never produce what a hand written mistake looks like.
    """
    block = {"area": area, **meta.pop("mabolo", {})}
    head = {"type": "reference", "title": "t", "description": "d", **meta, "mabolo": block}
    return frontmatter.dump(head, body)


@pytest.fixture
def git_vault(tmp_path, git_identity):
    """A vault that is a Git repository, with no remote."""
    made = Vault(tmp_path / "gv")
    made.initialise()
    made.git_initialise()
    return made


@pytest.fixture
def remote(tmp_path, git_vault):
    """A bare repository the vault pushes to, already in step with it."""
    import subprocess

    where = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "--initial-branch=main", str(where)], check=True
    )
    git_vault.git_set_remote(str(where))
    git_vault.git("push", "-q", "origin", "main")
    return where
