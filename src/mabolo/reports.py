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

The file is per machine, under the state directory, never among the entries:
the vault holds what a person agreed to keep, and this is not that. Like
`health`, nothing here raises, except in `sync`, which is never called from a
hook's own deadline.

**Every machine sees every machine's record.** `sync` puts this machine's file
on a branch called `reports` in the vault's repository, as `<host>.jsonl`
beside the others, and brings theirs back. Like the inbox it is one parentless
commit, never merged and never checked out. Each machine only ever replaces its
own file, so two machines can never disagree about a line: there is nothing to
merge but a set of files. A problem from one machine can be settled on another,
because a status line counts wherever it was written; the lines of all files
are read in the order they were written.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

from .consent import state_home

REPORTS_FILE = "reports.jsonl"
SYNCED_FILE = "reports.synced"

#: The branch, in the vault's repository, that carries every machine's record.
BRANCH = "reports"

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


def host() -> str:
    """This machine's name, as a file name on the branch."""
    try:
        name = socket.gethostname().split(".")[0].lower()
    except OSError:
        name = ""
    return re.sub(r"[^a-z0-9-]+", "-", name).strip("-") or "unknown"


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
    moment = _stamp(at)
    _append({"id": found, "at": moment, "host": host(), "source": source, "where": where,
             "message": message})
    return found


def settle(found: str, status: str, note: str = "", commit: str = "",
           at: dt.datetime | None = None, elsewhere: list[dict] | None = None) -> bool:
    """Record what became of a problem. False when no such problem is known."""
    if status not in STATUSES or found not in {problem.id for problem in problems(elsewhere)}:
        return False
    moment = _stamp(at)
    line = {"id": found, "at": moment, "host": host(), "status": status,
            "note": str(note)[:MAX_MESSAGE]}
    if commit:
        line["commit"] = commit
    _append(line)
    return True


def problems(elsewhere: list[dict] | None = None) -> list[Problem]:
    """Every problem on record, in the order it was first seen.

    `elsewhere` is what `sync` brought back from the other machines. Without
    it, this machine's own record is the whole answer.
    """
    known: dict[str, Problem] = {}
    for item in _ordered(_items() + list(elsewhere or [])):
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


def open_problems(elsewhere: list[dict] | None = None) -> list[Problem]:
    """The ones nobody has dealt with, the most frequent first, then the newest."""
    found = [problem for problem in problems(elsewhere) if problem.open]
    found.sort(key=lambda problem: str(problem.last.get("at", "")), reverse=True)
    found.sort(key=lambda problem: problem.since, reverse=True)
    return found


def find(found: str, elsewhere: list[dict] | None = None) -> Problem | None:
    return next((problem for problem in problems(elsewhere) if problem.id == found), None)


def line(problem: Problem) -> str:
    """One line for a listing."""
    times = "once" if problem.since == 1 else f"{problem.since} times"
    if not problem.open:
        times = problem.status.get("status", "") if problem.status else "settled"
    last = (str(problem.last.get("message", "")).splitlines() or [""])[0][:120] if problem.seen else ""
    when = str(problem.last.get("at", ""))[:16].replace("T", " ")
    machines = sorted({str(item.get("host", "")) for item in problem.seen if item.get("host")})
    on = f" on {', '.join(machines)}" if machines else ""
    return f"{problem.id}  {problem.source} {problem.where}{on}, {times}, last {when}: {last}"


def needs_sync() -> bool:
    """Whether this machine's record changed since it was last published."""
    return _digest(_own_bytes()) != _read_marker()


def sync(root: Path, remote: str | None) -> list[dict]:
    """Publish this machine's record on the vault's `reports` branch, and
    return the lines of every other machine's.

    An empty answer when the vault has no remote or the remote cannot be
    reached: then this machine's own record is all there is to read, and the
    next sync takes this one along.
    """
    from . import PRODUCER, git, inbox
    from .write import lock, signed

    if not remote or not git.is_repository_inside(root) or not git.has_remote(root, remote):
        return []
    name = f"{host()}.jsonl"
    mine = _own_bytes()
    with lock(root):
        # The inbox's two readers: where the remote's branch is, and what its
        # one commit holds. The shape is the same, a parentless commit of files.
        reachable, there = inbox._fetch_branch(root, remote, BRANCH)
        theirs = inbox._files(root, there)
        for _ in range(2):
            if not reachable or not mine or theirs.get(name) == mine:
                break
            files = {**theirs, name: mine}
            commit = git.build_commit(root, None, files, signed(f"reports of {host()}", "reports", PRODUCER))
            if git.push(root, remote, BRANCH, commit, there) == "pushed":
                theirs = files
                break
            # Another machine published in between. Its file is taken as it
            # now is, and this one's is put beside it once more.
            reachable, there = inbox._fetch_branch(root, remote, BRANCH)
            theirs = inbox._files(root, there)
    if theirs.get(name) == mine:
        _write_marker(_digest(mine))
    return [item for other, data in theirs.items() if other != name for item in _parse(data)]


def _stamp(at: dt.datetime | None) -> str:
    """The moment of a line, to the microsecond. Lines of two machines are put
    in order by it, and a fix on one and a recurrence on the other can easily
    fall into the same second."""
    return (at or dt.datetime.now().astimezone()).isoformat(timespec="microseconds")


def _ordered(items: list[dict]) -> list[dict]:
    """The lines of several machines in the order they were written.

    By the moment, compared in UTC, and by position within a file when two
    moments are the same, which keeps a status after what it was about.
    """
    from .schema import as_utc

    def moment(pair: tuple[int, dict]) -> tuple[dt.datetime, int]:
        try:
            when = as_utc(dt.datetime.fromisoformat(str(pair[1].get("at", ""))))
        except ValueError:
            when = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return when, pair[0]

    return [item for _, item in sorted(enumerate(items), key=moment)]


def _own_bytes() -> bytes:
    try:
        return reports_path().read_bytes()
    except OSError:
        return b""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest() if data else ""


def _read_marker() -> str:
    try:
        return (state_home() / "mabolo" / SYNCED_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_marker(value: str) -> None:
    try:
        (state_home() / "mabolo" / SYNCED_FILE).write_text(value, encoding="utf-8")
    except OSError:
        pass


def _parse(data: bytes) -> list[dict]:
    found = []
    for raw in data.decode("utf-8", "replace").splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


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
    return _parse("\n".join(_lines(reports_path())).encode("utf-8"))


def _lines(path: Path) -> list[str]:
    try:
        return [one for one in path.read_text(encoding="utf-8").splitlines() if one.strip()]
    except (OSError, UnicodeDecodeError):
        return []
