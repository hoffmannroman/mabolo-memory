"""What a session has already been shown, so that it is not shown twice.

Quiet recall and the design tier both speak without being asked, and both fire
often. A rule that arrives on every single touch of the same file stops being
read after the third time, which is the same failure as never arriving, only
more expensive.

So the caller remembers. Not the vault: the vault holds knowledge, and what one
session happened to see is not knowledge. These notes live beside the prompt
notes under `XDG_STATE_HOME`, they expire on the same schedule, and losing them
costs a repetition and nothing else.

**The key is `name@revision`, not the name.** A rule that was corrected is a
different rule, and a session that was shown the old one has not been shown the
new one. This is the rule quiet recall was designed with, and two tiers that
remember two ways would be two things to reason about.

**No session id, no memory.** A client that does not say which session this is
gets the rule on every matching touch. The alternative, falling back to the
directory the way the consent check does, would let two sessions working in one
project silence each other, and a rule that silently never arrived is the
failure this project exists to prevent. Repetition is the visible cost, silence
is the hidden one, and this project pays the visible one.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .consent import RETENTION_DAYS, forget_old, session_id, state_home, trim

SEEN_DIRNAME = "seen"


def seen_dir() -> Path:
    return state_home() / "mabolo" / SEEN_DIRNAME


def key(name: str, revision: str) -> str:
    """How one shown thing is written down. A revision of its own per entry."""
    return f"{name}@{revision}" if revision else name


def remember(keys: list[str], *, session: object, at: dt.datetime | None = None,
             directory: Path | None = None) -> Path | None:
    """Write down what was just shown, and never fail the caller over it.

    A hook that could not write its note has shown something twice. A hook that
    raised has interrupted a tool call, which is a different order of damage.
    """
    if not keys:
        return None
    named = session if isinstance(session, str) and session.strip() else None
    if named is None:
        return None
    where = Path(directory) if directory else seen_dir()
    target = where / f"{session_id(named)}.jsonl"
    moment = at or dt.datetime.now().astimezone()
    try:
        where.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            for one in keys:
                handle.write(
                    json.dumps({"at": moment.isoformat(timespec="seconds"), "key": one},
                               ensure_ascii=False)
                    + "\n"
                )
        trim(target)
        forget_old(where, now=moment, days=RETENTION_DAYS)
    except OSError:
        return None
    return target


def already(session: object, directory: Path | None = None) -> set[str]:
    """Everything this session has been shown, as `name@revision` keys."""
    named = session if isinstance(session, str) and session.strip() else None
    if named is None:
        return set()
    where = Path(directory) if directory else seen_dir()
    target = where / f"{session_id(named)}.jsonl"
    out: set[str] = set()
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("key"), str):
            out.add(data["key"])
    return out
