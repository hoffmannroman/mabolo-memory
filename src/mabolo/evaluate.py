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

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import frontmatter
from .errors import MaboloError
from .index import DEFAULT_LIMIT, Hit, Index

#: The file the gate compares against, beside the cases and versioned with them.
BASELINE_FILE = "baseline.json"
#: Bumped when the shape of that file changes. An older Mabolo refuses a newer
#: baseline rather than comparing against fields it does not understand.
BASELINE_VERSION = 2

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
#: What a case can be about. Only the first is measurable without a model, and
#: the rest are exactly the keys of DEFERRED, so a new tier cannot be accepted
#: while the sentence explaining why it is deferred is still missing.
TIERS = ("index", *DEFERRED)

DEFERRED_CITE = "must_cite needs a model in the loop, which the harness does not run"

DEFAULT_RANK_WITHIN = DEFAULT_LIMIT


@dataclass(frozen=True)
class Case:
    """One question, and what the memory is expected to do with it."""

    id: str
    query: str
    path: Path
    entries: tuple[str, ...] = ()
    rank_within: int = DEFAULT_RANK_WITHIN
    must_cite: bool = False
    silence: bool = False
    tier: str = "index"

    @property
    def measurable(self) -> bool:
        """True when the retrieval harness alone can decide this case."""
        return self.tier == "index"


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


def _as_bool(value: Any, field_name: str, where: Path) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise MaboloError(f"{where}: {field_name} is yes or no, not {value!r}")


def _as_names(value: Any, where: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise MaboloError(f"{where}: expect.entries is a list of entry names")
    return tuple(v.strip() for v in value if v.strip())


def parse_case(data: Any, path: Path) -> Case:
    """One case from one file, or an error saying what is wrong with it.

    Nothing here is lenient. A case is a measurement instrument, and an
    instrument that ignores a field it does not understand reports a number that
    means something else than the person writing the case intended.
    """
    if not isinstance(data, dict):
        raise MaboloError(f"{path}: a case is one mapping, with id, query and expect")
    unknown = sorted(set(data) - {"id", "query", "expect", "tier", "note"})
    if unknown:
        raise MaboloError(f"{path}: unknown key(s) {', '.join(unknown)} in this case")

    case_id = str(data.get("id") or "").strip()
    if not case_id:
        raise MaboloError(f"{path}: the case needs an id")
    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise MaboloError(f"{path}: the case needs a query")

    tier = str(data.get("tier") or "index").strip()
    if tier not in TIERS:
        raise MaboloError(f"{path}: tier is one of {', '.join(TIERS)}, not {tier!r}")

    expect = data.get("expect")
    if expect is None:
        expect = {}
    if not isinstance(expect, dict):
        raise MaboloError(f"{path}: expect is a mapping")
    unknown = sorted(set(expect) - {"entries", "rank_within", "must_cite", "silence"})
    if unknown:
        raise MaboloError(f"{path}: unknown key(s) {', '.join(unknown)} under expect")

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


def run_case(index: Index, case: Case) -> Result:
    """Run one case against one index."""
    if not case.measurable:
        return Result(case=case, passed=False, measured=False, reasons=(DEFERRED[case.tier],))

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
        document = index.resolve(wanted)
        if document is None:
            reasons.append(
                f"{wanted} is not an entry in this vault, so the case cannot be measured. "
                "It was probably renamed or removed."
            )
            continue
        hit = by_name.get(document.name)
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

    @property
    def positives(self) -> list[Result]:
        return [r for r in self.measured if not r.case.silence]

    @property
    def negatives(self) -> list[Result]:
        return [r for r in self.measured if r.case.silence]

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
        costs = [r.cost for r in self.measured if not r.case.silence]
        if not costs:
            return (0, 0)
        return (round(sum(costs) / len(costs)), max(costs))


def run(index: Index, cases: list[Case]) -> Run:
    """Run every case against one index."""
    return Run(results=[run_case(index, case) for case in cases], entries=len(index))


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
    """What the baseline remembers about one case: whether it passed, and where."""

    passed: bool
    rank: int | None = None

    @classmethod
    def from_json(cls, data: Any, case_id: str, where: Path) -> BaselineCase:
        if not isinstance(data, dict):
            raise MaboloError(f"{where}: the record for {case_id!r} is not a mapping")
        passed = data.get("passed")
        rank = data.get("rank")
        # `isinstance(True, int)` is true in Python, so a rank of `true` would
        # sail through a plain int check and then be compared against a number.
        if not isinstance(passed, bool):
            raise MaboloError(f"{where}: {case_id!r} says passed is {passed!r}, which is not yes or no")
        if rank is not None and (not isinstance(rank, int) or isinstance(rank, bool) or rank < 1):
            raise MaboloError(f"{where}: {case_id!r} says rank is {rank!r}, which is not a position")
        return cls(passed=passed, rank=rank)

    def to_json(self) -> dict[str, Any]:
        return {"passed": self.passed, "rank": self.rank}


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
                r.case.id: BaselineCase(passed=r.passed, rank=r.rank)
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

    measured = result.measured
    failed = result.failures
    lines.append(
        f"index    {_plural(len(measured), 'case')}, "
        f"{len(measured) - len(failed)} pass, {len(failed)} fail"
    )
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
    if result.uncited:
        count = len(result.uncited)
        asks = "case asks" if count == 1 else "cases ask"
        lines.append(f"{count} {asks} for a citation: {DEFERRED_CITE}")

    if failed:
        lines.append("")
        for item in failed:
            lines.append(f"fail  {item.case.id}")
            lines.append(f"      query: {item.case.query}")
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
