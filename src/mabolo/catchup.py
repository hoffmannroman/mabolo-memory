"""Catching up with the remote before a session starts reading.

A write fetches, lines up and pushes, so everything written anywhere reaches
the remote at once. Reading never did any of that: a machine that was off for
two weeks started its first session on the vault as it was two weeks ago, and
nothing said so. This closes that gap at the one moment every session passes
through, its start.

A start is not enough for a session that never ends. One that runs for days,
a session kept alive by a service so a phone can reach it, read the vault of
the morning it began, while other machines went on writing. So a prompt starts
a catch-up as well, in the background and about once a minute: the prompt
hook has two seconds and the person is waiting, and a fetch belongs to neither.
What arrives is there for the next read, the tools a moment later included.

The rules are the write path's, applied to reading:

1. **Fast forward or nothing.** A clone that has moved on by itself is never
   merged into, and a file somebody edited by hand is never overwritten; Git's
   `--ff-only` refuses both. What happened is reported instead.
2. **The write lock is held**, so a catch-up cannot move the branch under a
   write another session's server is making, nor the other way round. A lock
   that is taken is waited on only briefly: somebody is writing, which means
   they fetched a moment ago.
3. **A deadline of its own**, shorter than the hook's. A slow or absent network
   costs the fetch, not the session start: the payload is still built from what
   is here, and says that it is.

Nothing here raises. Every outcome is a `CatchUp`, because the caller is a hook
that must start the session whatever the network does.
"""

from __future__ import annotations

import fcntl
import time
from dataclasses import dataclass
from pathlib import Path

from . import git, write
from .consent import state_home
from .errors import MaboloError

#: How long the fetch may take. The session start hook has five seconds in
#: all and still has to build its payload afterwards.
FETCH_SECONDS = 2.5

#: How long to wait for a write that is already running.
LOCK_SECONDS = 0.5

#: The catch-up a prompt leaves running has nobody waiting for it, but it
#: holds the write lock while it fetches, and a write waits for that lock
#: `write.LOCK_SECONDS`. So the fetch ends well before that wait does: a slow
#: remote costs this catch-up, never a write the person asked for.
BACKGROUND_SECONDS = 5.0
BACKGROUND_LOCK_SECONDS = 3.0

#: How old the last catch-up may be before a prompt starts another one. Short
#: enough that what one machine wrote shows up on another within a minute or
#: two, long enough that a burst of prompts costs one fetch and not ten.
STALE_SECONDS = 60

#: When this machine last tried, as the modification time of an empty file,
#: and the lock that makes "is it due, then mark it" one step.
STAMP_FILE = "caught-up"
CLAIM_FILE = "caught-up.lock"

CURRENT = "current"
MOVED = "moved"
NO_REMOTE = "no-remote"
OFFLINE = "offline"
AHEAD = "ahead"
DIVERGED = "diverged"
BLOCKED = "blocked"


@dataclass(frozen=True)
class CatchUp:
    """What the catch-up did, in a sentence a session can be handed."""

    state: str
    message: str
    commits: int = 0

    @property
    def settled(self) -> bool:
        """True when the vault is as current as it can be and nothing needs saying."""
        return self.state in (CURRENT, MOVED, NO_REMOTE)

    def note(self) -> str:
        """One line for the start of a session, or nothing when all is well."""
        if self.settled:
            return ""
        return f"Memory not synced with the other machines: {self.message}"


def _stamp() -> Path:
    return state_home() / "mabolo" / STAMP_FILE


def due(now: float | None = None) -> bool:
    """Whether the last catch-up on this machine is old enough to try again.

    A stamp far in the future, after the clock was set back, counts as due:
    otherwise the vault would stand still until the clock caught up. A little
    ahead does not: a state directory on a network share stamps with the
    server's clock, and a server a second ahead would make every prompt due.
    """
    try:
        then = _stamp().stat().st_mtime
    except OSError:
        return True
    age = (time.time() if now is None else now) - then
    return age >= STALE_SECONDS or age < -STALE_SECONDS


def mark() -> bool:
    """Note that a catch-up was tried just now, whatever comes of it.

    Tried, not succeeded: a remote that cannot be reached is asked again a
    minute later, not on every prompt in between. False when the note could
    not be written, and then nothing should be started on the strength of it:
    every prompt would find the catch-up due and start one.
    """
    try:
        stamp = _stamp()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        return False
    return True


def claim(now: float | None = None) -> bool:
    """Take the next catch-up for the caller, if one is due and nobody has it.

    "Is it due" and "mark it" as one step. Two sessions prompting at the same
    moment both found it due and both started one before this; the write lock
    kept them apart, but the second fetch was waste.
    """
    try:
        stamp = _stamp()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        with open(stamp.parent / CLAIM_FILE, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False  # another prompt is claiming it this instant
            return due(now) and mark()
    except OSError:
        return False


def catch_up(
    root: Path,
    remote: str | None,
    branch: str,
    *,
    seconds: float = FETCH_SECONDS,
    lock_seconds: float = LOCK_SECONDS,
) -> CatchUp:
    """Bring the vault forward to the remote, if that can be done safely."""
    if not remote:
        return CatchUp(NO_REMOTE, "this vault has no remote")
    mark()
    try:
        if not git.is_repository_inside(root) or not git.has_remote(root, remote):
            return CatchUp(NO_REMOTE, f"this vault has no remote called {remote}")
        with write.lock(root, seconds=lock_seconds):
            return _catch_up(root, remote, branch, seconds)
    except MaboloError as exc:
        # The lock was busy, git could not be run, or the fetch ran out of
        # time. All of them leave the vault exactly as it was.
        return CatchUp(OFFLINE if "did not finish" in str(exc) else BLOCKED, str(exc))
    except OSError as exc:
        # The lock file itself could not be made: a vault on a full or
        # read-only disk. Nothing was touched either.
        return CatchUp(BLOCKED, f"the lock could not be taken: {exc.strerror or exc}")


def _catch_up(root: Path, remote: str, branch: str, seconds: float) -> CatchUp:
    busy = git.mutation_in_progress(root)
    if busy:
        return CatchUp(BLOCKED, f"{busy} is running in the vault")
    here = git.current_branch(root)
    if here != branch:
        where = "a detached HEAD" if here is None else f"branch {here!r}"
        return CatchUp(BLOCKED, f"the vault is on {where}, not {branch!r}")

    fetched = git.run(root, "fetch", "--quiet", remote, branch, check=False, timeout=seconds)
    if fetched.returncode != 0:
        return CatchUp(OFFLINE, f"{remote} could not be reached, this is the state of the last sync")

    remote_at = git.rev(root, f"refs/remotes/{remote}/{branch}")
    base = git.rev(root, "HEAD")
    if remote_at is None or remote_at == base:
        return CatchUp(CURRENT, "already up to date")
    if base is None or git.is_ancestor(root, base, remote_at):
        count = git.count_between(root, base, remote_at) if base else 0
        if not git.fast_forward(root, remote_at):
            return CatchUp(
                BLOCKED,
                "a file edited by hand stands in the way of the newer state, "
                "so nothing was pulled. Commit or undo it in the vault",
            )
        return CatchUp(MOVED, f"moved forward by {count} commits", commits=count)
    if git.is_ancestor(root, remote_at, base):
        count = git.count_between(root, remote_at, base)
        return CatchUp(
            AHEAD,
            f"{count} commits here have not reached {remote} yet; the next write sends them",
            commits=count,
        )
    return CatchUp(
        DIVERGED,
        f"this clone and {remote} have both moved on, so nothing was pulled. Sort them out by hand",
    )
