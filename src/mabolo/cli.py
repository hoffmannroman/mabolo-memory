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
from pathlib import Path

from dataclasses import replace

from . import __version__, context, evaluate, git, journal
from .config import Config, default_config_path, default_vault_path, is_approver
from .errors import MaboloError
from . import index as index_module
from .index import Index
from .schema import FIXED_AREAS, PROJECT_PREFIX, now, parse_time
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


def _project_for(
    args: argparse.Namespace, documents: list[index_module.Document], folder: str
) -> tuple[str | None, str]:
    """Which project this run is about, and where that came from.

    Given by hand beats derived, and derived beats nothing. The source travels
    back with the answer rather than being worked out again for the message: a
    line that says where a value came from has to come from the code that
    decided it.
    """
    if args.no_project:
        return None, "asked for no project"
    if args.project:
        return args.project, "as given"
    found = context.project_for(folder, {d.area for d in documents})
    if found:
        return found, f"from the folder {folder}"
    return None, f"the folder {folder} is not a project in this vault"


def cmd_context(args: argparse.Namespace) -> int:
    """Print the session index: what a session would be handed at its start."""
    vault = _vault_from(args)
    if args.core_budget < 1:
        raise MaboloError("--core-budget is a whole number of tokens, at least 1")
    if args.budget < 1:
        # The case format asks for at least one token, and a budget of zero or
        # less prints "budget -1" above an empty payload, which is arithmetic
        # rather than an answer.
        raise MaboloError("--budget is a whole number of tokens, at least 1")
    if args.project_budget < 1:
        raise MaboloError("--project-budget is a whole number of tokens, at least 1")
    as_of = None
    if args.as_of:
        as_of = parse_time(args.as_of)
        if as_of is None:
            raise MaboloError(f"{args.as_of!r} is not a date. Use 2026-09-18 or a full timestamp.")
        if as_of.year < 2:
            # Seven days before year one is not a date Python can express, and
            # the resulting OverflowError would leave this command as a
            # traceback rather than a sentence.
            raise MaboloError(f"{args.as_of!r} is too far back to count a week from")
    elif not args.no_clock:
        as_of = now()

    # No search index here: this command does not search, and the projection
    # from files to documents is what both readers of a vault share.
    documents = index_module.documents_of(vault.entries())
    project, source = _project_for(args, documents, repo_root(Path.cwd()).name)
    # The journal is read for the active project only, and only by the caller:
    # `context.build` stays a function of its arguments, and the file it would
    # otherwise have to find is the one thing that is not in the entry list.
    notes = journal.recent(vault.notes(), project, as_of.date() if as_of else None)
    payload = context.build(
        documents,
        project=project,
        as_of=as_of,
        target_tokens=args.budget,
        core_tokens=args.core_budget,
        notes=notes,
        project_tokens=args.project_budget,
    )
    print(payload.text())
    print()
    when = "no moment given, so nothing counts as recent" if as_of is None else f"as of {as_of.isoformat()}"
    # From the payload rather than from the arguments, so the line describes
    # what was built and not what was asked for.
    where = f"project {payload.project}" if payload.project else "no active project"
    print(f"{where}, {source}, {when}")
    if payload.project and not any(a == f"{PROJECT_PREFIX}{payload.project}" for a, _ in payload.counts):
        print(f"there is no project/{payload.project} in this vault, so nothing was added for it")
    print(
        f"{len(payload.shown)} of {payload.total} entries shown, "
        f"about {payload.cost()} tokens, estimated, budget {payload.target_tokens}"
    )
    if payload.cut:
        # Said out loud rather than left to the count above: the rule chose
        # these and the budget took them away again, which is the one thing a
        # person may want to fix by raising the budget.
        print(f"{payload.cut} chosen {'entry' if payload.cut == 1 else 'entries'} did not fit in the budget")
    if payload.core:
        print(
            f"{len(payload.core)} standing "
            f"{'rule' if len(payload.core) == 1 else 'rules'} pinned, about "
            f"{payload.core_cost()} tokens of the {payload.core_tokens} the core is meant to cost"
        )
    if payload.core_is_over_budget:
        # A finding, not a failure: nothing was dropped, and the vault still
        # works. But a person has to decide which rule stops being one, and
        # nobody decides what nobody is told.
        return EXIT_FINDINGS
    return EXIT_OK


#: What a session start may take before it is given up on. The client is
#: waiting on it, so the number is a promise to the person, not to the tool.
HOOK_SECONDS = 5

#: The one sentence a session gets when Mabolo is installed and not set up. Not
#: an error: nothing is wrong, the person simply has not been asked yet, and a
#: hook that stayed silent would leave them wondering whether it works at all.
NO_CONFIG = "Mabolo has no vault configured yet. Run `mabolo init` to set one up."


class _OutOfTime(Exception):
    """Raised by the alarm when a session start has taken too long."""


def cmd_hook_session_start(args: argparse.Namespace) -> int:
    """Print what a session starts with, as a client's hook expects it.

    **This command never fails.** Whatever happens it exits 0, and a session
    that gets nothing from it starts the way it would have without Mabolo
    installed. A memory that can stop a session from starting is worse than no
    memory: the failure would arrive at the worst possible moment, before the
    person has typed anything, and it would look like the agent is broken.

    That is also why the payload is checked before it is sent. The check cannot
    repair anything; what it can do is refuse a payload that does not hold what
    it promises and fall back to the standing rules alone. Losing the map costs
    a session the knowledge that an entry exists, and that is recoverable by
    asking. A rule that silently went missing is not.
    """
    signal.signal(signal.SIGALRM, _raise_out_of_time)
    signal.setitimer(signal.ITIMER_REAL, args.seconds)
    try:
        text = _session_payload(args)
    except (_OutOfTime, MaboloError, OSError, ValueError, UnicodeDecodeError) as exc:
        # Quietly, on stderr, where a person debugging the hook will look and a
        # session will not. The exit code stays 0: see the docstring.
        print(f"mabolo: session start gave up ({exc})", file=sys.stderr)
        return EXIT_OK
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    if text:
        print(json.dumps(_hook_output(text)))
    return EXIT_OK


def _raise_out_of_time(signum: int, frame: object) -> None:
    raise _OutOfTime(f"no payload within {HOOK_SECONDS} seconds")


def _hook_output(text: str) -> dict:
    """The shape both supported clients read a session start in."""
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


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

    documents = index_module.documents_of(vault.entries())
    folder = repo_root(Path(event.get("cwd") or Path.cwd())).name
    project, _ = _project_for(args, documents, folder)
    as_of = None if args.no_clock else now()
    payload = context.build(
        documents,
        project=project,
        as_of=as_of,
        target_tokens=args.budget,
        core_tokens=args.core_budget,
        notes=journal.recent(vault.notes(), project, as_of.date() if as_of else None),
        project_tokens=args.project_budget,
    )
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

    with Index.build(vault.entries(), language=language) as index:
        if not cases:
            print(f"no cases in {vault.eval_dir}, so nothing was measured")
            print(f"The vault holds {len(index)} entries. A case is one YAML file, see docs/eval.md.")
            return EXIT_FINDINGS

        result = evaluate.run(index, cases, vault.notes())
        # Not read when it is about to be replaced. `--save-baseline` is the way
        # out of a baseline this version cannot read or that was measured in
        # another language, so it must not be the command that trips over one.
        baseline = None if args.no_baseline or args.save_baseline else store.read_baseline()
        if baseline is not None:
            baseline.check_language(language)
        changes = evaluate.compare(baseline, result, subset=bool(args.case))
        print(evaluate.render(result, changes))

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
        print(f"  --      {wanted}  is not in the index")
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
        "--core-budget",
        type=int,
        default=context.DEFAULT_CORE_TOKENS,
        help=f"what the standing rules may cost, default {context.DEFAULT_CORE_TOKENS}",
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
        "--core-budget", type=int, default=context.DEFAULT_CORE_TOKENS, help="standing rules budget"
    )
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
