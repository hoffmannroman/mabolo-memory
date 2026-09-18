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
from .schema import PROJECT_PREFIX, as_utc

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


def one_line(text: str) -> str:
    """Text reduced to a single printable line.

    The payload has a structure a reader is meant to trust: a heading per area,
    one line per entry, and a last line saying how many were left out. All three
    come from an entry, and an entry is a file a person or an agent wrote. A
    description holding a newline can therefore forge a heading and the closing
    line, in a payload built to be injected into a prompt automatically. The
    validator calls a multi line description an error, but `mabolo context`
    renders whatever is on disk, so the structure is defended here rather than
    upstream. Control characters go the same way: they cannot be seen, and what
    cannot be seen cannot be checked.
    """
    collapsed = " ".join(text.split())
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in collapsed)


def heading(area: str, count: int) -> str:
    """The line that introduces one area. The one place that decides its shape."""
    return f"## {one_line(area)} ({_plural(count, 'entry', 'entries')})"


def footer(omitted: int) -> str:
    """The last line, which is written even when nothing was left out.

    Its absence would have to be read as "nothing omitted", and a list that
    quietly stops is the one failure this whole design is against.
    """
    if not omitted:
        return "Every entry is listed above."
    return (
        f"{_plural(omitted, 'entry', 'entries')} not shown. "
        "Search the memory by name or topic to reach them."
    )


@dataclass(frozen=True)
class Line:
    """One entry as the session index shows it."""

    name: str
    area: str
    summary: str
    reason: str
    at: dt.datetime | None = None

    def render(self) -> str:
        return f"- {one_line(self.name)}: {self.summary}"


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
            out.append(heading(area, total))
            # Sorted by name inside an area: this is the part a person reads,
            # and cut order would look arbitrary to them.
            out.extend(line.render() for line in sorted(shown.get(area, ()), key=lambda l: l.name))
            out.append("")
        out.append(footer(self.omitted))
        return "\n".join(out)

    def cost(self) -> int:
        return estimate_tokens(self.text())


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one}" if count == 1 else f"{count} {many}"


def _summary(document: Document) -> str:
    """The one line that has to make a model go and look for the rest."""
    return one_line(document.description or document.title or document.name)


def _reason(
    document: Document,
    project: str | None,
    cutoff: dt.datetime | None,
    as_of: dt.datetime | None,
) -> str | None:
    """Why this entry would be in the index, or None if it would not be.

    Checked strongest first, so an entry that qualifies twice is held by the
    stronger claim. A pinned entry of the active project must not be cut with
    the project.

    Recent has two bounds, not one. With only a lower bound a timestamp in the
    future stays "recent" forever, so one typo or one machine with a wrong clock
    keeps an entry at the front of every session and pushes out the entries that
    really did move this week.
    """
    if document.pin:
        return PINNED
    if project and document.area == f"{PROJECT_PREFIX}{project}":
        return PROJECT
    if cutoff is not None and as_of is not None and document.at is not None:
        touched = as_utc(document.at)
        if cutoff < touched <= as_of:
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

    moment = as_utc(as_of) if as_of is not None else None
    cutoff = moment - dt.timedelta(days=FRESH_DAYS) if moment is not None else None
    chosen: list[Line] = []
    for document in documents:
        reason = _reason(document, project, cutoff, moment)
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
#: The other end, so that "newest first" can be expressed as an age.
_LATEST = dt.datetime.max.replace(tzinfo=dt.timezone.utc)


def _cut_key(line: Line) -> tuple[int, dt.timedelta, str]:
    """Cut order: by reason, then newest first, then by name.

    The name breaks the tie so that two entries written in the same second come
    out in the same order on every machine. Without it the order would follow
    whatever order the files were read in, and the baseline would fail on a
    different filesystem rather than on a real change.

    Age is a `timedelta`, not a float. `timestamp()` loses microseconds at
    distant dates, where two moments an entry apart compare equal and the name
    silently decides instead of the time. Every moment goes through `as_utc`
    first, which is where "no offset means UTC" is decided.
    """
    moment = as_utc(line.at) if line.at else _OLDEST
    return (REASONS.index(line.reason), _LATEST - moment, line.name)


def _fit(
    chosen: Sequence[Line], counts: dict[str, int], total: int, target_tokens: int
) -> list[Line]:
    """As many lines as the budget holds, taken in cut order.

    Counted in characters and converted once, rather than by rendering the whole
    payload after every line: the two agree because `estimate_tokens` is a
    division, and this one stays linear in the number of entries.

    **It counts what the renderer produces, never a rebuilt copy of it.** An
    earlier version wrote the heading out a second time here and already
    disagreed with `text()` about one entry versus one entries. It was
    conservative by two characters, so nothing broke, and the next person to
    change the heading would have had no warning at all.

    The headings and the last line are reserved before any entry is fitted, the
    last line at its longest possible wording: reserving the real one is
    circular, because how it reads depends on how many lines fit. A budget that
    does not even hold those yields a payload with no entries in it, which is a
    true statement about a budget that small.
    """
    fixed = sum(len(heading(area, count)) + 1 for area, count in sorted(counts.items()))
    fixed += len(counts)  # the blank line after each area
    longest_footer = max(len(footer(0)), len(footer(total)))
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
