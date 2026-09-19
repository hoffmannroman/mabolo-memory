"""What a session is handed, assembled once for everyone who hands it over.

Three callers want the same payload and arrive from different directions: the
`context` command prints it for a person, the session start hook wraps it for a
client, and the eval harness measures it. A fourth is coming, because the MCP
server will offer it as a tool. Each of them was assembling it themselves:
project out of the folder, entries out of the vault, journal lines out of
`log.md`, then `context.build` with the same seven arguments.

That is the shape of a scar this project already carries. `index.entry_line`
exists because the session index and the search preview each built the same
line, and by the time anybody noticed they had already drifted: one folded its
text and the other did not, which is how the folded one's defence was reachable
around the back. Three copies of a longer chain is the same bet at worse odds.

**The clock and the working directory stay outside.** This module reads files,
because a vault is files; it does not read the clock and does not ask where the
process happens to be standing. Those are the caller's to know and to pass, so
that the same arguments produce the same payload at an old commit, which is the
property the whole tier is built on.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from . import context, index as index_module, journal
from .context import SessionIndex
from .index import Document
from .schema import PROJECT_PREFIX
from .vault import Vault


@dataclass(frozen=True)
class Start:
    """One assembled session start, and what a caller needs to explain it.

    The documents travel with the payload because two callers want to say more
    about it than it holds: `--why-not` reports on entries the payload only
    counted, and reading the vault twice to find them would be a second opinion
    about what is in it.
    """

    payload: SessionIndex
    documents: list[Document]
    #: Where the project came from, in words, for a line a person reads.
    source: str

    @property
    def project(self) -> str | None:
        return self.payload.project


def repo_root(start: Path) -> Path:
    """The repository a folder belongs to, or the folder itself.

    The nearest `.git` walking upwards wins, so a session in a subfolder of a
    project is about that project and not about the subfolder. `.git` is a file
    rather than a folder inside a worktree or a submodule, so both count, and a
    repository inside a repository resolves to the inner one, which is where the
    work is actually happening.

    This reads the filesystem, the way `now()` reads the clock, and that is the
    caller's job. Nothing in `context` does either.
    """
    for folder in (start, *start.parents):
        if (folder / ".git").exists():
            return folder
    return start


def standing(vault: Vault, cwd: Path | None = None) -> tuple[Path, str | None]:
    """Where a session is standing: the repository, and the project it is in.

    One answer for every caller. It lived in the command line, so the server
    worked the project out from the name of the directory it happened to be
    started in, without the walk upwards: a client started one folder deeper
    than the repository saw no project at all, and said so about entries that
    have one. Two answers to "which project is this" is the kind of second
    truth that is only visible when the two disagree.
    """
    folder = repo_root(Path(cwd) if cwd else Path.cwd())
    areas = {entry.area for entry in vault.entries()}
    return folder, context.project_for(folder.name, areas)


def project_for(
    documents: list[Document], folder: str, *, given: str | None = None, none: bool = False
) -> tuple[str | None, str]:
    """Which project a session is about, and where that came from.

    Given by hand beats derived, and derived beats nothing. The source travels
    back with the answer rather than being worked out again for the message: a
    line that says where a value came from has to come from the code that
    decided it.
    """
    if none:
        return None, "asked for no project"
    if given:
        return given, "as given"
    found = context.project_for(folder, {d.area for d in documents})
    if found:
        return found, f"from the folder {folder}"
    return None, f"the folder {folder} is not a project in this vault"


def start(
    vault: Vault,
    *,
    folder: str,
    as_of: dt.datetime | None,
    given_project: str | None = None,
    no_project: bool = False,
    target_tokens: int = context.DEFAULT_TARGET_TOKENS,
    project_tokens: int = context.DEFAULT_PROJECT_TOKENS,
) -> Start:
    """Everything a session start needs, read once and built once.

    `folder` is a name, not a path: which repository the session is standing in
    is the caller's question, and this only asks the vault whether a project
    answers to that name.
    """
    # No search index here: nothing in a session start searches, and the
    # projection from files to documents is what both readers of a vault share.
    documents = index_module.documents_of(vault.entries())
    project, source = project_for(documents, folder, given=given_project, none=no_project)
    # The journal is read for the active project only. `context.build` stays a
    # function of its arguments, and `log.md` is the one input that is not in
    # the entry list, so finding it belongs on this side of the line.
    notes = journal.recent(vault.notes(), project, as_of.date() if as_of else None)
    payload = context.build(
        documents,
        project=project,
        as_of=as_of,
        target_tokens=target_tokens,
        notes=notes,
        project_tokens=project_tokens,
    )
    return Start(payload=payload, documents=documents, source=source)


def has_project_area(payload: SessionIndex) -> bool:
    """Whether the vault holds an area for the project this was built for.

    A payload built for a project that does not exist is not an error and not
    empty: the pinned entries still arrive, the recent ones still arrive, and
    the only thing missing is the one thing that was asked for. Two callers say
    so out loud, and they used to spell the same question two different ways.
    """
    if payload.project is None:
        return True
    return any(area == f"{PROJECT_PREFIX}{payload.project}" for area, _ in payload.counts)
