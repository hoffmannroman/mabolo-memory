import io
import json

import pytest

from conftest import entry_text
from mabolo import cli, context, schema
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
    assert "old-news" not in out, "a year old, not pinned, no project"
    assert "1 entry not shown" in out
    assert "1 of 2 entries shown" in out
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
    assert "written-just-now" not in out, "no moment was given, so nothing is recent"


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
    assert "omitted 1 of 2 entries, 0 cut by the budget" in out


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


def test_context_reports_a_core_over_its_budget_as_a_finding(vault, capsys):
    """Nothing was dropped, so it is not a failure. But somebody has to decide
    which rule stops being one, and nobody decides what nobody is told."""
    context_vault(vault)
    assert main(["context", str(vault.root), "--as-of", "2026-09-18", "--core-budget", "5"]) == 1
    out = capsys.readouterr().out
    assert "The core is over its budget" in out
    assert "Nothing was dropped" in out
    assert "1 standing rule pinned, about" in out


def test_context_is_clean_when_the_core_fits(vault, capsys):
    context_vault(vault)
    assert main(["context", str(vault.root), "--as-of", "2026-09-18"]) == 0
    out = capsys.readouterr().out
    assert "over its budget" not in out
    assert "of the 300 the core is meant to cost" in out


def test_context_refuses_a_core_budget_that_is_not_one(vault, capsys):
    context_vault(vault)
    assert main(["context", str(vault.root), "--core-budget", "0"]) == 2
    assert "at least 1" in capsys.readouterr().err


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

    real = cli.index_module.documents_of
    monkeypatch.setattr(
        cli.index_module, "documents_of", lambda entries: (time.sleep(0.4), real(entries))[1]
    )
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
