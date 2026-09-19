import io
import os
import time
import json
from pathlib import Path

import pytest

from conftest import entry_text
from mabolo import cli, context, index, schema
from mabolo.cli import main
from mabolo.config import Config
from mabolo.vault import Vault


def test_init_creates_vault_and_configuration(tmp_path, capsys):
    code = main(["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
                 "--actor", "human:someone", "--yes", "--no-git"])
    out = capsys.readouterr().out
    assert code == 0
    assert (tmp_path / "v" / "index.md").exists()
    config = Config.load(tmp_path / "c.toml")
    assert config.vault == tmp_path / "v" and config.actor == "human:someone"
    assert "Plan" in out and "Check" in out
    assert "0 errors" in out


def test_a_dry_run_writes_nothing(tmp_path, capsys):
    code = main(["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
                 "--yes", "--dry-run"])
    assert code == 0
    assert not (tmp_path / "v").exists()
    assert not (tmp_path / "c.toml").exists()
    assert "Dry run" in capsys.readouterr().out


def test_init_is_safe_to_run_twice(tmp_path):
    args = ["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
            "--actor", "human:someone", "--yes", "--no-git"]
    assert main(args) == 0
    assert main(args) == 0


def test_init_keeps_the_answers_of_an_earlier_run(tmp_path):
    config = str(tmp_path / "c.toml")
    main(["--config", config, "init", "--vault", str(tmp_path / "v"), "--actor", "human:someone",
          "--yes", "--no-git"])
    main(["--config", config, "init", "--yes", "--no-git"])
    assert Config.load(tmp_path / "c.toml").actor == "human:someone"


def test_validate_reports_findings_with_its_exit_code(tmp_path, capsys):
    config = str(tmp_path / "c.toml")
    main(["--config", config, "init", "--vault", str(tmp_path / "v"), "--yes", "--no-git"])
    assert main(["--config", config, "validate"]) == 0
    (tmp_path / "v" / "infra" / "broken.md").write_text("---\ntitle: no type\n---\n\ntext\n",
                                                        encoding="utf-8")
    assert main(["--config", config, "validate"]) == 1
    assert "okf.type.missing" in capsys.readouterr().out


def test_validate_without_a_configuration_says_what_to_do(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "missing.toml"), "validate"]) == 2
    assert "mabolo init" in capsys.readouterr().err




def test_an_unknown_command_is_refused():
    with pytest.raises(SystemExit):
        main(["nonsense"])


def test_a_password_in_a_remote_url_is_never_printed(tmp_path, capsys):
    """The plan printed it in full and the line below it redacted."""
    main(["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
          "--actor", "human:someone", "--remote", "https://user:s3cret@example.invalid/r.git",
          "--yes", "--dry-run"])
    out = capsys.readouterr().out
    assert "s3cret" not in out
    assert "***@example.invalid" in out


def test_an_override_for_one_run_is_not_written_into_the_file(tmp_path, monkeypatch):
    config = tmp_path / "c.toml"
    main(["--config", str(config), "init", "--vault", str(tmp_path / "a"),
          "--actor", "human:someone", "--yes", "--no-git"])
    monkeypatch.setenv("MABOLO_VAULT", str(tmp_path / "b"))
    main(["--config", str(config), "init", "--actor", "human:someone", "--yes", "--no-git"])
    assert str(tmp_path / "a") in config.read_text(encoding="utf-8")
    assert str(tmp_path / "b") not in config.read_text(encoding="utf-8")


def test_warnings_do_not_look_like_a_clean_run(tmp_path, capsys):
    main(["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
          "--actor", "human:someone", "--yes", "--no-git"])
    (tmp_path / "v" / "infra" / "no-title.md").write_text(
        "---\ntype: reference\ndescription: A description long enough.\n"
        "mabolo:\n  area: infra\n---\n\nBody.\n",
        encoding="utf-8",
    )
    code = main(["--config", str(tmp_path / "c.toml"), "validate", str(tmp_path / "v")])
    assert "mabolo.title.missing" in capsys.readouterr().out
    assert code == 1
    assert main(["--config", str(tmp_path / "c.toml"), "validate", str(tmp_path / "v"), "--errors-only"]) == 0


def test_a_missing_permission_reads_as_a_sentence(tmp_path, capsys):
    import os

    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        code = main(["--config", str(tmp_path / "c.toml"), "init", "--vault", str(locked / "v"),
                     "--actor", "human:someone", "--yes", "--no-git"])
    finally:
        os.chmod(locked, 0o755)
    assert code == 2
    assert "mabolo:" in capsys.readouterr().err


def eval_vault(tmp_path, config: str) -> str:
    """A vault with one entry and one case, wired to its own configuration."""
    vault = tmp_path / "v"
    main(["--config", config, "init", "--vault", str(vault), "--actor", "human:someone",
          "--yes", "--no-git"])
    (vault / "infra" / "deploy-from-main.md").write_text(
        entry_text(title="Deploy from main only", description="Releases are cut from main",
                   body="Releases are cut from main. A tag marks what shipped."),
        encoding="utf-8",
    )
    (vault / ".mabolo" / "eval" / "deploy-source.yaml").write_text(
        "id: deploy-source\nquery: where are releases cut from\n"
        "expect:\n  entries: [deploy-from-main]\n  rank_within: 1\n",
        encoding="utf-8",
    )
    return str(vault)


def test_eval_measures_a_vault_and_says_what_it_did_not_measure(tmp_path, capsys):
    config = str(tmp_path / "c.toml")
    eval_vault(tmp_path, config)
    assert main(["--config", config, "eval"]) == 0
    out = capsys.readouterr().out
    assert "1 case, 1 pass, 0 fail" in out
    assert "no baseline yet" in out


def test_eval_on_a_vault_without_cases_does_not_report_success(tmp_path, capsys):
    """Measured nothing, printed above a green exit code, is the lie to avoid."""
    config = str(tmp_path / "c.toml")
    main(["--config", config, "init", "--vault", str(tmp_path / "v"), "--yes", "--no-git"])
    assert main(["--config", config, "eval"]) == 1
    assert "nothing was measured" in capsys.readouterr().out


def test_eval_saves_a_baseline_and_then_guards_it(tmp_path, capsys):
    config = str(tmp_path / "c.toml")
    vault = eval_vault(tmp_path, config)
    assert main(["--config", config, "eval", "--save-baseline"]) == 0
    assert (tmp_path / "v" / ".mabolo" / "eval" / "baseline.json").exists()
    capsys.readouterr()

    # The entry the case depends on loses the words the question uses.
    (tmp_path / "v" / "infra" / "deploy-from-main.md").write_text(
        entry_text(title="Where a build comes from", description="The trunk is the only origin",
                   body="Builds come from the trunk."),
        encoding="utf-8",
    )
    assert main(["--config", config, "eval", vault]) == 1
    out = capsys.readouterr().out
    assert "was not returned at all" in out
    assert "passed in the baseline and fails now" in out


def test_eval_can_run_one_case_and_explain_it(tmp_path, capsys):
    config = str(tmp_path / "c.toml")
    eval_vault(tmp_path, config)
    assert main(["--config", config, "eval", "--case", "deploy-source", "--explain"]) == 0
    out = capsys.readouterr().out
    assert "stems" in out and "deploy-from-main" in out and "<-- expected" in out


def test_eval_names_a_case_that_does_not_exist(tmp_path, capsys):
    config = str(tmp_path / "c.toml")
    eval_vault(tmp_path, config)
    assert main(["--config", config, "eval", "--case", "nothing-like-this"]) == 2
    assert "no case called nothing-like-this" in capsys.readouterr().err


def test_eval_refuses_to_save_a_baseline_from_a_subset(tmp_path, capsys):
    """The most natural way to accept one change used to disarm the whole gate.

    A run over one case wrote a baseline holding that one case. Every other case
    was then "new" on the next run, which is not a failure, so nothing fired.
    """
    config = str(tmp_path / "c.toml")
    eval_vault(tmp_path, config)
    assert main(["--config", config, "eval", "--save-baseline"]) == 0
    before = (tmp_path / "v" / ".mabolo" / "eval" / "baseline.json").read_bytes()
    assert main(["--config", config, "eval", "--case", "deploy-source", "--save-baseline"]) == 2
    assert "cannot be combined with --case" in capsys.readouterr().err
    assert (tmp_path / "v" / ".mabolo" / "eval" / "baseline.json").read_bytes() == before


def test_eval_can_replace_a_baseline_it_cannot_read(tmp_path, capsys):
    """`--save-baseline` is the way out, so it must not trip over the old file."""
    config = str(tmp_path / "c.toml")
    eval_vault(tmp_path, config)
    baseline = tmp_path / "v" / ".mabolo" / "eval" / "baseline.json"
    baseline.write_text('{"version": 1, "cases": {}}', encoding="utf-8")
    assert main(["--config", config, "eval"]) == 2
    assert main(["--config", config, "eval", "--save-baseline"]) == 0
    assert main(["--config", config, "eval"]) == 0


def test_the_search_reads_the_language_from_the_vault(tmp_path, capsys):
    """Not from this machine's configuration, or two clones would rank differently."""
    config = str(tmp_path / "c.toml")
    eval_vault(tmp_path, config)
    assert Vault(tmp_path / "v").declared_language() == "en"
    main(["--config", config, "eval", "--save-baseline"])
    capsys.readouterr()

    index = tmp_path / "v" / "index.md"
    index.write_text(index.read_text(encoding="utf-8").replace("language: en", "language: de"),
                     encoding="utf-8")
    assert main(["--config", config, "eval"]) == 2
    assert "cannot be compared" in capsys.readouterr().err


def test_init_writes_the_language_into_the_vault_it_creates(tmp_path, capsys):
    config = str(tmp_path / "c.toml")
    main(["--config", config, "init", "--vault", str(tmp_path / "v"), "--yes", "--no-git"])
    assert "declares its language as en" in capsys.readouterr().out
    assert Vault(tmp_path / "v").declared_language() == "en"


def test_init_run_twice_does_not_restate_the_language_of_an_existing_vault(tmp_path):
    """A file on this machine must not quietly change what the vault says."""
    config = str(tmp_path / "c.toml")
    main(["--config", config, "init", "--vault", str(tmp_path / "v"), "--yes", "--no-git"])
    index = tmp_path / "v" / "index.md"
    index.write_text(index.read_text(encoding="utf-8").replace("language: en", "language: de"),
                     encoding="utf-8")
    main(["--config", config, "init", "--yes", "--no-git"])
    assert Vault(tmp_path / "v").declared_language() == "de"


# `mabolo context`: what a session would start with


def context_vault(vault):
    (vault.root / "persona" / "working-hours.md").write_text(
        entry_text(
            area="persona",
            title="Works late",
            description="Deep work after 20:00",
            mabolo={"pin": True},
            generated={"by": "mabolo/0.1.0", "at": "2026-01-01T10:00:00+03:00"},
        ),
        encoding="utf-8",
    )
    (vault.root / "infra" / "old-news.md").write_text(
        entry_text(
            title="Old news",
            description="Nothing has touched this in a year",
            generated={"by": "mabolo/0.1.0", "at": "2026-01-01T10:00:00+03:00"},
        ),
        encoding="utf-8",
    )
    return vault


def test_context_prints_the_payload_and_what_it_costs(vault, capsys):
    context_vault(vault)
    code = main(["context", str(vault.root), "--as-of", "2026-09-18"])
    out = capsys.readouterr().out
    assert code == 0
    assert "- working-hours: Deep work after 20:00" in out
    assert "- old-news:" not in out, "a year old, not pinned, no project, so no line"
    assert "also here: old-news" in out, "and still a name to ask with"
    assert "1 entry above is named only" in out
    assert "1 of 2 entries described, 1 named only" in out
    assert "budget 800" in out


def test_context_can_be_told_not_to_read_the_clock(vault, capsys):
    """With an entry written moments ago, so that reading the clock anyway
    would show up as an extra line rather than as nothing at all."""
    context_vault(vault)
    (vault.root / "infra" / "written-just-now.md").write_text(
        entry_text(
            title="Just now",
            description="Written moments ago",
            generated={"by": "mabolo/0.1.0", "at": schema.now().isoformat()},
        ),
        encoding="utf-8",
    )
    assert main(["context", str(vault.root), "--no-clock"]) == 0
    out = capsys.readouterr().out
    assert "nothing counts as recent" in out
    assert "- working-hours" in out, "a pin does not depend on a moment"
    assert "- written-just-now:" not in out, "no moment was given, so nothing is recent"


def test_context_refuses_a_date_it_cannot_read(vault, capsys):
    context_vault(vault)
    assert main(["context", str(vault.root), "--as-of", "last tuesday"]) == 2
    assert "is not a date" in capsys.readouterr().err


def test_context_says_when_the_budget_cut_something(vault, capsys):
    """With an entry the rule picked and the budget took away again. A pinned
    one would not do: the core is never cut."""
    context_vault(vault)
    (vault.root / "infra" / "written-today.md").write_text(
        entry_text(
            title="Today",
            description="Touched this week, so the rule picks it",
            generated={"by": "mabolo/0.1.0", "at": "2026-09-17T10:00:00+03:00"},
        ),
        encoding="utf-8",
    )
    assert main(["context", str(vault.root), "--as-of", "2026-09-18", "--budget", "1"]) == 0
    assert "1 chosen entry did not fit in the budget" in capsys.readouterr().out


def test_context_narrows_to_a_project(vault, capsys):
    folder = vault.area_dir("project/atlas")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "atlas-tone.md").write_text(
        entry_text(area="project/atlas", title="Tone", description="Flat and factual"),
        encoding="utf-8",
    )
    context_vault(vault)
    assert main(["context", str(vault.root), "--project", "atlas", "--as-of", "2026-09-18"]) == 0
    out = capsys.readouterr().out
    assert "- atlas-tone: Flat and factual" in out
    assert "project atlas," in out


def test_eval_explains_a_hint_case_with_the_payload_it_measured(vault, capsys):
    context_vault(vault)
    (vault.eval_dir).mkdir(parents=True, exist_ok=True)
    (vault.eval_dir / "c.yaml").write_text(
        "id: c\ntier: hint\nstate:\n  as_of: 2026-09-18\n"
        "expect:\n  in_payload: [working-hours]\n  not_in_payload: [old-news]\n",
        encoding="utf-8",
    )
    assert main(["eval", str(vault.root), "--no-baseline", "--explain"]) == 0
    out = capsys.readouterr().out
    assert "hint     1 case, 1 pass, 0 fail" in out
    assert "state   session start, no active project, as of 2026-09-18" in out
    assert "1.      working-hours  (persona, pinned) <-- expected" in out
    assert "named   1 of 2 entries" in out
    assert "omitted 0 of 2 entries, 0 cut by the budget" in out


def test_context_refuses_a_budget_that_is_not_a_budget(vault, capsys):
    context_vault(vault)
    for budget in ("0", "-1"):
        assert main(["context", str(vault.root), "--budget", budget]) == 2
        assert "at least 1" in capsys.readouterr().err


def test_context_refuses_a_date_it_cannot_count_a_week_back_from(vault, capsys):
    """Seven days before year one is not a date Python can express, and the
    command promises sentences rather than tracebacks."""
    context_vault(vault)
    assert main(["context", str(vault.root), "--as-of", "0001-01-03"]) == 2
    assert "too far back" in capsys.readouterr().err


def test_context_says_when_the_named_project_does_not_exist(vault, capsys):
    context_vault(vault)
    assert main(["context", str(vault.root), "--project", "atlsa", "--as-of", "2026-09-18"]) == 0
    assert "there is no project/atlsa in this vault" in capsys.readouterr().out


# Which project a session is about, when nobody says


def test_context_takes_the_project_from_the_folder_you_are_in(vault, tmp_path, monkeypatch, capsys):
    """The repository root, not the folder you happen to stand in: a session in
    a subfolder of a project is about that project."""
    context_vault(vault)
    folder = vault.area_dir("project/atlas")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "atlas-tone.md").write_text(
        entry_text(area="project/atlas", title="Tone", description="Flat and factual"),
        encoding="utf-8",
    )
    work = tmp_path / "atlas" / "backend" / "src"
    work.mkdir(parents=True)
    (tmp_path / "atlas" / ".git").mkdir()
    monkeypatch.chdir(work)

    assert main(["context", str(vault.root), "--as-of", "2026-09-18"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Active project: atlas"
    assert "- atlas-tone: Flat and factual" in out
    assert "project atlas, from the folder atlas," in out


def test_a_folder_that_is_no_project_is_said_out_loud(vault, tmp_path, monkeypatch, capsys):
    context_vault(vault)
    work = tmp_path / "scratch"
    work.mkdir()
    (work / ".git").mkdir()
    monkeypatch.chdir(work)

    assert main(["context", str(vault.root), "--as-of", "2026-09-18"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "No active project"
    assert "the folder scratch is not a project in this vault" in out


def test_a_project_given_by_hand_beats_the_folder(vault, tmp_path, monkeypatch, capsys):
    context_vault(vault)
    work = tmp_path / "atlas"
    work.mkdir()
    (work / ".git").mkdir()
    monkeypatch.chdir(work)

    assert main(["context", str(vault.root), "--project", "beacon", "--as-of", "2026-09-18"]) == 0
    assert "project beacon, as given," in capsys.readouterr().out


def test_no_project_can_be_asked_for(vault, tmp_path, monkeypatch, capsys):
    context_vault(vault)
    work = tmp_path / "atlas"
    work.mkdir()
    (work / ".git").mkdir()
    monkeypatch.chdir(work)

    assert main(["context", str(vault.root), "--no-project", "--as-of", "2026-09-18"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "No active project"
    assert "asked for no project" in out


def test_a_worktree_marker_counts_as_a_repository(vault, tmp_path, monkeypatch, capsys):
    """Inside a worktree or a submodule `.git` is a file, not a folder."""
    from mabolo.cli import repo_root

    work = tmp_path / "atlas" / "deep"
    work.mkdir(parents=True)
    (tmp_path / "atlas" / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert repo_root(work).name == "atlas"


def test_the_nearest_repository_wins(tmp_path):
    from mabolo.cli import repo_root

    inner = tmp_path / "outer" / "inner"
    inner.mkdir(parents=True)
    (tmp_path / "outer" / ".git").mkdir()
    (inner / ".git").mkdir()
    assert repo_root(inner).name == "inner"


def test_a_folder_in_no_repository_at_all_is_its_own_answer(tmp_path):
    from mabolo.cli import repo_root

    lonely = tmp_path / "nowhere"
    lonely.mkdir()
    assert repo_root(lonely) == lonely


def test_context_reports_a_rule_without_a_seat_as_a_finding(vault, capsys):
    """Nothing was lost from the vault, so it is not a failure. But a rule
    without a seat is not being followed, and nobody decides what nobody is
    told. The name and the reason are both in the output."""
    context_vault(vault)
    for i in range(schema_seats := context_seats()):
        (vault.root / "persona" / f"extra-{i:02d}.md").write_text(
            entry_text(
                area="persona",
                title=f"Extra {i}",
                description="one more standing rule",
                mabolo={"pin": True},
                generated={"by": "mabolo/0.1.0", "at": f"2026-09-{10 + i % 8:02d}T10:00:00+03:00"},
            ),
            encoding="utf-8",
        )
    assert main(["context", str(vault.root), "--as-of", "2026-09-18"]) == 1
    out = capsys.readouterr().out
    assert "not loaded:" in out
    assert "no seat:" in out
    assert f"of {schema_seats} seats taken" in out


def context_seats() -> int:
    return context.CORE_SEATS


def test_context_is_clean_when_every_rule_has_a_seat(vault, capsys):
    context_vault(vault)
    assert main(["context", str(vault.root), "--as-of", "2026-09-18"]) == 0
    out = capsys.readouterr().out
    assert "not loaded:" not in out
    assert f"1 of {context.CORE_SEATS} seats taken by standing rules" in out


def test_the_core_size_is_not_a_command_line_option_any_more(vault, capsys):
    """It was the back door: one flag and the whole mechanism is off. A budget
    may be lowered from here, never raised, and the number of seats is not a
    budget at all."""
    context_vault(vault)
    with pytest.raises(SystemExit) as exit:
        main(["context", str(vault.root), "--core-budget", "5000"])
    assert exit.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


# The session start hook: it never fails, and it never sends a payload it cannot stand behind


def hook_vault(tmp_path):
    """A vault with one standing rule and one ordinary entry."""
    made = Vault(tmp_path / "hv")
    made.initialise()
    (made.root / "infra" / "rule.md").write_text(
        entry_text(title="Rule", description="Deep work after 20:00", mabolo={"pin": True}),
        encoding="utf-8",
    )
    (made.root / "infra" / "thing.md").write_text(
        entry_text(title="Thing", description="A machine that builds nightly"),
        encoding="utf-8",
    )
    return made


def session_start(tmp_path, capsys, monkeypatch, event: str = "{}", extra: list[str] | None = None):
    """Run the hook with `event` on stdin: its code, its parsed output, its stderr.

    All three at once, because reading the capture empties it: a test that asked
    for stdout here and stderr later would find the second one blank.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(event))
    code = main(["hook", "session-start", str(hook_vault(tmp_path).root), *(extra or [])])
    captured = capsys.readouterr()
    out = captured.out.strip()
    return code, (json.loads(out) if out else None), captured.err


def test_the_hook_prints_a_payload_a_client_can_read(tmp_path, capsys, monkeypatch):
    code, out, _ = session_start(tmp_path, capsys, monkeypatch)
    assert code == 0
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Deep work after 20:00" in out["hookSpecificOutput"]["additionalContext"]


def test_the_hook_exits_zero_when_the_vault_cannot_be_read(tmp_path, capsys, monkeypatch):
    """A memory that can stop a session from starting is worse than no memory."""
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "index.md").write_text("---\nnot: [valid\n---\n", encoding="utf-8")
    (tmp_path / "broken" / "bad.md").write_text("no frontmatter here", encoding="utf-8")
    code = main(["hook", "session-start", str(tmp_path / "broken")])
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_the_hook_says_one_sentence_when_nothing_is_configured(tmp_path, capsys, monkeypatch):
    """Nothing is wrong: the person has not been asked yet. Silence would leave
    them wondering whether the thing they installed works at all."""
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    code = main(["--config", str(tmp_path / "missing.toml"), "hook", "session-start"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["hookSpecificOutput"]["additionalContext"] == cli.NO_CONFIG


def test_the_hook_reads_the_project_from_the_folder_the_session_is_in(tmp_path, capsys, monkeypatch):
    made = hook_vault(tmp_path)
    folder = made.area_dir("project/atlas")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "atlas-tone.md").write_text(
        entry_text(area="project/atlas", title="Tone", description="Flat and factual"),
        encoding="utf-8",
    )
    session = tmp_path / "somewhere" / "atlas"
    (session / ".git").mkdir(parents=True)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(session)})))
    main(["hook", "session-start", str(made.root)])
    context_text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "Active project: atlas" in context_text
    assert "atlas-tone" in context_text


def test_the_hook_gives_up_quietly_when_it_runs_out_of_time(tmp_path, capsys, monkeypatch):
    """The deadline is a promise to the person waiting on the session, so it is
    kept by giving up rather than by finishing late. Slow work is simulated
    here: a real clock race would make this test pass or fail by chance."""
    import time

    real = index.documents_of
    monkeypatch.setattr(index, "documents_of", lambda entries: (time.sleep(0.4), real(entries))[1])
    code, out, err = session_start(tmp_path, capsys, monkeypatch, extra=["--seconds", "0.05"])
    assert code == 0
    assert out is None
    assert "gave up" in err


def test_a_payload_that_fails_its_check_is_replaced_by_the_rules_alone(tmp_path, capsys, monkeypatch):
    """The check cannot repair anything. What it can do is refuse to send a
    payload that does not hold what it promises, and keep the part that was
    never up for cutting."""
    monkeypatch.setattr(context, "violations", lambda payload: ("invented for this test",))
    code, out, _ = session_start(tmp_path, capsys, monkeypatch)
    sent = out["hookSpecificOutput"]["additionalContext"]
    assert code == 0
    assert "Deep work after 20:00" in sent
    assert "builds nightly" not in sent
    assert context.DEGRADED in sent


def prompt_hook(tmp_path, capsys, monkeypatch, event: str, extra: list[str] | None = None):
    monkeypatch.setattr("sys.stdin", io.StringIO(event))
    code = main(["hook", "prompt", str(hook_vault(tmp_path).root), *(extra or [])])
    captured = capsys.readouterr()
    out = captured.out.strip()
    return code, (json.loads(out) if out else None), captured.err


def test_the_prompt_hook_offers_what_the_prompt_should_have_known(tmp_path, capsys, monkeypatch):
    event = json.dumps({"prompt": "the machine that builds nightly is out of memory"})
    code, out, _ = prompt_hook(tmp_path, capsys, monkeypatch, event)
    assert code == 0
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "thing" in out["hookSpecificOutput"]["additionalContext"]


def test_the_prompt_hook_says_nothing_when_nothing_is_relevant(tmp_path, capsys, monkeypatch):
    """Silence is the normal answer, and it is a bare empty output: no heading,
    no blank line, nothing for a client to strip."""
    code, out, _ = prompt_hook(tmp_path, capsys, monkeypatch, json.dumps({"prompt": "what is for dinner"}))
    assert code == 0 and out is None


def test_the_prompt_hook_says_nothing_when_there_is_no_prompt(tmp_path, capsys, monkeypatch):
    code, out, _ = prompt_hook(tmp_path, capsys, monkeypatch, "{}")
    assert code == 0 and out is None


def test_the_prompt_hook_stays_quiet_about_a_missing_configuration(tmp_path, capsys, monkeypatch):
    """The session start says one sentence about setup. Repeating it on every
    prompt would be the tool nagging about itself."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "anything at all"})))
    code = main(["--config", str(tmp_path / "missing.toml"), "hook", "prompt"])
    assert code == 0 and capsys.readouterr().out.strip() == ""


def test_the_prompt_hook_gives_up_quietly_when_it_runs_out_of_time(tmp_path, capsys, monkeypatch):
    import time

    real = cli.Index.build
    monkeypatch.setattr(
        cli.Index, "build", lambda *a, **k: (time.sleep(0.4), real(*a, **k))[1]
    )
    event = json.dumps({"prompt": "the machine that builds nightly"})
    code, out, err = prompt_hook(tmp_path, capsys, monkeypatch, event, extra=["--seconds", "0.05"])
    assert code == 0 and out is None
    assert "gave up" in err


def test_context_can_say_why_an_entry_has_no_line(vault, capsys):
    """A count says how much is missing. This says which lever moves it."""
    context_vault(vault)
    assert main(["context", str(vault.root), "--as-of", "2026-09-18", "--why-not"]) == 0
    out = capsys.readouterr().out
    assert "old-news  no rule chose it" in out
    assert "working-hours" not in out.split("budget 800")[1], "it has a line, so nothing to explain"


# What two audits found in the hooks


@pytest.mark.parametrize("cwd", [123, ["a"], {"a": 1}, True])
def test_the_hook_survives_a_client_that_sends_nonsense_for_the_working_directory(
    tmp_path, capsys, monkeypatch, cwd
):
    """The net used to be a list of the failures somebody thought of, and it
    left out TypeError. A session start ended in a traceback because a client
    sent an object where a path belongs."""
    code, _, err = session_start(tmp_path, capsys, monkeypatch, json.dumps({"cwd": cwd}))
    assert code == 0
    assert err == "" or "gave up" in err


def test_the_hook_survives_an_event_too_deeply_nested_to_parse(tmp_path, capsys, monkeypatch):
    code, _, _ = session_start(tmp_path, capsys, monkeypatch, "[" * 60000)
    assert code == 0


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "1e30"])
def test_a_deadline_out_of_range_is_clamped_rather_than_refused(tmp_path, capsys, monkeypatch, seconds):
    """`--seconds 0` disabled the timer outright, which is the opposite of what
    a deadline is for, and nan raised before the guard was even armed. A hook
    cannot refuse to run over an argument."""
    code, out, _ = session_start(tmp_path, capsys, monkeypatch, extra=["--seconds", seconds])
    assert code == 0
    assert out is not None, "it still did its work"


def test_the_timeout_message_names_the_deadline_that_was_used(tmp_path, capsys, monkeypatch):
    """It named the constant, so the two second prompt hook reported five. A
    message about a deadline that states the wrong one sends the reader
    looking in the wrong place."""
    import time

    real = index.documents_of
    monkeypatch.setattr(index, "documents_of", lambda entries: (time.sleep(0.4), real(entries))[1])
    _, _, err = session_start(tmp_path, capsys, monkeypatch, extra=["--seconds", "0.05"])
    assert "0.05 seconds" in err


def test_the_deadline_covers_the_writing_too(tmp_path, capsys, monkeypatch):
    """Printing used to happen after the timer was cancelled, so a client that
    had stopped reading its pipe could hang the very hook whose whole promise
    is not to."""
    slow = type("Slow", (), {"write": lambda self, text: time.sleep(0.4) or len(text), "flush": lambda self: None})()
    root = str(hook_vault(tmp_path).root)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setattr("sys.stdout", slow)
    code = main(["hook", "session-start", root, "--seconds", "0.05"])
    monkeypatch.undo()
    err = capsys.readouterr().err
    assert code == 0
    # The alarm has to have fired *during* the write. Outside the guard the
    # write simply took its time and the hook ended cleanly, which is the same
    # exit code and the wrong behaviour.
    assert "gave up" in err and "0.05 seconds" in err


def test_a_budget_may_be_lowered_from_the_command_line_but_not_raised(vault, capsys):
    """Raising it is how a ceiling stops being one: the flag is always to hand,
    and the next person who finds a payload cut short reaches for it instead of
    for the reason."""
    context_vault(vault)
    assert main(["context", str(vault.root), "--budget", "200"]) == 0
    assert main(["context", str(vault.root), "--budget", "5000"]) == 2
    assert "can be lowered here, not raised" in capsys.readouterr().err


# The prompt hook writes down what it was handed, which is the only evidence a
# write tool has that a person said anything at all.


def prompt_notes(tmp_path) -> str:
    where = Path.home() / ".local" / "state" / "mabolo" / "prompts"
    return "".join(p.read_text(encoding="utf-8") for p in sorted(where.glob("*.jsonl")))


def test_the_prompt_hook_writes_the_prompt_down(tmp_path, capsys, monkeypatch):
    event = json.dumps({"prompt": "remember that we deploy on fridays", "session_id": "abc-1",
                        "cwd": str(tmp_path)})
    code, _, _ = prompt_hook(tmp_path, capsys, monkeypatch, event)
    assert code == 0
    notes = prompt_notes(tmp_path)
    assert "remember that we deploy on fridays" in notes
    assert str(tmp_path) in notes
    assert (Path.home() / ".local/state/mabolo/prompts/abc-1.jsonl").exists()


def test_the_prompt_hook_can_be_told_not_to(tmp_path, capsys, monkeypatch):
    event = json.dumps({"prompt": "remember that we deploy on fridays", "session_id": "abc-2"})
    prompt_hook(tmp_path, capsys, monkeypatch, event, extra=["--no-record"])
    assert prompt_notes(tmp_path) == ""


def test_a_prompt_is_written_down_even_when_the_memory_stays_quiet(tmp_path, capsys, monkeypatch):
    """Recall answers one prompt in twenty. The note has to be written on the
    other nineteen too, or the write gate would only work on the prompts that
    happened to find an entry."""
    event = json.dumps({"prompt": "what is for dinner tonight", "session_id": "abc-3"})
    code, out, _ = prompt_hook(tmp_path, capsys, monkeypatch, event)
    assert code == 0 and out is None
    assert "what is for dinner tonight" in prompt_notes(tmp_path)


def test_a_prompt_is_written_down_before_anything_else_is_decided(tmp_path, capsys, monkeypatch):
    """Even with no configuration at all. The note is not a by-product of the
    recall tier: it is the evidence a write tool checks a quote against, and a
    machine that has not been set up yet is exactly where the first sentence
    worth remembering gets typed."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "remember the fridays rule"})))
    assert main(["--config", str(tmp_path / "missing.toml"), "hook", "prompt"]) == 0
    assert "remember the fridays rule" in prompt_notes(tmp_path)


def test_serve_says_so_when_there_is_no_vault(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    assert main(["serve", str(tmp_path / "empty")]) == 2
    assert "not a Mabolo vault" in capsys.readouterr().err


def test_serve_builds_the_server_from_the_configuration(tmp_path, monkeypatch, capsys):
    """The command is wired to the real settings: the actor from the
    configuration, the remote when there is one, and read mode when asked."""
    vault = hook_vault(tmp_path)
    seen = {}

    class Fake:
        def run(self, transport):
            seen["transport"] = transport

    monkeypatch.setattr("mabolo.server.build", lambda v, s: seen.setdefault("settings", s) and None or Fake())
    assert main(["serve", str(vault.root), "--read-only", "--session", "s-9"]) == 0
    assert seen["transport"] == "stdio"
    assert seen["settings"].read_only is True
    assert seen["settings"].session == "s-9"


# The file hook: the standing rules about the kind of file a tool is opening.


def pretool(tmp_path, capsys, monkeypatch, event: str, extra: list[str] | None = None):
    monkeypatch.setattr("sys.stdin", io.StringIO(event))
    code = main(["hook", "pretool", str(hook_vault(tmp_path).root), *(extra or [])])
    captured = capsys.readouterr()
    out = captured.out.strip()
    return code, (json.loads(out) if out else None), captured.err


def design_rule(tmp_path, name="left-aligned", patterns=("*.css",), scope=None):
    vault = hook_vault(tmp_path)
    block = {"applies_to": list(patterns)}
    if scope:
        block["scope"] = scope
    (vault.root / "design" / f"{name}.md").write_text(
        entry_text(area="design", type="feedback", description="text is left aligned",
                   mabolo=block),
        encoding="utf-8",
    )
    return vault


def test_the_file_hook_raises_the_rule_about_that_kind_of_file(tmp_path, capsys, monkeypatch):
    design_rule(tmp_path)
    event = json.dumps({"tool_input": {"file_path": "src/landing.css"}, "session_id": "s1"})
    code, out, _ = pretool(tmp_path, capsys, monkeypatch, event)
    assert code == 0
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "left-aligned" in out["hookSpecificOutput"]["additionalContext"]


def test_the_file_hook_says_nothing_about_a_file_nothing_was_written_about(
    tmp_path, capsys, monkeypatch
):
    design_rule(tmp_path)
    event = json.dumps({"tool_input": {"file_path": "src/main.rs"}, "session_id": "s1"})
    code, out, _ = pretool(tmp_path, capsys, monkeypatch, event)
    assert code == 0 and out is None


def test_the_file_hook_says_nothing_when_the_event_names_no_file(tmp_path, capsys, monkeypatch):
    """A tool call with no path is most tool calls. Guessing which argument is
    a file would raise a rule about the wrong one."""
    design_rule(tmp_path)
    event = json.dumps({"tool_input": {"command": "ls *.css"}, "session_id": "s1"})
    code, out, _ = pretool(tmp_path, capsys, monkeypatch, event)
    assert code == 0 and out is None


def test_the_file_hook_does_not_say_the_same_thing_twice_in_one_session(
    tmp_path, capsys, monkeypatch
):
    design_rule(tmp_path)
    event = json.dumps({"tool_input": {"file_path": "src/landing.css"}, "session_id": "s1"})
    first = pretool(tmp_path, capsys, monkeypatch, event)
    second = pretool(tmp_path, capsys, monkeypatch, event)
    assert first[1] is not None
    assert second[1] is None, "the same rule on every touch teaches the reader to skip it"


def test_another_session_is_told_all_the_same(tmp_path, capsys, monkeypatch):
    design_rule(tmp_path)
    here = {"tool_input": {"file_path": "src/landing.css"}}
    pretool(tmp_path, capsys, monkeypatch, json.dumps({**here, "session_id": "s1"}))
    _, out, _ = pretool(tmp_path, capsys, monkeypatch, json.dumps({**here, "session_id": "s2"}))
    assert out is not None


def test_the_file_hook_never_fails_a_tool_call(tmp_path, capsys, monkeypatch):
    """It runs ahead of every single tool call. A hook that can stop one is
    worse than a hook that says nothing."""
    design_rule(tmp_path)
    monkeypatch.setattr(cli.design, "rules_for", lambda *a, **k: 1 / 0)
    event = json.dumps({"tool_input": {"file_path": "src/landing.css"}, "session_id": "s1"})
    code, out, err = pretool(tmp_path, capsys, monkeypatch, event)
    assert code == 0 and out is None
    assert "gave up" in err


# `why`: the command that turns the auditability claim into something a person
# can operate.


def test_why_prints_where_an_entry_came_from(tmp_path, capsys):
    vault = hook_vault(tmp_path)
    (vault.root / "infra" / "a-thing.md").write_text(
        entry_text(area="infra", description="a thing that is known"), encoding="utf-8"
    )
    assert main(["why", "a-thing", str(vault.root)]) == 0
    out = capsys.readouterr().out
    assert "a-thing" in out
    assert "who and when" in out and "evidence" in out and "history" in out


def test_why_says_the_anchor_was_not_checked_rather_than_that_it_is_fine(tmp_path, capsys):
    """Two modules answer this and neither may call the other, so the note is
    the command's to set. Left unset it has to read as a question nobody asked,
    because "not checked" and "has not moved" are different answers."""
    vault = hook_vault(tmp_path)
    (vault.root / "infra" / "watched.md").write_text(
        entry_text(area="infra", description="d", mabolo={"anchor": "src/build.yml"}),
        encoding="utf-8",
    )
    assert main(["why", "watched", str(vault.root)]) == 0
    out = capsys.readouterr().out
    assert "src/build.yml" in out
    assert "not checked" in out


def test_why_names_an_entry_it_cannot_find(tmp_path, capsys):
    hook_vault(tmp_path)
    assert main(["why", "nothing-like-this", str(hook_vault(tmp_path).root)]) == 2
    assert "nothing-like-this" in capsys.readouterr().err


# `init` wires up the clients it finds, and refuses to overwrite what somebody
# else put under our own keys.


def claude_home(tmp_path):
    home = Path(os.environ["HOME"])
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    return home


def test_init_wires_up_a_client_it_finds(tmp_path, capsys, git_identity):
    home = claude_home(tmp_path)
    code = main(["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
                 "--yes"])
    assert code == 0
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert sorted(settings["hooks"]) == ["PreToolUse", "SessionStart", "UserPromptSubmit"]
    assert json.loads((home / ".claude.json").read_text())["mcpServers"]["mabolo"]
    assert "wired" in capsys.readouterr().out


def test_init_run_twice_changes_a_wired_client_not_at_all(tmp_path, capsys, git_identity):
    home = claude_home(tmp_path)
    arguments = ["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
                 "--yes"]
    main(arguments)
    before = (home / ".claude" / "settings.json").read_bytes()
    capsys.readouterr()
    main(arguments)
    assert (home / ".claude" / "settings.json").read_bytes() == before
    written = [line for line in capsys.readouterr().out.splitlines() if line.startswith("  wired")]
    assert written == [], "a second run writes nothing and says nothing about writing"


def test_init_leaves_an_entry_of_ours_that_says_something_else(tmp_path, capsys, git_identity):
    """Somebody wrote that themselves. Overwriting it quietly is the kind of
    repair this tool refuses, and a half wired client is not a success."""
    home = claude_home(tmp_path)
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"mabolo": {"command": "somewhere-else"}}}), encoding="utf-8"
    )
    code = main(["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
                 "--yes"])
    assert code == 1
    out = capsys.readouterr().out
    assert "--rewire" in out
    assert json.loads((home / ".claude.json").read_text())["mcpServers"]["mabolo"] == {
        "command": "somewhere-else"
    }


def test_init_rewires_when_it_is_told_to(tmp_path, capsys, git_identity):
    home = claude_home(tmp_path)
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"mabolo": {"command": "somewhere-else"}}}), encoding="utf-8"
    )
    assert main(["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
                 "--yes", "--rewire"]) == 0
    assert json.loads((home / ".claude.json").read_text())["mcpServers"]["mabolo"]["command"] != (
        "somewhere-else"
    )


def test_init_can_be_told_to_wire_up_nothing(tmp_path, capsys, git_identity):
    home = claude_home(tmp_path)
    main(["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
          "--yes", "--no-clients"])
    assert json.loads((home / ".claude" / "settings.json").read_text()) == {"hooks": {}}


def dated(**over):
    """An entry that carries a date, so that only what a test asks about fires."""
    fields = {
        "area": "infra",
        "description": "d",
        "generated": {"by": "mabolo/0.1.0", "at": "2026-09-18T10:00:00+00:00"},
    }
    fields.update(over)
    return entry_text(**fields)


def tidy_vault(tmp_path):
    made = Vault(tmp_path / "lv")
    made.initialise()
    (made.root / "infra" / "a-thing.md").write_text(dated(), encoding="utf-8")
    return made


def test_lint_reports_what_it_finds_and_exits_one(tmp_path, capsys):
    vault = tidy_vault(tmp_path)
    (vault.root / "infra" / "b-thing.md").write_text(
        dated(body="See [gone](nowhere.md)."), encoding="utf-8"
    )
    assert main(["lint", str(vault.root), "--as-of", "2026-09-19"]) == 1
    assert "nowhere.md" in capsys.readouterr().out


def test_lint_says_nothing_is_wrong_and_exits_zero(tmp_path, capsys):
    vault = tidy_vault(tmp_path)
    assert main(["lint", str(vault.root), "--as-of", "2026-09-19"]) == 0
    assert "0 findings" in capsys.readouterr().out


def test_lint_judges_ages_against_the_moment_it_was_given(tmp_path, capsys):
    """Every check in it is built so a run can be replayed, and a report that
    read the clock could not be."""
    vault = tidy_vault(tmp_path)
    (vault.root / "infra" / "old-thing.md").write_text(
        dated(
            description="something else entirely, so that nothing looks like a duplicate",
            body="unrelated prose about quite another subject",
            generated={"by": "mabolo/0.1.0", "at": "2026-01-01T00:00:00+00:00"},
        ),
        encoding="utf-8",
    )
    assert main(["lint", str(vault.root), "--as-of", "2026-02-01"]) == 0
    assert main(["lint", str(vault.root), "--as-of", "2027-01-01"]) == 1
    assert "old-thing" in capsys.readouterr().out


# `uninstall`: the command that proves the promise that removing this costs
# you nothing.


def test_uninstall_unwires_the_clients_and_keeps_the_vault(tmp_path, capsys, git_identity):
    home = claude_home(tmp_path)
    config = tmp_path / "c.toml"
    main(["--config", str(config), "init", "--vault", str(tmp_path / "v"), "--yes"])
    (Path.home() / ".local" / "state" / "mabolo" / "prompts").mkdir(parents=True, exist_ok=True)
    capsys.readouterr()

    assert main(["--config", str(config), "uninstall", "--yes"]) == 0
    assert json.loads((home / ".claude" / "settings.json").read_text())["hooks"] == {}
    assert "mabolo" not in json.loads((home / ".claude.json").read_text()).get("mcpServers", {})
    assert not (Path.home() / ".local" / "state" / "mabolo").exists()
    assert config.exists(), "the configuration stays"
    assert (tmp_path / "v" / "index.md").exists(), "and so does every entry"


def test_uninstall_leaves_a_hook_that_is_not_ours(tmp_path, capsys, git_identity):
    """The same predicate that writes has to be the one that removes, or
    uninstalling would leave behind exactly what a later init refuses to
    overwrite, and take with it what was never ours."""
    home = claude_home(tmp_path)
    config = tmp_path / "c.toml"
    main(["--config", str(config), "init", "--vault", str(tmp_path / "v"), "--yes"])
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    settings["hooks"]["SessionStart"].append(
        {"hooks": [{"type": "command", "command": "somebody-elses-tool"}]}
    )
    (home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    main(["--config", str(config), "uninstall", "--yes"])
    after = json.loads((home / ".claude" / "settings.json").read_text())
    commands = [
        hook["command"]
        for groups in after["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert commands == ["somebody-elses-tool"]


def test_uninstall_prints_its_plan_and_stops(tmp_path, capsys, git_identity):
    home = claude_home(tmp_path)
    config = tmp_path / "c.toml"
    main(["--config", str(config), "init", "--vault", str(tmp_path / "v"), "--yes"])
    before = (home / ".claude" / "settings.json").read_bytes()
    capsys.readouterr()
    assert main(["--config", str(config), "uninstall", "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert (home / ".claude" / "settings.json").read_bytes() == before
