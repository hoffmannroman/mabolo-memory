"""One change to the vault, as a transaction.

Several sessions on several machines write into the same vault, and Git is the
database. A revision check on the local file is not enough: two machines can
read the same state, both decide they are up to date, and both commit. The
first push wins and the second is left with a diverged branch and no idea. So
the comparison belongs on the remote, and Git has exactly that primitive.

The order, and what each step is for:

1. **Take the lock.** One mutation at a time on this machine.
2. **Refuse if Git is already busy.** A half finished merge is the one state
   where writing produces a commit nobody asked for.
3. **Ask the remote where it is**, and remember that commit. It is the lease.
4. **Commit what a person changed by hand**, in a commit of its own. A vault
   that cannot be corrected in an editor is a database with extra steps.
5. **Line up with the remote**, by fast forward or not at all. A divergence is
   reported, never merged behind somebody's back.
6. **Check the revision.** If another session changed this entry meanwhile, the
   caller reads again and decides. Nothing is overwritten on a guess.
7. **Build the commit without a working tree**, straight into the object store.
8. **Push with a lease on the commit from step 3.** If the remote moved, the
   push is refused and nothing local has changed.
9. **Only then write the files the person can see**, and line their index up.

The way back is `git revert` of one commit. The way out of a conflict is to
read the entry again, which is what the answer says.

**The user's working tree is never reset and never cleaned.** The only files
touched are the ones this change names, and only after the commit holding them
is safe.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import __version__, frontmatter, git
from .consent import state_home
from .errors import MaboloError

#: How long a caller waits for another mutation on this machine to finish.
LOCK_SECONDS = 10.0

#: What the commit for a hand made change says. The actor is the person, since
#: that is who made it; Mabolo only noticed.
FOREIGN_MESSAGE = "edit outside mabolo by {actor}"

#: The trailer every commit Mabolo makes carries, and the only evidence about a
#: commit that Mabolo controls. It commits as the person, with the person's own
#: Git identity, so author and committer cannot tell Mabolo's commits from
#: anybody else's. `doctor` reads this key and reports what has none.
#:
#: It is detection, not proof: anybody can type the same line. What it buys is
#: that a vault whose history quietly grew a commit from somewhere else can say
#: so, which is the difference between a claim and a check.
TRAILER_KEY = "Mabolo"

#: The kinds a commit can be. `foreign` is a change a person made by hand that
#: Mabolo committed for them, which is not the same as a change Mabolo made.
KINDS = (
    "adopt",
    "import",
    "write",
    "edit",
    "forget",
    "journal",
    "foreign",
    "approve",
    "reject",
    # The inbox branch. Its commits are not in the vault's history and
    # never will be, but they are commits Mabolo made, and a person
    # looking at a stray branch should not have to guess where it came
    # from.
    "propose",
    "unfile",
    # The way back, which must not itself look like a commit from nowhere.
    "revert",
    "recover",
)


def trailer(kind: str, actor: str) -> str:
    """The last paragraph of a commit message, in Git's trailer shape."""
    if kind not in KINDS:
        raise MaboloError(f"{kind!r} is not a kind of commit Mabolo makes")
    return f"{TRAILER_KEY}: {kind} by {actor}, mabolo-memory/{__version__}"


def signed(message: str, kind: str, actor: str) -> str:
    """A commit message with the trailer as a paragraph of its own.

    Its own paragraph on purpose. Git reads trailers out of the last block of
    the message, and a block holding prose as well is a block a future Git may
    decline to parse.
    """
    return f"{message.rstrip()}\n\n{trailer(kind, actor)}"

#: Every outcome a caller has to be able to tell apart.
WRITTEN = "written"
LOCAL = "local"
OFFLINE = "offline"
CONFLICT = "conflict"
STALE = "stale"
BLOCKED = "blocked"
NOTHING = "nothing"


@dataclass(frozen=True)
class Change:
    """One file this transaction writes, or deletes when `data` is None."""

    path: str
    data: bytes | None
    #: The revision the caller last saw. None for a file that must not exist yet.
    expect: str | None = None


@dataclass(frozen=True)
class Result:
    """What actually happened, in the words the caller will repeat."""

    outcome: str
    message: str
    commit: str | None = None
    revisions: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in (WRITTEN, LOCAL, OFFLINE, NOTHING)


def _lock_path(root: Path) -> Path:
    """One lock per vault, kept outside it.

    Keyed by the resolved path, so two names for the same directory take the
    same lock, and a symlinked vault cannot be written through twice at once.
    """
    try:
        key = str(root.resolve())
    except OSError:
        key = str(root)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return state_home() / "mabolo" / "locks" / f"{digest}.lock"


@contextmanager
def lock(root: Path, seconds: float = LOCK_SECONDS) -> Iterator[None]:
    """Hold the write lock for this vault, or say who has it."""
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + seconds
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise MaboloError(
                        f"another write to this vault has been running for over {seconds:g} "
                        "seconds. Try again in a moment."
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def current_revision(root: Path, path: str) -> str | None:
    """The revision of a file as it is on disk right now, or None if it is gone."""
    try:
        return frontmatter.revision((root / path).read_bytes())
    except (OSError, ValueError):
        return None


def _stale(root: Path, changes: list[Change]) -> str | None:
    """The first change whose file is not what the caller last read."""
    for change in changes:
        found = current_revision(root, change.path)
        if change.expect is None:
            if found is not None and change.data is not None:
                return (
                    f"{change.path} already exists. Read it first, then edit it, "
                    "so that what is there is not overwritten by accident."
                )
            continue
        if found is None:
            return f"{change.path} is gone. Search again: somebody removed it."
        if found != change.expect:
            return (
                f"{change.path} has changed since you read it "
                f"({change.expect} then, {found} now). Read it again and decide."
            )
    return None


def _effective(root: Path, changes: list[Change]) -> list[Change]:
    """The changes that would actually change something."""
    out: list[Change] = []
    for change in changes:
        target = root / change.path
        if change.data is None:
            if target.exists():
                out.append(change)
            continue
        try:
            if target.read_bytes() == change.data:
                continue
        except OSError:
            pass
        out.append(change)
    return out


def _materialise(root: Path, changes: list[Change]) -> dict[str, str]:
    """Put the committed bytes where the person can see them."""
    revisions: dict[str, str] = {}
    for change in changes:
        target = root / change.path
        if change.data is None:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            continue
        revisions[change.path] = frontmatter.write_file(target, change.data)
    return revisions


def apply(
    root: Path,
    changes: list[Change],
    message: str,
    *,
    actor: str,
    kind: str = "write",
    remote: str | None = None,
    branch: str | None = None,
) -> Result:
    """Run one mutation through every step above, and report what happened."""
    message = signed(message, kind, actor)
    if not changes:
        return Result(NOTHING, "nothing to write")
    for change in changes:
        if Path(change.path).is_absolute() or ".." in Path(change.path).parts:
            raise MaboloError(f"{change.path} is not a path inside the vault")

    if not git.is_repository_inside(root):
        # A vault without Git still works; it just has no history and no way
        # back. Saying so is better than refusing to remember anything.
        problem = _stale(root, changes)
        if problem:
            return Result(STALE, problem)
        todo = _effective(root, changes)
        if not todo:
            return Result(NOTHING, "the vault already says exactly that")
        revisions = _materialise(root, todo)
        return Result(LOCAL, "written, but this vault is not a Git repository", revisions=revisions)

    with lock(root):
        half_made = git.staged_and_edited(root)
        if half_made:
            # The same class as a merge in progress: somebody is building a
            # commit by hand. Git holds two versions of such a file, and
            # committing the one on disk would throw away the one they staged
            # on purpose. Refusing costs a sentence; the other way costs a
            # version that no command can bring back.
            return Result(
                BLOCKED,
                f"{', '.join(sorted(half_made))} is staged and has been edited since. "
                "Commit it or unstage it in the vault, then try again; nothing was written.",
            )
        busy = git.mutation_in_progress(root)
        if busy:
            return Result(
                BLOCKED,
                f"{busy} is already running in the vault. Finish it first; nothing was written.",
            )
        here = git.current_branch(root)
        if here is None:
            return Result(
                BLOCKED,
                "the vault is on a detached HEAD, so there is no branch to write to.",
            )
        target_branch = branch or here
        if here != target_branch:
            # The commit is built on whatever is checked out and pushed to the
            # configured branch, so a person who made a scratch branch in their
            # vault would have published its commits into the shared one, ended
            # up with a local branch behind its own push, and seen a raw Git
            # error rather than any of that. It is the detached HEAD case
            # wearing a different name.
            return Result(
                BLOCKED,
                f"the vault is on {here!r} and this writes to {target_branch!r}. "
                f"Switch back with `git switch {target_branch}` in the vault; nothing was written.",
            )

        reachable = False
        remote_at: str | None = None
        if remote and git.has_remote(root, remote):
            reachable = git.fetch(root, remote, target_branch)
            if reachable:
                remote_at = git.rev(root, f"refs/remotes/{remote}/{target_branch}")

        # A hand made change becomes its own commit before anything else, so
        # that the diff of this write holds only what this write did.
        hand_made = git.foreign_changes(root)
        if hand_made:
            git.commit_paths(
                root,
                hand_made,
                signed(FOREIGN_MESSAGE.format(actor=actor), "foreign", actor),
            )

        base = git.rev(root, "HEAD")
        if remote_at and base and remote_at != base:
            if git.is_ancestor(root, base, remote_at):
                if not git.fast_forward(root, remote_at):
                    return Result(
                        CONFLICT,
                        "the vault could not be moved forward onto the remote. "
                        "Sort the repository out by hand; nothing was written.",
                    )
                base = git.rev(root, "HEAD")
            elif not git.is_ancestor(root, remote_at, base):
                return Result(
                    CONFLICT,
                    "this clone and the remote have both moved on. Sort them out by hand; "
                    "nothing was written.",
                )

        problem = _stale(root, changes)
        if problem:
            return Result(STALE, problem)
        todo = _effective(root, changes)
        if not todo:
            return Result(NOTHING, "the vault already says exactly that")

        commit = git.build_commit(root, base, {c.path: c.data for c in todo}, message)

        outcome = LOCAL
        if remote and git.has_remote(root, remote):
            if not reachable:
                outcome = OFFLINE
            else:
                sent = git.push(root, remote, target_branch, commit, remote_at)
                if sent == "rejected":
                    # Nothing local has moved, so there is nothing to undo: the
                    # commit object is unreferenced and Git will collect it.
                    return Result(
                        CONFLICT,
                        "the remote moved while this was being written, so the push was "
                        "refused and nothing changed. Read the entry again and decide.",
                    )
                outcome = WRITTEN if sent == "pushed" else OFFLINE

        # The ref moves first. A failure while writing the files then leaves a
        # repository that is consistent with itself and a remote that holds the
        # truth, which one command puts back; the other order leaves a commit
        # the next mutation would quietly revert.
        git.update_ref(root, f"refs/heads/{target_branch}", commit, base)
        try:
            revisions = _materialise(root, todo)
            git.reset_paths(root, [c.path for c in todo])
        except (OSError, MaboloError) as exc:
            # `MaboloError` as well, and that is the whole point of this branch:
            # `frontmatter.write_file` turns every `OSError` into one, so a
            # catch on `OSError` alone never fired. The change was committed
            # and pushed, the caller was told "refused", and the next mutation
            # committed the stale file back as a hand edit nobody made.
            names = " ".join(change.path for change in todo)
            raise MaboloError(
                f"the change is committed as {commit[:12]} and is safe, but writing the files "
                f"failed ({getattr(exc, 'strerror', None) or exc}). Put them back with "
                f"`mabolo recover files {names}`."
            ) from exc

    short = commit[:12]
    said = {
        WRITTEN: f"written and pushed as {short}",
        LOCAL: f"written as {short}, and this vault has no remote",
        OFFLINE: f"written as {short}, but the remote could not be reached, so it is only here",
    }[outcome]
    return Result(outcome, said, commit=short, revisions=revisions)
