"""What a person actually typed, kept long enough to check a quote against.

The write tools take a `quote`: the sentence that authorised the change. The
model supplies it, so on its own it proves nothing, and a gate that trusts it
is a gate made of paper. This module is the other half. The prompt hook writes
down every prompt it is handed, the server reads those notes back, and only a
quote that appears verbatim in one of them produces `verified.by: human:<id>`.

Four things that are not obvious:

* **It lives outside the vault.** The vault is the only source of knowledge and
  everything here is disposable, so the notes go under `XDG_STATE_HOME`. A
  prompt is also not something anybody asked to keep, which is why they expire
  on their own.
* **A quote is matched against a message, not against a session.** The server
  is started by the client without ever being told which session it belongs to,
  so it cannot honestly claim "this sentence, in this conversation". What it
  can show is "this sentence, typed in this directory, within the window", and
  that is exactly what it says. A client that sets `MABOLO_SESSION_ID` gets the
  stronger reading for free.
* **Capitalisation does not decide.** A model relaying a sentence may start it
  with a capital where the person did not, and refusing over that teaches
  callers to pad the quote until something matches. The words have to be the
  person's; the shift key does not.
* **A short quote is not a quote.** "yes" appears in half of all prompts. Below
  the floor there is nothing to recognise, so there is nothing to verify.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .git import redact as redact_credentials

#: Where the notes live, one file per session.
STATE_DIRNAME = "mabolo"
PROMPTS_DIRNAME = "prompts"

#: How long a note is any use. A quote is checked against what somebody typed
#: in the same sitting; a week later the sentence may still be on disk, and the
#: person who typed it is long gone.
RETENTION_DAYS = 7

#: How far back the search for a quote looks when the client did not say which
#: session this is. Long enough for a day's work, short enough that yesterday's
#: sentence cannot authorise today's write.
WINDOW_HOURS = 12

#: The shortest quote that can be recognised at all.
MIN_QUOTE_CHARS = 12

#: The longest prompt kept whole. A quote out of a longer one cannot be
#: verified, which is the honest outcome: what was not kept cannot be shown.
MAX_PROMPT_CHARS = 8000

#: When a session's file grows past this it is rewritten with its newest lines.
#: A long session should not turn into an unbounded file nobody reads.
MAX_LINES = 500

#: The environment variable a client may set so that a session can be named.
ENV_SESSION = "MABOLO_SESSION_ID"

#: A session id becomes a file name, and a file system that does not tell
#: `abc` from `ABC` would then let one session's sentences authorise another's
#: writes. Folding the case here costs nothing and closes that.
_SAFE_ID = re.compile(r"[^a-z0-9._-]+")
_SPACE = re.compile(r"\s+")

#: Assignments that carry a secret. A prompt is the one place where a person
#: pastes one by accident, and this file outlives the sentence they pasted it
#: into. A redacted secret can no longer authorise a write that quotes it,
#: which is the right trade in both directions.
_SECRET = re.compile(
    r"""(?ix)
    \b(
        (?:api[_-]?key|secret|token|password|passwd|pwd|authorization|bearer)
        \s*[:=]?\s*
    )
    (\S{8,})
    """
)


@dataclass(frozen=True)
class Prompt:
    """One thing a person typed, as it was written down."""

    at: dt.datetime
    cwd: str
    text: str
    session: str


@dataclass(frozen=True)
class Consent:
    """The evidence behind one write, or the reason there is none."""

    quote: str
    verified: bool
    at: dt.datetime | None = None
    session: str | None = None
    reason: str = ""

    def source_note(self) -> str:
        """One line naming where the sentence was seen, for a commit message."""
        if not self.verified or self.at is None:
            return "unverified quote"
        return f"quoted from a prompt at {self.at.isoformat(timespec='seconds')}"


def state_home() -> Path:
    override = os.environ.get("XDG_STATE_HOME")
    return Path(override) if override else Path.home() / ".local" / "state"


def prompt_dir() -> Path:
    return state_home() / STATE_DIRNAME / PROMPTS_DIRNAME


def session_id(value: object = None) -> str:
    """A file name for a session, from whatever the client called it."""
    raw = value if isinstance(value, str) and value.strip() else os.environ.get(ENV_SESSION, "")
    cleaned = _SAFE_ID.sub("-", str(raw).strip().lower())[:64].strip("-")
    return cleaned or "unnamed"


def redact(text: str) -> str:
    """A prompt with the obvious secrets taken out of it."""
    return _SECRET.sub(lambda m: m.group(1) + "***", redact_credentials(text))


def normalise(text: str) -> str:
    """One line of whitespace, for comparing a quote with a prompt."""
    return _SPACE.sub(" ", text).strip()


def record(text: str, *, cwd: str | Path | None = None, session: object = None,
           at: dt.datetime | None = None, directory: Path | None = None) -> Path | None:
    """Write one prompt down, and never fail the caller over it.

    The prompt hook runs ahead of the person's own sentence and promises not to
    get in the way, so every failure here is swallowed. A note that could not
    be written means a quote that cannot be verified later, which is a refused
    write rather than a lost one.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    where = Path(directory) if directory else prompt_dir()
    name = session_id(session)
    target = where / f"{name}.jsonl"
    moment = at or dt.datetime.now().astimezone()
    line = json.dumps(
        {
            "at": moment.isoformat(timespec="seconds"),
            "cwd": str(cwd or ""),
            "text": redact(text)[:MAX_PROMPT_CHARS],
        },
        ensure_ascii=False,
    )
    try:
        where.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        _trim(target)
        forget_old(where, now=moment)
    except OSError:
        return None
    return target


def _trim(target: Path) -> None:
    """Keep the newest lines of one session's file and drop the rest."""
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return
    if len(lines) <= MAX_LINES:
        return
    target.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")


def forget_old(directory: Path | None = None, *, now: dt.datetime | None = None,
               days: int = RETENTION_DAYS) -> int:
    """Delete session files nothing can be verified against any more."""
    where = Path(directory) if directory else prompt_dir()
    moment = now or dt.datetime.now().astimezone()
    cutoff = moment - dt.timedelta(days=days)
    gone = 0
    try:
        files = sorted(where.glob("*.jsonl"))
    except OSError:
        return 0
    for path in files:
        try:
            touched = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            if touched < cutoff:
                path.unlink()
                gone += 1
        except OSError:
            continue
    return gone


def _same_place(one: str | Path, other: str | Path) -> bool:
    """Whether two paths are the same directory, asked of the file system.

    Comparing the resolved text is wrong on a file system that does not
    distinguish case: the same folder written two ways becomes two places, and
    a sentence the person really typed stops authorising anything. `samefile`
    asks about inodes, which is the question actually being asked. It needs
    both to exist, so the text comparison stays as the fallback.
    """
    try:
        return os.path.samefile(one, other)
    except OSError:
        try:
            return str(Path(one).resolve()) == str(Path(other).resolve())
        except OSError:
            return False


def _read(path: Path) -> list[Prompt]:
    session = path.stem
    out: list[Prompt] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            at = dt.datetime.fromisoformat(str(data.get("at", "")))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.astimezone()
        text = data.get("text")
        if not isinstance(text, str):
            continue
        out.append(Prompt(at=at, cwd=str(data.get("cwd") or ""), text=text, session=session))
    return out


def seen(*, cwd: str | Path | None = None, session: object = None, now: dt.datetime | None = None,
         window_hours: int = WINDOW_HOURS, directory: Path | None = None) -> list[Prompt]:
    """The prompts a quote may be checked against, newest first.

    With a session named, only that session's notes count and the window does
    not apply: the client has said which conversation this is, and that is the
    stronger claim. Without one, the notes are those written in this directory
    inside the window.
    """
    where = Path(directory) if directory else prompt_dir()
    moment = now or dt.datetime.now().astimezone()
    asked = session if isinstance(session, str) else ""
    named = asked.strip() or os.environ.get(ENV_SESSION, "").strip()
    try:
        files = sorted(where.glob("*.jsonl"))
    except OSError:
        return []
    if named:
        wanted = session_id(named)
        files = [f for f in files if f.stem == wanted]
    prompts: list[Prompt] = []
    here = str(Path(cwd).resolve()) if cwd else ""
    cutoff = moment - dt.timedelta(hours=window_hours)
    for path in files:
        for prompt in _read(path):
            if not named:
                if prompt.at < cutoff:
                    continue
                if here and prompt.cwd and not _same_place(prompt.cwd, here):
                    continue
            prompts.append(prompt)
    return sorted(prompts, key=lambda p: p.at, reverse=True)


def check(quote: str, *, cwd: str | Path | None = None, session: object = None,
          now: dt.datetime | None = None, directory: Path | None = None) -> Consent:
    """Whether this sentence was typed by a person, and where it was seen."""
    text = normalise(quote or "")
    if len(text) < MIN_QUOTE_CHARS:
        return Consent(
            quote=text,
            verified=False,
            reason=(
                f"the quote is shorter than {MIN_QUOTE_CHARS} characters, "
                "which is not enough of a sentence to recognise"
            ),
        )
    wanted = text.casefold()
    for prompt in seen(cwd=cwd, session=session, now=now, directory=directory):
        if wanted in normalise(prompt.text).casefold():
            return Consent(quote=text, verified=True, at=prompt.at, session=prompt.session)
    return Consent(
        quote=text,
        verified=False,
        reason=(
            "no prompt on this machine holds that sentence. The quote has to be what the "
            "person typed, word for word, in this session"
        ),
    )
