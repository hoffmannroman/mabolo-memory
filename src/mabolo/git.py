"""Talking to Git, and turning its failures into sentences.

Git is the database, so a failure here has to arrive as something a person can
act on rather than as a stack trace. A remote URL may carry a password, so it
never reaches a message unredacted.

Two rules that are not obvious:

* **The repository has to be inside the vault.** `.git` can be a file pointing
  somewhere else entirely, and then a commit writes into a repository nobody
  named, running that repository's hooks.
* **The user's index is never used.** Committing through the shared index picks
  up whatever the person had staged. Every commit here is built in an index of
  its own, seeded from HEAD, so it contains what it says it contains and nothing
  from the working session around it.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
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


def run(root: Path, *args: str, check: bool = True, index: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run git in `root`. Failures become `MaboloError`, never a traceback."""
    environment = dict(os.environ)
    # A stray GIT_DIR or GIT_WORK_TREE in the environment points the command at
    # a repository the caller never named, which is exactly what the containment
    # check above is meant to prevent.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY"):
        environment.pop(name, None)
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=TIMEOUT_SECONDS,
            env=environment,
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


def repository_dir(root: Path) -> Path | None:
    """The directory Git would actually write to, or None if there is no repository."""
    result = run(root, "rev-parse", "--absolute-git-dir", check=False)
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return Path(text) if text else None


def is_repository_inside(root: Path) -> bool:
    """True when `root` holds a repository that writes inside `root`.

    `.git` can be a file containing `gitdir: /somewhere/else`, which a plain
    existence check reads as a perfectly ordinary repository. Committing then
    writes into that other repository and runs its hooks.
    """
    git_dir = repository_dir(root)
    if git_dir is None:
        return False
    try:
        return git_dir.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def commit_paths(root: Path, paths: list[str], message: str) -> str | None:
    """Commit exactly these paths in an index of their own.

    Returns the short revision, or None when the paths held nothing new. The
    person's own staged work is not part of it and stays staged.
    """
    # Inside the repository's own directory: never part of the working tree, and
    # on the same filesystem as the objects it writes.
    scratch_parent = repository_dir(root) or root
    with tempfile.TemporaryDirectory(dir=scratch_parent) as scratch:
        index = Path(scratch) / "index"
        # Seed from HEAD so that a commit built here keeps every file the last
        # commit had. An empty index would record the rest of the tree as deleted.
        if run(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
            run(root, "read-tree", "HEAD", index=index)
        run(root, "add", "--", *paths, index=index)
        staged = run(root, "diff", "--cached", "--name-only", check=False, index=index)
        if not staged.stdout.strip():
            return None
        run(root, "commit", "--no-verify", "-m", message, index=index)
    # The commit went through an index of its own, so the person's index still
    # holds the state from before it and would show these files as deleted.
    # A reset limited to these paths lines them up with the new HEAD and leaves
    # everything else, staged or not, exactly as it was.
    run(root, "reset", "--quiet", "--", *paths, check=False)
    return run(root, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or None
