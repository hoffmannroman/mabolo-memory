"""Saying so when Mabolo itself is not working as it should.

Two failures used to be silent, and both look like an empty memory rather than
like an error.

**A server running old code.** A client starts the server once per session and
keeps it. Updating Mabolo changes the files on disk and nothing in the running
process, so a session that was open across an update went on with the code of
before, including the bugs the update fixed. The server now remembers which
code it loaded, and every answer it gives says so once that code has changed.

**A hook that gave up.** A hook never fails a session, which is right, and it
says why on stderr, which nobody reads. The reason is now also written down
here, and the next session start reports it once. `doctor` reports it too,
without using it up.

Nothing here raises. It is called from the two places that must keep working
whatever happens, a hook and a tool answer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from .consent import state_home

#: How many failures are kept. A hook that fails on every prompt would
#: otherwise grow the file for as long as nobody looks.
KEEP = 50

PROBLEMS_FILE = "problems.jsonl"

STALE_NOTE = (
    "Note for the person: Mabolo was updated after this server started, so it is "
    "still running the old code. Reconnect it: /mcp, then mabolo, then Reconnect."
)


def code_fingerprint(package: Path | None = None) -> str:
    """What the code on disk is right now, as one short string.

    Name, size and modification time of every module, because the version
    number stays the same between two builds and an editable install changes
    nothing but the files. Cheap enough to take on every tool call.
    """
    root = package or Path(__file__).parent
    digest = hashlib.sha256()
    try:
        for path in sorted(root.glob("*.py")):
            stat = path.stat()
            digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    except OSError:
        return ""
    return digest.hexdigest()[:16]


def stale_note(loaded: str, package: Path | None = None) -> str:
    """The line an answer ends with when the code has changed, or nothing."""
    now = code_fingerprint(package)
    return STALE_NOTE if loaded and now and now != loaded else ""


def problems_path() -> Path:
    return state_home() / "mabolo" / PROBLEMS_FILE


def record(hook: str, message: str, at: dt.datetime | None = None) -> None:
    """Write down that a hook gave up, and why."""
    moment = (at or dt.datetime.now().astimezone()).isoformat(timespec="seconds")
    line = json.dumps({"at": moment, "hook": hook, "error": message}, ensure_ascii=False)
    path = problems_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        kept = _lines(path)[-(KEEP - 1):]
        temporary = path.with_suffix(".tmp")
        temporary.write_text("\n".join([*kept, line]) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def pending() -> list[dict]:
    """The failures nobody has been told about yet, oldest first."""
    found = []
    for line in _lines(problems_path()):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("hook"):
            found.append(item)
    return found


def take() -> list[dict]:
    """The pending failures, and forget them: they are about to be reported."""
    found = pending()
    if found:
        try:
            problems_path().unlink()
        except OSError:
            pass
    return found


def summary(problems: list[dict]) -> str:
    """One line per hook: how often it gave up, since when, and the last reason."""
    by_hook: dict[str, list[dict]] = {}
    for item in problems:
        by_hook.setdefault(str(item["hook"]), []).append(item)
    lines = []
    for hook, items in by_hook.items():
        times = "once" if len(items) == 1 else f"{len(items)} times"
        since = str(items[0].get("at", ""))[:16].replace("T", " ")
        lines.append(f"the {hook} gave up {times} since {since}, last: {items[-1].get('error', '')}")
    return "; ".join(lines)


def _lines(path: Path) -> list[str]:
    try:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError):
        return []
