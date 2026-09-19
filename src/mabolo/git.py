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


# Everything below is what a mutation needs: the state of the branch, a commit
# built without a working tree, and a push that cannot overwrite somebody else.


def current_branch(root: Path) -> str | None:
    """The branch HEAD is on, or None when it is detached."""
    result = run(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    name = result.stdout.strip()
    return name if result.returncode == 0 and name else None


def rev(root: Path, ref: str) -> str | None:
    """The commit a ref points at, or None when there is no such ref."""
    result = run(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    text = result.stdout.strip()
    return text if result.returncode == 0 and text else None


def has_remote(root: Path, name: str) -> bool:
    result = run(root, "remote", "get-url", name, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def mutation_in_progress(root: Path) -> str | None:
    """The name of the Git operation already running here, or None.

    The one condition that stops a write. Committing into a half finished merge
    produces a commit nobody asked for, and the way out of it is not something
    a memory tool should decide for the person.
    """
    git_dir = repository_dir(root)
    if git_dir is None:
        return None
    for marker, what in (
        ("MERGE_HEAD", "a merge"),
        ("rebase-merge", "a rebase"),
        ("rebase-apply", "a rebase"),
        ("CHERRY_PICK_HEAD", "a cherry-pick"),
        ("REVERT_HEAD", "a revert"),
        ("BISECT_LOG", "a bisect"),
    ):
        if (git_dir / marker).exists():
            return what
    return None


def foreign_changes(root: Path) -> list[str]:
    """Paths a person changed in the vault without going through Mabolo.

    Untracked files count. Somebody writing a new note in an editor is the
    normal way a vault grows, and leaving it out of the commit would mean the
    next mutation carries it along silently under a message about something
    else.
    """
    result = run(root, "status", "--porcelain", "-z", "--untracked-files=all", check=False)
    if result.returncode != 0:
        return []
    fields = [f for f in result.stdout.split("\0") if f]
    paths: list[str] = []
    skip_next = False
    for field in fields:
        if skip_next:
            skip_next = False
            continue
        status, _, path = field.partition(" ")
        # A rename reports the old path in the field after it.
        if status.startswith("R") or status.startswith("C"):
            skip_next = True
        if len(field) > 3:
            paths.append(field[3:])
    return paths


def is_ancestor(root: Path, earlier: str, later: str) -> bool:
    return run(root, "merge-base", "--is-ancestor", earlier, later, check=False).returncode == 0


def fast_forward(root: Path, ref: str) -> bool:
    """Move the branch forward to `ref`, or leave everything as it was.

    `--ff-only` is the whole point: it refuses rather than merging, and it
    refuses rather than overwriting a file somebody is working on.
    """
    return run(root, "merge", "--ff-only", "--quiet", ref, check=False).returncode == 0


def fetch(root: Path, remote: str, branch: str) -> bool:
    """Ask the remote where it is. False when it could not be reached."""
    result = run(root, "fetch", "--quiet", remote, branch, check=False)
    return result.returncode == 0


def hash_object(root: Path, data: bytes) -> str:
    """Write these bytes into the object store and return their id."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "hash-object", "-w", "--stdin"],
            input=data,
            capture_output=True,
            check=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise MaboloError(f"git hash-object failed: {redact(detail[-1]) if detail else exc.returncode}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MaboloError("git hash-object did not finish in time") from exc
    except OSError as exc:
        raise MaboloError(f"git could not be run: {exc.strerror}") from exc
    return result.stdout.decode("utf-8").strip()


def build_commit(root: Path, base: str | None, changes: dict[str, bytes | None], message: str) -> str:
    """One commit, built from `base`, without touching the working tree.

    The bytes go straight into the object store and the tree is assembled in an
    index of its own. Nothing is written into the directory the person has
    open, and nothing is written there later either unless this commit reaches
    the remote, which is what makes a refused push cost nothing.
    """
    if identity_missing(root):
        raise MaboloError(
            "git has no user.email here, so a commit would have no author. "
            "Set one with `git config --global user.email`."
        )
    scratch_parent = repository_dir(root) or root
    with tempfile.TemporaryDirectory(dir=scratch_parent) as scratch:
        index = Path(scratch) / "index"
        if base:
            run(root, "read-tree", base, index=index)
        for path, data in changes.items():
            if data is None:
                run(root, "update-index", "--force-remove", "--", path, index=index)
                continue
            blob = hash_object(root, data)
            run(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", index=index)
        tree = run(root, "write-tree", index=index).stdout.strip()
    parents = ["-p", base] if base else []
    return run(root, "commit-tree", tree, *parents, "-m", message).stdout.strip()


def push(root: Path, remote: str, branch: str, commit: str, expect: str | None) -> str:
    """Push this commit, refusing to overwrite anything the lease did not cover.

    Three answers, because three different things happen and a caller that
    cannot tell them apart will report the wrong one: `pushed`, `rejected`
    when the remote has moved, and `unreachable` when there was nobody to ask.
    """
    target = f"refs/heads/{branch}"
    lease = [f"--force-with-lease={target}:{expect}"] if expect else []
    result = run(root, "push", *lease, remote, f"{commit}:{target}", check=False)
    if result.returncode == 0:
        return "pushed"
    text = f"{result.stderr}\n{result.stdout}".lower()
    if "stale info" in text or "rejected" in text or "non-fast-forward" in text:
        return "rejected"
    return "unreachable"


def update_ref(root: Path, ref: str, new: str, old: str | None) -> None:
    """Move a branch, and only from where it was last seen."""
    run(root, "update-ref", ref, new, old or "")


def reset_paths(root: Path, paths: list[str]) -> None:
    """Line the person's index up with HEAD for these paths and nothing else."""
    if paths:
        run(root, "reset", "--quiet", "--", *paths, check=False)
