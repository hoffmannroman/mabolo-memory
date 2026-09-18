import pytest

from mabolo.config import Config, default_config_path, default_vault_path
from mabolo.errors import MaboloError


def test_the_default_vault_sits_next_to_the_home_directory():
    assert default_vault_path().name == "mabolo-data"
    assert default_config_path().parts[-2:] == ("mabolo", "config.toml")


def test_a_configuration_round_trips_through_the_file(tmp_path):
    config = Config(
        vault=tmp_path / "vault",
        actor="human:someone",
        areas=["persona", "design"],
        remote_url="git@example.invalid:me/mabolo-data.git",
        remote_branch="main",
    )
    path = config.save(tmp_path / "config.toml")
    loaded = Config.load(path)
    assert loaded.vault == config.vault
    assert loaded.actor == "human:someone"
    assert loaded.areas == ["persona", "design"]
    assert loaded.remote_url == config.remote_url


def test_an_empty_remote_means_a_local_vault(tmp_path):
    path = Config(vault=tmp_path / "v", actor="human:x").save(tmp_path / "c.toml")
    assert Config.load(path).remote_url is None


def test_the_file_is_not_readable_by_other_users(tmp_path):
    path = Config(vault=tmp_path / "v", actor="human:x").save(tmp_path / "c.toml")
    assert path.stat().st_mode & 0o077 == 0


def test_a_missing_configuration_says_what_to_run(tmp_path):
    with pytest.raises(MaboloError, match="mabolo init"):
        Config.load(tmp_path / "nothing.toml")


def test_broken_toml_names_the_file(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("vault = [unclosed\n", encoding="utf-8")
    with pytest.raises(MaboloError, match="readable TOML"):
        Config.load(path)


def test_a_configuration_without_a_vault_is_refused(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('actor = "human:x"\n', encoding="utf-8")
    with pytest.raises(MaboloError, match="no vault"):
        Config.load(path)


def test_the_environment_can_point_at_another_vault(tmp_path, monkeypatch):
    path = Config(vault=tmp_path / "v", actor="human:x").save(tmp_path / "c.toml")
    monkeypatch.setenv("MABOLO_VAULT", str(tmp_path / "other"))
    assert Config.load(path).vault == tmp_path / "other"


def test_values_with_quotes_survive_being_written(tmp_path):
    config = Config(vault=tmp_path / 'a"b', actor="human:x")
    path = config.save(tmp_path / "c.toml")
    assert Config.load(path).vault == tmp_path / 'a"b'


def test_project_areas_are_known_without_being_listed(tmp_path):
    config = Config(vault=tmp_path / "v", actor="human:x")
    assert config.area_is_known("project/anything")
    assert not config.area_is_known("nonsense")


def test_a_configuration_with_a_path_as_an_area_is_refused(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(f'vault = "{tmp_path}/v"\nareas = ["../elsewhere"]\n', encoding="utf-8")
    with pytest.raises(MaboloError, match="plain folder name"):
        Config.load(path)


def test_the_configuration_is_not_written_through_a_symlink(tmp_path):
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text("not yours\n", encoding="utf-8")
    link = tmp_path / "config.toml"
    link.symlink_to(elsewhere)
    with pytest.raises(MaboloError, match="symlink"):
        Config(vault=tmp_path / "v", actor="human:x").save(link)
    assert elsewhere.read_text(encoding="utf-8") == "not yours\n"


def test_a_setting_from_a_newer_mabolo_survives_a_rewrite(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        f'version = 1\nvault = "{tmp_path}/v"\nactor = "human:x"\nsomething_new = "keep me"\n',
        encoding="utf-8",
    )
    config = Config.load(path)
    config.save(path)
    assert "keep me" in path.read_text(encoding="utf-8")
    assert Config.load(path).unknown["something_new"] == "keep me"


def test_a_configuration_from_a_newer_version_is_not_rewritten(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(f'version = 99\nvault = "{tmp_path}/v"\nactor = "human:x"\n', encoding="utf-8")
    with pytest.raises(MaboloError, match="newer Mabolo"):
        Config.load(path)


def test_an_actor_that_is_not_a_person_is_refused(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(f'vault = "{tmp_path}/v"\nactor = "someone"\n', encoding="utf-8")
    with pytest.raises(MaboloError, match="human:"):
        Config.load(path)


def test_deleting_every_area_is_allowed_because_the_readme_says_so(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(f'vault = "{tmp_path}/v"\nactor = "human:x"\nareas = []\n', encoding="utf-8")
    assert Config.load(path).areas == []


def test_a_vault_from_the_environment_is_never_written_into_the_file(tmp_path, monkeypatch):
    path = tmp_path / "c.toml"
    Config(vault=tmp_path / "real", actor="human:x").save(path)
    monkeypatch.setenv("MABOLO_VAULT", str(tmp_path / "just-for-now"))
    config = Config.load(path)
    assert config.vault == tmp_path / "just-for-now"
    with pytest.raises(MaboloError, match="this run only"):
        config.save(path)


def test_a_control_character_in_a_value_does_not_break_the_file(tmp_path):
    """An actor is validated, but a path is whatever the filesystem allows."""
    path = tmp_path / "c.toml"
    odd = tmp_path / "vault\x01name"
    Config(vault=odd, actor="human:x").save(path)
    assert Config.load(path).vault == odd


def test_only_a_person_can_be_configured_as_the_approver(tmp_path):
    """The general actor rule also allows a tool, and the validator then refuses it."""
    for actor in ("process:robot", "some-tool/1.0", "someone"):
        with pytest.raises(MaboloError, match="human:"):
            Config.from_dict({"vault": str(tmp_path / "v"), "actor": actor})
    assert Config.from_dict({"vault": str(tmp_path / "v"), "actor": "human:alex"}).actor == "human:alex"


def test_a_setting_this_version_cannot_write_back_stops_the_rewrite(tmp_path):
    """Falling back to str() turned a date into text and a table into a repr."""
    path = tmp_path / "c.toml"
    path.write_text(
        'vault = "/tmp/v"\nactor = "human:alex"\nwhen = 2026-01-01T00:00:00Z\n',
        encoding="utf-8",
    )
    config = Config.load(path)
    with pytest.raises(MaboloError, match="newer Mabolo"):
        config.save(path)
    assert "2026-01-01T00:00:00Z" in path.read_text(encoding="utf-8")


def test_an_unknown_key_under_remote_survives_a_rewrite(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        'vault = "/tmp/v"\nactor = "human:alex"\n\n[remote]\nbranch = "main"\nfuture_flag = true\n',
        encoding="utf-8",
    )
    Config.load(path).save(path)
    assert "future_flag = true" in path.read_text(encoding="utf-8")


def test_the_vault_language_defaults_to_english_and_round_trips(tmp_path):
    config = Config.default(vault=tmp_path / "v", actor="human:someone")
    assert config.language == "en"
    config.language = "de"
    saved = config.save(tmp_path / "c.toml")
    assert Config.load(saved).language == "de"


def test_a_language_that_is_not_a_code_is_refused(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(f'vault = "{tmp_path}/v"\nactor = "human:someone"\nlanguage = "Deutsch"\n',
                    encoding="utf-8")
    with pytest.raises(MaboloError, match="short code"):
        Config.load(path)
