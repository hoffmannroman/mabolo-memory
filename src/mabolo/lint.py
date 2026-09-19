"""The content pass: whether the notes have gone stale or tangled.

`doctor` and `lint` are a pair on purpose. `doctor` asks whether the machinery
is sound: the configuration, the repository, the remote, the inbox, the files
that will not parse. `lint` asks a different question about the same vault,
whether the content has gone stale or tangled, and it asks nothing else. A
report that mixes the two is the one nobody finishes: somebody opens a list to
tidy their notes, meets a broken remote on line four, and closes it again. So
every finding in here is about what an entry says, and anything about the
machine it sits on belongs to the other command.

Five findings, and the list is closed. Each one is mechanical, needs no model,
and can be replayed: dead links, an entry named without a link, two entries in
one area that look like one entry, an entry past its own expiry date, and an
entry nothing has touched in a long time. Usage counts were asked for and left
out, because nothing in Mabolo records usage yet and a count of nothing is a
lie with a number on it.

**Nothing here writes.** No proposal is filed, no entry is edited, no file is
touched. Filing would fill the inbox with findings a person then has to answer
one by one, and the inbox is for sentences a person actually said. `inspect`
returns findings, `render` prints them, and whether that is worth an exit code
is the caller's decision: a non empty list is the whole signal.

Judging and rendering are kept apart, the way they are everywhere else here.
The moment a run is measured against arrives as a parameter and is never read
off the clock inside a function, so that the same vault at the same commit
produces the same report tomorrow.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from . import drift, query, validate
from . import git
from .schema import PROJECT_PREFIX, DEFAULT_LANGUAGE, Entry, as_utc, parse_time

#: The kinds, in the order a report prints them. Dead links first because they
#: are the cheapest to fix and the least arguable; the two that ask a person to
#: think come last.
DEAD_LINK = "dead link"
MISSING_LINK = "missing link"
DUPLICATE = "duplicate candidate"
DRIFTED = "drifted description"
EXPIRED = "expired"
UNTOUCHED = "untouched"
UNDATED = "undated"

KINDS = (DEAD_LINK, MISSING_LINK, DUPLICATE, DRIFTED, EXPIRED, UNTOUCHED, UNDATED)

HEADINGS = {
    DEAD_LINK: "## Links that point at nothing",
    MISSING_LINK: "## Entries named without a link",
    DUPLICATE: "## Pairs that may be one entry",
    DRIFTED: "## Descriptions that have come loose from their entry",
    EXPIRED: "## Past the date they set themselves",
    UNTOUCHED: "## Nobody has touched these",
    UNDATED: "## These say nothing about when they were written",
}

#: What a count calls a group of these. A dict rather than an `s` on the end,
#: because "2 expireds" is how a report starts to read like a machine wrote it.
PLURALS = {
    DEAD_LINK: "dead links",
    MISSING_LINK: "missing links",
    DUPLICATE: "duplicate candidates",
    DRIFTED: "drifted descriptions",
    EXPIRED: "expired",
    UNTOUCHED: "untouched",
    UNDATED: "undated",
}

#: Two entries in one area sharing this much of their vocabulary are worth a
#: look. Settled at the desk rather than measured, and deliberately low: this
#: finding asks a question, it does not make a claim.
SIMILARITY = 0.2

#: How little of a description may still be found in the entry below it before
#: the two have stopped being about the same thing. A tenth, which in practice
#: means "shares nothing, or one word out of many".
#:
#: Measured rather than chosen, against a vault of 188 entries: at this floor
#: every finding was real, three of them descriptions that had outlived a
#: project being discarded or renamed. The first false alarm sat at a sixth, a
#: description that summarises its entry accurately in different words, which
#: is why the floor is not simply `SIMILARITY`.
#:
#: **This is the one kind of staleness no search for a name can find.** A
#: description reading "only a working title" stops being true the moment the
#: title does, and it never mentions anything a rename would look for.
DRIFTED_WORDS = 0.1

#: Longer than this without a touch and an entry is worth a question. The
#: number is a decision, not a measurement, and it is a parameter of the check
#: rather than a constant buried in it so that a vault can disagree with it.
UNTOUCHED_DAYS = 120

#: A name shorter than this is never hunted for in prose. The search already
#: refuses to look for a word this short, and an alias like `ci` would
#: otherwise be found in half of every entry.
MIN_NAME = query.MIN_TOKEN

#: What a Markdown link is, borrowed rather than written again. A second
#: regular expression for the same thing is how the validator and this report
#: come to disagree about whether a line contains a link, and then one of them
#: is wrong and nobody can tell which.
_LINK = validate._MD_LINK

#: Spans that are not prose, blanked before a name is hunted for. An inline
#: link (or an image), a wikilink, a reference or footnote definition, a bare
#: URL, and a code span. A footnote definition holds a quotation of something
#: somebody said, and you do not reach into a person's sentence to add a link.
#:
#: A code span is the one of these that cannot hold a link at all: Markdown
#: renders `[a](b)` inside backticks as those characters and not as a link, so
#: a finding about a name in there asks for something nobody can write. It is
#: also where a name is least likely to be a reference: `ssh <host>` is a
#: command, `~/p/<name>/docs` is a path, and `<name>-deploy-temp` is a key. A
#: report that asks for a link nobody can write is a report people stop
#: reading, which costs more than the findings were worth.
_CODE_SPAN = re.compile(r"`+[^`\n]*`+")
_LINK_SPAN = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
_WIKILINK_SPAN = re.compile(r"\[\[[^\]]*\]\]")
_DEFINITION = re.compile(r"^\[[^\]]*\]:.*$", re.MULTILINE)
_BARE_URL = re.compile(r"\S+://\S+")

#: The characters that can sit inside an entry name, used as the boundary a
#: name has to stand alone within. `deploy-from-main` is not named by
#: `redeploy-from-main-again`, and a check that thought so would be wrong in
#: the one direction this report cannot survive.
_EDGE = r"[0-9A-Za-z_.\-]"


@dataclass(frozen=True)
class Finding:
    """One thing worth a person's attention, and everything needed to print it.

    The message is a whole sentence about this one entry, so that a caller can
    print a finding without knowing which kind it is. Everything a caller might
    want to sort, group or link by is a field of its own rather than something
    to be read back out of the sentence.
    """

    kind: str
    #: The entry the finding is about. For a pair it is the first of the two by
    #: name, which is what keeps a pair from being reported twice.
    name: str
    message: str
    #: The second entry: the one that should have been linked to, or the other
    #: half of a duplicate pair. Empty for the kinds that concern one entry.
    other: str = ""
    #: A link exactly as the entry spells it, for a dead link.
    target: str = ""
    #: The overlap: how much two entries share for a duplicate candidate, and
    #: how much of a description is still in its own entry for a drifted one.
    #: None everywhere else, because zero is a score and "there is no score"
    #: is not.
    score: float | None = None
    #: The file, so that a caller can print a path it can open.
    path: Path | None = None

    def render(self) -> str:
        return f"{self.name}: {self.message}"


def dead_links(entries: Sequence[Entry], root: Path) -> list[Finding]:
    """Relative links from an entry to a path that is not in the vault.

    A link out of the vault is not reported here. Pointing at a file in your
    own project is a reasonable thing to write down, Mabolo never follows it,
    and the validator already says so once; saying it twice in two reports
    teaches people that both reports repeat themselves.

    Images are not links: `_LINK` refuses them, and a missing picture is not a
    tangle in the knowledge. Code fences are stripped first, because the whole
    point of an example link is that it goes nowhere.
    """
    out: list[Finding] = []
    resolved_root = _resolve(root)
    if resolved_root is None:
        return out
    for entry in entries:
        if entry.path is None:
            continue
        for written, relative in links_of(entry.body):
            base = root if relative.startswith("/") else entry.path.parent
            destination = base / relative.lstrip("/")
            resolved = _resolve(destination)
            if resolved is None or not resolved.is_relative_to(resolved_root):
                continue
            try:
                missing = not destination.exists()
            except OSError:
                # Unreadable is not absent, and the folder nobody can read is
                # already a finding of the validator's own.
                continue
            if missing:
                out.append(
                    Finding(
                        DEAD_LINK,
                        entry.name,
                        f"the link {written!r} points at nothing",
                        target=written,
                        path=entry.path,
                    )
                )
    return sorted(out, key=lambda f: (f.name, f.target))


def missing_links(entries: Sequence[Entry], root: Path) -> list[Finding]:
    """An entry that names another entry in its prose and does not link to it.

    This is the one finding here that can be wrong, so it is the one that says
    out loud what it does.

    **What counts as naming.** The other entry's file name, or one of its
    aliases, spelled out in full and standing alone: the character before and
    after it may not be one a name is made of. Case is ignored, because a
    sentence starting with a name capitalises it. Nothing else counts.

    **What deliberately does not count**, each one a way this report would
    start crying wolf:

    * A fenced code block. An example is not a sentence about an entry.
    * The inside of a link that is already there, in its text or in its target.
      A link to somewhere else whose words happen to include a name is a
      sentence a person already wrote deliberately.
    * Anything at all, when the entry already links to that other entry
      somewhere in its body. One link is a link; asking for a second one at
      every later mention is how a report becomes noise.
    * A reference or footnote definition, which holds a quotation.
    * A bare URL, whose path happens to end in a word.
    * A name shorter than `MIN_NAME`, or one that is a stop word in the vault's
      language. Both would be found everywhere and mean nothing.
    * The entry's own name in its own body.

    **What it does not do, and could.** It does not look for the words of a
    name spread through a sentence: `deploy-from-main` is not named by "we
    deploy from main". The search does read a name that way, and doing it here
    as well would find real mentions, and also every sentence that happens to
    use three ordinary words in a row. A person reads this list every month,
    and one false line in it is how the whole list stops being read.

    * The entry that gives this entry's own area its name. An entry inside
      `project/atlas` saying "Atlas" is naming the place it lives in, and every
      finding this check produced against the example vault was that one
      relationship.

    **Where it will still cry wolf.** An entry whose name is one ordinary word,
    say `schedule`, is named by every sentence that uses that word, and nothing
    here can tell the two apart. A single word name is not excluded for being
    short: machines and projects are single words, and dropping them would
    silence the check exactly where it is most likely to be right. What keeps a
    long list readable is that the report groups by the name that was not
    linked, so twenty entries naming one project are one line and twenty names
    beneath it.
    """
    hunted = [(e, _patterns(e)) for e in entries if e.path is not None]
    out: list[Finding] = []
    for entry in entries:
        if entry.path is None:
            continue
        prose = _prose(entry.body)
        if not prose.strip():
            continue
        linked = _linked(entry, root)
        for other, patterns in hunted:
            if other.path == entry.path:
                continue
            if _is_home_of(other, entry):
                # An entry inside `project/atlas` that says "Atlas" is naming
                # the place it already lives in, and asking it to link to its
                # own overview is asking for a link from a chapter to the book
                # it is printed in. Measured against the example vault, every
                # single finding this check produced was that one relationship.
                continue
            resolved = _resolve(other.path)
            if resolved is not None and resolved in linked:
                continue
            named = next((name for name, rule in patterns if rule.search(prose)), None)
            if named is None:
                continue
            out.append(
                Finding(
                    MISSING_LINK,
                    entry.name,
                    f"it names {named!r} and does not link to it",
                    other=other.name,
                    path=entry.path,
                )
            )
    return sorted(out, key=lambda f: (f.name, f.other))


def _is_home_of(other: Entry, entry: Entry) -> bool:
    """Whether `other` is the entry that gives `entry`'s area its name."""
    area = entry.mabolo.area
    return area.startswith(PROJECT_PREFIX) and other.name == area[len(PROJECT_PREFIX):]


def duplicates(
    entries: Sequence[Entry], language: str = DEFAULT_LANGUAGE
) -> list[Finding]:
    """Two entries in one area whose words overlap enough to ask about.

    The words are the ones the search already produces: folded, stripped of
    stop words in the vault's language, and cut to stems. Writing a second
    tokeniser here would mean this report and the search disagreed about what a
    word is, and then a pair could look like one entry to the person and like
    two entries to every question they ask.

    The name is left out of the comparison and the title, description and body
    go in. A name is the one thing two entries that should be one entry are
    guaranteed to spell differently.

    Only within an area. Two entries about the same idea in two areas are two
    entries: that is what an area is for, and a report that proposed merging
    across one would be proposing to break the layout.

    Each pair is reported once, under the first of the two names, because a
    person fixing a duplicate fixes it once.
    """
    ordered = sorted((e for e in entries if e.path is not None), key=lambda e: e.name)
    marked = [(e, frozenset(query.stems_of(_words(e), language))) for e in ordered]
    out: list[Finding] = []
    for (left, mine), (right, theirs) in combinations(marked, 2):
        if left.area != right.area:
            continue
        score = _jaccard(mine, theirs)
        if score < SIMILARITY:
            continue
        out.append(
            Finding(
                DUPLICATE,
                left.name,
                f"it shares {score:.2f} of its words with {right.name!r} in the same area",
                other=right.name,
                score=score,
                path=left.path,
            )
        )
    return sorted(out, key=lambda f: (f.name, f.other))


def drifted(entries: Sequence[Entry], language: str = DEFAULT_LANGUAGE,
            floor: float = DRIFTED_WORDS) -> list[Finding]:
    """Every description that no longer shares its words with what it describes.

    A description is the line a session is shown before it reads anything, and
    it is the field that goes stale most quietly: nothing links to it, nothing
    validates it against the prose below it, and a search for an old name will
    never find "only a working title" once the title is settled.

    **A body that several entries share is skipped, and that rule needs no
    threshold.** A stock line an importer wrote into nineteen project overviews
    belongs to none of them, so nothing can have drifted from it; comparing
    anyway made every one of those a finding and buried the four that were
    real. Whether a body is this entry's own is a fact, not a judgement.
    """
    out: list[Finding] = []
    shared = Counter(_flat(entry.body) for entry in entries if entry.path is not None)
    for entry in entries:
        if entry.path is None or not entry.description:
            continue
        if shared[_flat(entry.body)] > 1:
            continue
        said = frozenset(query.stems_of(entry.description, language))
        if not said:
            continue
        holds = frozenset(query.stems_of(entry.body, language))
        overlap = len(said & holds) / len(said)
        if overlap >= floor:
            continue
        out.append(
            Finding(
                DRIFTED,
                entry.name,
                f"its description shares {'nothing' if not (said & holds) else 'almost nothing'} "
                f"with the entry below it, so one of the two has moved on",
                score=overlap,
                path=entry.path,
            )
        )
    return sorted(out, key=lambda f: f.name)


def _flat(text: str) -> str:
    """One spelling of a body, for asking whether two entries hold the same one."""
    return " ".join(text.split())


def expired(entries: Sequence[Entry], moment: dt.datetime) -> list[Finding]:
    """Entries whose `stale_after` has passed, measured against `moment`.

    The comparison is `drift.judge` and not a second one written here. It
    already decides what an expiry date means, in UTC, for a timestamp written
    with any offset, and two answers to that question is one more than a vault
    can survive.

    `judge` is asked without a project and without a repository, so it never
    reaches a verdict about an anchor. That is on purpose: an anchor whose file
    moved is a fact about a repository, which is doctor's half of the pair, and
    only the expiry reason is passed on from here.
    """
    out: list[Finding] = []
    for entry in entries:
        result = drift.judge(entry, moment=moment)
        if result.state != drift.STALE or result.reason != drift.EXPIRED:
            continue
        out.append(
            Finding(
                EXPIRED,
                entry.name,
                f"its stale_after passed on {_day(result.expires_at)}",
                path=entry.path,
            )
        )
    return sorted(out, key=lambda f: f.name)


def untouched(
    entries: Sequence[Entry], moment: dt.datetime, days: int = UNTOUCHED_DAYS
) -> list[Finding]:
    """Entries nobody has touched in longer than `days`, measured against `moment`.

    A touch is what the entry itself says it is: the later of `generated.at`
    and every `verified.at`, which is `Entry.touched_at` and is not the file's
    modification time. An mtime is not part of a commit, so a fresh clone would
    report a whole vault as touched this morning.

    Strictly longer. Exactly at the line is not a finding, because a boundary
    that fires on the day it is reached makes the number in the message off by
    one from the number in the rule.

    An entry that carries no date at all is its own finding rather than a long
    silence: "untouched for 120 days" is a claim about time, and an entry with
    no date supports no such claim. Where the vault is a Git repository the
    last commit touching the file stands in, which is exactly "last touched",
    so a hand written entry in a repository raises no false alarm and only one
    with no date and no history is left saying nothing.

    `moment` is a parameter and the clock is never read here, so a run can be
    replayed and two people can compare two reports.
    """
    edge = as_utc(moment)
    limit = dt.timedelta(days=days)
    out: list[Finding] = []
    for entry in entries:
        touched = entry.touched_at()
        borrowed = False
        if touched is None and entry.path is not None:
            touched = _committed_at(entry.path)
            borrowed = touched is not None
        if touched is None:
            out.append(
                Finding(
                    UNDATED,
                    entry.name,
                    "it carries no date and has no commit, so nothing can say how old it is",
                    path=entry.path,
                )
            )
            continue
        age = edge - touched
        if borrowed and age > limit:
            out.append(
                Finding(
                    UNTOUCHED,
                    entry.name,
                    f"nothing has touched it for {age.days} days, dated by its last commit",
                    path=entry.path,
                )
            )
            continue
        if age <= limit:
            continue
        out.append(
            Finding(
                UNTOUCHED,
                entry.name,
                f"nothing has touched it for {age.days} days",
                path=entry.path,
            )
        )
    return sorted(out, key=lambda f: f.name)


def _committed_at(path: Path) -> dt.datetime | None:
    """When the file was last committed, or None outside a repository.

    Lint is allowed to ask Git. Only the ranking has to be a pure function of
    the files, because that is what has to be replayable at an old commit; a
    report about how old something is may use the one record that actually
    says so.
    """
    if not git.is_repository_inside(path.parent) and not git.repository_dir(path.parent):
        return None
    result = git.run(
        path.parent, "log", "-1", "--format=%cI", "--", path.name, check=False
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    found = parse_time(result.stdout.strip())
    return as_utc(found) if found else None


def inspect(
    entries: Iterable[Entry],
    *,
    root: Path,
    moment: dt.datetime,
    language: str = DEFAULT_LANGUAGE,
    days: int = UNTOUCHED_DAYS,
) -> list[Finding]:
    """Every check over one vault, in the order a report prints them.

    A list, and an empty one means there is nothing to say. Nothing is printed
    and no exit code is decided here: a caller that wants to fail a run fails
    it because this came back non empty.
    """
    seen = list(entries)
    found = (
        dead_links(seen, root)
        + missing_links(seen, root)
        + duplicates(seen, language)
        + drifted(seen, language)
        + expired(seen, moment)
        + untouched(seen, moment, days)
    )
    return sorted(found, key=lambda f: (KINDS.index(f.kind), f.name, f.other, f.target))


def render(findings: Sequence[Finding]) -> str:
    """The findings grouped by kind, with a count that accounts for all of them.

    The closing line names every kind that appeared and its number. A list that
    stops without saying how long it was reads like a complete one, which is
    the failure this whole project is built against.
    """
    if not findings:
        return "0 findings"
    lines: list[str] = []
    counted: list[str] = []
    for kind in KINDS:
        group = [f for f in findings if f.kind == kind]
        if not group:
            continue
        counted.append(f"{len(group)} {PLURALS[kind] if len(group) != 1 else kind}")
        lines += [HEADINGS[kind], ""]
        lines += _grouped(group) if kind == MISSING_LINK else [f.render() for f in group]
        lines.append("")
    word = "finding" if len(findings) == 1 else "findings"
    lines.append(f"{len(findings)} {word}: {', '.join(counted)}")
    return "\n".join(lines)


def _grouped(findings: Sequence[Finding]) -> list[str]:
    """Missing links, one line per name that was not linked.

    Twenty entries naming one project is one relationship and one thing to fix,
    and printing it twenty times is how a monthly report stops being read. The
    entries are still all there, indented under the name.
    """
    out: list[str] = []
    for target in sorted({f.other for f in findings}):
        named = sorted(f.name for f in findings if f.other == target)
        word = "entry" if len(named) == 1 else "entries"
        out.append(f"{target}: named without a link in {len(named)} {word}")
        out += [f"  {one}" for one in named]
    return out


def links_of(body: str) -> Iterator[tuple[str, str]]:
    """Every relative Markdown link of a body, as written and as a path.

    External links, fragments and mail addresses are not paths in a vault and
    are passed over rather than reported: a report that flagged every `https`
    link would be a report about the internet.
    """
    for match in _LINK.finditer(validate.strip_code(body)):
        written = match.group(1)
        if "://" in written or written.startswith("#") or written.startswith("mailto:"):
            continue
        relative = written.split("#")[0]
        if relative:
            yield written, relative


def _linked(entry: Entry, root: Path) -> set[Path]:
    """The files this entry already links to, resolved, as far as they resolve."""
    out: set[Path] = set()
    if entry.path is None:
        return out
    for _, relative in links_of(entry.body):
        base = root if relative.startswith("/") else entry.path.parent
        resolved = _resolve(base / relative.lstrip("/"))
        if resolved is not None:
            out.add(resolved)
    return out


def _prose(body: str) -> str:
    """A body with everything that is not a sentence about an entry blanked out.

    Blanked rather than deleted, so that two words either side of a removed
    link do not become one word that was never written.

    Code spans go first, before the link patterns, because a span is the outer
    thing: backticks around something shaped like a link mean the brackets are
    characters, and blanking the inner shape first would leave the backticks
    behind as prose.
    """
    text = validate.strip_code(body)
    text = _CODE_SPAN.sub(" ", text)
    text = _WIKILINK_SPAN.sub(" ", text)
    text = _LINK_SPAN.sub(" ", text)
    text = _DEFINITION.sub(" ", text)
    return _BARE_URL.sub(" ", text)


def _patterns(entry: Entry) -> list[tuple[str, re.Pattern[str]]]:
    """The names this entry answers to, each as the rule that finds it standing alone."""
    out: list[tuple[str, re.Pattern[str]]] = []
    for name in [entry.name, *entry.mabolo.aliases]:
        if not _worth_hunting(name):
            continue
        rule = re.compile(
            rf"(?<!{_EDGE}){re.escape(name)}(?!{_EDGE})", re.IGNORECASE
        )
        out.append((name, rule))
    return out


def _worth_hunting(name: str) -> bool:
    """Whether a name is distinctive enough to look for in somebody else's prose."""
    word = name.strip()
    if len(word) < MIN_NAME:
        return False
    return not query.is_stopword(query.fold(word))


def _words(entry: Entry) -> str:
    """The text a duplicate is judged on: what the entry says, not what it is called."""
    return "\n".join(part for part in (entry.title, entry.description, entry.body) if part)


def _jaccard(mine: frozenset[str], theirs: frozenset[str]) -> float:
    """How much two sets of stems overlap, and zero when there is nothing to compare.

    Two entries with no searchable words at all are not similar, they are both
    empty, and calling that a perfect overlap would put every stub in the vault
    into a pair with every other stub.
    """
    union = mine | theirs
    if not union:
        return 0.0
    return len(mine & theirs) / len(union)


def _resolve(path: Path | None) -> Path | None:
    """One resolved path, or None when the filesystem will not say."""
    if path is None:
        return None
    try:
        return Path(path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _day(when: dt.datetime | None) -> str:
    """The day of a moment, in UTC, which is what a sentence about it needs."""
    if when is None:
        return "an unreadable date"
    return when.astimezone(dt.timezone.utc).date().isoformat()
