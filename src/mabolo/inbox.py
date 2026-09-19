"""The inbox: where a proposal waits until a person decides.

Automation may suggest, and only a person commits. So a suggestion needs
somewhere to live that is visible on every machine and is not the vault. That
place is a branch called `inbox` in the vault's own repository: one
`<id>.json` file per proposal, never merged into `main`, never checked out.

Five things that are not obvious:

* **The branch has no history, on purpose.** It is always exactly one
  parentless commit. Proposals are disposable, they expire, and a rejected one
  leaves no trace here; a history of them would be a second record of what
  somebody was thinking, kept forever, in the repository that is meant to hold
  only what they approved. Because there is no history, two clones can never
  fast forward onto each other, so a merge here is a set union of files keyed
  by id and never a Git merge.
* **Nothing touches the working tree or `HEAD`.** The tree is assembled in the
  object store, `refs/heads/inbox` is moved, and that is all. This is why a
  merge or a rebase running in the vault does not block the inbox the way it
  blocks a write: there is nothing here for it to collide with.
* **The push is best effort, and `offline` is an answer.** Refusing to file a
  suggestion because a laptop is on a train would lose the suggestion. It is
  filed here, and the next inbox operation with a reachable remote pushes it
  along, because every operation rebuilds the union first.
* **Expiry happens on a rebuild, never on its own.** A pass whose whole job is
  to delete is a write nobody asked for, and it would run at a moment nobody
  chose. So a proposal past `proposal.EXPIRES_AFTER_DAYS` falls out the next
  time the branch is written for another reason.
* **A decision is not stored here.** Whether a proposal was approved or refused
  is a line in the vault's own ledger at `.mabolo/decided.md`, so the answer
  survives this branch being rebuilt and travels with the entries. This module
  reads that ledger and renders one line for it; writing it goes through
  `write.apply` like any other change to the vault.

What is deliberately missing: the quote never reaches the ledger. A rejection
is often precisely "I do not want this remembered", and a record that repeats
the sentence in order to say it was refused has remembered it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import git
from .errors import MaboloError
from .proposal import ID_CHARS, Proposal
from .schema import as_utc
from .vault import LEDGER_FILE
from . import PRODUCER
from .write import CONFLICT, LOCAL, NOTHING, OFFLINE, WRITTEN, lock, signed

#: The branch, in the vault's repository, that holds what nobody has decided yet.
BRANCH = "inbox"

#: Where the answers live, on `main`, in the vault itself.
LEDGER = LEDGER_FILE

#: What a ledger file starts with when there is not one yet. Prose, because a
#: person opening the vault in an editor should be able to tell what they are
#: looking at without knowing the tool.
LEDGER_HEADING = (
    "# Decided\n"
    "\n"
    "<!-- One line per answered proposal, newest last, written by Mabolo. The\n"
    "sentence a proposal quoted is never repeated here. -->\n"
)

#: A fifth outcome next to the four a write can have: the proposal is already on
#: the branch. Not an error, and not `written` either, because nothing new was
#: filed and a caller that reports "filed" would be claiming something it did
#: not do.
ALREADY = "already"

#: A file on the branch is named after the id it carries. Anything else is not
#: ours: it is left where it is and named, never parsed and never deleted.
_FILENAME = re.compile(rf"^[0-9a-f]{{{ID_CHARS}}}\.json$")

#: One decision, exactly as the ledger writes it. Anchored, because a line that
#: nearly matches is a line somebody hand edited, and guessing at it would
#: answer "already decided" for a proposal nobody ever saw.
_DECISION = re.compile(
    rf"^- (?P<id>[0-9a-f]{{{ID_CHARS}}}) (?P<at>\d{{4}}-\d{{2}}-\d{{2}}) "
    r"(?P<by>\S+) (?P<answer>yes|no) (?P<action>\S+) (?P<target>\S.*)$"
)


@dataclass(frozen=True)
class Result:
    """What happened, in the words the caller will repeat."""

    outcome: str
    message: str
    #: The proposals this touched: one for a filing, however many a forget removed.
    ids: tuple[str, ...] = ()
    commit: str | None = None
    #: Proposals that fell out because nobody looked at them in time. Reported
    #: rather than swallowed: a caller that never says so leaves a person
    #: wondering where a suggestion went.
    expired: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.ids[0] if self.ids else ""

    @property
    def ok(self) -> bool:
        return self.outcome in (WRITTEN, LOCAL, OFFLINE, NOTHING, ALREADY)


@dataclass(frozen=True)
class Listing:
    """Everything on the branch, including what would not read.

    The unreadable files are carried in the answer rather than raised, because
    one broken file must not hide the other nine. It is named, so that it can
    be looked at, and it stays on the branch: deleting what cannot be parsed
    would throw away the only copy of somebody's suggestion.
    """

    proposals: list[Proposal]
    unreadable: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    """One answered proposal, as the ledger records it."""

    id: str
    #: The day, as `YYYY-MM-DD`. A date, not a moment: the ledger is read by
    #: people, and the hour of an approval has never settled anything.
    at: str
    by: str
    approved: bool
    action: str
    target: str

    @property
    def answer(self) -> str:
        return "yes" if self.approved else "no"


# Reading the branch. Nothing here checks anything out.


def _files(root: Path, commit: str | None) -> dict[str, bytes]:
    """Every file in that commit's tree, by name. An empty answer for no commit."""
    if not commit:
        return {}
    listed = git.run_bytes(root, "ls-tree", "-z", "--name-only", commit)
    names = [name for name in listed.decode("utf-8", "surrogateescape").split("\0") if name]
    return {name: git.run_bytes(root, "cat-file", "blob", f"{commit}:{name}") for name in names}


def _read_one(name: str, data: bytes) -> Proposal:
    """One file as a proposal, or a sentence saying why it is not one.

    The name is checked against the content. The id is the file's name and a
    claim about what is inside it, and `Proposal.from_json` can only compare
    the content with the id the content claims: a file somebody renamed would
    pass that check and then be approved under an id a person read next to a
    different sentence.
    """
    if not _FILENAME.match(name):
        raise MaboloError("not a proposal file name")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MaboloError(f"not UTF-8: {exc}") from exc
    found = Proposal.from_json(text)
    if found.filename != name:
        raise MaboloError(f"the file is called {name} and its content is {found.filename}")
    return found


def _moment(found: Proposal) -> dt.datetime | None:
    """When it was filed, as a moment that can be compared with another one.

    A naive timestamp is read as this machine's local time, the same way
    `Proposal.expired` reads it. Two clones writing the same vault can easily
    be in two time zones, and comparing a naive moment with an aware one is a
    `TypeError` in the middle of a merge.
    """
    when = found.filed_on()
    if when is None:
        return None
    # The same rule as everywhere: a stamp without an offset is UTC. A file on
    # this branch was written on whichever machine filed it.
    return as_utc(when)


def read(root: Path, *, branch: str = BRANCH) -> Listing:
    """What is on the branch in this clone, newest first, with the bad files named."""
    proposals: list[Proposal] = []
    unreadable: list[str] = []
    for name, data in sorted(_files(root, git.rev(root, f"refs/heads/{branch}")).items()):
        try:
            proposals.append(_read_one(name, data))
        except MaboloError as exc:
            unreadable.append(f"{name}: {exc}")
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    proposals.sort(key=lambda p: (_moment(p) or floor, p.id), reverse=True)
    return Listing(proposals, unreadable)


def pending(root: Path, *, branch: str = BRANCH) -> list[Proposal]:
    """The open proposals, newest first.

    Everything on the branch, including one that is old enough to be dropped on
    the next rebuild. A reader that hid it would be running the tidy pass in
    the one place that is supposed to be a pure read, and a person is still
    allowed to approve a suggestion from five weeks ago.
    """
    return read(root, branch=branch).proposals


# Building the branch: fetch, union, expiry, push.


def _fetch_branch(root: Path, remote: str, branch: str) -> tuple[bool, str | None]:
    """Where the remote's inbox is: `(reachable, commit)`.

    A remote that has never seen a proposal has no such branch, and a fetch for
    one fails exactly the way an unreachable host does. The two mean opposite
    things, so the remote is asked rather than the wording of the refusal read:
    `--exit-code` answers 2 for "reachable, no such ref".
    """
    spec = f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"
    if git.run(root, "fetch", "--quiet", remote, spec, check=False).returncode == 0:
        return True, git.rev(root, f"refs/remotes/{remote}/{branch}")
    # Reachable and no such branch is the ordinary state of a remote that has
    # never seen a proposal, and it is the one this has to tell from a dead
    # host. The reading of `--exit-code` lives in `git.reachable`, in one copy.
    return git.reachable(root, remote, f"refs/heads/{branch}"), None


def _earlier(mine: bytes, theirs: bytes) -> bytes:
    """Of two files with the same id, the one that was filed first.

    Same id means the same action, the same target and the same sentence, so
    the two differ only in when and by whom they were filed. The first filing
    is the one that happened; the second is the same suggestion made twice, and
    keeping it would move the expiry date of a proposal every time an
    extraction pass runs.
    """
    left, right = _parse(mine), _parse(theirs)
    if left is None:
        return theirs
    if right is None:
        return mine
    here, there = _moment(left), _moment(right)
    if here is None:
        return theirs
    if there is None:
        return mine
    return mine if here <= there else theirs


def _parse(data: bytes) -> Proposal | None:
    try:
        return Proposal.from_json(data.decode("utf-8"))
    except (MaboloError, UnicodeDecodeError):
        return None


def _merge(mine: dict[str, bytes], theirs: dict[str, bytes]) -> dict[str, bytes]:
    """Both sides, keyed by id. A branch with no history has no other merge."""
    union: dict[str, bytes] = {}
    for name in sorted(set(mine) | set(theirs)):
        here, there = mine.get(name), theirs.get(name)
        if here is None or there is None:
            union[name] = here if there is None else there
        elif here == there:
            union[name] = here
        else:
            union[name] = _earlier(here, there)
    return union


def _stale(name: str, data: bytes, now: dt.datetime) -> bool:
    """Whether this file is past its date. A file that will not read never is."""
    try:
        found = _read_one(name, data)
    except MaboloError:
        return False
    return found.expired(now)


@dataclass(frozen=True)
class _Plan:
    """The files the next commit will hold, and what it took to get there."""

    files: dict[str, bytes]
    already: bool = False
    removed: tuple[str, ...] = ()
    expired: tuple[str, ...] = ()


def _plan(
    mine: dict[str, bytes],
    theirs: dict[str, bytes],
    *,
    add: Proposal | None,
    drop: frozenset[str],
    now: dt.datetime,
) -> _Plan:
    """What the branch should hold: both sides, minus the stale and the decided."""
    union = _merge(mine, theirs)
    expired = tuple(sorted(name for name, data in union.items() if _stale(name, data, now)))
    for name in expired:
        union.pop(name)
    removed = tuple(sorted(name for name in drop if name in union))
    for name in removed:
        union.pop(name)
    already = add is not None and add.filename in union
    if add is not None and not already:
        union[add.filename] = add.to_json().encode("utf-8")
    return _Plan(
        files=dict(sorted(union.items())),
        already=already,
        removed=tuple(name.removesuffix(".json") for name in removed),
        expired=tuple(name.removesuffix(".json") for name in expired),
    )


def _sync(
    root: Path,
    *,
    message: str,
    kind: str = "propose",
    actor: str = "",
    add: Proposal | None = None,
    drop: frozenset[str] = frozenset(),
    remote: str | None = None,
    branch: str = BRANCH,
    now: dt.datetime | None = None,
) -> Result:
    """Rebuild the branch as one parentless commit, and try to push it.

    Every operation goes through here, which is what makes a filing that could
    not be pushed last week leave with the next one: the union is rebuilt from
    the remote and this clone every single time, so there is no backlog to
    remember and nothing to retry by hand.
    """
    if not git.is_repository_inside(root):
        raise MaboloError(
            "the inbox is a branch, so it needs a Git repository. "
            "This vault is a plain folder, and a proposal would have nowhere to wait."
        )
    here = git.current_branch(root)
    if here is not None and branch == here:
        # The inbox is rebuilt as one parentless commit every time, so pointing
        # it at the branch the vault lives on would replace the entire history
        # of that branch with a tree holding nothing but proposals. One caller
        # passed the configured remote branch here by mistake, which is exactly
        # how that happens.
        raise MaboloError(
            f"the inbox cannot live on {branch!r}, which is the branch this vault is on. "
            "It is an orphan branch and it is rebuilt from nothing every time."
        )
    now = now or dt.datetime.now().astimezone()
    with lock(root):
        here = git.rev(root, f"refs/heads/{branch}")
        mine = _files(root, here)

        configured = bool(remote) and git.has_remote(root, str(remote))
        reachable, there = False, None
        if configured:
            reachable, there = _fetch_branch(root, str(remote), branch)
        theirs = _files(root, there)

        plan = _plan(mine, theirs, add=add, drop=drop, now=now)
        ids = (add.id,) if add is not None else plan.removed

        if plan.files == mine and (not reachable or plan.files == theirs):
            outcome = ALREADY if plan.already else NOTHING
            said = (
                f"{ids[0]} is already in the inbox"
                if plan.already
                else "the inbox already says exactly that"
            )
            return Result(outcome, said, ids=ids, expired=plan.expired)

        commit = git.build_commit(root, None, dict(plan.files), signed(message, kind, actor or PRODUCER))
        outcome = LOCAL
        if reachable:
            sent = git.push(root, str(remote), branch, commit, there)
            if sent == "rejected":
                # Somebody filed something between the fetch and the push. The
                # lease did its job; the union is rebuilt against where the
                # remote actually is and pushed once more. Twice is the limit:
                # a third round against a remote that keeps moving is a loop,
                # and the caller can see a conflict and try again.
                reachable, there = _fetch_branch(root, str(remote), branch)
                plan = _plan(mine, _files(root, there), add=add, drop=drop, now=now)
                ids = (add.id,) if add is not None else plan.removed
                commit = git.build_commit(root, None, dict(plan.files), signed(message, kind, actor or PRODUCER))
                sent = "rejected"
                if reachable:
                    sent = git.push(root, str(remote), branch, commit, there)
                if sent == "rejected":
                    # Nothing local moved, so there is nothing to undo: the
                    # commit object is unreferenced and Git collects it.
                    return Result(
                        CONFLICT,
                        "the inbox on the remote moved twice while this was being filed, "
                        "so nothing was changed here. Try again.",
                        ids=ids,
                    )
            outcome = WRITTEN if sent == "pushed" else OFFLINE
        elif configured:
            outcome = OFFLINE

        git.update_ref(root, f"refs/heads/{branch}", commit, here)

    short = commit[:12]
    said = {
        WRITTEN: f"{short}, and the remote has it",
        LOCAL: f"{short}, and this vault has no remote",
        OFFLINE: (
            f"{short}, but the remote could not be reached, so it is only here. "
            "The next inbox operation with a reachable remote takes it along"
        ),
    }[outcome]
    if plan.already:
        said = f"{ids[0]} is already in the inbox: {said}"
        return Result(ALREADY, said, ids, short, plan.expired)
    return Result(outcome, f"the inbox is now {said}", ids, short, plan.expired)


def _one_line(text: str, limit: int = 60) -> str:
    """Text that cannot add a line of its own to a commit message or a ledger."""
    return " ".join(str(text).split())[:limit]


def file(
    root: Path,
    proposal: Proposal,
    *,
    remote: str | None = None,
    branch: str = BRANCH,
    now: dt.datetime | None = None,
) -> Result:
    """Put one proposal on the branch, unless its id is already there.

    An id that is already on the branch is answered with `already` rather than
    with an error. The id is derived from the action, the target and the
    sentence, so the same suggestion made twice really is the same suggestion,
    and an extraction pass that runs over a transcript twice must not turn one
    thought into two things to read. The branch is still rebuilt, because a
    proposal filed while the remote was unreachable is waiting to go out.
    """
    now = now or dt.datetime.now().astimezone()
    if not proposal.filed_at:
        # Stamped here rather than by the caller: a proposal with no date never
        # expires, so a producer that forgets the field would fill the inbox
        # with suggestions that can only be removed by hand.
        proposal = replace(proposal, filed_at=now.isoformat(timespec="seconds"))
    source = _one_line(proposal.source)
    message = f"inbox: file {proposal.id}" + (f", from {source}" if source else "")
    return _sync(
        root,
        message=message,
        kind="propose",
        actor=proposal.source or PRODUCER,
        add=proposal,
        remote=remote,
        branch=branch,
        now=now,
    )


def forget(
    root: Path,
    ids: list[str] | tuple[str, ...],
    *,
    remote: str | None = None,
    branch: str = BRANCH,
    now: dt.datetime | None = None,
) -> Result:
    """Take decided proposals off the branch.

    An id that is not there is not an error. A person answers a proposal and
    the answer goes into the ledger; whether this clone still had the file is
    a detail of which machine ran the extraction, and failing here would leave
    the ledger and the branch disagreeing over a typo.
    """
    wanted = frozenset(f"{one.strip().lower()}.json" for one in ids if one.strip())
    named = ", ".join(sorted(name.removesuffix(".json") for name in wanted)) or "nothing"
    return _sync(
        root,
        message=f"inbox: forget {_one_line(named, 200)}",
        kind="unfile",
        drop=wanted,
        remote=remote,
        branch=branch,
        now=now,
    )


def sync(
    root: Path,
    *,
    remote: str | None = None,
    branch: str = BRANCH,
    now: dt.datetime | None = None,
) -> Result:
    """Bring this clone's branch and the remote's into step, without deciding anything.

    What a caller runs before it shows a person a list. A clone that has only
    ever read has no local `inbox` branch at all, and every other operation
    here is triggered by somebody filing or answering something, so without
    this a fresh machine would show an empty inbox and be wrong.

    It is not the tidy pass that was ruled out. Expiry still happens as a
    consequence of a rebuild somebody asked for, rather than in a job that runs
    on its own and deletes at a moment nobody chose.
    """
    return _sync(root, message="inbox: rebuild", kind="propose", remote=remote, branch=branch, now=now)


# The ledger: what was answered, in the vault, on `main`.


def decisions(root: Path) -> list[Decision]:
    """Every answer the vault records, oldest first, skipping what does not parse.

    Read off disk rather than out of Git. The ledger lives on `main`, which is
    the branch the person has checked out, and reading it from a commit would
    answer about a commit: a line somebody appended in an editor five minutes
    ago is an answer too, and a proposal must not come back because it has not
    been committed yet.
    """
    try:
        text = (root / LEDGER).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    found: list[Decision] = []
    for line in text.splitlines():
        match = _DECISION.match(line.rstrip())
        if match:
            found.append(
                Decision(
                    id=match["id"],
                    at=match["at"],
                    by=match["by"],
                    approved=match["answer"] == "yes",
                    action=match["action"],
                    target=match["target"],
                )
            )
    return found


def decided(root: Path, id: str) -> Decision | None:
    """The answer this id already got, or None.

    The last line wins. The ledger is appended newest last, and a proposal that
    was refused in March and approved in June was approved: reading the first
    line would let one old "no" outlive every decision after it.
    """
    wanted = id.strip().lower()
    for one in reversed(decisions(root)):
        if one.id == wanted:
            return one
    return None


def decision_line(
    proposal: Proposal,
    *,
    approved: bool,
    by: str,
    at: dt.date | dt.datetime | None = None,
) -> str:
    """The one line an answer adds to the ledger.

    The sentence the proposal quoted is not in it, and that is the point. A
    refusal is often exactly "I do not want this remembered", and a ledger that
    repeated the sentence in order to record that it was refused would have
    remembered it, in the vault, for as long as the vault exists. What the line
    carries is enough to recognise the same suggestion when it comes back: the
    id is derived from the sentence, so the sentence never has to be kept.
    """
    if not by.strip():
        raise MaboloError("a decision is somebody's, so it says whose")
    when = at or dt.datetime.now().astimezone()
    day = when.date() if isinstance(when, dt.datetime) else when
    answer = "yes" if approved else "no"
    return (
        f"- {proposal.id} {day.isoformat()} {_one_line(by, 40)} {answer} "
        f"{_one_line(proposal.action, 20)} {_one_line(proposal.target, 120)}"
    )


def append(existing: str, line: str) -> str:
    """The ledger with one more line at the end, newline handling included.

    Offered here so that every caller writes the same file. The write itself
    goes through `write.apply`, because the ledger is part of the vault and
    everything in the vault arrives by the one path that commits atomically.
    """
    body = existing if existing.strip() else LEDGER_HEADING
    if not body.endswith("\n"):
        body += "\n"
    return f"{body}{line.rstrip()}\n"
