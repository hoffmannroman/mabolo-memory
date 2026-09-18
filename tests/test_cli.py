import pytest

from conftest import entry_text
from mabolo.cli import main
from mabolo.config import Config


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
