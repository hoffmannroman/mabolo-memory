"""Assembling a session start: the one chain three callers share.

These tests exist because the chain used to be written out per caller, which is
the shape of a scar this project already carries: two copies of one line had
already drifted apart before anybody looked.
"""

import datetime as dt

from conftest import entry_text
from mabolo import session
from mabolo.index import Document

NOW = dt.datetime(2026, 9, 18, 12, tzinfo=dt.timezone.utc)


def doc(name: str, area: str = "infra") -> Document:
    return Document(name, name, "a line", area, None, (), pin=False, at=NOW)


def filled(vault):
    """A vault with one pinned rule, one project entry and one journal line."""
    (vault.root / "infra" / "rule.md").write_text(
        entry_text(title="Rule", description="Deep work after 20:00", mabolo={"pin": True}),
        encoding="utf-8",
    )
    folder = vault.area_dir("project/atlas")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "atlas-tone.md").write_text(
        entry_text(area="project/atlas", title="Tone", description="Flat and factual"),
        encoding="utf-8",
    )
    vault.log_file.write_text(
        "## 2026-09-16\n\n- moved the release, [atlas-tone](project/atlas/atlas-tone.md)\n",
        encoding="utf-8",
    )
    return vault


def test_a_start_reads_the_vault_once_and_carries_what_it_read(vault):
    """The documents travel with the payload because two callers want to say
    more about it than it holds, and reading the vault twice to find them would
    be a second opinion about what is in it."""
    started = session.start(filled(vault), folder="atlas", as_of=NOW)
    assert started.project == "atlas"
    assert "rule" in started.payload.names
    assert [d.name for d in started.documents] != []
    assert any(note.project == "atlas" for note in started.payload.notes)


def test_the_journal_is_read_for_the_active_project_only(vault):
    started = session.start(filled(vault), folder="nothing-like-it", as_of=NOW)
    assert started.project is None
    assert started.payload.notes == (), "no project, nothing to say about one"


def test_given_beats_derived_and_derived_beats_nothing(vault):
    documents = [doc("a", area="project/atlas")]
    assert session.project_for(documents, "atlas") == ("atlas", "from the folder atlas")
    assert session.project_for(documents, "elsewhere", given="atlas") == ("atlas", "as given")
    assert session.project_for(documents, "atlas", none=True) == (None, "asked for no project")
    project, source = session.project_for(documents, "elsewhere")
    assert project is None and "not a project in this vault" in source


def test_a_project_the_vault_does_not_hold_is_reported_rather_than_guessed(vault):
    """Not an error and not an empty payload: the pinned entries still arrive,
    and the only thing missing is the one thing that was asked for."""
    started = session.start(filled(vault), folder="x", as_of=NOW, given_project="beacon")
    assert started.project == "beacon"
    assert not session.has_project_area(started.payload)
    assert "rule" in started.payload.names


def test_no_clock_and_no_folder_are_read_here(vault, monkeypatch):
    """A library that read either would put a second answer to "when is now"
    below the line, and the payload could no longer be rebuilt at an old
    commit from the same arguments."""
    import mabolo.session as module

    monkeypatch.setattr(
        module.index_module, "documents_of", lambda entries: [doc("a", area="project/atlas")]
    )
    first = session.start(filled(vault), folder="atlas", as_of=NOW)
    second = session.start(filled(vault), folder="atlas", as_of=NOW)
    assert first.payload.text() == second.payload.text()


def test_where_a_session_is_standing_walks_up_to_the_repository(tmp_path, vault):
    """One answer for every caller. It lived in the command line, so the server
    worked the project out from the name of the directory it happened to be
    started in: a client started one folder deeper saw no project at all and
    said so about entries that have one."""
    from conftest import entry_text

    area = vault.root / "project" / "atlas"
    area.mkdir(parents=True)
    (area / "a-thing.md").write_text(entry_text(area="project/atlas", description="d"),
                                     encoding="utf-8")
    work = tmp_path / "atlas"
    (work / "src" / "deep").mkdir(parents=True)
    (work / ".git").mkdir()

    folder, project = session.standing(vault, work / "src" / "deep")
    assert folder == work
    assert project == "atlas"


def test_a_folder_that_is_in_no_repository_is_its_own_answer(tmp_path, vault):
    folder, project = session.standing(vault, tmp_path / "nowhere")
    assert folder == tmp_path / "nowhere"
    assert project is None
