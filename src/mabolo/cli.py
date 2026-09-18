"""The `mabolo` command.

Two commands so far: `init` creates a vault and a configuration, `validate` says
what is wrong with one. Everything else arrives with the part of the tool that
gives it something to talk about.

Everything prints its plan before it writes anything, and `init` ends with a
real check rather than a success message.

Exit codes: 0 when there is nothing to fix, 1 when the run found something in
the vault, 2 when the command itself could not do its job. Warnings count as
something found, because a script that cannot tell "clean" from "a person should
look at this" will never show anybody the warnings.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dataclasses import replace

from . import __version__, git
from .config import Config, default_config_path, default_vault_path, is_approver
from .errors import MaboloError
from .schema import FIXED_AREAS
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
    vault = Vault(config.vault, areas=tuple(config.areas))

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
    config = Config.load_if_present(Path(args.config).expanduser() if args.config else None)
    areas = tuple(config.areas) if config else FIXED_AREAS
    root = Path(args.path).expanduser() if args.path else (config.vault if config else None)
    if root is None:
        raise MaboloError("no vault given and no configuration found. Run `mabolo init` first.")
    if not root.is_dir():
        raise MaboloError(f"{root} is not a folder")
    report = validate_vault(root, areas)
    print(report.render(root, show_warnings=not args.errors_only))
    if not report.ok:
        return EXIT_FINDINGS
    # With --errors-only the warnings were not shown, so they cannot be what the
    # caller is being told about, and a clean exit is the honest answer.
    return EXIT_OK if args.errors_only or not report.warnings else EXIT_FINDINGS


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
