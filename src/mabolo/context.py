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

**The map is not the whole payload.** Above it sit the standing rules, and
between the two the journal lines of the active project: the map says what
exists, a rule says what always holds, and a journal line says what was decided
last week. Each has its own budget, and only the map's is the target this
module is named after.

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

from .index import CHARS_PER_TOKEN, Document, entry_line, estimate_tokens, one_line
from .journal import Note
from .schema import PROJECT_PREFIX, as_utc

#: What the session index is meant to cost. A target rather than a limit: the
#: hard ceiling belongs to whichever client receives it, and those disagree.
#: At the time of writing Claude Code truncates a hook payload above 10,000
#: characters and Codex injects about 2,500 tokens by default, so budgeting to
#: the smaller of the two is the only number that is right in both.
DEFAULT_TARGET_TOKENS = 800

#: What the core may cost. A pinned entry is a standing rule: it has to be there
#: before anybody knows they need it, because nobody looks up a rule they have
#: forgotten. So the core is never cut, and this number is the point at which
#: the payload says so out loud instead of quietly dropping the oldest rule.
#: Measured: a rule written as one sentence costs about 20 tokens, so fifteen
#: fit. That is the real limit on how many standing rules a session can carry,
#: and it is better said than discovered.
DEFAULT_CORE_TOKENS = 300

#: What the journal block may cost. Its own budget rather than a share of the
#: map's, because unlike the core it *can* be cut: the oldest line goes first
#: and the payload says how many went. So it needs no right of way, only a
#: ceiling. Deliberately not taken out of `DEFAULT_TARGET_TOKENS`: raising the
#: map's budget to make room was the one answer ruled out when the core was
#: built, and taking the room from the map would be the same thing by stealth.
DEFAULT_PROJECT_TOKENS = 500

#: How long an entry counts as recent. Seven days is a working week: whatever
#: was decided since the last one is what a session is most likely to be about.
FRESH_DAYS = 7

#: Why a line is in the index, strongest first. The order is the rule: it
#: decides both what survives a tight budget and what the baseline compares.
PINNED = "pinned"
PROJECT = "project"
FRESH = "fresh"
REASONS = (PINNED, PROJECT, FRESH)


def project_for(folder: str, areas: Iterable[str]) -> str | None:
    """The project a folder name points at, or None when it points at nothing.

    Exact, never a guess. A folder is a project when the vault already holds
    `project/<that name>`, and otherwise this session is simply not about a
    project. Matching loosely would be worse than matching nothing: a session in
    the wrong folder would be handed somebody else's decisions and never say so.

    Case matters, because a project name may carry capitals and a folder on
    Linux is case sensitive. Two folders cannot point at one project and one
    folder cannot point at two, so there is no tie to break.
    """
    return folder if f"{PROJECT_PREFIX}{folder}" in set(areas) else None


def header(project: str | None) -> str:
    """The first line, which names the project this payload was built for.

    Without it the reader cannot tell why an entry is in the list. A line about
    a release checklist looks the same whether it arrived because the session is
    in that project or because somebody pinned it, and those are two different
    reasons to trust it.
    """
    return f"Active project: {one_line(project)}" if project else "No active project"


#: The heading the standing rules sit under. They are not a part of the map:
#: the map says what exists, these say what to do.
CORE_HEADING = "## Always"


def over_core(count: int, cost: int, target: int) -> str:
    """The sentence a vault gets when it pins more than the core can hold.

    A statement, not a cut. Everywhere else this tool refuses to trim a list
    quietly, and the one list where a silent trim would do the most damage is
    this one: a rule that vanishes is not missed, it is simply not followed.
    """
    return (
        f"The core is over its budget: {_plural(count, 'rule', 'rules')}, about {cost} tokens, "
        f"target {target}. Nothing was dropped. Unpin what is no longer a rule."
    )


#: The heading the journal lines sit under. They are not a part of the map
#: either: the map says what exists, these say what happened.
LATELY_HEADING = "## Lately"


def lately_footer(dropped: int) -> str:
    """What the journal block says when its budget cut the oldest lines.

    Written only when something went, unlike the map's footer. The map is a
    claim about the whole vault and has to say "complete" out loud; this block
    is openly the newest few of an ever growing file, and a line under every
    session saying so would cost more than it tells.
    """
    return (
        f"{_plural(dropped, 'older line', 'older lines')} not shown. "
        "Read the journal for the rest."
    )


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
        return entry_line(self.name, self.summary, self.summary)


@dataclass(frozen=True)
class SessionIndex:
    """The L1 payload: what was chosen, what it costs, and what was left out."""

    #: The standing rules, always present, never cut, in name order.
    core: tuple[Line, ...] = ()
    #: The journal lines of the active project, newest first, already fitted to
    #: `project_tokens`. Not entries: they carry no name, they are never
    #: searched, and they are counted apart from `shown` for that reason.
    notes: tuple[Note, ...] = ()
    #: How many journal lines of this project the block's budget cut.
    notes_cut: int = 0
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
    core_tokens: int = DEFAULT_CORE_TOKENS
    project_tokens: int = DEFAULT_PROJECT_TOKENS

    @property
    def shown(self) -> tuple[Line, ...]:
        """Every line the payload holds, core first.

        Whoever asks what is in the payload asks this. Counting `lines` alone
        answers a different question, and two of them were already being asked
        by mistake: an explanation called the index empty while it held rules,
        and a failing case reported a size that left them out.
        """
        return (*self.core, *self.lines)

    @property
    def names(self) -> tuple[str, ...]:
        """Every name in the payload, core first. What a case asks about."""
        return tuple(line.name for line in self.shown)

    @property
    def total(self) -> int:
        """Every entry in the vault, shown or not."""
        return len(self.shown) + self.omitted

    def core_cost(self) -> int:
        """What the standing rules cost on their own."""
        return estimate_tokens("\n".join([CORE_HEADING, *(l.render() for l in self.core)]))

    @property
    def core_is_over_budget(self) -> bool:
        return bool(self.core) and self.core_cost() > self.core_tokens

    def position(self, name: str) -> int | None:
        """Where a name sits, 1 being the safest, or None if it is not there.

        The core comes first and in its own order, because a standing rule is
        never cut: its position says "safe", not "close to the edge".
        """
        for position, line in enumerate(self.shown, start=1):
            if line.name == name:
                return position
        return None

    def text(self) -> str:
        """The payload: the standing rules, then the map, then what was left out."""
        shown: dict[str, list[Line]] = {}
        for line in self.lines:
            shown.setdefault(line.area, []).append(line)
        out: list[str] = [header(self.project), ""]
        if self.core:
            # Above the map and under their own heading. A reader has to be able
            # to tell a rule that always holds from an entry that happens to be
            # relevant today, and the map cannot say that.
            out.append(CORE_HEADING)
            out.extend(line.render() for line in self.core)
            out.append("")
        if self.notes:
            # Between the rules and the map, and before the map's footer, which
            # is a claim about entries and would read as one about these too.
            # A rule holds always, a journal line held last Tuesday, and the
            # map says what exists: that is the order they are useful in.
            out.append(LATELY_HEADING)
            out.extend(note.render() for note in self.notes)
            if self.notes_cut:
                out.append(lately_footer(self.notes_cut))
            out.append("")
        for area, total in self.counts:
            out.append(heading(area, total))
            # Sorted by name inside an area: this is the part a person reads,
            # and cut order would look arbitrary to them.
            out.extend(line.render() for line in sorted(shown.get(area, ()), key=lambda l: l.name))
            out.append("")
        out.append(footer(self.omitted))
        if self.core_is_over_budget:
            out.append(over_core(len(self.core), self.core_cost(), self.core_tokens))
        return "\n".join(out)

    def cost(self) -> int:
        return estimate_tokens(self.text())


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one}" if count == 1 else f"{count} {many}"


def _summary(document: Document) -> str:
    """The one line that has to make a model go and look for the rest."""
    return document.description or document.title or document.name


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


def _fit_notes(notes: Sequence[Note], target_tokens: int) -> tuple[list[Note], int]:
    """As many journal lines as their own budget holds, newest first.

    Same shape as `_fit` and for the same reason: count characters, convert
    once, and reserve the fixed parts before any line is fitted. The footer is
    reserved at its longest possible wording, because how it reads depends on
    how many lines were cut, and reserving the real one is circular.

    Returns what fits and how many were left behind, never a silently short
    list: the count is what the block's footer is written from.
    """
    fixed = len(LATELY_HEADING) + 1 + len(lately_footer(len(notes))) + 1
    budget = target_tokens * CHARS_PER_TOKEN - fixed
    kept: list[Note] = []
    used = 0
    for note in notes:
        cost = len(note.render()) + 1
        if used + cost > budget:
            break
        used += cost
        kept.append(note)
    return kept, len(notes) - len(kept)


def build(
    documents: Iterable[Document],
    *,
    project: str | None = None,
    as_of: dt.datetime | dt.date | None = None,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    core_tokens: int = DEFAULT_CORE_TOKENS,
    notes: Sequence[Note] = (),
    project_tokens: int = DEFAULT_PROJECT_TOKENS,
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

    # The standing rules come out first and are never offered to the budget.
    # Whatever is left of the target is what the map may spend, so pinning a
    # rule costs the map a line, visibly, rather than costing another rule its
    # place without saying so.
    core = tuple(sorted((l for l in chosen if l.reason == PINNED), key=lambda l: l.name))
    rest = [l for l in chosen if l.reason != PINNED]
    core_text = "\n".join([CORE_HEADING, *(l.render() for l in core)]) if core else ""
    left = target_tokens - estimate_tokens(core_text)

    kept = _fit(rest, counts, len(documents), left, project)
    # The journal lines arrive already picked for this project by the caller,
    # which is the only reader that knows where `log.md` lives. What is decided
    # here is how many of them the block's own budget holds.
    shown_notes, notes_cut = _fit_notes(list(notes), project_tokens)
    return SessionIndex(
        core=core,
        notes=tuple(shown_notes),
        notes_cut=notes_cut,
        lines=tuple(kept),
        counts=tuple(sorted(counts.items())),
        omitted=len(documents) - len(core) - len(kept),
        cut=len(rest) - len(kept),
        project=project,
        target_tokens=target_tokens,
        core_tokens=core_tokens,
        project_tokens=project_tokens,
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
    chosen: Sequence[Line],
    counts: dict[str, int],
    total: int,
    target_tokens: int,
    project: str | None,
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
    fixed = len(header(project)) + 2  # the line, its newline, and the blank one
    # The core is already paid for by the caller, which subtracted it from the
    # target. What is left here is the map.
    fixed += sum(len(heading(area, count)) + 1 for area, count in sorted(counts.items()))
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
