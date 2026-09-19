"""The `mabolo` command.

Five commands so far: `init` creates a vault and a configuration, `validate`
says what is wrong with one, `eval` measures whether the memory actually hands
over the entry that answers a question, `context` prints what a session would
start with, and `hook` is what a client calls to be handed it. Everything else
arrives with the part of the tool that gives it something to talk about.

`context` and `hook session-start` build the same payload on purpose, through
the same arguments and the same code. One prints it for a person to read and
argue with, the other wraps it for a client. A tier nobody has looked at is a
tier nobody can argue with, and two code paths to the same payload would mean
the one that was looked at is not the one that ships.

`init` prints its plan before it writes anything and ends with a real check
rather than a success message. The other commands report; the one place they
write is `eval --save-baseline`, and only because it was asked for by name.

Exit codes: 0 when there is nothing to fix, 1 when the run found something in
the vault, 2 when the command itself could not do its job. Warnings count as
something found, because a script that cannot tell "clean" from "a person should
look at this" will never show anybody the warnings.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import datetime as dt
from pathlib import Path
from typing import Callable

from dataclasses import dataclass, replace

from . import (
    __version__,
    consent,
    context,
    decide,
    design,
    drift,
    evaluate,
    git,
    inbox,
    provenance,
    proposal,
    recall,
    seen,
    session,
)
from .config import Config, default_actor, default_config_path, default_vault_path, is_approver
from .errors import MaboloError
from .index import Index
from .schema import FIXED_AREAS, PROJECT_PREFIX, now, parse_moment, parse_time
from .validate import validate_vault
from .vault import Vault

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def _ask(question: str, default: str, interactive: bool) -> str:
    """One question with a prepared answer. Enter is always enough."""
    if not interactive:
        return default
    shown = default or "none"
    try:
        answer = input(f"{question} [{shown}] ").strip()
    except EOFError:
        return default
    return answer or default


def cmd_init(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    existing = Config.load_if_present(config_path)
    interactive = sys.stdin.isatty() and not args.yes

    vault_default = str(
        Path(args.vault).expanduser() if args.vault
        else (existing.vault if existing else default_vault_path())
    )
    actor_default = args.actor or (existing.actor if existing else Config.default().actor)
    remote_default = args.remote if args.remote is not None else (existing.remote_url if existing else "")

    if existing and interactive:
        print(f"A configuration already exists at {config_path}. Enter keeps each answer.\n")

    # Resolved, because a relative path in the configuration means a different
    # vault depending on where the next command is run from.
    vault_path = Path(_ask("Where should the vault live?", vault_default, interactive)).expanduser().resolve()
    actor = _ask("Who approves entries? (human:<id>)", actor_default, interactive)
    if not is_approver(actor):
        raise MaboloError(f"{actor!r} is not a usable actor. Use human:<id>, for instance human:alex.")
    remote = _ask(
        "Git remote for syncing between machines, empty for a local vault",
        remote_default or "",
        interactive,
    )

    # Built from what the file already said, so that a key this version does not
    # know survives, and so that "the vault came from the environment" is not
    # lost on the way: without it, one run with MABOLO_VAULT set would write
    # that path into the file for good.
    base = existing or Config.default()
    config = replace(
        base,
        vault=vault_path,
        actor=actor,
        remote_url=remote or None,
        path=config_path,
        vault_from_environment=base.vault_from_environment and not args.vault and vault_path == base.vault,
    )
    # The language is written into a vault that is being created, and left
    # alone in one that already declares its own. `init` is safe to run twice,
    # and a second run must not quietly restate what the vault says about
    # itself from a file on this machine.
    fresh = not Vault(config.vault).is_initialised()
    vault = Vault(
        config.vault,
        areas=tuple(config.areas),
        language=config.language if fresh else None,
    )

    print("\nPlan")
    for action in vault.plan():
        print(action.render(vault.root))
    print(f"  {'exists' if config_path.exists() else 'create':7} {config_path}  (configuration)")
    if remote and not args.no_git:
        # Through redact(), like every other place a URL is shown: this line
        # used to print the password and the next one the redacted form.
        print(f"  {'set':7} remote origin -> {git.redact(remote)}")
    if args.no_git:
        print("  skip    Git, because --no-git was passed")
    print("\nNothing outside these paths is touched. The vault keeps working if Mabolo is removed.")

    if args.dry_run:
        print("\nDry run, nothing written.")
        return EXIT_OK
    if interactive:
        try:
            if input("\nDo it? [Y/n] ").strip().lower() in ("n", "no"):
                print("Nothing written.")
                return EXIT_OK
        except EOFError:
            pass

    print()
    actions = vault.initialise()
    for action in actions:
        print(action.render(vault.root))
    # The configuration is written before Git runs, so a failing hook or a
    # missing identity cannot leave a vault behind that nothing points at.
    saved = config.save(config_path)
    print(f"  create  {saved}  (configuration, mode 600)")
    git_result = None
    if not args.no_git:
        git_result = vault.git_initialise(branch=config.remote_branch)
        print(f"  git     {git_result.message}")
        if remote and git_result.completed:
            print(f"  git     {vault.git_set_remote(remote)}")

    # The functional check: read back what was written, and validate the vault.
    print("\nCheck")
    reread = Config.load(saved)
    print(f"  configuration reads back, vault {reread.vault}, approver {reread.actor}")
    print(f"  the vault declares its language as {vault.declared_language()} in index.md")
    report = vault.validate()
    print(f"  {report.render(vault.root)}")
    if not vault.is_initialised():
        raise MaboloError("the vault has no index.md after init, which should not happen")
    if report.ok and not report.warnings:
        count = len(report.entries)
        print(f"\nThe vault is valid and holds {count} entries." if count else "\nThe vault is empty and valid.")
    elif report.ok:
        print("\nThe vault was created. The warnings above are worth a look.")
    else:
        print("\nThe vault was created, but the files above need attention.")
    if git_result is not None and not git_result.completed:
        # Git was asked for and did not happen. Saying so in passing and exiting
        # 0 is how a script concludes the vault is versioned when it is not.
        print("Git was requested but the repository has no commit yet, see the line above.")
    print("Writing entries and wiring up an agent are not built yet.")
    if not report.ok or (git_result is not None and not git_result.completed):
        return EXIT_FINDINGS
    return EXIT_OK if not report.warnings else EXIT_FINDINGS


def cmd_validate(args: argparse.Namespace) -> int:
    vault = _vault_from(args)
    report = validate_vault(vault.root, vault.areas)
    print(report.render(vault.root, show_warnings=not args.errors_only))
    if not report.ok:
        return EXIT_FINDINGS
    # With --errors-only the warnings were not shown, so they cannot be what the
    # caller is being told about, and a clean exit is the honest answer.
    return EXIT_OK if args.errors_only or not report.warnings else EXIT_FINDINGS


def repo_root(start: Path) -> Path:
    """The repository a folder belongs to, or the folder itself.

    The nearest `.git` walking upwards wins, so a session in a subfolder of a
    project is about that project and not about the subfolder. `.git` is a file
    rather than a folder inside a worktree or a submodule, so both count, and a
    repository inside a repository resolves to the inner one, which is where the
    work is actually happening.

    This reads the filesystem, the way `now()` reads the clock, and that is the
    caller's job. Nothing in `context` does either.
    """
    for folder in (start, *start.parents):
        if (folder / ".git").exists():
            return folder
    return start


def _budget_at_most(flag: str, value: int, ceiling: int) -> None:
    """A budget is a ceiling, so an argument may only come in under it."""
    if value < 1:
        # A budget of zero or less prints "budget -1" above an empty payload,
        # which is arithmetic rather than an answer.
        raise MaboloError(f"{flag} is a whole number of tokens, at least 1")
    if value > ceiling:
        raise MaboloError(
            f"{flag} is {value}, over the {ceiling} this version ships with. "
            "A budget can be lowered here, not raised: raising it is how a ceiling stops being one."
        )


def _start(
    args: argparse.Namespace, vault: Vault, folder: str, as_of: "dt.datetime | None"
) -> session.Start:
    """One session start, with the arguments unpacked exactly once.

    The clock and the working directory are already decided by the time this is
    called: `session.start` reads files, and reading either of those would put
    a second answer to "when is now" inside the library.
    """
    return session.start(
        vault,
        folder=folder,
        as_of=as_of,
        given_project=args.project,
        no_project=args.no_project,
        target_tokens=args.budget,
        project_tokens=args.project_budget,
    )


def cmd_context(args: argparse.Namespace) -> int:
    """Print the session index: what a session would be handed at its start."""
    vault = _vault_from(args)
    # A budget may be lowered from the command line and never raised. Raising
    # it is how a ceiling stops being one: the flag is always to hand, and the
    # next person who finds a payload cut short reaches for it instead of for
    # the reason. Lowering is safe, because it can only make a session cheaper
    # than the contract promises.
    _budget_at_most("--budget", args.budget, context.DEFAULT_TARGET_TOKENS)
    _budget_at_most("--project-budget", args.project_budget, context.DEFAULT_PROJECT_TOKENS)
    as_of = None
    if args.as_of:
        as_of = parse_moment(args.as_of)
        if as_of is None:
            raise MaboloError(f"{args.as_of!r} is not a date. Use 2026-09-18 or a full timestamp.")
        if as_of.year < 2:
            # Seven days before year one is not a date Python can express, and
            # the resulting OverflowError would leave this command as a
            # traceback rather than a sentence.
            raise MaboloError(f"{args.as_of!r} is too far back to count a week from")
    elif not args.no_clock:
        as_of = now()

    started = _start(args, vault, repo_root(Path.cwd()).name, as_of)
    payload, documents, source = started.payload, started.documents, started.source
    print(payload.text())
    print()
    when = "no moment given, so nothing counts as recent" if as_of is None else f"as of {as_of.isoformat()}"
    # From the payload rather than from the arguments, so the line describes
    # what was built and not what was asked for.
    where = f"project {payload.project}" if payload.project else "no active project"
    print(f"{where}, {source}, {when}")
    if not session.has_project_area(payload):
        print(f"there is no project/{payload.project} in this vault, so nothing was added for it")
    named = f", {len(payload.named)} named only" if payload.named else ""
    print(
        f"{len(payload.shown)} of {payload.total} entries described{named}, "
        f"about {payload.cost()} tokens, estimated, budget {payload.target_tokens}"
    )
    if payload.cut:
        # Said out loud rather than left to the count above: the rule chose
        # these and the budget took them away again, which is the one thing a
        # person may want to fix by raising the budget.
        print(f"{payload.cut} chosen {'entry' if payload.cut == 1 else 'entries'} did not fit in the budget")
    if payload.core or payload.unseated:
        print(
            f"{len(payload.core)} of {context.CORE_SEATS} seats taken by standing rules, "
            f"about {payload.core_cost()} tokens"
        )
    for name, why in payload.unseated:
        print(f"  no seat: {name} ({why})")
    if args.why_not:
        # After the counts rather than instead of them: the counts say how much
        # is missing, and this says which lever moves each piece of it.
        print()
        rows = context.why_not(payload, documents)
        if not rows:
            print("every entry in this vault has a line of its own")
        for name, reason in rows:
            print(f"  {name}  {reason}")
    if payload.core_is_full:
        # A finding, not a failure: the vault still works, and everything it
        # holds is still readable. But a rule that did not get a seat is not
        # being followed, and nobody decides what nobody is told.
        return EXIT_FINDINGS
    return EXIT_OK


#: What a session start may take before it is given up on. The client is
#: waiting on it, so the number is a promise to the person, not to the tool.
HOOK_SECONDS = 5

#: What a prompt may take. Less than half of a session start, because this one
#: runs ahead of every single prompt and the person is mid sentence: a session
#: start happens once and is expected to take a moment, while a pause here is
#: felt every time and is blamed on the agent.
PROMPT_SECONDS = 2

#: The one sentence a session gets when Mabolo is installed and not set up. Not
#: an error: nothing is wrong, the person simply has not been asked yet, and a
#: hook that stayed silent would leave them wondering whether it works at all.
NO_CONFIG = "Mabolo has no vault configured yet. Run `mabolo init` to set one up."


class _OutOfTime(Exception):
    """Raised by the alarm when a session start has taken too long."""


#: The longest a deadline may be set to, and the shortest. A hook that was
#: handed a nonsensical `--seconds` is still a hook: it clamps, says so on
#: stderr, and runs. Refusing would be the one thing it must never do.
MIN_SECONDS, MAX_SECONDS = 0.01, 3600.0


@dataclass(frozen=True)
class HookContract:
    """What tells one hook from another. Everything else they share.

    Three values rather than three flags. A flag says "behave differently here"
    and leaves a reader to work out where; these say which event is being
    answered, how long the client will wait, and what to call this in a message
    to a person. What a hook does when there is no configuration is not in
    here, because it is not a variation on a shared behaviour: it lives in the
    payload function, where the answer to "and then what do we say" belongs.
    """

    event: str
    label: str
    seconds: float


SESSION_START = HookContract(event="SessionStart", label="session start", seconds=HOOK_SECONDS)
PROMPT = HookContract(event="UserPromptSubmit", label="prompt hook", seconds=PROMPT_SECONDS)
PRETOOL = HookContract(event="PreToolUse", label="file hook", seconds=PROMPT_SECONDS)

#: Where a client puts the path a tool is reaching for. Several spellings,
#: because this is the one hook whose input is a tool's own arguments, and
#: those differ per tool and per client. An unknown shape is silence, never a
#: guess: a rule raised for the wrong file is worse than no rule.
PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")


def cmd_hook_session_start(args: argparse.Namespace) -> int:
    """What a session starts with, as a client's hook expects it."""
    return _run_hook(args, SESSION_START, _session_payload)


def cmd_hook_prompt(args: argparse.Namespace) -> int:
    """The entry a prompt should have known about, or nothing at all."""
    return _run_hook(args, PROMPT, _prompt_payload)


def cmd_inbox(args: argparse.Namespace) -> int:
    """Show what automation suggested, or answer it.

    With no verdicts it lists and changes nothing. With them it is the one
    place a proposal becomes an entry, and the person typing into their own
    shell is the consent: the quote gate stands in front of an agent's tool
    call, not in front of a person's keyboard.
    """
    config = Config.load_if_present(Path(args.config).expanduser() if args.config else None)
    vault = _vault_from(args)
    remote = "origin" if config and config.remote_url else None
    branch = config.remote_branch if config else None
    actor = config.actor if config else default_actor()

    listing = inbox.read(vault.root)
    for broken in listing.unreadable:
        # Named every time and never dropped: it is the only copy of somebody's
        # suggestion, and a file that cannot be read is not a file that can be
        # thrown away.
        _say(f"mabolo: {broken}")
    if not args.verdicts:
        if not listing.proposals:
            print("nothing is waiting.")
            return EXIT_FINDINGS if listing.unreadable else EXIT_OK
        for one in listing.proposals:
            print(one.line())
        print("")
        print("Answer with `mabolo inbox <id> yes` or `<id> no`, ids may be shortened.")
        return EXIT_OK

    decisions = _verdicts(args.verdicts)
    answers = []
    for wanted, approved in decisions:
        one = proposal.resolve(wanted, listing.proposals)
        answers.append(
            decide.answer(vault, one, approved=approved, by=actor, remote=remote, branch=branch)
        )
        print(answers[-1].line())
    return EXIT_OK if all(a.ok for a in answers) else EXIT_FINDINGS


def _verdicts(words: list[str]) -> list[tuple[str, bool]]:
    """`a3f2 yes 7c01 no` as pairs, or an error naming what was not understood.

    Refused rather than guessed at. A word that is neither an id nor a verdict
    in a line that answers proposals is a typo, and the two ways of guessing
    are "skip it" and "assume yes", both of which write something nobody asked
    for.
    """
    if len(words) % 2:
        raise MaboloError("every id needs an answer: `mabolo inbox a3f2 yes 7c01 no`")
    out: list[tuple[str, bool]] = []
    for wanted, said in zip(words[0::2], words[1::2]):
        answer = said.strip().lower()
        if answer not in ("yes", "no"):
            raise MaboloError(f"{said!r} is not an answer. It is yes or no.")
        out.append((wanted, answer == "yes"))
    return out


def cmd_why(args: argparse.Namespace) -> int:
    """Print where one entry came from, and whether what it watches has moved.

    Two modules answer that, and neither may call the other: provenance reads
    the file and the history, drift asks the repository the session is standing
    in. Only a caller knows both, which is why the anchor note is set here and
    not inside either of them. Left unset, the report says the question was
    never asked, which is the honest answer and not the same as "it is fine".
    """
    vault = _vault_from(args)
    found = provenance.of(vault, args.entry)
    if found.anchor:
        entry = provenance.find(vault, args.entry)
        folder = repo_root(Path.cwd())
        areas = {e.area for e in vault.entries()}
        judged = drift.judge(
            entry,
            project=context.project_for(folder.name, areas),
            repository=folder,
            moment=now(),
        )
        found = replace(found, anchor_note=judged.reason)
    print(found.render())
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the MCP server on stdin and stdout until the client goes away.

    The import of the SDK happens here rather than at the top of the file. It
    costs a third of a second, and the prompt hook, which shares this module,
    has two seconds in total to say something useful.
    """
    from .server import Settings, build

    config = Config.load_if_present(Path(args.config).expanduser() if args.config else None)
    vault = _vault_from(args)
    if not vault.is_initialised():
        raise MaboloError(f"{vault.root} is not a Mabolo vault. Run `mabolo init` first.")
    settings = Settings(
        actor=config.actor if config else default_actor(),
        remote="origin" if config and config.remote_url else None,
        branch=config.remote_branch if config else None,
        read_only=bool(args.read_only),
        cwd=Path.cwd(),
        session=args.session or None,
    )
    build(vault, settings).run("stdio")
    return EXIT_OK


def cmd_hook_pretool(args: argparse.Namespace) -> int:
    """The standing rules about the kind of file a tool is about to touch."""
    return _run_hook(args, PRETOOL, _pretool_payload)


def _run_hook(
    args: argparse.Namespace,
    contract: HookContract,
    payload: "Callable[[argparse.Namespace], str]",
) -> int:
    """Run one hook inside the guarantees every hook makes.

    **It never fails.** Whatever happens it exits 0, and a session that gets
    nothing starts the way it would have without Mabolo installed. A memory
    that can stop a session from starting is worse than no memory: the failure
    arrives before the person has typed anything and looks like the agent is
    broken. So the net is `BaseException`, not a list of the failures somebody
    thought of: the list left out `TypeError` from a client that sent an object
    where a path belongs, and `RecursionError` from a deeply nested event, and
    both of those ended a session start with a traceback.

    **The deadline covers the writing too.** Setting up the timer, building the
    payload and printing it are all inside it. Printing used to happen after
    the timer was cancelled, so a client that had stopped reading its pipe
    could hang the very hook whose whole promise is not to.
    """
    seconds = _deadline(args, contract)
    try:
        previous = signal.signal(signal.SIGALRM, _raise_out_of_time(seconds))
        try:
            signal.setitimer(signal.ITIMER_REAL, seconds)
            text = payload(args)
            if text:
                print(json.dumps(_hook_output(contract.event, text)))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    except SystemExit:
        raise
    except BaseException as exc:
        # Quietly, on stderr, where a person debugging the hook will look and a
        # session will not. The exit code stays 0: see the docstring.
        _say(f"mabolo: {contract.label} gave up ({type(exc).__name__}: {exc})")
    return EXIT_OK


def _deadline(args: argparse.Namespace, contract: HookContract) -> float:
    """The deadline this run will use, whatever was asked for.

    A hook cannot refuse to run over an argument. `--seconds 0` disabled the
    timer outright, which is the opposite of what a deadline is for, and `nan`
    or a number too large for the platform raised before the guard was even
    armed.
    """
    asked = args.seconds
    if not isinstance(asked, (int, float)) or asked != asked:  # NaN is not equal to itself
        asked = contract.seconds
    seconds = min(max(float(asked), MIN_SECONDS), MAX_SECONDS)
    if seconds != asked:
        _say(f"mabolo: {contract.label} deadline {args.seconds} is out of range, using {seconds}")
    return seconds


def _say(message: str) -> None:
    """One line to stderr, and never a failure of its own.

    Even this can raise: stderr may be closed or full. A hook that died while
    explaining why it gave up would break the promise it was in the middle of
    keeping.
    """
    try:
        print(message, file=sys.stderr)
    except (OSError, ValueError):
        pass


def _prompt_payload(args: argparse.Namespace) -> str:
    """The block a prompt is interrupted with, or an empty string for silence."""
    event = _hook_event()
    prompt = args.prompt or event.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return ""
    if not args.no_record:
        # Before anything else, and regardless of what this hook goes on to
        # say. Quiet recall is off by default and returns nothing most of the
        # time; the note is what a write tool checks a quote against later, and
        # tying it to the tier that usually stays silent would mean the gate
        # only works on the prompts that happened to find an entry.
        consent.record(prompt, cwd=event.get("cwd"), session=event.get("session_id"))
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    if not args.path and not config_path.exists():
        # Silence, not the sentence the session start gives. That one is said
        # once when a session begins; said again on every prompt it would be
        # the tool nagging about its own setup.
        return ""
    vault = _vault_from(args)
    if not vault.is_initialised():
        return ""
    with Index.build(vault.entries(), language=vault.declared_language()) as index:
        found = index.search(prompt, limit=recall.LIMIT)
        return recall.block(found.hits)


def _tool_path(event: dict) -> str:
    """The file a tool call is about, or an empty string."""
    given = event.get("tool_input")
    if not isinstance(given, dict):
        return ""
    for name in PATH_KEYS:
        value = given.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pretool_payload(args: argparse.Namespace) -> str:
    """The design rules this file raises, or nothing at all.

    Nothing at all is the normal answer, and it has to stay cheap: this runs
    ahead of every single tool call, which is dozens of times in a session
    where the prompt hook runs a handful.
    """
    event = _hook_event()
    where = args.file or _tool_path(event)
    if not where:
        return ""
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    if not args.path and not config_path.exists():
        return ""
    vault = _vault_from(args)
    if not vault.is_initialised():
        return ""
    folder = Path(event.get("cwd") or Path.cwd())
    project = repo_root(folder).name
    # Relative to the repository when it lies inside it, because a pattern like
    # `docs/*.md` is written about a project and not about a machine.
    try:
        shown = str(Path(where).resolve().relative_to(repo_root(folder).resolve()))
    except (OSError, ValueError):
        shown = where
    session = event.get("session_id")
    rules = design.rules_for(
        vault.entries(), shown, project=project, seen=seen.already(session)
    )
    if not rules:
        return ""
    text = design.block(rules, shown)
    seen.remember([design.key_for(entry) for entry in rules[: design.LIMIT]], session=session)
    return text


def _raise_out_of_time(seconds: float) -> "Callable[[int, object], None]":
    """The alarm handler for one run, which knows that run's deadline.

    It used to name the constant instead, so the two second prompt hook
    reported five, and so did every run given `--seconds`. A message about a
    deadline that states the wrong deadline is worse than no message: it sends
    the reader looking in the wrong place.
    """

    def handler(signum: int, frame: object) -> None:
        raise _OutOfTime(f"no payload within {seconds:g} seconds")

    return handler


def _hook_output(event: str, text: str) -> dict:
    """The shape both supported clients read injected context in."""
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _session_payload(args: argparse.Namespace) -> str:
    """The text of the payload, or an empty string when there is nothing to say."""
    event = _hook_event()
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    if not args.path and not config_path.exists():
        # A vault named on the command line needs no configuration: that is how
        # the hook is tried out before it is installed, and how a test runs it.
        return NO_CONFIG
    vault = _vault_from(args)
    if not vault.is_initialised():
        return NO_CONFIG

    where = event.get("cwd") or Path.cwd()
    folder = repo_root(Path(where)).name
    payload = _start(args, vault, folder, None if args.no_clock else now()).payload
    found = context.violations(payload)
    if found:
        # The payload did not hold what it promises. Nothing here tries to work
        # out which line to drop: the rules alone are the part that was never up
        # for cutting and the cheapest to be sure of.
        for problem in found:
            print(f"mabolo: {problem}", file=sys.stderr)
        return context.degraded(payload)
    return payload.text()


def _hook_event() -> dict:
    """Whatever the client sent on stdin, as a dictionary.

    An empty dictionary when there is nothing to read or it is not JSON. The
    event carries the working directory, which is how a session says which
    project it is about, and a session start that refused to run without it
    would fail for every client that sends a shape this version has not seen.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return event if isinstance(event, dict) else {}


def _vault_from(args: argparse.Namespace) -> Vault:
    """The vault a command was pointed at, from the argument or the configuration."""
    config = Config.load_if_present(Path(args.config).expanduser() if args.config else None)
    areas = tuple(config.areas) if config else FIXED_AREAS
    root = Path(args.path).expanduser() if args.path else (config.vault if config else None)
    if root is None:
        raise MaboloError("no vault given and no configuration found. Run `mabolo init` first.")
    if not root.is_dir():
        raise MaboloError(f"{root} is not a folder")
    return Vault(root, areas=areas)


def cmd_eval(args: argparse.Namespace) -> int:
    """Run the question set against the vault and say what moved.

    Exit 1 covers three different disappointments, and the output distinguishes
    them: a case failed, a case got worse than the baseline, or there were no
    cases at all. The last one is the reason this does not exit 0 on an empty
    folder: "measured nothing" printed above a green exit code is exactly the
    kind of quiet success this whole tool exists to prevent.
    """
    vault = _vault_from(args)
    if args.save_baseline and args.case:
        # The baseline is written from the run, so a run over a subset would
        # replace the whole file with that subset. Every other case would then
        # be "new" on the next run, which is not a failure, and the gate would
        # be disarmed by the most natural way to accept one change.
        raise MaboloError(
            "--save-baseline writes the whole set, so it cannot be combined with --case. "
            "Run it once without --case when the full run is the line you want to hold."
        )
    store = evaluate.EvalStore(vault)
    cases = store.cases()
    if args.case:
        cases = evaluate.select(cases, args.case)
    language = vault.declared_language()
    # Read once and handed to both. The index is a reduced view built for
    # searching, and the design tier needs the fields it leaves out.
    entries = vault.entries()

    with Index.build(entries, language=language) as index:
        if not cases:
            print(f"no cases in {vault.eval_dir}, so nothing was measured")
            print(f"The vault holds {len(index)} entries. A case is one YAML file, see docs/eval.md.")
            return EXIT_FINDINGS

        result = evaluate.run(index, cases, vault.notes(), entries)
        # Not read when it is about to be replaced. `--save-baseline` is the way
        # out of a baseline this version cannot read or that was measured in
        # another language, so it must not be the command that trips over one.
        baseline = None if args.no_baseline or args.save_baseline else store.read_baseline()
        note = None
        if baseline is not None:
            baseline.check_language(language)
            baseline.check_seats()
            note = baseline.policy_note()
        changes = evaluate.compare(baseline, result, subset=bool(args.case))
        print(evaluate.render(result, changes))
        if note:
            # Above nothing and below everything: it qualifies the comparison
            # that was just printed, so it is read after it and not instead.
            print(f"note  {note}")

        if args.explain:
            print()
            for item in result.results:
                _explain(index, item)

        if args.save_baseline:
            saved = store.write_baseline(result, language)
            print(f"\nbaseline written to {saved}, {len(result.measured)} cases, language {language}")
            return EXIT_OK if result.ok else EXIT_FINDINGS

        if baseline is None and not args.no_baseline:
            print(f"\nno baseline yet. `mabolo eval --save-baseline` writes {store.baseline_path}.")
        if not result.ok or any(c.is_worse for c in changes):
            return EXIT_FINDINGS
        return EXIT_OK


def _explain(index: Index, result: evaluate.Result) -> None:
    """Everything the ranking used for one case, so a number can be argued with.

    Every line comes from what the search itself produced. Rebuilding the query
    here and re-deriving the name evidence was a second copy of the rule, and a
    second copy explains something other than what happened.
    """
    case = result.case
    print(f"{case.id}  ({case.tier})")
    print(f"  {'state' if case.tier == 'hint' else 'query'}   {case.trigger}")
    if not result.measured:
        print(f"  skipped {result.reasons[0] if result.reasons else 'not measured'}")
        print()
        return
    if case.tier == "hint":
        _explain_hint(index, result)
        return
    parsed = result.query
    print(f"  stems   {', '.join(parsed.stems) or 'none'}")
    if parsed.phrases:
        print(f"  phrases {', '.join(parsed.phrases)}")
    named = sorted(index.names_in(parsed))
    print(f"  names   {', '.join(named) or 'none of the query is a name this vault knows'}")
    expected = {d.name for d in (index.resolve_entry(w) for w in case.entries) if d is not None}
    if not result.hits:
        print("  result  nothing, the relevance floor turned every candidate away")
    for hit in result.hits:
        wanted = " <-- expected" if hit.name in expected else ""
        evidence = ", ".join(hit.matched) or f"named as {', '.join(hit.named)}"
        print(f"  {hit.rank}.      {hit.name}  score {hit.score:.3f}  matched {evidence}{wanted}")
    print(f"  cost    about {result.cost} tokens for a preview of those, estimated")
    print()


def _explain_hint(index: Index, result: evaluate.Result) -> None:
    """The session index a hint case was judged against, line by line.

    Printed from the payload the run produced rather than from a second build
    of it, for the same reason the search explains itself from its own result.
    """
    case = result.case
    payload = result.payload
    if payload is None:
        print("  result  no session index was built")
        print()
        return
    expected = {n for n, _ in (evaluate.resolve_expected(index, w) for w in case.in_payload)}
    unwanted = {n for n, _ in (evaluate.resolve_expected(index, w) for w in case.not_in_payload)}
    if not payload.shown:
        print("  result  the session index is empty")
    for position, line in enumerate(payload.shown, start=1):
        mark = " <-- expected" if line.name in expected else ""
        mark = " <-- should not be here" if line.name in unwanted else mark
        print(f"  {position}.      {line.name}  ({line.area}, {line.reason}){mark}")
    for wanted in sorted(expected - set(payload.names)):
        named = " (named only)" if wanted in {line.name for line in payload.named} else ""
        print(f"  --      {wanted}  is not in the index{named}")
    print(
        f"  named   {len(payload.named)} of {payload.total} entries, by name and nothing more"
    )
    print(f"  omitted {payload.omitted} of {payload.total} entries, {payload.cut} cut by the budget")
    print(f"  cost    about {result.cost} tokens for the whole payload, estimated")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mabolo",
        description="A memory for coding agents: Markdown and Git, with a person in front of every write.",
    )
    parser.add_argument("--version", action="version", version=f"mabolo {__version__}")
    parser.add_argument("--config", help="path to the configuration file")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a vault and a configuration")
    init.add_argument("--vault", help="where the vault lives")
    init.add_argument("--actor", help="who approves entries, as human:<id>")
    init.add_argument("--remote", help="Git remote for syncing, empty for a local vault")
    init.add_argument("--no-git", action="store_true", help="do not create a Git repository")
    init.add_argument("-y", "--yes", action="store_true", help="take the prepared answers, ask nothing")
    init.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    init.set_defaults(func=cmd_init)

    validate = sub.add_parser("validate", help="check a vault against the schema")
    validate.add_argument("path", nargs="?", help="the vault, default is the configured one")
    validate.add_argument("--errors-only", action="store_true", help="hide warnings")
    validate.set_defaults(func=cmd_validate)

    measure = sub.add_parser("eval", help="run the question set against the vault")
    measure.add_argument("path", nargs="?", help="the vault, default is the configured one")
    measure.add_argument("--case", action="append", help="run only this case, repeatable")
    measure.add_argument("--explain", action="store_true", help="show stems, ranks and scores")
    measure.add_argument(
        "--save-baseline", action="store_true", help="write this run as the baseline to compare against"
    )
    measure.add_argument(
        "--no-baseline", action="store_true", help="report the run without comparing it"
    )
    measure.set_defaults(func=cmd_eval)

    shown = sub.add_parser("context", help="print what a session would start with")
    shown.add_argument("path", nargs="?", help="the vault, default is the configured one")
    shown.add_argument(
        "--project", help="the project the session is about, default is the folder you are in"
    )
    shown.add_argument(
        "--no-project", action="store_true", help="build the payload for no project at all"
    )
    shown.add_argument("--as-of", help="the moment the session starts at, default is now")
    shown.add_argument(
        "--no-clock",
        action="store_true",
        help="do not read the clock, so nothing counts as recent",
    )
    shown.add_argument(
        "--budget",
        type=int,
        default=context.DEFAULT_TARGET_TOKENS,
        help=f"what the payload should cost, in tokens, default {context.DEFAULT_TARGET_TOKENS}",
    )
    shown.add_argument(
        "--why-not",
        action="store_true",
        help="for every entry without a line of its own, the first reason why",
    )
    shown.add_argument(
        "--project-budget",
        type=int,
        default=context.DEFAULT_PROJECT_TOKENS,
        help=f"what the journal block may cost, default {context.DEFAULT_PROJECT_TOKENS}",
    )
    shown.set_defaults(func=cmd_context)

    hook = sub.add_parser("hook", help="what a client's hook calls")
    events = hook.add_subparsers(dest="event", required=True)
    start = events.add_parser("session-start", help="print the payload a session starts with")
    start.add_argument("path", nargs="?", help="the vault, default is the configured one")
    start.add_argument("--project", help="the project, default is the folder the session is in")
    start.add_argument("--no-project", action="store_true", help="no project at all")
    start.add_argument(
        "--no-clock", action="store_true", help="do not read the clock, so nothing counts as recent"
    )
    start.add_argument("--budget", type=int, default=context.DEFAULT_TARGET_TOKENS, help="map budget")
    start.add_argument(
        "--project-budget", type=int, default=context.DEFAULT_PROJECT_TOKENS, help="journal budget"
    )
    start.add_argument(
        "--seconds",
        type=float,
        default=HOOK_SECONDS,
        help=f"give up after this long, default {HOOK_SECONDS}",
    )
    start.set_defaults(func=cmd_hook_session_start)

    prompt = events.add_parser("prompt", help="offer what a prompt should have known")
    prompt.add_argument("path", nargs="?", help="the vault, default is the configured one")
    prompt.add_argument("--prompt", help="the prompt, default is the one on stdin")
    prompt.add_argument(
        "--seconds",
        type=float,
        default=PROMPT_SECONDS,
        help=f"give up after this long, default {PROMPT_SECONDS}",
    )
    prompt.add_argument(
        "--no-record",
        action="store_true",
        help="do not write the prompt down, and give up verifying quotes against it",
    )
    prompt.set_defaults(func=cmd_hook_prompt)

    touched = events.add_parser("pretool", help="the rules about the file a tool is opening")
    touched.add_argument("path", nargs="?", help="the vault, default is the configured one")
    touched.add_argument("--file", help="the file being touched, default is the one in the event")
    touched.add_argument(
        "--seconds",
        type=float,
        default=PROMPT_SECONDS,
        help=f"give up after this long, default {PROMPT_SECONDS}",
    )
    touched.set_defaults(func=cmd_hook_pretool)

    waiting = sub.add_parser("inbox", help="what automation suggested, and your answer")
    waiting.add_argument(
        "verdicts",
        nargs="*",
        help="pairs of id and yes or no, for example a3f2 yes 7c01 no",
    )
    waiting.add_argument("--path", dest="path", help="the vault, default is the configured one")
    waiting.set_defaults(func=cmd_inbox)

    why = sub.add_parser("why", help="where one entry came from, and what it rests on")
    why.add_argument("entry", help="the entry's name, or one of its aliases")
    why.add_argument("path", nargs="?", help="the vault, default is the configured one")
    why.set_defaults(func=cmd_why)

    serve = sub.add_parser("serve", help="run the MCP server a client talks to")
    serve.add_argument("path", nargs="?", help="the vault, default is the configured one")
    serve.add_argument(
        "--read-only",
        action="store_true",
        help="offer only the reading tools, which is what an unattended agent gets",
    )
    serve.add_argument("--session", help="the session id, when the client knows one")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MaboloError as exc:
        print(f"mabolo: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        # A missing permission or a full disk is the user's situation, not a bug
        # in this program, so it reads as a sentence and exits like one.
        where = f"{exc.filename}: " if getattr(exc, "filename", None) else ""
        print(f"mabolo: {where}{exc.strerror or exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
