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


@pytest.fixture
def git_identity(tmp_path, monkeypatch):
    """A Git identity that exists only for this test."""
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
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
