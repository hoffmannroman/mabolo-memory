"""The session index: the handful of lines a session starts with.

A session cannot be handed the whole vault, and it must not be handed a
truncated version of it either. So the first tier is a map rather than a copy:
every area with how many entries it holds, and inside it the few entries this
session is most likely to need. The last line always says how many were left
out, because a list that stops without saying so reads like a complete one.

**The selection is a rule, not a model.** An entry is in when it is pinned, when
it belongs to the active project, or when it was touched in the last seven days.
That order is also the order it survives in: when the budget is tight the least
safe line goes first, and inside one class the oldest goes before the newest.
Nothing here counts how often an entry was read or what was useful yesterday,
because a session index that learns cannot be rebuilt at an old commit, and
rebuilding it at an old commit is what makes a drop in recall traceable.

**Two orders, on purpose.** `lines` is the order the budget cuts in, safest
first, and that is the position the eval measures and the baseline stores: it is
the number that says how close an entry is to falling out. What a reader gets is
grouped by area instead, because a map is read by area and not by how endangered
its entries are.

**The clock is an argument, never a call.** `as_of` is passed in. A function
that reads the clock itself produces a different answer tomorrow from the same
commit, and then no measurement of it holds for longer than a week.

One limit worth naming rather than discovering: every area gets a heading, so a
vault with hundreds of projects would spend its budget on headings. The shape
this is built for is a handful of fixed areas plus a folder per project.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Sequence

from .index import CHARS_PER_TOKEN, Document, estimate_tokens
from .schema import PROJECT_PREFIX

#: What the session index is meant to cost. A target rather than a limit: the
#: hard ceiling belongs to whichever client receives it, and those disagree.
#: At the time of writing Claude Code truncates a hook payload above 10,000
#: characters and Codex injects about 2,500 tokens by default, so budgeting to
#: the smaller of the two is the only number that is right in both.
DEFAULT_TARGET_TOKENS = 800

#: How long an entry counts as recent. Seven days is a working week: whatever
#: was decided since the last one is what a session is most likely to be about.
FRESH_DAYS = 7

#: Why a line is in the index, strongest first. The order is the rule: it
#: decides both what survives a tight budget and what the baseline compares.
PINNED = "pinned"
PROJECT = "project"
FRESH = "fresh"
REASONS = (PINNED, PROJECT, FRESH)


def _utc(when: dt.datetime | dt.date) -> dt.datetime:
    """Any date or datetime as an aware UTC datetime.

    A naive value is read as UTC rather than as local time. The alternative
    sorts the same commit differently in two time zones, and this value decides
    what gets cut.
    """
    if not isinstance(when, dt.datetime):
        when = dt.datetime.combine(when, dt.time.min)
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class Line:
    """One entry as the session index shows it."""

    name: str
    area: str
    summary: str
    reason: str
    at: dt.datetime | None = None

    def render(self) -> str:
        return f"- {self.name}: {self.summary}"


@dataclass(frozen=True)
class SessionIndex:
    """The L1 payload: what was chosen, what it costs, and what was left out."""

    #: Chosen entries in cut order, safest first. Not the order they are shown.
    lines: tuple[Line, ...] = ()
    #: Every area in the vault and how many entries it holds, including the
    #: ones no line was written for. That count is the whole point of a map.
    counts: tuple[tuple[str, int], ...] = ()
    #: How many entries of the vault got no line.
    omitted: int = 0
    #: How many entries the budget cut, of those the rule had chosen. Separate
    #: from `omitted`, because "the rule did not pick it" and "there was no room
    #: for it" are different problems with different fixes.
    cut: int = 0
    project: str | None = None
    target_tokens: int = DEFAULT_TARGET_TOKENS

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(line.name for line in self.lines)

    @property
    def total(self) -> int:
        """Every entry in the vault, shown or not."""
        return len(self.lines) + self.omitted

    def position(self, name: str) -> int | None:
        """Where a name sits in cut order, 1 being the safest, or None if it is
        not in the index at all."""
        for position, line in enumerate(self.lines, start=1):
            if line.name == name:
                return position
        return None

    def text(self) -> str:
        """The payload itself, grouped by area, with the omitted count last."""
        shown: dict[str, list[Line]] = {}
        for line in self.lines:
            shown.setdefault(line.area, []).append(line)
        out: list[str] = []
        for area, total in self.counts:
            out.append(f"## {area} ({_plural(total, 'entry', 'entries')})")
            # Sorted by name inside an area: this is the part a person reads,
            # and cut order would look arbitrary to them.
            out.extend(line.render() for line in sorted(shown.get(area, ()), key=lambda l: l.name))
            out.append("")
        out.append(self.footer())
        return "\n".join(out)

    def footer(self) -> str:
        """The last line, which is written even when nothing was left out.

        A missing line would have to be read as "nothing omitted", and the one
        failure this whole design is against is a list that quietly stops.
        """
        if not self.omitted:
            return "Every entry is listed above."
        return (
            f"{_plural(self.omitted, 'entry', 'entries')} not shown. "
            "Search the memory by name or topic to reach them."
        )

    def cost(self) -> int:
        return estimate_tokens(self.text())


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one}" if count == 1 else f"{count} {many}"


def _summary(document: Document) -> str:
    """The one line that has to make a model go and look for the rest."""
    return document.description or document.title or document.name


def _reason(document: Document, project: str | None, cutoff: dt.datetime | None) -> str | None:
    """Why this entry would be in the index, or None if it would not be.

    Checked strongest first, so an entry that qualifies twice is held by the
    stronger claim. A pinned entry of the active project must not be cut with
    the project.
    """
    if document.pin:
        return PINNED
    if project and document.area == f"{PROJECT_PREFIX}{project}":
        return PROJECT
    if cutoff is not None and document.at is not None and _utc(document.at) > cutoff:
        return FRESH
    return None


def build(
    documents: Iterable[Document],
    *,
    project: str | None = None,
    as_of: dt.datetime | dt.date | None = None,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
) -> SessionIndex:
    """The session index for a vault, as a pure function of its arguments.

    `as_of` may be left out, and then nothing counts as recent: no clock is read
    here. A caller that wants the freshness rule says which moment it means, and
    that is the moment the answer can be replayed at.
    """
    documents = list(documents)
    counts: dict[str, int] = {}
    for document in documents:
        counts[document.area] = counts.get(document.area, 0) + 1

    cutoff = _utc(as_of) - dt.timedelta(days=FRESH_DAYS) if as_of is not None else None
    chosen: list[Line] = []
    for document in documents:
        reason = _reason(document, project, cutoff)
        if reason is None:
            continue
        chosen.append(
            Line(
                name=document.name,
                area=document.area,
                summary=_summary(document),
                reason=reason,
                at=document.at,
            )
        )
    chosen.sort(key=_cut_key)

    kept = _fit(chosen, counts, len(documents), target_tokens)
    return SessionIndex(
        lines=tuple(kept),
        counts=tuple(sorted(counts.items())),
        omitted=len(documents) - len(kept),
        cut=len(chosen) - len(kept),
        project=project,
        target_tokens=target_tokens,
    )


#: The oldest possible moment, for an entry whose file states no time at all.
#: Such an entry sorts last inside its class: it is the one the budget should
#: drop first, because nothing about it says it is current.
_OLDEST = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def _cut_key(line: Line) -> tuple[int, float, str]:
    """Cut order: by reason, then newest first, then by name.

    The name breaks the tie so that two entries written in the same second come
    out in the same order on every machine. Without it the order would follow
    whatever order the files were read in, and the baseline would fail on a
    different filesystem rather than on a real change.

    Every moment goes through `_utc` first. `timestamp()` on a naive datetime
    reads the machine's own time zone, so an entry whose file states no offset
    would sort one way in Athens and another in Berlin, and this is the order
    that decides what gets cut.
    """
    moment = _utc(line.at) if line.at else _OLDEST
    return (REASONS.index(line.reason), -moment.timestamp(), line.name)


def _fit(
    chosen: Sequence[Line], counts: dict[str, int], total: int, target_tokens: int
) -> list[Line]:
    """As many lines as the budget holds, taken in cut order.

    Counted in characters and converted once, rather than by rendering the whole
    payload after every line: the two agree because `estimate_tokens` is a
    division, and this one stays linear in the number of entries.

    The headings and the last line are reserved for before any entry is fitted,
    and the last line is reserved at its longest possible length. A budget that
    does not even hold those yields an index with no entries in it, which is a
    true statement about a budget that small.
    """
    fixed = sum(len(f"## {area} ({count} entries)") + 1 for area, count in sorted(counts.items()))
    fixed += len(counts)  # the blank line after each area
    # The footer, at the longest it could get for this vault. Reserving the
    # actual one is circular: its wording depends on how many lines fit.
    longest_footer = max(
        len(SessionIndex(omitted=0).footer()),
        len(SessionIndex(omitted=total).footer()),
    )
    budget = target_tokens * CHARS_PER_TOKEN - fixed - longest_footer

    kept: list[Line] = []
    used = 0
    for line in chosen:
        cost = len(line.render()) + 1
        if used + cost > budget:
            break
        used += cost
        kept.append(line)
    return kept
