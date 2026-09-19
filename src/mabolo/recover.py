"""Getting back, when something went wrong.

A memory that can be written by a tool has to have a way back that a person can
take without knowing Git, and that way back must not itself look like damage.
Three commands, and everything else is a procedure printed for a person rather
than carried out for them.

* **`revert`** undoes one commit. Git already does that; what this adds is that
  the undoing carries the same trailer as everything else Mabolo commits, goes
  out with the same lease, and therefore does not show up in `doctor` as a
  commit from nowhere.
* **`files`** puts named files back to what the last commit says they are. It
  is for the one case the transaction leaves open: the commit was safe and
  writing the file afterwards failed. It refuses a file whose current bytes
  were never committed under that path, because that is somebody's hand edit
  and losing it would be the repair doing the damage.
* **`push`** sends a branch that has commits nobody else has yet. On a refusal
  it lists both sides and stops, because merging or rebasing on somebody's
  behalf is a decision, and a decision belongs to a person.

Nothing here resets, cleans or forces.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import frontmatter, git, write
from .errors import MaboloError


@dataclass(frozen=True)
class Plan:
    """What a recovery would do, before it does any of it."""

    action: str
    #: One line per file or commit, for a person to read and refuse.
    lines: tuple[str, ...] = ()
    #: The changes a write would carry out. Empty for a plan with nothing to do.
    changes: tuple[write.Change, ...] = ()
    note: str = ""

    @property
    def empty(self) -> bool:
        return not self.changes

    def render(self) -> str:
        head = f"{self.action}:"
        body = "\n".join(f"  {line}" for line in self.lines) or "  nothing to do"
        return f"{head}\n{body}" + (f"\n{self.note}" if self.note else "")


def _blob(root: Path, revision: str, path: str) -> bytes | None:
    """The bytes of one path at one commit, or None when it was not there."""
    found = git.run(root, "cat-file", "-e", f"{revision}:{path}", check=False)
    if found.returncode != 0:
        return None
    return git.read_blob(root, f"{revision}:{path}")


def _touched(root: Path, revision: str) -> list[str]:
    result = git.run(root, "show", "--name-only", "--format=", "--no-renames", revision)
    return [line for line in result.stdout.splitlines() if line.strip()]


def plan_revert(root: Path, revision: str) -> Plan:
    """The inverse of one commit, path by path.

    Built from the trees rather than from a patch, because a patch can fail to
    apply and this must either be a whole commit or nothing at all. A file the
    commit added goes away; a file it changed or removed comes back as its
    parent had it.
    """
    if not git.is_repository_inside(root):
        raise MaboloError("this vault is not a Git repository, so there is nothing to revert")
    found = git.rev(root, revision)
    if found is None:
        raise MaboloError(f"{revision} is not a commit in this vault")
    parents = git.run(root, "rev-list", "--parents", "-n", "1", found).stdout.split()
    if len(parents) != 2:
        raise MaboloError(
            f"{revision[:12]} has {len(parents) - 1} parents. Mabolo never merges, so this is "
            "not a commit it made, and reverting it is a decision for a person and for Git."
        )
    parent = parents[1]

    changes: list[write.Change] = []
    lines: list[str] = []
    for path in _touched(root, found):
        before = _blob(root, parent, path)
        now = (root / path).read_bytes() if (root / path).exists() else None
        if before == now:
            continue
        expect = frontmatter.revision(now) if now is not None else None
        changes.append(write.Change(path=path, data=before, expect=expect))
        lines.append(f"{'remove' if before is None else 'restore'} {path}")
    subject = git.run(root, "log", "-1", "--format=%s", found).stdout.strip()
    return Plan(
        action=f"revert {found[:12]} ({subject})",
        lines=tuple(lines),
        changes=tuple(changes),
    )


def plan_files(root: Path, paths: list[str]) -> Plan:
    """Put named files back to what HEAD says they hold.

    A file whose current bytes were never committed under this path is refused.
    That is a hand edit, and a repair that silently threw one away would be the
    damage rather than the cure.
    """
    if not git.is_repository_inside(root):
        raise MaboloError("this vault is not a Git repository, so there is nothing to recover")
    head = git.rev(root, "HEAD")
    if head is None:
        raise MaboloError("this vault has no commit yet, so there is nothing to recover from")

    changes: list[write.Change] = []
    lines: list[str] = []
    refused: list[str] = []
    for path in paths:
        target = root / path
        wanted = _blob(root, head, path)
        if wanted is None:
            refused.append(f"{path} is not in the last commit, so there is nothing to put back")
            continue
        now = target.read_bytes() if target.exists() else None
        if now == wanted:
            continue
        if now is not None and not _was_committed(root, path, now):
            refused.append(
                f"{path} holds bytes that were never committed under that name. "
                "That is a hand edit, not a failed write, and this will not throw it away."
            )
            continue
        changes.append(
            write.Change(
                path=path,
                data=wanted,
                expect=frontmatter.revision(now) if now is not None else None,
            )
        )
        lines.append(f"restore {path}")
    return Plan(
        action="recover files",
        lines=tuple(lines),
        changes=tuple(changes),
        note="\n".join(f"  refused: {one}" for one in refused),
    )


def _was_committed(root: Path, path: str, data: bytes) -> bool:
    """Whether these exact bytes ever stood at this path in the history."""
    wanted = git.hash_object_id(root, data)
    seen = git.run(
        root, "log", "--format=%H", "--follow", "--", path, check=False
    ).stdout.split()
    for revision in seen:
        if _blob_id(root, revision, path) == wanted:
            return True
    return False


def _blob_id(root: Path, revision: str, path: str) -> str | None:
    result = git.run(root, "rev-parse", f"{revision}:{path}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def carry_out(root: Path, plan: Plan, *, actor: str, kind: str,
              remote: str | None = None, branch: str | None = None) -> write.Result:
    """Run a plan through the same transaction every other change goes through."""
    if plan.empty:
        return write.Result(write.NOTHING, "nothing to do")
    return write.apply(
        root, list(plan.changes), plan.action, actor=actor, kind=kind, remote=remote, branch=branch
    )


@dataclass(frozen=True)
class Standing:
    """Where this clone and the remote stand relative to each other."""

    ahead: tuple[str, ...] = ()
    behind: tuple[str, ...] = ()
    reachable: bool = True
    pushed: bool = False
    message: str = ""

    def render(self) -> str:
        if not self.reachable:
            return self.message
        if self.pushed:
            return self.message
        lines = [self.message]
        if self.ahead:
            lines += ["", "here and not on the remote:"] + [f"  {one}" for one in self.ahead]
        if self.behind:
            lines += ["", "on the remote and not here:"] + [f"  {one}" for one in self.behind]
        return "\n".join(lines)


def push(root: Path, *, remote: str, branch: str) -> Standing:
    """Send what this clone has, or say exactly how the two sides differ.

    A refusal is where this stops. Merging or rebasing on somebody's behalf is
    a decision, and both sides are printed so that the person making it can see
    what they are deciding about.
    """
    if not git.has_remote(root, remote):
        return Standing(reachable=False, message=f"there is no remote called {remote}")
    if not git.fetch(root, remote, branch):
        return Standing(reachable=False, message=f"{remote} could not be reached")
    here = git.rev(root, f"refs/heads/{branch}")
    there = git.rev(root, f"refs/remotes/{remote}/{branch}")
    if here is None:
        return Standing(message=f"there is no branch called {branch} here")
    if here == there:
        return Standing(pushed=True, message="the remote already has everything this clone has")
    if there is not None and not git.is_ancestor(root, there, here):
        # Asked before pushing, because `--force-with-lease` is a form of
        # `--force`: it refuses when the remote moved somewhere unexpected, and
        # happily overwrites when it moved exactly where we last looked. Here
        # the two sides have diverged, so sending this would delete somebody
        # else's commit while the lease said everything was fine.
        return Standing(
            ahead=_log(root, there, here),
            behind=_log(root, here, there),
            message=(
                "this clone and the remote have both moved on, so nothing was sent. "
                "Look at both lists, then merge or rebase by hand; Mabolo will not decide this."
            ),
        )

    sent = git.push(root, remote, branch, here, there)
    if sent == "pushed":
        return Standing(pushed=True, message=f"pushed {here[:12]} to {remote}/{branch}")
    if sent == "unreachable":
        return Standing(reachable=False, message=f"{remote} could not be reached")
    return Standing(
        ahead=_log(root, there, here),
        behind=_log(root, here, there),
        message=(
            f"{remote} refused the push, so the two sides have both moved. Nothing was changed. "
            "Look at both lists, then merge or rebase by hand; Mabolo will not decide this."
        ),
    )


def _log(root: Path, base: str | None, tip: str | None) -> tuple[str, ...]:
    if tip is None:
        return ()
    span = f"{base}..{tip}" if base else tip
    result = git.run(root, "log", "--format=%h %s", span, check=False)
    return tuple(line for line in result.stdout.splitlines() if line.strip())
