"""Talking to Git, and turning its failures into sentences.

Git is the database, so a failure here has to arrive as something a person can
act on rather than as a stack trace. A remote URL may carry a password, so it
never reaches a message unredacted.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .errors import MaboloError

#: A URL may carry a password. It is the user's own, but it has no business in
#: a message that gets pasted into an issue.
_CREDENTIALS = re.compile(r"(?<=//)[^/@\s]+(?=@)")

TIMEOUT_SECONDS = 30


def redact(text: str) -> str:
    """A message with any credentials in a URL replaced."""
    return _CREDENTIALS.sub("***", text)


def available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def run(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git in `root`. Failures become `MaboloError`, never a traceback."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or "").strip().splitlines()
        detail = redact(message[-1]) if message else f"exit status {exc.returncode}"
        raise MaboloError(f"git {args[0]} failed: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MaboloError(f"git {args[0]} did not finish within {TIMEOUT_SECONDS} seconds") from exc
    except OSError as exc:
        raise MaboloError(f"git could not be run: {exc.strerror}") from exc


def identity_missing(root: Path) -> bool:
    result = run(root, "config", "user.email", check=False)
    return result.returncode != 0 or not result.stdout.strip()
