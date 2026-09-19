"""Turning a proposal into an answer, which is the only way one becomes an entry.

The inbox holds what automation suggested. This is the step a person takes, and
it is the reason the inbox exists: a proposal is disposable, an entry is not,
and the difference between them is somebody saying yes.

Three things that are not obvious:

* **A yes is one commit.** The entry and the line in the ledger that records the
  answer travel together. Two commits would leave a window in which the vault
  holds a claim that nothing accounts for, and the ledger is what `why` reads
  to say who approved something.
* **A no is also written down.** The proposal file goes, but the id stays in
  the ledger, so the pass that suggested it does not suggest the same sentence
  again next week. The sentence itself is never written there: a refusal is
  often precisely "I do not want this remembered".
* **The validator runs again here.** It ran when the proposal was filed, and
  the vault has moved since: an entry that was valid against last week's areas
  may not be valid now, and approving is a write like any other.

The consent gate is not repeated at this door for the command line, because
somebody typing `mabolo inbox a3f2 yes` into their own shell is the person. A
model calling the tool is a different matter, and that path carries a quote
naming the id.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from . import frontmatter, inbox, write
from .errors import MaboloError
from .proposal import Proposal
from .schema import Entry, Verification, iso, now
from .vault import PassageNotUnique, Vault


@dataclass(frozen=True)
class Answer:
    """What one decision did, in the words the caller repeats."""

    id: str
    approved: bool
    result: write.Result
    #: Where the entry went, for a yes. Empty for a no.
    where: str = ""

    @property
    def ok(self) -> bool:
        return self.result.ok

    def line(self) -> str:
        said = "approved" if self.approved else "refused"
        target = f" {self.where}" if self.where else ""
        return f"{self.id} {said}{target}: {self.result.message}"


def _ledger(vault: Vault, proposal: Proposal, *, approved: bool, by: str,
            at: dt.datetime) -> write.Change:
    """The one line that records the answer, as a change to the vault."""
    path = vault.root / inbox.LEDGER
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    revision = frontmatter.revision(existing.encode("utf-8")) if existing else None
    line = inbox.decision_line(proposal, approved=approved, by=by, at=at)
    return write.Change(
        path=inbox.LEDGER,
        data=inbox.append(existing, line).encode("utf-8"),
        expect=revision,
    )


def _written(vault: Vault, proposal: Proposal, *, by: str, at: dt.datetime) -> write.Change:
    """The entry a `write` proposal would add, validated all over again."""
    meta, body = frontmatter.parse(proposal.entry)
    if meta is None:
        raise MaboloError(f"{proposal.id}: the proposed entry has no frontmatter")
    area, _, name = proposal.target.rpartition("/")
    if not area or not name:
        raise MaboloError(
            f"{proposal.id}: {proposal.target!r} does not say which area the entry belongs to"
        )
    entry = Entry.from_meta(meta, body=body)
    # Approval is the verification. Nobody has to confirm a sentence twice, and
    # the person who said yes is the person the entry names.
    entry.approve(by, iso(at) or "")
    entry.path = vault.path_for(area, name)
    target, data = vault.render_entry(entry)
    return write.Change(path=target.relative_to(vault.root).as_posix(), data=data, expect=None)


def _edited(vault: Vault, proposal: Proposal, *, by: str, at: dt.datetime) -> write.Change:
    """The entry an `edit` proposal would change, with the passage still unique."""
    path = _entry_path(vault, proposal)
    document = frontmatter.read(path)
    try:
        entry = vault.replace_once(path, proposal.old, proposal.new)
    except PassageNotUnique as ambiguous:
        raise MaboloError(
            f"{proposal.id}: the passage it replaces is in {path.stem} {ambiguous.seen} times, "
            "so the edit is not the one that was proposed any more"
        ) from ambiguous
    entry.approve(by, iso(at) or "")
    target, data = vault.render_entry(entry)
    return write.Change(
        path=target.relative_to(vault.root).as_posix(), data=data, expect=document.revision
    )


def _entry_path(vault: Vault, proposal: Proposal):
    """The file an edit or a forget is about.

    By name, because that is what a proposal for those two carries, and a name
    is unique in a vault. Two files with one name is a vault that `doctor`
    reports, and guessing which of them was meant would be the worse answer.
    """
    found = [p for p in vault.entry_paths() if p.stem == proposal.target]
    if not found:
        raise MaboloError(f"{proposal.id}: there is no entry called {proposal.target!r}")
    if len(found) > 1:
        where = ", ".join(sorted(p.relative_to(vault.root).as_posix() for p in found))
        raise MaboloError(f"{proposal.id}: {proposal.target!r} is in more than one file: {where}")
    return found[0]


def _forgotten(vault: Vault, proposal: Proposal) -> write.Change:
    """The entry a `forget` proposal would remove."""
    path = _entry_path(vault, proposal)
    return write.Change(
        path=path.relative_to(vault.root).as_posix(),
        data=None,
        expect=frontmatter.read(path).revision,
    )


def answer(
    vault: Vault,
    proposal: Proposal,
    *,
    approved: bool,
    by: str,
    remote: str | None = None,
    branch: str | None = None,
    at: dt.datetime | None = None,
) -> Answer:
    """Record one decision, and carry it out when it is a yes.

    The order matters and is the reverse of what looks natural: the vault is
    written first and the proposal is taken off the branch afterwards. A
    proposal that is still in the inbox after a successful write is a thing to
    answer twice, which is a nuisance; an entry that was written while the
    inbox forgot the proposal is a decision with no record, which is not.
    """
    moment = at or now()
    changes = [_ledger(vault, proposal, approved=approved, by=by, at=moment)]
    where = ""
    if approved:
        if proposal.action == "write":
            change = _written(vault, proposal, by=by, at=moment)
        elif proposal.action == "edit":
            change = _edited(vault, proposal, by=by, at=moment)
        else:
            change = _forgotten(vault, proposal)
        where = change.path
        changes.insert(0, change)

    said = "approve" if approved else "reject"
    result = write.apply(
        vault.root,
        changes,
        f"{said} {proposal.id}: {proposal.action} {proposal.target}",
        actor=by,
        kind=said,
        remote=remote,
        branch=branch,
    )
    if result.ok:
        inbox.forget(vault.root, [proposal.id], remote=remote)
    return Answer(id=proposal.id, approved=approved, result=result, where=where)
