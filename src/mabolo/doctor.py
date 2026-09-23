"""`mabolo doctor`: the findings first, and a count at the end that adds up.

This is the command somebody runs when they suspect something is wrong, so the
shape of the report is the whole design. A report nobody reads to the end is a
report that hid its finding: so every line in the middle is something to act
on, one line each, and the checks that found nothing are folded into the
closing line rather than printed as a wall of reassurance.

**A check that could not run is never counted as clean.** That single rule is
what the rest of this module is arranged around, because the opposite is the
failure this whole project exists against: an unreachable remote reported as
"in step", an unread folder reported as "no problems", a vault nobody could
look at reported as healthy. Every check is therefore in one of three states
and never in a fourth: it found something, it ran and found nothing, or it did
not run and says why. The closing line prints all three numbers.

The scope is a closed list, settled at the desk rather than grown by whoever
touched this file last. Six groups, in the order a person can act on them:
configuration and wiring, the repository, the remote, the history, the content,
and what is waiting for a decision. Dead links, duplicate candidates, an
expired `stale_after` on its own, entries nobody has touched in a year and
usage counts are deliberately *not* here: they are a lint pass, they fire on a
healthy vault, and a doctor whose output is normal is a doctor nobody reads.

Two things arrive as parameters rather than as imports, because they belong to
modules other people are building and a doctor that cannot run until every
other part is finished is a doctor that gets written last and used never:

* `clients` names the client configuration files that should point at Mabolo.
* `pending` is the list of proposals waiting in the inbox, filled in by the
  reader for the `inbox` orphan branch. Doctor never reads that branch itself.

Both default to `None`, which means "nobody filled this in", and that is
reported as a check that did not run. An empty list is a different answer: it
means somebody looked and there was nothing. Collapsing those two would be the
reassuring lie again, in the one place it is easiest to tell.

This module prints nothing and picks no exit code. It returns a `Report`, and
`Report.ok` is false as soon as there is a finding *or* a check that did not
run, which is what a caller exits 1 on.
"""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from . import consent, drift, git, health, proposal, seen, validate, write
from .config import CONFIG_MODE, Config, default_config_path
from .errors import MaboloError
from .vault import Vault
from .query import fold
from .schema import FIXED_AREAS, INDEX_FILE
from .vault import LEDGER_FILE, STATE_DIR

#: The six groups, in the order they are reported. The order is the report:
#: a broken configuration explains a silent remote, and a person reading top
#: down should meet the cause before the symptom.
CONFIGURATION = "configuration"
REPOSITORY = "repository"
REMOTE = "remote"
HISTORY = "history"
CONTENT = "content"
WAITING = "waiting"
GROUPS = (CONFIGURATION, REPOSITORY, REMOTE, HISTORY, CONTENT, WAITING)

#: What a configuration file's mode should be. From `config`, which writes it.
EXPECTED_MODE = CONFIG_MODE

#: How long a proposal may wait before waiting is the problem. It matches the
#: expiry the state machine gives a proposal: past this, nobody is going to
#: decide it, and it will go away on its own without ever being read.
#: How long a proposal waits before it is litter. The number lives with the
#: proposal, not here: a diagnosis that carried its own copy would go on
#: reporting the old one after somebody moved it.
PROPOSAL_DAYS = proposal.EXPIRES_AFTER_DAYS

#: The kinds of commit that mark where Mabolo's own history begins. Before one
#: of these, the history belonged to somebody else and saying anything about it
#: would be an accusation rather than a check.
BOUNDARY_KINDS = ("adopt", "import")

#: The kinds a path list means anything for. A hand edit may add a picture or a
#: folder, an import writes the whole vault, an adopt writes the skeleton, and a
#: revert touches whatever the commit it undoes touched. Checking those four
#: against a list of what Mabolo writes would make every one of them a finding,
#: which is a report crying wolf about its own tool. Spelled out rather than
#: derived, so that a new kind cannot arrive without somebody deciding whether
#: the check applies to it.
PATH_KINDS = ("write", "edit", "forget", "journal", "approve", "reject")

#: Splits a client configuration into candidate command words. It is deliberately
#: crude: these files are JSON, TOML and shell in three different shapes, and a
#: parser per client is a parser per client to keep working.
_WORDS = re.compile(r"[\s\"',:;=\[\]{}()]+")


@dataclass(frozen=True)
class Pending:
    """One proposal waiting for a decision, as doctor needs to see it.

    Its own small shape rather than the inbox's, so that this module does not
    have to be rewritten every time the proposal format gains a field, and so
    that the inbox reader and the doctor can be built in either order.
    """

    name: str
    at: dt.datetime


ClientSource = Sequence[Path | str] | Callable[[], Sequence[Path | str]] | None
PendingSource = Sequence[Pending] | Callable[[], Sequence[Pending]] | None


@dataclass(frozen=True)
class Finding:
    """One thing to act on, addressed to the person who has to act on it."""

    group: str
    message: str
    #: Lines that belong under the finding, such as the individual errors of a
    #: file that does not validate. The finding itself stays one line, because
    #: that is what makes a list of them readable.
    detail: tuple[str, ...] = ()

    def render(self) -> str:
        return "\n".join([f"{self.group}: {self.message}", *(f"    {d}" for d in self.detail)])


@dataclass(frozen=True)
class Check:
    """One thing doctor looked at, and what came of looking.

    `reason` is filled in exactly when the check did not run, and a check with
    a reason is never clean no matter how few findings it has. Keeping the
    reason on the check rather than in a separate list is what makes that
    impossible to get wrong by accident.
    """

    name: str
    group: str
    findings: tuple[Finding, ...] = ()
    reason: str = ""

    @property
    def ran(self) -> bool:
        return not self.reason

    @property
    def clean(self) -> bool:
        return self.ran and not self.findings


@dataclass
class Report:
    """What doctor found, in the order it is printed.

    The checks stay in the order they were made, which is the order of
    `GROUPS`, so rendering never has to sort and two runs against the same
    vault cannot disagree about what came first.
    """

    checks: list[Check] = field(default_factory=list)

    @property
    def findings(self) -> list[Finding]:
        return [f for check in self.checks for f in check.findings]

    @property
    def not_run(self) -> list[Check]:
        return [check for check in self.checks if not check.ran]

    @property
    def clean(self) -> list[Check]:
        return [check for check in self.checks if check.clean]

    @property
    def ok(self) -> bool:
        """False as soon as anything was found or anything was not looked at."""
        return not self.findings and not self.not_run

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def render(self) -> str:
        lines = [f.render() for f in self.findings]
        lines += [f"not run: {check.name}, {check.reason}" for check in self.not_run]
        lines.append(
            f"{len(self.findings)} findings, {len(self.clean)} checks clean, "
            f"{len(self.not_run)} not run"
        )
        return "\n".join(lines)


def writes(path: str) -> bool:
    """Whether a path in the vault is one Mabolo itself could have written.

    Mabolo writes Markdown, the `.gitignore` that `init` lays down, the eval
    cases that travel with the entries, and the ledger of answered proposals. A commit that carries Mabolo's
    trailer and touches anything else was either hand made under a borrowed
    signature or produced by something that copied the trailer, and either way
    the trailer is no longer evidence of what the commit is.
    """
    if path == ".gitignore":
        return True
    if path == f"{STATE_DIR}/eval" or path.startswith(f"{STATE_DIR}/eval/"):
        return True
    if path == LEDGER_FILE:
        # The ledger. Left out of this list, every approve and every reject was
        # reported as a commit claiming Mabolo and touching what it never
        # writes, which is the report crying wolf about its own tool.
        return True
    return path.endswith(".md") and not path.startswith(".")


def examine(
    config: Config | None = None,
    *,
    config_path: Path | None = None,
    root: Path | None = None,
    remote: str = "origin",
    clients: ClientSource = None,
    executable: str | None = None,
    pending: PendingSource = None,
    project: str | None = None,
    repository: Path | None = None,
    now: dt.datetime | None = None,
) -> Report:
    """Look at everything on the list once, and report what came of it.

    `config` may be absent, in which case the file at `config_path` is read
    here and a failure to read it becomes the first finding rather than an
    exception: a doctor that raises on the first thing it was called to
    diagnose is a doctor that only works on a healthy machine.

    `project` and `repository` are the session's, not the vault's. Without
    them no anchor can be held against anything, and that is reported as a
    check that did not run rather than as a clean bill for every anchor in the
    vault.
    """
    moment = now or dt.datetime.now().astimezone()
    report = Report()

    where = Path(config_path) if config_path else (config.path if config else default_config_path())
    config, config_finding = _load(config, where)
    report.add(Check("config.file", CONFIGURATION, findings=config_finding))

    root = Path(root) if root else (config.vault if config else None)
    report.add(_vault_check(root))
    report.add(_git_check())
    report.add(_identity_check(root))
    for check in _wiring_checks(clients, executable):
        report.add(check)

    usable = _usable_repository(root)
    for check in _repository_checks(root, usable, moment):
        report.add(check)
    for check in _remote_checks(root, remote, usable):
        report.add(check)
    for check in _history_checks(root, usable):
        report.add(check)
    for check in _content_checks(root, config, project, repository, moment):
        report.add(check)
    for check in _waiting_checks(pending, moment):
        report.add(check)
    return report


# Configuration and wiring.


def _load(config: Config | None, where: Path) -> tuple[Config | None, tuple[Finding, ...]]:
    """The configuration and what is wrong with the file it came from."""
    try:
        present = where.exists()
    except OSError as exc:
        return config, (_f(CONFIGURATION, f"{where} cannot be looked at ({exc.strerror})"),)
    if not present:
        return config, (
            _f(CONFIGURATION, f"there is no configuration at {where}. Run `mabolo init`"),
        )

    findings: list[Finding] = []
    try:
        mode = stat.S_IMODE(where.stat().st_mode)
        if mode != EXPECTED_MODE:
            findings.append(
                _f(
                    CONFIGURATION,
                    f"{where} is mode {mode:04o}, not {EXPECTED_MODE:04o}. It names your vault "
                    "and may hold a remote URL with a password in it",
                )
            )
    except OSError as exc:
        findings.append(_f(CONFIGURATION, f"{where} cannot be looked at ({exc.strerror})"))

    if config is None:
        try:
            config = Config.load(where)
        except MaboloError as exc:
            findings.append(_f(CONFIGURATION, f"the configuration cannot be read: {exc}"))
    return config, tuple(findings)


def _vault_check(root: Path | None) -> Check:
    if root is None:
        return Check(
            "config.vault",
            CONFIGURATION,
            reason="no vault was named, because no configuration could be read",
        )
    try:
        there = root.is_dir()
    except OSError as exc:
        return Check("config.vault", CONFIGURATION, reason=f"{root} cannot be looked at ({exc.strerror})")
    if not there:
        return Check(
            "config.vault",
            CONFIGURATION,
            findings=(_f(CONFIGURATION, f"the vault at {root} is not there"),),
        )
    return Check("config.vault", CONFIGURATION)


def _git_check() -> Check:
    if not git.available():
        return Check(
            "config.git",
            CONFIGURATION,
            findings=(
                _f(
                    CONFIGURATION,
                    "git is not installed, so the vault has no history and nothing can be pushed",
                ),
            ),
        )
    return Check("config.git", CONFIGURATION)


def _identity_check(root: Path | None) -> Check:
    if root is None or not root.is_dir():
        return Check("config.identity", CONFIGURATION, reason="there is no vault to ask git in")
    if not git.available():
        return Check("config.identity", CONFIGURATION, reason="git is not installed")
    try:
        missing = git.identity_missing(root)
    except MaboloError as exc:
        return Check("config.identity", CONFIGURATION, reason=f"git could not be asked ({exc})")
    if missing:
        return Check(
            "config.identity",
            CONFIGURATION,
            findings=(
                _f(
                    CONFIGURATION,
                    "git has no user.email here, so a commit would have no author. "
                    "Set one with `git config --global user.email`",
                ),
            ),
        )
    return Check("config.identity", CONFIGURATION)


def _wiring_checks(clients: ClientSource, executable: str | None) -> list[Check]:
    """Whether the clients that were wired up still point at this Mabolo.

    Two checks rather than one, because they fail for different reasons and one
    of them can be answered without knowing where Mabolo lives. A wiring file
    that has gone is a finding whatever else is true; whether the command in it
    is *this* Mabolo cannot be decided when the executable itself cannot be
    located, and that is a check that did not run.
    """
    paths = _resolve(clients)
    if paths is None:
        return [
            Check(name, CONFIGURATION, reason="no client wiring was supplied to look at")
            for name in ("wiring.present", "wiring.executable")
        ]

    present: list[Finding] = []
    commands: list[tuple[Path, list[str]]] = []
    for raw in paths:
        path = Path(raw)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            present.append(_f(CONFIGURATION, f"the wiring at {path} is gone"))
            continue
        except (OSError, UnicodeDecodeError) as exc:
            present.append(
                _f(CONFIGURATION, f"the wiring at {path} cannot be read ({exc}), so it was not checked")
            )
            continue
        found = [word for word in _WORDS.split(text) if word and Path(word).name == "mabolo"]
        if not found:
            present.append(_f(CONFIGURATION, f"the wiring at {path} names no mabolo command"))
            continue
        commands.append((path, found))

    checks = [Check("wiring.present", CONFIGURATION, findings=tuple(present))]
    expect = executable or shutil.which("mabolo")
    if not expect:
        checks.append(
            Check(
                "wiring.executable",
                CONFIGURATION,
                reason="the mabolo executable could not be located, so no wiring could be compared with it",
            )
        )
        return checks
    wrong = tuple(
        _f(
            CONFIGURATION,
            f"the wiring at {path} points at {', '.join(sorted(set(found)))}, not at {expect}",
        )
        for path, found in commands
        if not any(_points_here(word, expect) for word in found)
    )
    checks.append(Check("wiring.executable", CONFIGURATION, findings=wrong))
    if paths:
        # Registered only when somebody wired a client up, the same way the
        # remote group is absent from a vault that has no remote: nothing is
        # unknown about hooks nobody asked for.
        checks.append(_hooks_seen_check())
        checks.append(_hooks_failed_check())
    return checks


def _hooks_failed_check() -> Check:
    """Whether a hook gave up since the last session start reported it.

    Read without using it up: the session start is the one that reports and
    forgets, and a person running doctor to find out why must still see it.
    """
    trouble = health.pending()
    findings = (
        (
            _f(
                CONFIGURATION,
                f"a hook gave up: {health.summary(trouble)}",
                tuple(f"{item.get('at', '')}  {item['hook']}: {item.get('error', '')}" for item in trouble[-5:]),
            ),
        )
        if trouble
        else ()
    )
    return Check("wiring.failures", CONFIGURATION, findings=findings)


def _hooks_seen_check() -> Check:
    """Whether a hook has ever actually run on this machine.

    A wired client whose hooks are never discovered starts the MCP server and
    runs none of them: no prompt notes, no session context, no rule raised by a
    file, and no error anywhere. That is the quietest failure this tool has, and
    the one thing that can speak to it is whether a note has ever arrived. It is
    reported as a check that could not be run rather than as a clean one,
    because "nothing has been seen yet" is not "it works", and the person's
    next session answers it either way.
    """
    notes = consent.prompt_dir()
    try:
        arrived = any(notes.glob("*.jsonl"))
    except OSError:
        arrived = False
    if arrived:
        return Check("wiring.seen", CONFIGURATION)
    return Check(
        "wiring.seen",
        CONFIGURATION,
        reason=(
            "the hooks are wired and no prompt note has ever arrived, so nothing can say "
            "whether they run. Your next session in a wired client answers this"
        ),
    )


def _points_here(word: str, expect: str) -> bool:
    """Whether a command word names the same executable as `expect`.

    A bare name is looked up on PATH, because that is what the client will do
    with it, and both ends are resolved: two names for one file are one
    executable, and a wiring that points at a deleted virtual environment is
    not this one.
    """
    found = word if os.sep in word else shutil.which(word)
    if not found:
        return False
    try:
        return os.path.realpath(found) == os.path.realpath(expect)
    except OSError:
        return False


# The repository.


def _usable_repository(root: Path | None) -> str:
    """Empty when Git can be asked about this vault, otherwise the reason not."""
    if root is None:
        return "no vault was named"
    if not root.is_dir():
        return f"the vault at {root} is not there"
    if not git.available():
        return "git is not installed"
    try:
        if not git.is_repository_inside(root):
            return "the vault is not a Git repository"
    except MaboloError as exc:
        return f"git could not be asked ({exc})"
    return ""


def _repository_checks(root: Path | None, blocked: str, now: dt.datetime) -> list[Check]:
    names = ("repository.operation", "repository.head", "repository.lock")
    if blocked:
        return [Check(name, REPOSITORY, reason=blocked) for name in names]
    assert root is not None

    checks: list[Check] = []
    try:
        busy = git.mutation_in_progress(root)
    except MaboloError as exc:
        checks.append(Check(names[0], REPOSITORY, reason=f"git could not be asked ({exc})"))
    else:
        found = (
            (_f(REPOSITORY, f"{busy} is running in the vault, and nothing can be written until it ends"),)
            if busy
            else ()
        )
        checks.append(Check(names[0], REPOSITORY, findings=found))

    try:
        branch = git.current_branch(root)
    except MaboloError as exc:
        checks.append(Check(names[1], REPOSITORY, reason=f"git could not be asked ({exc})"))
    else:
        found = (
            ()
            if branch
            else (_f(REPOSITORY, "HEAD is detached, so there is no branch to write to"),)
        )
        checks.append(Check(names[1], REPOSITORY, findings=found))

    checks.append(_lock_check(root, now))
    return checks


def _lock_check(root: Path, now: dt.datetime) -> Check:
    """Whether a write has been holding this vault's lock longer than anyone waits.

    The lock file is asked for by name through `write`, never rebuilt from the
    same digest here: two places that derive one path eventually derive two.

    **The file lying there means nothing.** `write.lock` creates it and never
    removes it, so every vault that has ever been written to has one, and a
    check on its existence or its age alone would fire on every healthy vault
    until people stopped reading the report. What says something is that the
    lock is held right now, and has been for longer than `LOCK_SECONDS`, which
    is the longest any writer would have waited for it. That is a write that
    died holding it, or one that is stuck.
    """
    path = write.lock_path(root)
    try:
        if not path.exists():
            return Check("repository.lock", REPOSITORY)
        held = _held(path)
        age = now.timestamp() - path.stat().st_mtime
    except OSError as exc:
        return Check("repository.lock", REPOSITORY, reason=f"the lock cannot be looked at ({exc.strerror})")
    if held is None:
        return Check("repository.lock", REPOSITORY, reason="this file system does not answer about locks")
    if not held:
        return Check("repository.lock", REPOSITORY)
    if age < write.LOCK_SECONDS:
        # A write is simply running, which is what the lock is for.
        return Check("repository.lock", REPOSITORY)
    return Check(
        "repository.lock",
        REPOSITORY,
        findings=(
            _f(
                REPOSITORY,
                f"a write has held the lock on this vault for {age:.0f} seconds, over the "
                f"{write.LOCK_SECONDS:g} any writer waits. Something died holding it",
            ),
        ),
    )


def _held(path: Path) -> bool | None:
    """Whether somebody holds the lock. None when the question cannot be asked."""
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            return None
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        os.close(handle)


# The remote.


def _remote_checks(root: Path | None, remote: str, blocked: str) -> list[Check]:
    """Whether this clone and the remote still say the same thing.

    Nothing is fetched. A fetch moves refs in the person's own repository, and
    a diagnosis that changes what it is diagnosing is not one; `ls-remote` asks
    the same question and writes nothing.

    A vault with no remote registers no checks at all here rather than three
    clean ones. There is no remote to be in step with, and counting three
    successes for a question nobody asked is the same inflation as counting an
    unreachable one as clean.
    """
    names = ("remote.reachable", "remote.unpushed", "remote.behind")
    if blocked:
        return [Check(name, REMOTE, reason=blocked) for name in names]
    assert root is not None
    try:
        if not git.has_remote(root, remote):
            return []
        branch = git.current_branch(root)
    except MaboloError as exc:
        return [Check(name, REMOTE, reason=f"git could not be asked ({exc})") for name in names]
    if branch is None:
        return [Check(name, REMOTE, reason="HEAD is detached, so there is no branch to compare") for name in names]

    target = f"refs/heads/{branch}"
    asked = _git(root, "ls-remote", "--exit-code", remote, target)
    if asked is None or asked.returncode not in (0, 2):
        return [
            Check(
                names[0],
                REMOTE,
                findings=(
                    _f(REMOTE, f"the remote {remote} is configured and could not be reached"),
                ),
            ),
            Check(names[1], REMOTE, reason=f"the remote {remote} could not be reached"),
            Check(names[2], REMOTE, reason=f"the remote {remote} could not be reached"),
        ]

    checks = [Check(names[0], REMOTE)]
    there = asked.stdout.split("\t")[0].strip() if asked.returncode == 0 else ""
    here = git.rev(root, "HEAD")
    if here is None:
        return checks + [
            Check(name, REMOTE, reason="this clone has no commit yet") for name in names[1:]
        ]
    if not there:
        return checks + [
            Check(
                names[1],
                REMOTE,
                findings=(
                    _f(REMOTE, f"{remote} has no branch {branch}, so nothing on it has been pushed"),
                ),
            ),
            Check(names[2], REMOTE),
        ]
    if _rc(root, "cat-file", "-e", f"{there}^{{commit}}") != 0:
        # The remote's tip is not in this object store, so it was never fetched.
        # How far ahead cannot be counted, and guessing "one" would be a number
        # somebody acts on.
        return checks + [
            Check(names[1], REMOTE, reason=f"the tip of {remote}/{branch} has never been fetched here"),
            Check(
                names[2],
                REMOTE,
                findings=(
                    _f(REMOTE, f"{remote}/{branch} holds commits this clone has never seen"),
                ),
            ),
        ]

    ahead = _count(root, f"{there}..{here}")
    behind = _count(root, f"{here}..{there}")
    if ahead is None or behind is None:
        return checks + [
            Check(name, REMOTE, reason="git could not count the commits between them")
            for name in names[1:]
        ]
    checks.append(
        Check(
            names[1],
            REMOTE,
            findings=(
                (_f(REMOTE, f"{ahead} commits on {branch} are not on {remote}"),) if ahead else ()
            ),
        )
    )
    checks.append(
        Check(
            names[2],
            REMOTE,
            findings=(
                (_f(REMOTE, f"{remote}/{branch} is {behind} commits ahead of this clone"),)
                if behind
                else ()
            ),
        )
    )
    return checks


def _count(root: Path, span: str) -> int | None:
    result = _git(root, "rev-list", "--count", span)
    if result is None or result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


# The history.


@dataclass(frozen=True)
class _Commit:
    sha: str
    parents: tuple[str, ...]
    kind: str
    paths: tuple[str, ...]


def _history_checks(root: Path | None, blocked: str) -> list[Check]:
    """Where Mabolo's own history begins, and what has happened in it since.

    The boundary is the **oldest** adopt or import commit, not the newest. A
    second import later in the history does not stop the commits before it from
    being Mabolo's own; what the first one marks is the moment the vault stopped
    being somebody else's.

    Without a boundary nothing is classified at all. A vault adopted from
    elsewhere would otherwise produce one red line per commit it arrived with,
    and a report that is entirely red on a healthy vault is one people learn to
    close.
    """
    names = ("history.boundary", "history.origin", "history.paths")
    if blocked:
        return [Check(name, HISTORY, reason=blocked) for name in names]
    assert root is not None

    commits = _log(root)
    if commits is None:
        return [Check(name, HISTORY, reason="the history could not be read") for name in names]
    if not commits:
        return [Check(name, HISTORY, reason="the repository has no commit yet") for name in names]

    boundary = None
    for position, commit in enumerate(commits):
        if commit.kind in BOUNDARY_KINDS:
            boundary = position
    if boundary is None:
        return [
            Check(
                names[0],
                HISTORY,
                findings=(
                    _f(
                        HISTORY,
                        "no adopt commit, so there is no point where Mabolo's own history starts",
                    ),
                ),
            ),
            Check(names[1], HISTORY, reason="there is no adopt commit to classify anything after"),
            Check(names[2], HISTORY, reason="there is no adopt commit to classify anything after"),
        ]

    before = len(commits) - boundary - 1
    checks = [
        Check(
            names[0],
            HISTORY,
            findings=(
                (_f(HISTORY, f"{before} commits before Mabolo, not classified"),) if before else ()
            ),
        )
    ]
    after = commits[:boundary]
    checks.append(
        Check(
            names[1],
            HISTORY,
            findings=tuple(
                _f(
                    HISTORY,
                    f"{c.sha[:12]} is a merge, and Mabolo never merges"
                    if len(c.parents) > 1
                    else f"{c.sha[:12]} carries no Mabolo trailer",
                )
                for c in after
                if len(c.parents) > 1 or not c.kind
            ),
        )
    )
    checks.append(
        Check(
            names[2],
            HISTORY,
            findings=tuple(
                _f(
                    HISTORY,
                    f"{c.sha[:12]} says it is Mabolo's and touches "
                    f"{', '.join(p for p in c.paths if not writes(p))}, which Mabolo never writes",
                )
                for c in after
                if c.kind in PATH_KINDS
                and len(c.parents) <= 1
                and any(not writes(p) for p in c.paths)
            ),
        )
    )
    return checks


def _log(root: Path) -> list[_Commit] | None:
    """Every commit, newest first, with its parents, its kind and its paths.

    One call rather than one per commit: a vault with a year in it would
    otherwise spend a second per hundred commits asking Git the same question
    in a loop.
    """
    result = _git(
        root,
        "log",
        "--format=%x1e%H%x1f%P%x1f%(trailers:key=Mabolo,valueonly,separator=%x2C)%x1f",
        "--name-only",
    )
    if result is None:
        return None
    if result.returncode != 0:
        # An empty repository answers this way, and so does one Git refuses.
        return [] if not result.stdout.strip() else None
    out: list[_Commit] = []
    for record in result.stdout.split("\x1e"):
        if not record.strip():
            continue
        head, _, rest = record.partition("\x1f")
        parents, _, rest = rest.partition("\x1f")
        trailer, _, rest = rest.partition("\x1f")
        value = trailer.split(",")[0].strip()
        out.append(
            _Commit(
                sha=head.strip(),
                parents=tuple(parents.split()),
                kind=value.split()[0] if value else "",
                paths=tuple(line for line in rest.splitlines() if line.strip()),
            )
        )
    return out


# The content.


def _content_checks(
    root: Path | None,
    config: Config | None,
    project: str | None,
    repository: Path | None,
    now: dt.datetime,
) -> list[Check]:
    names = ("content.validates", "content.folding", "content.index", "content.anchors")
    if root is None:
        return [Check(name, CONTENT, reason="no vault was named") for name in names]
    if not root.is_dir():
        return [Check(name, CONTENT, reason=f"the vault at {root} is not there") for name in names]

    areas = tuple(config.areas) if config else FIXED_AREAS
    try:
        found = validate.validate_vault(root, areas)
    except MaboloError as exc:
        return [Check(name, CONTENT, reason=f"the vault could not be read ({exc})") for name in names]

    per_file: dict[str, list[str]] = {}
    for problem in found.errors:
        per_file.setdefault(str(_relative(root, problem.path) if problem.path else root), []).append(
            problem.message
        )
    broken = [
        _f(CONTENT, f"{where} does not validate", tuple(sorted(messages)))
        for where, messages in sorted(per_file.items())
    ]
    if found.warnings:
        # One count, not one line each. A warning breaks nothing, and a doctor
        # that lists them all buries the errors above them in its own output.
        broken.append(
            _f(CONTENT, f"{len(found.warnings)} warnings, which break nothing. See `mabolo validate`")
        )
    checks = [Check(names[0], CONTENT, findings=tuple(broken))]
    checks.append(_folding_check(root, found.entries))
    checks.append(_index_check(root, found.entries, areas))
    checks.append(_anchor_check(found.entries, project, repository, now))
    return checks


def _folding_check(root: Path, entries: list) -> Check:
    """Two entries that are one name to everything that resolves names.

    The validator catches two entries spelled identically. This catches two
    spelled differently that fold to one, which is what the index does to every
    name before it looks it up: on a file system that folds case they are also
    one file, and a clone then loses one of them without a word.
    """
    claims: dict[str, list[str]] = {}
    for entry in entries:
        where = str(_relative(root, entry.path)) if entry.path else entry.name
        for key in (entry.name, *entry.mabolo.aliases):
            if key:
                claims.setdefault(fold(key), []).append(where)
    findings = tuple(
        _f(CONTENT, f"{' and '.join(sorted(set(owners)))} both fold to {key!r}")
        for key, owners in sorted(claims.items())
        if len(set(owners)) > 1
    )
    return Check("content.folding", CONTENT, findings=findings)


def _index_check(root: Path, entries: list, areas: tuple[str, ...] = ()) -> Check:
    """Whether every entry is named by the index of the folder it sits in, and
    whether what the index says about it is still true.

    An entry missing from its own index is invisible to anybody reading the
    vault by hand, which is the way out this project promises, and it is the
    shape a half finished rebuild leaves behind.

    **Being named is not the same as being described.** This asked only whether
    the link was there, so an index could name every entry and be wrong about
    all of them and no check said a word. It happened: an entry written while
    its file was not yet on disk got the line `- unreadable frontmatter`, which
    names the file correctly and describes a vault that does not exist. The
    second question is asked by rebuilding the indexes and seeing whether they
    come out the same, which is the only way to ask it that cannot drift from
    what a write actually produces.
    """
    listed: dict[Path, str | None] = {}
    missing_index: list[Path] = []
    findings: list[Finding] = []
    for entry in entries:
        if entry.path is None:
            continue
        folder = entry.path.parent
        if folder not in listed:
            target = folder / INDEX_FILE
            try:
                listed[folder] = target.read_text(encoding="utf-8")
            except FileNotFoundError:
                listed[folder] = None
                missing_index.append(folder)
            except (OSError, UnicodeDecodeError) as exc:
                return Check(
                    "content.index",
                    CONTENT,
                    reason=f"{_relative(root, target)} cannot be read ({exc}), so no entry was checked against it",
                )
        text = listed[folder]
        if text is not None and f"]({entry.path.name})" not in text:
            findings.append(
                _f(CONTENT, f"{_relative(root, entry.path)} is missing from its area's {INDEX_FILE}")
            )
    findings = [
        _f(CONTENT, f"{_relative(root, folder)} holds entries and has no {INDEX_FILE}")
        for folder in sorted(set(missing_index))
    ] + findings
    try:
        stale = Vault(root, areas or None).stale_indexes()
    except (MaboloError, OSError) as exc:
        return Check(
            "content.index",
            CONTENT,
            findings=tuple(findings),
            reason=f"the indexes could not be rebuilt to compare ({exc})",
        )
    findings += [
        _f(
            CONTENT,
            f"{change.path} does not match the entries beside it. Run `mabolo reindex`",
        )
        for change in stale
    ]
    return Check("content.index", CONTENT, findings=tuple(findings))


def _anchor_check(
    entries: list, project: str | None, repository: Path | None, now: dt.datetime
) -> Check:
    """What became of the files the entries watch, asked of `drift`.

    Asked of `drift` and never worked out here: a second implementation of
    "has this moved" would answer differently from the one the session payload
    uses, and then the vault would be stale in a report and fresh in a session.

    Without a repository to hold the anchors against, this does not run. The
    alternative is one "not checked" line per anchored entry, which is a wall
    of red saying nothing, and worse, a wall that looks like findings.
    """
    if repository is None or project is None:
        return Check(
            "content.anchors",
            CONTENT,
            reason="no project and repository were named, so no anchor could be held against one",
        )
    results = drift.review(entries, project=project, repository=repository, moment=now)
    return Check(
        "content.anchors",
        CONTENT,
        findings=tuple(_f(CONTENT, f"{result.name}: {result.reason}") for result in results),
    )


# What is waiting.


def _waiting_checks(pending: PendingSource, now: dt.datetime) -> list[Check]:
    return [_inbox_check(pending, now), _notes_check(now)]


def _inbox_check(pending: PendingSource, now: dt.datetime) -> Check:
    """What is waiting for a yes or a no, and how long it has been waiting.

    The proposals arrive as a parameter. They live on the `inbox` orphan
    branch, the reader for that branch is its own module, and doctor is of no
    use to anybody until that module exists if it has to import it.
    """
    waiting = _resolve(pending)
    if waiting is None:
        return Check(
            "waiting.inbox",
            WAITING,
            reason="nothing supplied the inbox, so what is waiting there is unknown",
        )
    if not waiting:
        return Check("waiting.inbox", WAITING)
    oldest = min(waiting, key=lambda p: p.at)
    cutoff = now - dt.timedelta(days=PROPOSAL_DAYS)
    expired = sum(1 for p in waiting if p.at < cutoff)
    return Check(
        "waiting.inbox",
        WAITING,
        findings=(
            _f(
                WAITING,
                f"{len(waiting)} proposals waiting, the oldest {oldest.name} from "
                f"{oldest.at.date().isoformat()}, {expired} past {PROPOSAL_DAYS} days",
            ),
        ),
    )


def _notes_check(now: dt.datetime) -> Check:
    """Whether the disposable notes were actually disposed of.

    The prompt notes and the seen notes are the two places outside the vault
    that hold fragments of what somebody typed, and both promise to delete
    themselves after a week. A promise nothing checks is a promise that quietly
    stops being kept the first time a hook fails to run.
    """
    findings: list[Finding] = []
    cutoff = now.timestamp() - consent.RETENTION_DAYS * 86400
    for what, where in (("prompt", consent.prompt_dir()), ("seen", seen.seen_dir())):
        try:
            files = sorted(where.glob("*.jsonl"))
        except OSError as exc:
            return Check("waiting.notes", WAITING, reason=f"{where} cannot be read ({exc.strerror})")
        old = 0
        for path in files:
            try:
                if path.stat().st_mtime < cutoff:
                    old += 1
            except OSError:
                continue
        if old:
            findings.append(
                _f(
                    WAITING,
                    f"{old} {what} notes in {where} are past the {consent.RETENTION_DAYS} "
                    "day promise and are still there",
                )
            )
    return Check("waiting.notes", WAITING, findings=tuple(findings))


# Small shared things.


def _f(group: str, message: str, detail: tuple[str, ...] = ()) -> Finding:
    return Finding(group=group, message=message, detail=detail)


def _resolve(source):
    """A list, or None when nobody supplied one. An empty list is an answer."""
    if source is None:
        return None
    return list(source() if callable(source) else source)


def _relative(root: Path, path: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _git(root: Path, *args: str):
    """Git, with a failure to run at all as None rather than as an exception."""
    try:
        return git.run(root, *args, check=False)
    except MaboloError:
        return None


def _rc(root: Path, *args: str) -> int:
    result = _git(root, *args)
    return result.returncode if result is not None else -1
