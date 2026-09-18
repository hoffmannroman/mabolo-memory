"""The eval: does the memory actually hand over the entry that answers the question.

The tempting bug in a memory is invisible. An entry exists, nothing ever
surfaces it, nobody notices, and the tool looks fine. That is why the question
set comes before the reading tier rather than after it: it is the only thing
that can show that an index replaced preloaded text instead of quietly losing
it.

Four rules hold this together, and each one was a decision:

* **Cases live in the vault**, under `.mabolo/eval/`, versioned beside the
  entries they query. A case and its entries have to move through history
  together, or `bisect-memory` has nothing to replay.
* **What cannot be measured yet is named, not passed.** A case that needs a
  model in the loop is reported as unmeasured and never counted as a success.
  A harness that quietly scores the easy half is the same failure as a memory
  that quietly forgets.
* **The baseline stores ranks, not scores.** A rank is ordinal and a BM25 score
  is a float that moves whenever anything about the corpus moves. A gate built
  on scores fires on noise and gets switched off.
* **A file is read through `EvalStore` or not at all.** The vault refuses to
  read or write through a symlink and never touches anything outside its root.
  An audit found that the eval folder had been quietly exempt from both: a
  linked `.mabolo/eval` was read straight through, and `--save-baseline` wrote
  the file outside the vault.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from . import context, frontmatter, journal
from .errors import MaboloError
from .index import DEFAULT_LIMIT, Hit, Index
from .schema import PROJECT_PREFIX, parse_time

#: The file the gate compares against, beside the cases and versioned with them.
BASELINE_FILE = "baseline.json"
#: Bumped when the shape of that file changes. An older Mabolo refuses a newer
#: baseline rather than comparing against fields it does not understand.
BASELINE_VERSION = 3

#: A case file is small. A larger one is a mistake worth naming rather than
#: parsing, and the entry reader has the same kind of limit.
MAX_CASE_BYTES = 200_000

#: Why the tiers below `index` are not measured yet. Printed verbatim, so that
#: nobody has to guess whether a missing number is a pass, a bug or an unbuilt
#: feature.
DEFERRED = {
    "recall": "quiet recall needs the session hook, which is not built yet",
    "design": "applies_to as a load trigger is not built yet",
}
#: The tiers this harness can decide on its own, without a model in the loop.
#: `index` is a search: a question goes in and entries come back. `hint` is the
#: line above it in the chain, and it is not a search at all: it asks whether an
#: entry is in the payload a session starts with. That is where a memory goes
#: quiet without failing anything, because the curation drops the oldest line
#: first and nobody is told.
MEASURED_TIERS = ("search", "hint")
#: What a case can be about. Everything beyond the measured ones is exactly the
#: keys of DEFERRED, so a new tier cannot be accepted while the sentence
#: explaining why it is deferred is still missing.
TIERS = (*MEASURED_TIERS, *DEFERRED)
#: The tier a case gets when it does not say. A search is what most cases are.
DEFAULT_TIER = "search"
#: `search` was called `index` until the word had four meanings in this project:
#: the SQLite index, the session index, this tier and `index.md`. An old case
#: still reads, and writes nothing back, so a vault can be renamed in its own
#: time.
TIER_ALIASES = {"index": "search"}

#: What each shape of case may carry. A tier is refused unless it appears in
#: both tables, so a new shape cannot be half defined. Listing what is allowed,
#: rather than each shape listing what the others own, is what keeps a fourth
#: shape from having to be mentioned in the three that came before it.
TRIGGER_KEYS = {"search": {"query"}, "hint": {"state"}}
EXPECT_KEYS = {
    "search": {"entries", "rank_within", "must_cite", "silence"},
    "hint": {"in_payload", "not_in_payload", "budget_tokens"},
}
COMMON_KEYS = {"id", "tier", "note", "expect"}

DEFERRED_CITE = "must_cite needs a model in the loop, which the harness does not run"

#: What a deferred tier is measured as, until it is built. They are search
#: shaped: a prompt goes in and entries are expected back.
DEFERRED_SHAPE = "search"

DEFAULT_RANK_WITHIN = DEFAULT_LIMIT


@dataclass(frozen=True)
class Case:
    """One trigger, and what the memory is expected to do with it.

    Two shapes share this class, and which fields mean anything depends on
    `tier`. A search case has a `query` and expects entries back. A `hint` case
    has no query at all: the session start is its trigger, so it carries the
    state that start happened in, and expects names to be in the payload or out
    of it. `parse_case` refuses the fields of the other shape rather than
    ignoring them, so a case can never quietly measure something else.
    """

    id: str
    query: str
    path: Path
    tier: str = DEFAULT_TIER
    # A search case.
    entries: tuple[str, ...] = ()
    rank_within: int = DEFAULT_RANK_WITHIN
    must_cite: bool = False
    silence: bool = False
    # A hint case: the session state it asks about, and what it expects to see.
    project: str | None = None
    #: The moment the session starts at. Required for a hint case and never
    #: taken from the clock: "touched in the last seven days" is part of the
    #: selection rule, so a case without a fixed moment would pass this week and
    #: fail the next without anything having changed.
    as_of: dt.datetime | None = None
    in_payload: tuple[str, ...] = ()
    not_in_payload: tuple[str, ...] = ()
    budget_tokens: int | None = None

    @property
    def measurable(self) -> bool:
        """True when the harness alone can decide this case, with no model."""
        return self.tier in MEASURED_TIERS

    @property
    def trigger(self) -> str:
        """What set this case off, in one line, for a report to print."""
        if self.tier != "hint":
            return self.query
        where = f"project {self.project}" if self.project else "no active project"
        return f"session start, {where}, as of {self.as_of.date().isoformat()}"


@dataclass(frozen=True)
class Result:
    """What one case did on this vault."""

    case: Case
    hits: tuple[Hit, ...] = ()
    passed: bool = False
    measured: bool = True
    #: Why it failed, or why it was not measured. Never empty when `passed` is
    #: false, because a failure a person cannot read is a failure twice.
    reasons: tuple[str, ...] = ()
    #: The worst rank among the entries this case expects, or None as soon as
    #: one of them is missing or came back too low. The worst one, because that
    #: is the rank that will fail first, and a gate should fire before the
    #: failure rather than with it.
    rank: int | None = None
    cost: int = 0
    #: The question as the search parsed it, kept so that an explanation shows
    #: what the search used rather than what a second code path reconstructs.
    query: Any = None
    #: The session index a hint case was judged against, for the same reason:
    #: an explanation shows the payload that was measured, not a second build
    #: of it that could differ.
    payload: context.SessionIndex | None = None


def _as_bool(value: Any, field_name: str, where: Path) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise MaboloError(f"{where}: {field_name} is yes or no, not {value!r}")


def _as_names(value: Any, where: Path, field_name: str = "expect.entries") -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise MaboloError(f"{where}: {field_name} is a list of entry names")
    if any(not v.strip() for v in value):
        # Dropping it quietly would leave a case measuring one name fewer than
        # the file shows, which is the whole failure this harness is against.
        raise MaboloError(f"{where}: {field_name} holds an empty name")
    return tuple(v.strip() for v in value)


def _as_count(value: Any, field_name: str, where: Path) -> int:
    """A whole positive number, and not a bool dressed up as one."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MaboloError(f"{where}: {field_name} is a whole number, at least 1")
    return value


def _tier_of(data: dict[str, Any], path: Path) -> str:
    """The tier a case declares, or the default when it declares none.

    Only an absent key is a default. `tier: false` and `tier: ""` used to become
    `search` through a truthiness test, which is a visibly broken case measuring
    something its file never asked for.
    """
    if "tier" not in data:
        return DEFAULT_TIER
    raw = data["tier"]
    if not isinstance(raw, str) or not raw.strip():
        raise MaboloError(f"{path}: tier is text, one of {', '.join(TIERS)}, not {raw!r}")
    tier = TIER_ALIASES.get(raw.strip(), raw.strip())
    if tier not in TIERS:
        raise MaboloError(f"{path}: tier is one of {', '.join(TIERS)}, not {raw.strip()!r}")
    return tier


def parse_case(data: Any, path: Path) -> Case:
    """One case from one file, or an error saying what is wrong with it.

    Nothing here is lenient. A case is a measurement instrument, and an
    instrument that ignores a field it does not understand reports a number that
    means something else than the person writing the case intended.
    """
    if not isinstance(data, dict):
        raise MaboloError(f"{path}: a case is one mapping, with id, a trigger and expect")

    raw_id = data.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        # Not `str(...)`: an id of 123 used to become "123" without a word, and
        # the baseline is keyed by that string.
        raise MaboloError(f"{path}: the case needs an id, as text")
    case_id = raw_id.strip()

    tier = _tier_of(data, path)
    shape = tier if tier in MEASURED_TIERS else DEFERRED_SHAPE

    expect = data.get("expect")
    if expect is None:
        expect = {}
    if not isinstance(expect, dict):
        raise MaboloError(f"{path}: expect is a mapping")

    # What this shape may carry, named once. A key belonging to another shape is
    # refused rather than ignored: a `query` under `tier: hint` would look like
    # it measures a question and measure a session start.
    allowed = COMMON_KEYS | TRIGGER_KEYS[shape]
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise MaboloError(
            f"{path}: {', '.join(unknown)} does not belong on a {tier} case. "
            f"It takes {', '.join(sorted(TRIGGER_KEYS[shape]))}."
        )
    unknown = sorted(set(expect) - EXPECT_KEYS[shape])
    if unknown:
        raise MaboloError(
            f"{path}: {', '.join(unknown)} does not belong under expect on a {tier} case. "
            f"It takes {', '.join(sorted(EXPECT_KEYS[shape]))}."
        )

    if shape == "hint":
        return _parse_hint_case(data, expect, case_id, tier, path)

    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise MaboloError(f"{path}: the case needs a query")

    entries = _as_names(expect.get("entries"), path)
    silence = _as_bool(expect.get("silence"), "expect.silence", path)
    rank_within = expect.get("rank_within", DEFAULT_RANK_WITHIN)
    if not isinstance(rank_within, int) or isinstance(rank_within, bool) or rank_within < 1:
        raise MaboloError(f"{path}: expect.rank_within is a whole number of lines, at least 1")

    if silence and entries:
        raise MaboloError(f"{path}: a case asserts silence or names entries, not both")
    if not silence and not entries:
        raise MaboloError(
            f"{path}: this case expects nothing. Name the entries it should find, "
            "or say `silence: true` if the memory is meant to stay out of it."
        )
    return Case(
        id=case_id,
        query=query.strip(),
        path=path,
        entries=entries,
        rank_within=rank_within,
        must_cite=_as_bool(expect.get("must_cite"), "expect.must_cite", path),
        silence=silence,
        tier=tier,
    )


def _parse_hint_case(data: dict[str, Any], expect: Any, case_id: str, tier: str, path: Path) -> Case:
    """A case about the session index, which has a state instead of a question.

    Every field is required to be explicit, `as_of` most of all. The selection
    rule counts seven days back from the moment the session starts, so a case
    that let the harness read the clock would measure a different vault every
    week and report the difference as a regression in the search.
    """
    state = data.get("state")
    if not isinstance(state, dict):
        raise MaboloError(
            f"{path}: a hint case needs state: with as_of, and project when one is active"
        )
    unknown = sorted(set(state) - {"project", "as_of"})
    if unknown:
        raise MaboloError(f"{path}: unknown key(s) {', '.join(unknown)} under state")

    project = state.get("project")
    if project is not None and (not isinstance(project, str) or not project.strip()):
        raise MaboloError(f"{path}: state.project is the name of a project, or absent")
    if "as_of" not in state or state["as_of"] is None:
        raise MaboloError(
            f"{path}: state.as_of is the date the session starts at, such as 2026-09-18. "
            "It is required, because the session index counts seven days back from it and "
            "a case without it would measure a different vault every week."
        )
    as_of = parse_time(state["as_of"])
    if as_of is None:
        # A separate sentence from the one above. Telling somebody who wrote
        # `as_of: yesterday` that the field is required is a true statement
        # about the wrong problem.
        raise MaboloError(
            f"{path}: state.as_of is {state['as_of']!r}, which is not a date. "
            "Write it as 2026-09-18, or as a full timestamp."
        )

    present = _as_names(expect.get("in_payload"), path, "expect.in_payload")
    absent = _as_names(expect.get("not_in_payload"), path, "expect.not_in_payload")
    budget = expect.get("budget_tokens")
    if budget is not None:
        budget = _as_count(budget, "expect.budget_tokens", path)
    if not present and not absent and budget is None:
        raise MaboloError(
            f"{path}: this case expects nothing. Name what has to be in the session index, "
            "what has to stay out of it, or what it may cost."
        )
    both = sorted(set(present) & set(absent))
    if both:
        raise MaboloError(
            f"{path}: {', '.join(both)} is expected both in the session index and out of it"
        )
    return Case(
        id=case_id,
        query="",
        path=path,
        tier=tier,
        project=project.strip() if isinstance(project, str) else None,
        as_of=as_of,
        in_payload=present,
        not_in_payload=absent,
        budget_tokens=budget,
    )


#: What a file in the eval folder may be called. Anything else is named rather
#: than skipped: a case nobody notices is a question nobody asked.
CASE_SUFFIXES = (".yaml", ".yml")
KNOWN_FILES = (BASELINE_FILE, ".gitkeep")


@dataclass(frozen=True)
class EvalStore:
    """The eval folder of one vault, and the only way into or out of it.

    Every read and every write goes through the vault's own containment check,
    which refuses a path outside the root and refuses to follow a symlink on the
    way there. Before this existed the eval folder was exempt from both rules
    without anybody deciding that: a linked `.mabolo/eval` was read straight
    through, and `--save-baseline` wrote its file wherever the link pointed.
    """

    vault: Any

    @property
    def directory(self) -> Path:
        return self.vault.eval_dir

    @property
    def baseline_path(self) -> Path:
        return self.directory / BASELINE_FILE

    def case_files(self) -> list[Path]:
        """Every case file, sorted, so two machines run them in the same order.

        A file that is not a case is an error and not a file to step over. The
        suffix is compared in lower case, because `case.YAML` is a case somebody
        wrote and silently ignoring it means measuring a smaller set than the
        folder shows.
        """
        directory = self.vault.ensure_inside(self.directory)
        if not directory.is_dir():
            return []
        files: list[Path] = []
        strays: list[str] = []
        for child in sorted(directory.iterdir()):
            if child.is_symlink():
                strays.append(f"{child.name} is a symlink, and the vault does not read through those")
            elif child.name in KNOWN_FILES:
                continue
            elif child.is_dir():
                strays.append(f"{child.name} is a folder, and a case is one file")
            elif child.suffix.lower() in CASE_SUFFIXES:
                files.append(child)
            else:
                strays.append(f"{child.name} is not a case file")
        if strays:
            raise MaboloError(
                f"{directory} holds something that is not a case: {'; '.join(strays)}. "
                "Move it aside, so that the run covers what the folder shows."
            )
        return files

    def cases(self) -> list[Case]:
        """Every case in the folder, or an error naming the first one that is wrong.

        A case file that cannot be read is an error and not a skipped file. The
        whole point of the run is a number, and a number over an unknown subset
        of the questions is not one.
        """
        cases: list[Case] = []
        for path in self.case_files():
            self.vault.ensure_inside(path)
            size = path.stat().st_size
            if size > MAX_CASE_BYTES:
                raise MaboloError(
                    f"{path} is {size} bytes, above the {MAX_CASE_BYTES} byte limit for a case"
                )
            try:
                # Strict: `yaml.safe_load` keeps the last of two identical keys
                # without a word, so a case file could show one query and
                # measure another.
                data = frontmatter.load_strict(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, MaboloError, OSError, UnicodeDecodeError) as exc:
                raise MaboloError(f"{path} cannot be read as a case: {exc}") from None
            cases.append(parse_case(data, path))
        seen: dict[str, Path] = {}
        for case in cases:
            if case.id in seen:
                raise MaboloError(
                    f"{case.path} and {seen[case.id]} both call themselves {case.id!r}, "
                    "and the baseline is keyed by that id"
                )
            seen[case.id] = case.path
        return cases

    def read_baseline(self) -> Baseline | None:
        """The stored baseline, or None if there is none yet."""
        path = self.vault.ensure_inside(self.baseline_path)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise MaboloError(f"{path} is not a readable baseline: {exc}") from None
        return Baseline.from_json(data, path)

    def write_baseline(self, result: Run, language: str) -> Path:
        """Write the baseline, atomically and with a stable byte order."""
        path = self.vault.ensure_inside(self.baseline_path)
        text = Baseline.from_run(result, language).to_text()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.vault.ensure_inside(path.parent)
        frontmatter.write_bytes(path, text.encode("utf-8"))
        return path


def select(cases: list[Case], wanted: Iterable[str]) -> list[Case]:
    """The named subset of the cases, or an error naming what does not exist."""
    wanted = set(wanted)
    missing = sorted(wanted - {c.id for c in cases})
    if missing:
        raise MaboloError(f"no case called {', '.join(missing)} in this vault")
    return [c for c in cases if c.id in wanted]


def _as_day(moment: dt.datetime | dt.date | None) -> dt.date | None:
    """The calendar day a moment falls on, for comparing against journal days.

    A journal heading is a day, not an instant, so this is where the two meet.
    Whatever offset the moment carries is the one its day is read in: a case
    fixed at midnight in one zone must not reach into the next day in another.
    """
    if moment is None:
        return None
    return moment.date() if isinstance(moment, dt.datetime) else moment


def run_case(index: Index, case: Case, notes: Sequence[journal.Note] = ()) -> Result:
    """Run one case against one index."""
    if not case.measurable:
        return Result(case=case, passed=False, measured=False, reasons=(DEFERRED[case.tier],))
    if case.tier == "hint":
        return run_hint_case(index, case, notes)

    # Searched as deep as the case asks, but costed over what a preview would
    # actually send. Costing the deeper list made a case with `rank_within: 10`
    # report a preview nobody would ever be given.
    found = index.search(case.query, limit=max(case.rank_within, DEFAULT_LIMIT))
    hits = found.hits
    cost = index.preview_cost(hits[:DEFAULT_LIMIT])

    if case.silence:
        if hits:
            names = ", ".join(f"{h.name} ({h.rank})" for h in hits[:3])
            return Result(
                case=case,
                hits=hits,
                passed=False,
                reasons=(f"expected silence, got {len(hits)} hit(s): {names}",),
                cost=cost,
                query=found.query,
            )
        return Result(case=case, hits=hits, passed=True, cost=cost, query=found.query)

    reasons, rank = _judge(index, case, hits)
    return Result(
        case=case,
        hits=hits,
        passed=not reasons,
        reasons=tuple(reasons),
        rank=rank,
        cost=cost,
        query=found.query,
    )


def run_hint_case(index: Index, case: Case, notes: Sequence[journal.Note] = ()) -> Result:
    """Run one hint case: build the session index and look at what is in it.

    The payload is always built with the shipped budget, never with the one the
    case asserts. `budget_tokens` is a statement about the result, and a case
    that changed the budget it measures would only ever confirm itself.

    The journal block is built here too, for the same reason the budget is not
    the case's to choose: what is measured has to be the payload a session
    actually receives. Measuring it without the block would report a cost
    nobody is ever charged, and it would grow quietly as the journal does.
    """
    payload = context.build(
        index.documents,
        project=case.project,
        as_of=case.as_of,
        notes=journal.recent(list(notes), case.project, _as_day(case.as_of)),
    )
    reasons: list[str] = []
    positions: list[int] = []

    if case.project and not any(area == f"{PROJECT_PREFIX}{case.project}" for area, _ in payload.counts):
        # Otherwise a typo in the project name is the quietest pass there is:
        # the project rule simply never fires, the pinned entries still show up,
        # and the case goes green having measured half of what it claims.
        reasons.append(
            f"state.project {case.project!r} is not a project area in this vault, "
            "so this case cannot measure the project rule"
        )

    for wanted in case.in_payload:
        name, missing = resolve_expected(index, wanted)
        if missing:
            reasons.append(missing)
            continue
        position = payload.position(name)
        if position is None:
            reasons.append(
                f"{wanted} is not in the session index, which holds "
                f"{len(payload.shown)} of {len(index)} entries"
            )
            continue
        positions.append(position)

    for unwanted in case.not_in_payload:
        name, missing = resolve_expected(index, unwanted)
        if missing:
            reasons.append(missing)
            continue
        position = payload.position(name)
        if position is not None:
            reasons.append(
                f"{unwanted} is in the session index at position {position}, "
                "and this case says it should not be"
            )

    cost = payload.cost()
    if case.budget_tokens is not None and cost > case.budget_tokens:
        reasons.append(
            f"the session index costs about {cost} tokens, wanted {case.budget_tokens} or fewer"
        )

    # The worst position, for the same reason a search case stores the worst
    # rank: it is the line closest to being cut, so it is the one that warns
    # first. None as soon as anything went wrong, because a failing case has no
    # position worth comparing, and None when the case names nothing that has to
    # be present: a case asserting only absences has no position at all, and
    # `max` of nothing used to end the whole run in a traceback.
    position = max(positions) if not reasons and positions else None
    return Result(
        case=case,
        passed=not reasons,
        reasons=tuple(reasons),
        rank=position,
        cost=cost,
        payload=payload,
    )


def resolve_expected(index: Index, wanted: str) -> tuple[str, str | None]:
    """The entry a case names, or the sentence saying it is not in this vault.

    Through `resolve_entry`, so a case names an entry or an alias and never a
    project. Both shapes of case go through here: the sentence a reader gets
    when a case names something that is gone has to be the same sentence.
    """
    document = index.resolve_entry(wanted)
    if document is None:
        return wanted, (
            f"{wanted} is not an entry in this vault, so the case cannot be measured. "
            "It was probably renamed or removed."
        )
    return document.name, None


def _judge(index: Index, case: Case, hits: tuple[Hit, ...]) -> tuple[list[str], int | None]:
    """Did the expected entries come back, and at what worst rank.

    The rank is what the baseline stores, so its rule has to be one sentence: it
    is the worst rank of the expected entries, and it is None as soon as one of
    them is missing or came back too low. Anything else and a still failing case
    reports that it improved.
    """
    reasons: list[str] = []
    ranks: list[int] = []
    by_name = {hit.name: hit for hit in hits}
    for wanted in case.entries:
        name, missing = resolve_expected(index, wanted)
        if missing:
            reasons.append(missing)
            continue
        hit = by_name.get(name)
        if hit is None:
            reasons.append(f"{wanted} was not returned at all")
            continue
        if hit.rank > case.rank_within:
            reasons.append(
                f"{wanted} came back at rank {hit.rank}, wanted {case.rank_within} or better"
            )
            continue
        ranks.append(hit.rank)
    rank = max(ranks) if not reasons and len(ranks) == len(case.entries) else None
    return reasons, rank


@dataclass
class Run:
    """Every case, run once. The numbers come off this rather than out of a loop."""

    results: list[Result] = field(default_factory=list)
    entries: int = 0

    @property
    def measured(self) -> list[Result]:
        return [r for r in self.results if r.measured]

    @property
    def deferred(self) -> list[Result]:
        return [r for r in self.results if not r.measured]

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.measured if not r.passed]

    def in_tier(self, tier: str) -> list[Result]:
        """Every measured case of one tier. A tier is a separate measurement,
        so the numbers are never added up across them."""
        return [r for r in self.measured if r.case.tier == tier]

    @property
    def searches(self) -> list[Result]:
        return self.in_tier("search")

    @property
    def hints(self) -> list[Result]:
        return self.in_tier("hint")

    @property
    def positives(self) -> list[Result]:
        return [r for r in self.searches if not r.case.silence]

    @property
    def negatives(self) -> list[Result]:
        return [r for r in self.searches if r.case.silence]

    @property
    def uncited(self) -> list[Result]:
        """Cases that ask for a citation, which nothing here can check yet."""
        return [r for r in self.measured if r.case.must_cite]

    @property
    def first_rank(self) -> int:
        return sum(1 for r in self.positives if r.rank == 1)

    @property
    def ok(self) -> bool:
        return not self.failures

    def cost(self) -> tuple[int, int]:
        """The average and the worst estimated cost of a preview, in tokens."""
        return _average_and_worst([r.cost for r in self.positives])

    def hint_cost(self) -> tuple[int, int]:
        """The same for the session index, which is a different payload and is
        therefore never averaged together with a search preview."""
        return _average_and_worst([r.cost for r in self.hints])


def _average_and_worst(costs: list[int]) -> tuple[int, int]:
    if not costs:
        return (0, 0)
    return (round(sum(costs) / len(costs)), max(costs))


def run(index: Index, cases: list[Case], notes: Sequence[journal.Note] = ()) -> Run:
    """Run every case against one index."""
    return Run(results=[run_case(index, case, notes) for case in cases], entries=len(index))


# The baseline, and what changed against it


@dataclass(frozen=True)
class Change:
    """One difference between this run and the baseline."""

    kind: str  # regressed | slipped | improved | new | gone
    case_id: str
    message: str

    @property
    def is_worse(self) -> bool:
        """Only these two fail a run. An improvement is news, not a problem."""
        return self.kind in ("regressed", "slipped")


@dataclass(frozen=True)
class BaselineCase:
    """What the baseline remembers about one case: its tier, whether it passed, and where.

    The tier is part of it because a rank means something different in each one.
    In a search it is where the entry came back in the results; in a hint it is
    how far an entry sits from the line the budget cuts at. A case that keeps its
    id and changes its tier used to be reported as having slipped from one to
    the other, which is a red run about nothing.
    """

    tier: str
    passed: bool
    rank: int | None = None

    @classmethod
    def from_json(cls, data: Any, case_id: str, where: Path) -> BaselineCase:
        if not isinstance(data, dict):
            raise MaboloError(f"{where}: the record for {case_id!r} is not a mapping")
        tier = data.get("tier")
        if not isinstance(tier, str) or not tier:
            raise MaboloError(f"{where}: the record for {case_id!r} does not say which tier it measured")
        passed = data.get("passed")
        rank = data.get("rank")
        # `isinstance(True, int)` is true in Python, so a rank of `true` would
        # sail through a plain int check and then be compared against a number.
        if not isinstance(passed, bool):
            raise MaboloError(f"{where}: {case_id!r} says passed is {passed!r}, which is not yes or no")
        if rank is not None and (not isinstance(rank, int) or isinstance(rank, bool) or rank < 1):
            raise MaboloError(f"{where}: {case_id!r} says rank is {rank!r}, which is not a position")
        return cls(tier=tier, passed=passed, rank=rank)

    def to_json(self) -> dict[str, Any]:
        return {"tier": self.tier, "passed": self.passed, "rank": self.rank}


@dataclass(frozen=True)
class Baseline:
    """The line a run is held against, as it is stored beside the cases.

    Deliberately thin. Every field in here is one the gate compares, and a
    baseline that also carried scores, timings or counts would invite a
    comparison that fires on noise.

    The language is in it because the search reads it: stop words and suffixes
    differ, so the same commit and the same question can rank differently under
    another one. A baseline written in one language says nothing about a run in
    another, and saying so out loud beats comparing the two anyway.
    """

    language: str
    cases: dict[str, BaselineCase] = field(default_factory=dict)
    version: int = BASELINE_VERSION

    @classmethod
    def from_run(cls, result: Run, language: str) -> Baseline:
        return cls(
            language=language,
            cases={
                r.case.id: BaselineCase(tier=r.case.tier, passed=r.passed, rank=r.rank)
                for r in sorted(result.measured, key=lambda r: r.case.id)
            },
        )

    @classmethod
    def from_json(cls, data: Any, where: Path) -> Baseline:
        if not isinstance(data, dict) or not isinstance(data.get("cases"), dict):
            raise MaboloError(f"{where} is not a baseline written by mabolo")
        version = data.get("version")
        if version != BASELINE_VERSION:
            raise MaboloError(
                f"{where} was written for baseline version {version}, and this Mabolo speaks "
                f"{BASELINE_VERSION}. Run `mabolo eval --save-baseline` to write a new one."
            )
        language = data.get("language")
        if not isinstance(language, str) or not language:
            raise MaboloError(f"{where} does not say which language it was measured in")
        return cls(
            language=language,
            cases={
                case_id: BaselineCase.from_json(record, case_id, where)
                for case_id, record in sorted(data["cases"].items())
            },
            version=version,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "language": self.language,
            "cases": {case_id: case.to_json() for case_id, case in sorted(self.cases.items())},
        }

    def to_text(self) -> str:
        return json.dumps(self.to_json(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def check_language(self, language: str) -> None:
        """Refuse to compare a run against a baseline measured in another language."""
        if language != self.language:
            raise MaboloError(
                f"the baseline was measured with language {self.language!r} and this vault "
                f"says {language!r}, so the two cannot be compared. Run "
                "`mabolo eval --save-baseline` once you are sure the vault is right."
            )


def compare(baseline: Baseline | None, result: Run, *, subset: bool = False) -> list[Change]:
    """What moved since the baseline, in the order a person wants to read it.

    `subset` says that this run covered only some of the cases, which is what
    `--case` does. A case the baseline knows and this run left out is then
    expected rather than missing, so it is not reported at all.
    """
    if baseline is None:
        return []
    stored = baseline.cases
    changes: list[Change] = []
    for current in sorted(result.measured, key=lambda r: r.case.id):
        was = stored.get(current.case.id)
        if was is None:
            changes.append(Change("new", current.case.id, "is new and not in the baseline"))
            continue
        if was.tier != current.case.tier:
            # Two measurements wearing one id. Comparing their ranks would
            # report a slip between two scales that have nothing to do with
            # each other.
            changes.append(
                Change(
                    "new",
                    current.case.id,
                    f"was measured as a {was.tier} case and is now a {current.case.tier} case",
                )
            )
            continue
        if was.passed and not current.passed:
            changes.append(Change("regressed", current.case.id, "passed in the baseline and fails now"))
            continue
        if not was.passed and current.passed:
            changes.append(Change("improved", current.case.id, "failed in the baseline and passes now"))
            continue
        # Both passed, or both failed. A rank only means the same thing on both
        # sides when both passed: a failing case has no rank at all.
        if not (was.passed and current.passed):
            continue
        if isinstance(was.rank, int) and isinstance(current.rank, int):
            if current.rank > was.rank:
                # Still passing, and already worse. This is the whole reason the
                # gate stores a rank: by the time it fails, the change that
                # caused it is several commits back.
                changes.append(
                    Change("slipped", current.case.id, f"slipped from rank {was.rank} to rank {current.rank}")
                )
            elif current.rank < was.rank:
                changes.append(
                    Change("improved", current.case.id, f"improved from rank {was.rank} to rank {current.rank}")
                )
    if not subset:
        present = {r.case.id for r in result.measured}
        for case_id in sorted(set(stored) - present):
            changes.append(Change("gone", case_id, "is in the baseline and was not run"))
    return changes


def _plural(count: int, thing: str) -> str:
    return f"{count} {thing}" if count == 1 else f"{count} {thing}s"


def render(result: Run, changes: list[Change] | None = None) -> str:
    """The whole run as text. Everything unmeasured is said out loud."""
    changes = changes or []
    counted = "1 entry" if result.entries == 1 else f"{result.entries} entries"
    lines: list[str] = [f"{counted}, {_plural(len(result.results), 'case')}", ""]

    for tier in MEASURED_TIERS:
        group = result.in_tier(tier)
        if not group:
            continue
        failed_here = [r for r in group if not r.passed]
        lines.append(
            f"{tier:8} {_plural(len(group), 'case')}, "
            f"{len(group) - len(failed_here)} pass, {len(failed_here)} fail"
        )
    failed = result.failures
    for tier, reason in DEFERRED.items():
        waiting = [r for r in result.deferred if r.case.tier == tier]
        if waiting:
            lines.append(f"{tier:8} {_plural(len(waiting), 'case')} not measured yet, {reason}")
    lines.append("")

    positives, negatives = result.positives, result.negatives
    if positives:
        within = sum(1 for r in positives if r.passed)
        lines.append(
            f"hit at rank 1 in {result.first_rank} of {len(positives)}, "
            f"inside the asked rank in {within} of {len(positives)}"
        )
    if negatives:
        held = sum(1 for r in negatives if r.passed)
        lines.append(f"silence held in {held} of {len(negatives)} negative cases")
    average, worst = result.cost()
    if average:
        lines.append(f"a preview costs about {average} tokens, {worst} at worst, estimated")
    if result.hints:
        held = sum(1 for r in result.hints if r.passed)
        average, worst = result.hint_cost()
        lines.append(
            f"the session index held what was asked in {held} of {len(result.hints)} cases "
            f"and costs about {average} tokens, {worst} at worst, estimated"
        )
    if result.uncited:
        count = len(result.uncited)
        asks = "case asks" if count == 1 else "cases ask"
        lines.append(f"{count} {asks} for a citation: {DEFERRED_CITE}")

    if failed:
        lines.append("")
        for item in failed:
            label = "state" if item.case.tier == "hint" else "query"
            lines.append(f"fail  {item.case.id}")
            lines.append(f"      {label}: {item.case.trigger}")
            for reason in item.reasons:
                lines.append(f"      {reason}")
            if item.hits and not item.case.silence:
                returned = ", ".join(f"{h.name} ({h.rank})" for h in item.hits)
                lines.append(f"      returned: {returned}")

    worse = [c for c in changes if c.is_worse]
    other = [c for c in changes if not c.is_worse]
    if changes:
        lines.append("")
        for change in worse:
            lines.append(f"worse {change.case_id} {change.message}")
        for change in other:
            lines.append(f"note  {change.case_id} {change.message}")
    return "\n".join(lines)
