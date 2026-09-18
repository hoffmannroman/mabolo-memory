import pytest

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
