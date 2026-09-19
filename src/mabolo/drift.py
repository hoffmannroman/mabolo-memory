"""Staleness by movement: an entry goes out of date when its subject does.

A date on an entry says when somebody wrote it and nothing about whether it is
still true. Nine months is no reason to distrust a note about a build server
nobody has touched; a commit to that server's configuration this morning is.
So `mabolo.anchor` names the thing an entry is about, and this module asks Git
one question about it: has that path moved since the last time a person
confirmed the entry.

Four rules, and each of them is a failure worth not repeating:

* **No threshold on the size of the change.** A one line commit and a rewrite
  raise the same flag. The flag means "a person should look", not "this is
  wrong", and any number in between would be one nobody could justify to the
  person it interrupted.
* **The clock is the last verification, not when the entry was written.** An
  edit through the write tools appends a verification, so an entry corrected
  yesterday must not be reported stale over a commit from last month. Reading
  `generated.at` alone would flag every entry somebody had just been through,
  and a flag that fires on work somebody just did is one people learn to skip.
* **Nothing checked is never fresh.** With no repository, no active project, or
  an anchor that points out of the repository, the state is `UNCHECKED` and the
  reason says which of the three it was. Reporting an entry as fresh when
  nothing was looked at is the exact failure this project exists to prevent: it
  is the reassuring answer, and it is unearned.
* **An anchor that is gone is not an anchor that has not moved.** No commit and
  no file on disk means the thing the entry describes was deleted or renamed,
  which is the strongest reason to look there is, and the one most likely to
  arrive as silence.

`stale_after` is answered here too, with a reason of its own, because "the file
it watches moved" and "it said it would expire" are one line apart in a report
and two different pieces of work for the person reading it.

Judging and rendering are kept apart, the way they are everywhere else here:
`judge` and `review` decide, `line` and `report` say it. The repository root
arrives as a parameter and is never worked out from the process: this module
reads Git, which is enough filesystem for one file, and which repository a
session is standing in is the caller's knowledge.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import git
from .errors import MaboloError
from .index import one_line
from .schema import PROJECT_PREFIX, Entry, as_utc, parse_time

#: The three answers. There is no fourth, and in particular there is no
#: "probably fine": an entry is either checked and unmoved, checked and moved,
#: or not checked, and the last one is not a kind of the first.
FRESH = "fresh"
STALE = "stale"
UNCHECKED = "unchecked"

#: Reasons. They are sentences because a reason is read by a person, and they
#: are constants because a caller has to be able to tell two of them apart
#: without matching on wording.
MOVED = "the file it watches moved"
GONE = "the file it watches is gone"
EXPIRED = "it said it would expire"
UNMOVED = "the file it watches has not moved since"
UNCOMMITTED = "the file it watches is there and has no commit of its own"
NOTHING_WATCHED = "nothing to watch: no anchor and no expiry date"
NO_REPOSITORY = "anchor not checked: no repository"
NO_PROJECT = "anchor not checked: no active project"
OTHER_PROJECT = "anchor not checked: it belongs to another project"
OUTSIDE = "anchor not checked: it points outside the repository"
NO_CLOCK = "anchor not checked: the entry does not say when it was last confirmed"
NO_ANSWER = "anchor not checked: git could not be asked"

#: What the longer form calls itself.
HEADING = "## Entries worth a look"


@dataclass(frozen=True)
class Drift:
    """What became of one entry when it was held against the repository.

    Everything a caller needs to print it is here, so that rendering never has
    to go back to the entry or to Git for a second look.
    """

    name: str
    state: str
    reason: str
    #: The path the entry watches, exactly as the entry spells it.
    anchor: str | None = None
    #: When the anchor last moved, in UTC, when that could be established.
    moved_at: dt.datetime | None = None
    #: The moment the verdict was measured against: the last verification, or
    #: `generated.at` for an entry nobody has confirmed since it was written.
    confirmed_at: dt.datetime | None = None
    #: The entry's own `stale_after`, in UTC, when it has one.
    expires_at: dt.datetime | None = None

    @property
    def worth_saying(self) -> bool:
        """Whether this belongs in a report at all.

        Fresh is silence. So is another project's anchor: it is honestly
        unchecked, and it stays unchecked in every session that is not about
        that project, so printing it would put a line nobody can act on into
        every payload until somebody changes projects.
        """
        return self.state != FRESH and self.reason != OTHER_PROJECT


def judge(
    entry: Entry,
    *,
    project: str | None = None,
    repository: Path | None = None,
    moment: dt.datetime | None = None,
) -> Drift:
    """One entry, held against the repository the session is standing in.

    `repository` is that repository's root, the nearest `.git` walking upwards
    from the working directory, and `project` is the project it belongs to.
    Both are the caller's to establish, and both may be absent, which is a
    state of its own and never a reason to say "fresh".

    `moment` is the clock for `stale_after`, and without one nothing expires:
    the same rule the session payload follows, so that a run can be replayed at
    an old commit and produce what it produced then.

    A moved anchor is reported ahead of an expiry date. Both mean "look at
    this", and the anchor also says where to look.
    """
    name = entry.path.stem if entry.path else ""
    anchor = entry.mabolo.anchor
    verdict = _anchor(entry, name, anchor, project, repository) if anchor else None
    if verdict is not None and verdict.state == STALE:
        return verdict
    expires_at = _expiry(entry)
    if expires_at is not None and moment is not None and expires_at <= as_utc(moment):
        return Drift(
            name=name,
            state=STALE,
            reason=EXPIRED,
            anchor=anchor,
            confirmed_at=_confirmed_at(entry),
            expires_at=expires_at,
        )
    if verdict is not None:
        return verdict
    return Drift(name=name, state=FRESH, reason=NOTHING_WATCHED, expires_at=expires_at)


def review(
    entries: Iterable[Entry],
    *,
    project: str | None = None,
    repository: Path | None = None,
    moment: dt.datetime | None = None,
) -> list[Drift]:
    """Only what is worth saying, what moved before what was not checked.

    Sorted rather than left in the order the files came off disk, because two
    runs against the same vault have to produce the same report or nobody can
    tell a change in the vault from a change in the filesystem.
    """
    judged = [judge(e, project=project, repository=repository, moment=moment) for e in entries]
    worth = [d for d in judged if d.worth_saying]
    return sorted(worth, key=lambda d: (d.state != STALE, d.name))


def line(result: Drift) -> str:
    """One verdict, as a payload can afford to say it.

    The name and the anchor both come off disk, so both are folded: a path
    holding a newline would otherwise write its own line into a block that is
    injected into a prompt without anybody reading it first.
    """
    said = f"- {one_line(result.name)}: {result.reason}"
    if result.anchor and result.reason != EXPIRED:
        said += f" ({one_line(result.anchor)})"
    return said


def report(results: list[Drift]) -> str:
    """The longer form: every verdict with the two moments it rests on.

    A flag without its dates is an assertion. With them a person can see at a
    glance that the commit was a typo fix from an hour after they last read the
    entry, and close it again without opening Git.
    """
    if not results:
        return ""
    lines = [HEADING, ""]
    for result in results:
        lines.append(line(result))
        detail = []
        if result.moved_at is not None:
            detail.append(f"moved {_stamp(result.moved_at)}")
        if result.confirmed_at is not None:
            detail.append(f"last confirmed {_stamp(result.confirmed_at)}")
        if result.expires_at is not None:
            detail.append(f"expiry date {_stamp(result.expires_at)}")
        if detail:
            lines.append("  " + ", ".join(detail))
    stale = sum(1 for r in results if r.state == STALE)
    lines += ["", f"{stale} stale, {len(results) - stale} that could not be checked."]
    return "\n".join(lines)


def _anchor(
    entry: Entry,
    name: str,
    anchor: str,
    project: str | None,
    repository: Path | None,
) -> Drift:
    """The verdict on one anchor, in the order the answers become possible.

    Scope is settled before Git is asked. An entry belonging to another project
    names a path in another repository, and running the question against this
    one would compare a note about one code base with the history of a
    different one.
    """
    if project is None:
        return Drift(name, UNCHECKED, NO_PROJECT, anchor)
    if entry.area != f"{PROJECT_PREFIX}{project}":
        return Drift(name, UNCHECKED, OTHER_PROJECT, anchor)
    if repository is None or not _is_repository(repository):
        return Drift(name, UNCHECKED, NO_REPOSITORY, anchor)
    if not _inside(repository, anchor):
        return Drift(name, UNCHECKED, OUTSIDE, anchor)
    try:
        moved_at = _last_commit(repository, anchor)
    except MaboloError:
        # Git was there and did not answer: a timeout, or a repository it
        # refuses. Saying "fresh" here would be a verdict built on a failure.
        return Drift(name, UNCHECKED, NO_ANSWER, anchor)
    if moved_at is None:
        if not (repository / anchor).exists():
            # No history and nothing on disk. The path was deleted or renamed,
            # and an entry about a file that no longer exists is the loudest
            # case there is, so it must not fall through as "nothing moved".
            return Drift(name, STALE, GONE, anchor)
        return Drift(name, FRESH, UNCOMMITTED, anchor)
    confirmed_at = _confirmed_at(entry)
    if confirmed_at is None:
        return Drift(name, UNCHECKED, NO_CLOCK, anchor, moved_at=moved_at)
    if moved_at > confirmed_at:
        return Drift(name, STALE, MOVED, anchor, moved_at=moved_at, confirmed_at=confirmed_at)
    return Drift(name, FRESH, UNMOVED, anchor, moved_at=moved_at, confirmed_at=confirmed_at)


def _is_repository(root: Path) -> bool:
    """Whether Git considers this a repository at all."""
    try:
        return git.repository_dir(root) is not None
    except MaboloError:
        return False


def _inside(repository: Path, anchor: str) -> bool:
    """Whether the anchor lands inside the repository, symlinks included.

    The validator already refuses an absolute or traversing anchor, and this
    asks again anyway: a vault is edited by hand, the validator reports rather
    than enforces, and the entry on disk is what gets read. Both ends are
    resolved, so a symlink inside the repository pointing out of it is caught
    as well.
    """
    if Path(anchor).is_absolute():
        return False
    try:
        root = repository.resolve()
        return (root / anchor).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _last_commit(repository: Path, anchor: str) -> dt.datetime | None:
    """When the newest commit touching this path was made, in UTC, or None.

    The path goes to Git as the entry spells it rather than resolved. Git knows
    a symlink as a symlink, so a resolved path would ask about a file the
    history may never have heard of.
    """
    result = git.run(repository, "log", "-1", "--format=%cI", "--", anchor, check=False)
    if result.returncode != 0:
        # A repository without a single commit, or a path Git refuses. Either
        # way there is no commit to compare against.
        return None
    when = parse_time(result.stdout.strip())
    return as_utc(when) if when is not None else None


def _confirmed_at(entry: Entry) -> dt.datetime | None:
    """When a person last stood behind this entry, in UTC.

    The last verification wins outright over `generated.at`, and is not merely
    the later of the two: an edit appends a verification, so verification is
    the only stamp that says somebody looked. `touched_at` answers a different
    question for the session index and takes the maximum of both, which is why
    this is its own function rather than a call to it.
    """
    verified = [as_utc(t) for t in (parse_time(v.at) for v in entry.verified) if t is not None]
    if verified:
        return max(verified)
    written = parse_time(entry.generated.at) if entry.generated else None
    return as_utc(written) if written is not None else None


def _expiry(entry: Entry) -> dt.datetime | None:
    """The entry's own `stale_after`, in UTC, or None when it has none."""
    when = parse_time(entry.stale_after) if entry.stale_after else None
    return as_utc(when) if when is not None else None


def _stamp(when: dt.datetime) -> str:
    """One moment, printed the one way this project prints moments."""
    return when.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
