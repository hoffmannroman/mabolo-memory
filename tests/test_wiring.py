"""Wiring a client up: what is read, what is refused, and what is written.

Every case here is about somebody else's file. Mabolo adds two things to it and
has no business with anything else in it, so most of what is asserted is what
did not change: the keys that were already there, their order, the comments in
a TOML file, a hook the person configured themselves, and the file's mode.

No test may reach the real configuration of a real client. Every path below is
built from `tmp_path`, and the fixture in `conftest.py` points HOME there as
well, so a default that slipped through would land in the test's own directory
rather than in somebody's editor.
"""

import json
import shlex
import tomllib
from pathlib import Path

import pytest

from mabolo import cli, wiring
from mabolo.errors import MaboloError

REPO = Path(__file__).resolve().parents[1]

OTHER_HOOK = {
    "hooks": [{"type": "command", "command": "notify-me --on-prompt", "timeout": 4}]
}


def settings_of(home: Path, client: wiring.Client) -> Path:
    return home / client.hooks_file


def servers_of(home: Path, client: wiring.Client) -> Path:
    return home / client.server_file


def put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def wire(home: Path, client: wiring.Client, *, rewire: bool = False) -> list[bool]:
    """Survey then write, which is the order `init` is required to work in."""
    return [wiring.apply(f, rewire=rewire) for f in wiring.survey(client, home=home)]


def fingerprint(paths) -> dict:
    """Bytes and inode per file, so that "not rewritten" is a real assertion."""
    return {p: (p.read_bytes(), p.stat().st_ino) for p in paths}


def test_a_configuration_with_nothing_of_ours_keeps_every_key_it_had_and_its_order(tmp_path):
    """The file belongs to the person. Mabolo adds one key to it and has no
    opinion about the rest, including keys this version has never heard of: a
    tool that rewrites a settings file from its own idea of what belongs in one
    deletes whatever the next version of the client invented."""
    settings = put(
        settings_of(tmp_path, wiring.CLAUDE_CODE),
        json.dumps({"zzz": {"deep": [1, 2]}, "model": "opus", "aaa": True}, indent=2),
    )
    wire(tmp_path, wiring.CLAUDE_CODE)

    after = json.loads(settings.read_text(encoding="utf-8"))
    assert list(after) == ["zzz", "model", "aaa", "hooks"]
    assert after["zzz"] == {"deep": [1, 2]}
    assert after["model"] == "opus"
    assert after["aaa"] is True
    assert after["hooks"] == wiring.hooks_block()


def test_running_it_twice_changes_nothing_the_second_time(tmp_path):
    """A second `init` on a wired machine has to be a no-op down to the file
    itself. Rewriting it with the same bytes still replaces the file, and then
    nobody can tell from the outside whether anything was decided."""
    touched = []
    for client in wiring.CLIENTS:
        wire(tmp_path, client)
        touched += [settings_of(tmp_path, client), servers_of(tmp_path, client)]
    before = fingerprint(touched)

    for client in wiring.CLIENTS:
        again = wiring.survey(client, home=tmp_path)
        assert [f.verdict for f in again] == [wiring.SAME, wiring.SAME]
        assert all(f.settled for f in again)
        assert [wiring.apply(f) for f in again] == [False, False]
    assert fingerprint(touched) == before


def test_an_entry_of_ours_that_differs_is_left_alone_and_both_versions_are_reported(tmp_path):
    """Somebody changed our hook, or an older Mabolo wrote it. Replacing it
    quietly is the repair this project refuses, and a caller that cannot show
    the person what is there cannot ask them to choose."""
    theirs = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "mabolo hook prompt --no-record"}]}
            ]
        }
    }
    settings = put(settings_of(tmp_path, wiring.CLAUDE_CODE), json.dumps(theirs, indent=2))
    before = settings.read_bytes()

    found = wiring.survey(wiring.CLAUDE_CODE, home=tmp_path)[0]
    assert found.verdict == wiring.DIFFERENT
    shown = "\n".join(found.render())
    assert "mabolo hook prompt --no-record" in shown
    assert "mabolo hook session-start" in shown
    assert "--rewire" in shown

    with pytest.raises(MaboloError) as refused:
        wiring.apply(found)
    assert str(settings) in str(refused.value)
    assert "--rewire" in str(refused.value)
    assert settings.read_bytes() == before


def test_a_file_that_is_not_valid_json_is_a_named_finding_and_not_a_crash(tmp_path):
    """`init` walks over whatever is on the machine. A stray comma in a file it
    was only ever going to add a key to must not end the command, and the
    person needs the name of the file to go and look at it."""
    settings = put(settings_of(tmp_path, wiring.CLAUDE_CODE), '{"hooks": {,}}')

    found = wiring.survey(wiring.CLAUDE_CODE, home=tmp_path)[0]
    assert found.verdict == wiring.UNREADABLE
    assert str(settings) in found.note
    assert "JSON" in found.note
    assert str(settings) in "\n".join(found.render())
    with pytest.raises(MaboloError):
        wiring.apply(found, rewire=True)


def test_a_rewire_replaces_our_older_entry_instead_of_adding_a_second_one(tmp_path):
    """The one case where a write runs over a wiring that is already there. A
    merge that only appends leaves the client running our session start hook
    twice, and the second version of it is the one that was supposed to be
    replaced: the older command, still being called, still being believed."""
    for client in wiring.CLIENTS:
        wire(tmp_path, client)
    wired = {p: p.read_bytes() for c in wiring.CLIENTS for p in (settings_of(tmp_path, c),)}

    for settings in wired:
        aged = settings.read_text(encoding="utf-8").replace('"timeout": 6', '"timeout": 99')
        settings.write_text(aged, encoding="utf-8")
    for client in wiring.CLIENTS:
        found = wiring.survey(client, home=tmp_path)[0]
        assert found.verdict == wiring.DIFFERENT
        wiring.apply(found, rewire=True)

    assert {p: p.read_bytes() for p in wired} == wired


def test_the_plugins_in_this_repository_are_what_this_module_renders(tmp_path):
    """The plugin and `init` are two doors onto one description. A hand edit to
    a file under `plugins/` is how the two start telling a client different
    things, and the client says nothing when they do: the hook is simply never
    called, or it is called under a name that does not exist."""
    for client in wiring.CLIENTS:
        root = REPO / "plugins" / client.plugin_dir
        rendered = wiring.plugin_files(client)
        on_disk = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        assert on_disk == set(rendered)
        for relative, text in rendered.items():
            assert (root / relative).read_text(encoding="utf-8") == text, (
                f"{relative} was edited by hand. Run `python -m mabolo.wiring` to render it."
            )


def test_the_marketplaces_in_this_repository_are_what_this_module_renders(tmp_path):
    """Same guarantee as the plugins, one level up. A marketplace entry that
    points at a directory the plugin no longer lives in installs nothing, and
    one that names a different version than the manifest it hands over tells a
    person something the plugin itself contradicts."""
    rendered = wiring.marketplace_files()
    assert set(rendered) == {c.marketplace_path for c in wiring.CLIENTS}
    for relative, text in rendered.items():
        assert (REPO / relative).read_text(encoding="utf-8") == text, (
            f"{relative} was edited by hand. Run `python -m mabolo.wiring` to render it."
        )
    for client in wiring.CLIENTS:
        listed = json.loads((REPO / client.marketplace_path).read_text(encoding="utf-8"))
        offered = listed["plugins"][0]["source"]
        where = offered if isinstance(offered, str) else offered["path"]
        assert (REPO / where).is_dir()
        assert (REPO / where / client.manifest_path).is_file()


def test_the_plugin_and_the_installer_hand_a_client_the_same_wiring(tmp_path):
    """Same again from the other end: not that the files match this module, but
    that what the plugin carries and what `init` leaves behind in a client's own
    configuration are the same entries."""
    for client in wiring.CLIENTS:
        wire(tmp_path, client)
        root = REPO / "plugins" / client.plugin_dir
        from_plugin = json.loads((root / client.plugin_hooks_path).read_text(encoding="utf-8"))
        written = json.loads(settings_of(tmp_path, client).read_text(encoding="utf-8"))
        assert from_plugin[wiring.HOOKS_KEY] == written[wiring.HOOKS_KEY]

        served = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
        entry = served["mcpServers"][wiring.SERVER_NAME]
        if client.server_form == wiring.TOML_FORM:
            held = tomllib.loads(servers_of(tmp_path, client).read_text(encoding="utf-8"))
        else:
            held = json.loads(servers_of(tmp_path, client).read_text(encoding="utf-8"))
        assert held[client.server_key][wiring.SERVER_NAME] == entry


def test_every_command_we_wire_is_one_the_tool_actually_answers(tmp_path):
    """The wiring names subcommands as text, and text does not fail to compile.
    A renamed subcommand would leave a client calling something that exits 2
    before it ever reaches the code, on every prompt, silently."""
    parser = cli.build_parser()
    for hook in wiring.hook_commands():
        words = shlex.split(hook.command)
        assert words[0] == wiring.EXECUTABLE
        assert parser.parse_args(words[1:]).event == words[2]
    entry = wiring.server_entry(wiring.CLAUDE_CODE)
    assert entry["command"] == wiring.EXECUTABLE
    assert parser.parse_args(entry["args"]).func is cli.cmd_serve


def test_a_hook_the_person_configured_themselves_keeps_its_place(tmp_path):
    """Hooks in an event run in the order they are listed. Appending ours is
    the only placement that cannot change what somebody else's hook sees."""
    put(
        settings_of(tmp_path, wiring.CLAUDE_CODE),
        json.dumps({"hooks": {"UserPromptSubmit": [OTHER_HOOK]}}, indent=2),
    )
    wire(tmp_path, wiring.CLAUDE_CODE)

    after = json.loads(settings_of(tmp_path, wiring.CLAUDE_CODE).read_text(encoding="utf-8"))
    groups = after["hooks"]["UserPromptSubmit"]
    assert groups[0] == OTHER_HOOK
    assert groups[1:] == wiring.hooks_block()["UserPromptSubmit"]


def test_a_hook_of_ours_sharing_a_group_with_another_command_is_never_touched(tmp_path):
    """Our command in the same group as somebody else's means the group is not
    only ours, and dropping it to write our own would delete their command.
    Refusing is the answer, even when the caller asked for a rewire."""
    shared = {
        "hooks": [
            {"type": "command", "command": "mabolo hook prompt"},
            {"type": "command", "command": "notify-me --on-prompt"},
        ]
    }
    settings = put(
        settings_of(tmp_path, wiring.CLAUDE_CODE),
        json.dumps({"hooks": {"UserPromptSubmit": [shared]}}, indent=2),
    )
    before = settings.read_bytes()

    found = wiring.survey(wiring.CLAUDE_CODE, home=tmp_path)[0]
    assert found.verdict == wiring.DIFFERENT
    with pytest.raises(MaboloError):
        wiring.apply(found, rewire=True)
    assert settings.read_bytes() == before


def test_a_toml_configuration_keeps_its_comments_and_every_table_but_ours(tmp_path):
    """Codex keeps its servers in TOML. Reading it with `tomllib` and writing
    the result back would parse, run, and silently delete every comment and
    every blank line the person put in their own configuration."""
    original = (
        "# the model I actually want\n"
        'model = "a-model"\n'
        "\n"
        "[mcp_servers.theirs]\n"
        'command = "their-server"  # started by hand once\n'
    )
    config = put(servers_of(tmp_path, wiring.CODEX), original)

    wire(tmp_path, wiring.CODEX)
    after = config.read_text(encoding="utf-8")
    assert after.startswith(original)
    assert "# the model I actually want" in after
    assert "# started by hand once" in after
    held = tomllib.loads(after)
    assert held["model"] == "a-model"
    assert held["mcp_servers"]["theirs"] == {"command": "their-server"}
    assert held["mcp_servers"][wiring.SERVER_NAME] == wiring.server_entry(wiring.CODEX)


def test_replacing_our_toml_table_stops_at_the_next_table(tmp_path):
    """A table ends where the next one begins. Getting that wrong swallows
    whatever follows ours, and what follows is somebody else's server."""
    config = put(
        servers_of(tmp_path, wiring.CODEX),
        "[mcp_servers.mabolo]\n"
        'command = "an-old-path"\n'
        "\n"
        "[mcp_servers.theirs]\n"
        'command = "their-server"\n'
        "\n"
        "[projects]\n"
        'trust = "high"\n',
    )

    found = wiring.survey(wiring.CODEX, home=tmp_path)[1]
    assert found.verdict == wiring.DIFFERENT
    wiring.apply(found, rewire=True)

    held = tomllib.loads(config.read_text(encoding="utf-8"))
    assert held["mcp_servers"][wiring.SERVER_NAME] == wiring.server_entry(wiring.CODEX)
    assert held["mcp_servers"]["theirs"] == {"command": "their-server"}
    assert held["projects"] == {"trust": "high"}


def test_a_file_keeps_the_mode_it_had_and_a_new_one_is_private(tmp_path):
    """These files say what runs at the start of every session, so a new one is
    not world readable. An existing one keeps whatever the person chose: a
    wiring pass that widens a mode is a change nobody asked for and nobody sees."""
    theirs = put(settings_of(tmp_path, wiring.CLAUDE_CODE), "{}")
    theirs.chmod(0o640)
    wire(tmp_path, wiring.CLAUDE_CODE)

    assert theirs.stat().st_mode & 0o777 == 0o640
    assert servers_of(tmp_path, wiring.CLAUDE_CODE).stat().st_mode & 0o777 == 0o600


def test_a_settings_file_that_is_a_link_is_written_through_and_stays_a_link(tmp_path):
    """People keep their client settings in a dotfiles repository and link them
    into place. Replacing the link is atomic, correct, and leaves them with a
    plain file where their link was and a repository that no longer holds their
    settings, which is the kind of tidying nobody notices until a sync."""
    kept = put(tmp_path / "dotfiles" / "settings.json", json.dumps({"model": "opus"}))
    settings = settings_of(tmp_path, wiring.CLAUDE_CODE)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.symlink_to(kept)

    wire(tmp_path, wiring.CLAUDE_CODE)

    assert settings.is_symlink()
    assert json.loads(kept.read_text(encoding="utf-8"))["hooks"] == wiring.hooks_block()


def test_a_client_that_is_not_on_this_machine_is_not_a_finding(tmp_path):
    """`init` wires up what it finds. A client nobody installed is not a
    failure to report, and asking about it is a question with one answer."""
    assert wiring.installed(home=tmp_path) == ()
    (tmp_path / wiring.CODEX.home_marker).mkdir()
    assert wiring.installed(home=tmp_path) == (wiring.CODEX,)


def test_a_plugin_never_carries_a_manifest_at_its_root(tmp_path):
    """A `plugin.json` there makes one client read it instead of the manifest
    in its own directory and silently disable every hook the plugin has. A
    failure with no error message is the one this project cannot afford."""
    for client in wiring.CLIENTS:
        files = wiring.plugin_files(client)
        assert wiring.FORBIDDEN_AT_ROOT not in files
        assert not any(
            name.count("/") == 0 and name == wiring.FORBIDDEN_AT_ROOT for name in files
        )


def test_the_hook_file_sits_where_each_client_looks_for_it(tmp_path):
    """Both clients look in `hooks/hooks.json` by default. The Codex plugin had
    it at the root, from the CLI's embedded specification, and a plugin whose
    hooks are never discovered starts its MCP server and runs none of them:
    no prompt notes, no session context, no design rule, and no error."""
    for client in wiring.CLIENTS:
        assert client.plugin_hooks_path == "hooks/hooks.json"
        assert client.plugin_hooks_path in wiring.plugin_files(client)


def test_the_hook_contracts_are_not_fetched_back_out_of_the_command_line():
    """They lived with the commands, so this module reached back into its own
    caller through a deferred import to avoid a cycle. A comment explaining why
    an import has to be late is a comment about an arrow pointing the wrong
    way."""
    source = Path(wiring.__file__).read_text(encoding="utf-8")
    assert "from .cli import" not in source
    events = {one.event for one in wiring.hook_commands()}
    assert events == {"SessionStart", "UserPromptSubmit", "PreToolUse"}
