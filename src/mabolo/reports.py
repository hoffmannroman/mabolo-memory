"""What went wrong with Mabolo itself, kept until somebody has dealt with it.

`health` says once that a hook gave up and then forgets it, which is right for
a notice and wrong for a fix: the investigation of a hook timeout found the
evidence already gone, used up by the session start that reported it. This is
the record that stays.

Three kinds of thing arrive here:

* **a hook that gave up**, from `health.record`;
* **a tool that failed** with something other than a refusal, from the server.
  A refusal is the gate working and is not recorded;
* **a session's report**, through `mabolo_report`, when an agent notices that
  Mabolo did something it should not have.

Repeats of one failure share an id, so a hook that times out on every prompt
is one problem seen forty times and not forty problems. The id comes from
where it happened and the kind of failure, not from the full message, which
often carries a number that changes every time.

**Nothing is deleted when it is dealt with.** A status line is appended
instead, and a problem is open again as soon as it happens once more after
that line. A fix that did not work therefore reopens itself.

The file is per machine, under the state directory, never in the vault: the
vault holds what a person agreed to keep, and this is not that. Like `health`,
nothing here raises.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .consent import state_home

REPORTS_FILE = "reports.jsonl"

#: Lines kept once the file is trimmed. Order is kept as well, so a status line
#: only ever goes after everything it was about.
KEEP = 500

#: The longest message kept. A traceback or a session's account is useful in
#: its first lines and only expensive after that.
MAX_MESSAGE = 2000

HOOK = "hook"
TOOL = "tool"
SESSION = "session"
SOURCES = (HOOK, TOOL, SESSION)

FIXED = "fixed"
EXAMINED = "examined"
CLOSED = "closed"
STATUSES = (FIXED, EXAMINED, CLOSED)


@dataclass
class Problem:
    """One kind of failure, with every time it was seen."""

    id: str
    source: str
    where: str
    seen: list[dict] = field(default_factory=list)
    #: The last status line, when there is one.
    status: dict | None = None
    #: How often it happened after that status line.
    since: int = 0

    @property
    def open(self) -> bool:
        return self.since > 0

    @property
    def last(self) -> dict:
        return self.seen[-1] if self.seen else {}


def reports_path() -> Path:
    return state_home() / "mabolo" / REPORTS_FILE


def problem_id(source: str, where: str, kind: str) -> str:
    return hashlib.sha256(f"{source}\n{where}\n{kind}".encode()).hexdigest()[:8]


def add(source: str, where: str, message: str, kind: str = "", at: dt.datetime | None = None) -> str:
    """Write down one occurrence, and return the id of the problem it belongs to.

    `kind` is what makes two occurrences the same problem. Left empty, the
    message is the kind, which is right for a session's report: two different
    accounts are two different problems.
    """
    message = str(message)[:MAX_MESSAGE]
    found = problem_id(source, where, kind or message)
    moment = (at or dt.datetime.now().astimezone()).isoformat(timespec="seconds")
    _append({"id": found, "at": moment, "source": source, "where": where, "message": message})
    return found


def settle(found: str, status: str, note: str = "", commit: str = "",
           at: dt.datetime | None = None) -> bool:
    """Record what became of a problem. False when no such problem is known."""
    if status not in STATUSES or found not in {problem.id for problem in problems()}:
        return False
    moment = (at or dt.datetime.now().astimezone()).isoformat(timespec="seconds")
    line = {"id": found, "at": moment, "status": status, "note": str(note)[:MAX_MESSAGE]}
    if commit:
        line["commit"] = commit
    _append(line)
    return True


def problems() -> list[Problem]:
    """Every problem on record, in the order it was first seen."""
    known: dict[str, Problem] = {}
    for item in _items():
        found = item.get("id")
        if not isinstance(found, str):
            continue
        if "status" in item:
            if found in known:
                known[found].status = item
                known[found].since = 0
            continue
        problem = known.get(found)
        if problem is None:
            problem = known[found] = Problem(found, str(item.get("source", "")), str(item.get("where", "")))
        problem.seen.append(item)
        problem.since += 1
    return list(known.values())


def open_problems() -> list[Problem]:
    """The ones nobody has dealt with, the most frequent first, then the newest."""
    found = [problem for problem in problems() if problem.open]
    found.sort(key=lambda problem: str(problem.last.get("at", "")), reverse=True)
    found.sort(key=lambda problem: problem.since, reverse=True)
    return found


def find(found: str) -> Problem | None:
    return next((problem for problem in problems() if problem.id == found), None)


def line(problem: Problem) -> str:
    """One line for a listing."""
    times = "once" if problem.since == 1 else f"{problem.since} times"
    if not problem.open:
        times = problem.status.get("status", "") if problem.status else "settled"
    last = str(problem.last.get("message", "")).splitlines()[0][:120] if problem.seen else ""
    when = str(problem.last.get("at", ""))[:16].replace("T", " ")
    return f"{problem.id}  {problem.source} {problem.where}, {times}, last {when}: {last}"


def _append(item: dict) -> None:
    path = reports_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Appended, not rewritten: a hook and a server can both be writing,
        # and one short write in append mode does not lose the other's line.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        _trim(path)
    except OSError:
        pass


def _trim(path: Path) -> None:
    lines = _lines(path)
    if len(lines) <= 2 * KEEP:
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines[-KEEP:]) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _items() -> list[dict]:
    found = []
    for raw in _lines(reports_path()):
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def _lines(path: Path) -> list[str]:
    try:
        return [one for one in path.read_text(encoding="utf-8").splitlines() if one.strip()]
    except (OSError, UnicodeDecodeError):
        return []
