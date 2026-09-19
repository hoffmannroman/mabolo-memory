"""A proposal: what automation is allowed to produce on its own.

Everything that is not a person filing a change ends here instead of in the
vault. An extraction pass over a finished transcript, a lint run, a monthly
tidy: all of them may say "this looks worth remembering", and none of them may
write it down. A person turns a proposal into an entry, and until then it lives
on a branch that is never merged into `main`.

Three things that are not obvious:

* **The id is derived, not invented.** It is a hash of the action, the target
  and the sentence, with the sentence folded the same way the consent check
  folds it. So the same suggestion made twice is the same id twice, a proposal
  that was rejected last week is recognised when it comes back this week, and
  the id can be recomputed from the file rather than trusted from it.
* **It is JSON, not Markdown.** One shape covers writing, editing and
  forgetting, where a frontmatter would only cover the first, and nothing that
  reads the vault can mistake a proposal for an entry. The Markdown an approval
  would commit rides inside it as text, and it goes through the validator when
  the proposal is filed and again when it is approved.
* **It carries no decision.** Whether a proposal was accepted or refused is not
  written on the proposal: it is a line in the vault's own ledger, so that the
  answer survives the branch being rebuilt and travels with the entries rather
  than with the suggestions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .consent import normalise, redact
from .schema import as_utc
from .errors import MaboloError

#: What a proposal can ask for. The same three verbs as the write tools,
#: because a proposal is a write tool call that has to wait for a person.
ACTIONS = ("write", "edit", "forget")

#: How much of the hash becomes the id. Eight hex is four billion values, which
#: is far past what an inbox of tens needs; the length is about being able to
#: recognise one, not about collisions.
ID_CHARS = 8

#: The shortest prefix a person may type. Below four there is nothing to
#: recognise, and an ambiguous prefix is answered with the candidates rather
#: than with a guess.
MIN_PREFIX = 4

#: A proposal that nobody has looked at in this long is not a backlog, it is
#: litter. The file is dropped when the inbox is next rebuilt.
EXPIRES_AFTER_DAYS = 30


def fold(quote: str) -> str:
    """The sentence as the id sees it: redacted, then folded like a consent check.

    Redacted first so that a proposal quoting a pasted secret cannot be
    recognised by that secret, and folded afterwards so that the same sentence
    with a capital letter or a doubled space is the same proposal.
    """
    return normalise(redact(quote)).casefold()


def identify(action: str, target: str, quote: str) -> str:
    """The id of this suggestion, recomputable from the file it is written in."""
    material = "\0".join((action, target, fold(quote))).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:ID_CHARS]


@dataclass(frozen=True)
class Proposal:
    """One suggestion, filed by something that is not allowed to write."""

    action: str
    #: What it is about: `<area>/<name>` for a write, the entry name otherwise.
    target: str
    #: The person's own sentence, verbatim, as the gate would want it later.
    quote: str
    #: The bytes an approval would commit, for a write. Text, so that one JSON
    #: file holds a whole entry without a second encoding.
    entry: str = ""
    #: For an edit: the passage that must occur exactly once, and its successor.
    old: str = ""
    new: str = ""
    #: Why the proposal exists, in the words of whatever produced it.
    note: str = ""
    #: Who produced it, as an actor. Never a person: a person does not propose.
    source: str = ""
    filed_at: str = ""

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise MaboloError(f"{self.action!r} is not something a proposal can ask for")
        if not self.target.strip():
            raise MaboloError("a proposal says what it is about")
        if not self.quote.strip():
            raise MaboloError("a proposal carries the sentence it came from, or it is a guess")
        if self.action == "write" and not self.entry.strip():
            raise MaboloError("a proposed entry carries the file it would write")
        if self.action == "edit" and not self.old.strip():
            raise MaboloError("a proposed edit says which passage it replaces")

    @property
    def id(self) -> str:
        return identify(self.action, self.target, self.quote)

    @property
    def filename(self) -> str:
        return f"{self.id}.json"

    def filed_on(self) -> dt.datetime | None:
        try:
            return dt.datetime.fromisoformat(self.filed_at)
        except ValueError:
            return None

    def expired(self, now: dt.datetime, days: int = EXPIRES_AFTER_DAYS) -> bool:
        """Whether this has sat unlooked at long enough to be litter.

        A proposal with no readable date never expires. Dropping a file because
        its timestamp could not be parsed would lose the one thing that can
        still be read: what somebody suggested.
        """
        filed = self.filed_on()
        if filed is None:
            return False
        # Read as UTC, not as this machine's local time. `filed_at` travels
        # between machines on the inbox branch, and reading it locally would
        # let two clones drop the same proposal on two different days.
        return as_utc(filed) < as_utc(now) - dt.timedelta(days=days)

    def to_json(self) -> str:
        meta: dict[str, Any] = {
            "id": self.id,
            "action": self.action,
            "target": self.target,
            "quote": self.quote,
            "filed_at": self.filed_at,
            "source": self.source,
        }
        for key in ("entry", "old", "new", "note"):
            value = getattr(self, key)
            if value:
                meta[key] = value
        return json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Proposal:
        """Read one back, and refuse a file whose id does not match its content.

        The id is the name of the file and a claim about what is inside it. A
        proposal whose content was changed after filing would otherwise be
        approved under an id somebody already looked at.
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MaboloError(f"a proposal that is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise MaboloError("a proposal is an object")
        made = cls(
            action=str(data.get("action", "")),
            target=str(data.get("target", "")),
            quote=str(data.get("quote", "")),
            entry=str(data.get("entry", "")),
            old=str(data.get("old", "")),
            new=str(data.get("new", "")),
            note=str(data.get("note", "")),
            source=str(data.get("source", "")),
            filed_at=str(data.get("filed_at", "")),
        )
        claimed = str(data.get("id", ""))
        if claimed and claimed != made.id:
            raise MaboloError(
                f"the proposal calls itself {claimed} and its content says {made.id}. "
                "Something changed it after it was filed."
            )
        return made

    def line(self) -> str:
        """The one line an inbox listing shows."""
        said = normalise(self.quote)
        if len(said) > 100:
            said = said[:97].rstrip() + "..."
        return f"{self.id}  {self.action} {self.target}  \"{said}\""


def resolve(prefix: str, proposals: list[Proposal]) -> Proposal:
    """The one proposal a typed prefix names, or an error listing the candidates.

    Never a guess. Picking the first match would mean a person approving
    something they did not read, which is the one thing the whole inbox exists
    to prevent.
    """
    wanted = prefix.strip().lower()
    if len(wanted) < MIN_PREFIX:
        raise MaboloError(f"{prefix!r} is too short to name a proposal, type at least {MIN_PREFIX}")
    found = [p for p in proposals if p.id.startswith(wanted)]
    if not found:
        raise MaboloError(f"no open proposal starts with {wanted!r}")
    if len(found) > 1:
        names = ", ".join(sorted(p.id for p in found))
        raise MaboloError(f"{wanted!r} could mean any of: {names}. Type more of it.")
    return found[0]
