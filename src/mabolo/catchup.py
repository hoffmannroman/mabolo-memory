"""Catching up with the remote before a session starts reading.

A write fetches, lines up and pushes, so everything written anywhere reaches
the remote at once. Reading never did any of that: a machine that was off for
two weeks started its first session on the vault as it was two weeks ago, and
nothing said so. This closes that gap at the one moment every session passes
through, its start.

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

from dataclasses import dataclass
from pathlib import Path

from . import git, write
from .errors import MaboloError

#: How long the fetch may take. The session start hook has five seconds in
#: all and still has to build its payload afterwards.
FETCH_SECONDS = 2.5

#: How long to wait for a write that is already running.
LOCK_SECONDS = 0.5

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
    try:
        if not git.is_repository_inside(root) or not git.has_remote(root, remote):
            return CatchUp(NO_REMOTE, f"this vault has no remote called {remote}")
        with write.lock(root, seconds=lock_seconds):
            return _catch_up(root, remote, branch, seconds)
    except MaboloError as exc:
        # The lock was busy, git could not be run, or the fetch ran out of
        # time. All of them leave the vault exactly as it was.
        return CatchUp(OFFLINE if "did not finish" in str(exc) else BLOCKED, str(exc))


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
