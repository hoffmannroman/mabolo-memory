"""The journal, read back: what happened lately, and to which project.

`log.md` is the one file in a vault that is written in the order things
happened rather than organised by what they are about. That makes it the only
place a session can be told what was *decided* last week, as opposed to what
exists. An entry says "releases are cut from main"; the journal says "we moved
the release to Friday because the migration did not run".

**A line points, it does not copy.** The convention this module reads is a
relative Markdown link to the project the line belongs to, which is the link
form the rest of the vault already uses. The link is kept in the payload on
purpose: it is the pointer that lets a session open the entry that holds the
long version, so the session index never has to carry it.

    ## 2026-08-14

    - [atlas](project/atlas/index.md): release moved to Friday, the migration
      did not run through
    - a line naming no project belongs to no project and is never shown

Lines with no link are not an error and not a fallback. A journal is allowed to
hold notes that belong to no project; they simply never appear in a payload
built for one.

The validator owns the file's shape (ISO headings, newest first, one group per
day). This module reads what is there and skips what it cannot parse, because a
session start that fails on a stray line teaches people to stop writing the
journal.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from .index import one_line
from .schema import PROJECT_PREFIX

#: A day heading, as the validator requires it. Matched at the start of a line
#: so that a `##` inside a fenced block cannot open a new day.
_DAY = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")

#: A bullet, with the text that follows it.
_BULLET = re.compile(r"^[-*]\s+(.*)$")

#: The project a line points at: a relative link into `project/<name>/`. The
#: name stops at the next slash or the closing bracket, so both a link to the
#: area index and a link to one entry inside it name the same project.
_LINK = re.compile(r"\]\(" + re.escape(PROJECT_PREFIX) + r"([^/)]+)[/)]")


@dataclass(frozen=True)
class Note:
    """One journal line: when it was written, what it says, what it is about."""

    at: dt.date
    text: str
    project: str | None

    def render(self) -> str:
        """The line as a payload shows it.

        The date rides along because these lines are read as a sequence and
        "last Tuesday" is half of what a decision means. `one_line` is not
        decoration: this text comes off disk, and a newline in it would let a
        journal line write its own heading into an injected payload.
        """
        return f"- {self.at.isoformat()}: {one_line(self.text)}"


def parse(text: str) -> list[Note]:
    """Every journal line that carries a day and a bullet, in file order.

    File order, not sorted: the file is required to run newest first, and
    re-sorting here would hide a file that does not. A caller that needs the
    newest sorts, and `validate` is what complains.

    A bullet before the first day heading has no date and is skipped. So is a
    heading that is not a date, which is the validator's finding to report, not
    this reader's to guess at.

    **A bullet runs until the next one.** A journal line wraps, in the file and
    in every editor that touches it, and a reader that took only the first
    physical line cut sentences in half at whatever column the author's editor
    happened to wrap at. So continuation lines are folded back in, indented or
    not, which is what Markdown itself does with them. A blank line ends the
    bullet.
    """
    out: list[Note] = []
    day: dt.date | None = None
    open_bullet: list[str] | None = None

    def close() -> None:
        nonlocal open_bullet
        if open_bullet is None or day is None:
            open_bullet = None
            return
        body = " ".join(" ".join(open_bullet).split())
        open_bullet = None
        if not body:
            return
        match = _LINK.search(body)
        out.append(Note(at=day, text=body, project=match.group(1) if match else None))

    for raw in text.splitlines():
        line = raw.rstrip()
        heading = _DAY.match(line)
        if heading:
            close()
            try:
                day = dt.date.fromisoformat(heading.group(1))
            except ValueError:
                day = None
            continue
        if line.lstrip().startswith("#"):
            # Any other heading closes the day rather than inheriting it: a
            # line under "## Notes" is not a line of the day above it.
            close()
            day = None
            continue
        if not line.strip():
            close()
            continue
        bullet = _BULLET.match(line.lstrip())
        if bullet:
            close()
            open_bullet = [bullet.group(1).strip()]
        elif open_bullet is not None:
            open_bullet.append(line.strip())
    close()
    return out


def recent(notes: list[Note], project: str | None, as_of: dt.date | None = None) -> list[Note]:
    """The notes of one project, newest first, ignoring anything dated later.

    `as_of` is the same argument the session index takes, and for the same
    reason: a payload has to be rebuildable at an old commit. Without the upper
    bound one line dated in the future would sit at the top of every session
    from now on, which is the mirror of the bug the freshness rule already has
    two bounds for.

    Returns nothing at all when no project is active. A journal line is written
    about a project, so a payload with no project has nothing to say here, and
    showing the newest lines of some other project would be worse than silence.
    """
    if project is None:
        return []
    picked = [n for n in notes if n.project == project and (as_of is None or n.at <= as_of)]
    # Stable: same day keeps file order, which is the order they were written.
    return sorted(picked, key=lambda n: n.at, reverse=True)
