"""What a client is told about Mabolo, and the one description both doors cut from.

Mabolo reaches a client two ways. A person can install the plugin that ships in
`plugins/`, or run `mabolo init`, which writes the same entries into the
client's own configuration. Two doors, one description: everything a client is
ever told lives in this module, and the files under `plugins/` are rendered from
it, so a test can prove they still agree. A plugin that starts a hook the
installer never writes, or an installer that writes a subcommand the plugin
spells differently, is a bug nobody sees: the wiring stays valid JSON, the
client stays quiet, and the memory simply stops arriving.

Four rules that are not obvious:

* **Mabolo owns its own keys and nothing else.** One MCP server named `mabolo`,
  and hook entries whose command starts with `mabolo hook`. Everything else in
  those files is somebody's decision, including keys this version has never
  heard of, and it comes through a write untouched and in its own order.
* **Deciding is separate from writing.** `survey` reads and compares, `apply`
  writes, and nothing decides while it writes. `init` prints what `survey`
  found before `apply` is allowed to touch a file, because a plan printed by
  the same pass that writes is not a plan, it is a receipt.
* **An entry of ours that differs is left alone.** Somebody edited it, or an
  older Mabolo wrote it, and quietly replacing it is the repair this project
  refuses. Both versions are reported and the caller stops: a client wired half
  our way and half somebody else's is not a success.
* **The two clients do not share a file format.** Claude Code keeps hooks and
  servers in JSON, Codex keeps its servers in TOML. The description above is
  one object either way; only the last step of writing it down differs, and a
  TOML file is edited by replacing our own table and no other byte, because
  reading TOML and writing it back would throw away every comment in it.

What is not established with confidence is marked with `#: UNCERTAIN` and sits
in exactly one constant, so a wrong guess is one line to correct.
"""

from __future__ import annotations

import json
import math
import os
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .errors import MaboloError

#: The executable, as a client has to be able to find it on PATH. `uv tool
#: install` puts it there; a path into a virtual environment would be wired to
#: a directory that a reinstall moves.
EXECUTABLE = "mabolo"

#: The MCP server's name, which is also the prefix its tools appear under. The
#: short name is the one that gets typed, and this is the key we own.
SERVER_NAME = "mabolo"

#: What makes a hook entry ours. Not the file, not its position: the command.
#: A person may keep their own hooks in the same event, and they stay.
HOOK_PREFIX = f"{EXECUTABLE} hook"

#: A client kills a hook that overruns. Mabolo gives up on its own deadline and
#: still exits 0, so the client's limit is set above ours: whoever gives up
#: first decides what the session sees, and ours can still say something.
GRACE_SECONDS = 1

#: UNCERTAIN. No matcher, so the file hook runs ahead of every tool call rather
#: than only the ones that open a file. A matcher is a list of tool names, and
#: the two clients do not share that vocabulary, so one matcher here would be
#: right for one client and silently wrong for the other. The hook answers
#: silence for a shape it does not recognise and gives up after two seconds,
#: which is the cheaper of the two mistakes. Narrowing this is one constant.
PRETOOL_MATCHER: str | None = None

#: What a comparison can say.
ABSENT = "absent"
SAME = "as we would write it"
DIFFERENT = "different"
UNREADABLE = "unreadable"

#: How a file is spelled on disk.
JSON_FORM = "json"
TOML_FORM = "toml"

#: Which of our two things a slot holds.
HOOKS = "hooks"
SERVER = "server"

#: The key the hook block sits under, in a plugin's hook file and in a client's
#: settings alike. Both supported clients spell it the same.
HOOKS_KEY = "hooks"


@dataclass(frozen=True)
class HookCommand:
    """One command a client runs, and the event that runs it."""

    event: str
    command: str
    timeout: int


#: What a session start may take before it is given up on. The client is
#: waiting on it, so the number is a promise to the person, not to the tool.
HOOK_SECONDS = 5

#: What a prompt may take. Less than half of a session start, because this one
#: runs ahead of every single prompt and the person is mid sentence: a session
#: start happens once and is expected to take a moment, while a pause here is
#: felt every time and is blamed on the agent.
PROMPT_SECONDS = 2


@dataclass(frozen=True)
class HookContract:
    """What tells one hook from another. Everything else they share.

    Three values rather than three flags. A flag says "behave differently here"
    and leaves a reader to work out where; these say which event is being
    answered, how long the client will wait, and what to call this in a message
    to a person. What a hook does when there is no configuration is not in
    here, because it is not a variation on a shared behaviour: it lives in the
    payload function, where the answer to "and then what do we say" belongs.

    It lives here rather than with the commands because two readers need it and
    they are on two sides of one dependency: the command answers the event, and
    this module writes the event's name into somebody's configuration. It was
    in the command line first, which meant this module reached back into its
    own caller through a deferred import to avoid a cycle. A comment explaining
    why an import has to be late is a comment about an arrow pointing the wrong
    way.
    """

    event: str
    label: str
    seconds: float


SESSION_START = HookContract(event="SessionStart", label="session start", seconds=HOOK_SECONDS)
PROMPT = HookContract(event="UserPromptSubmit", label="prompt hook", seconds=PROMPT_SECONDS)
PRETOOL = HookContract(event="PreToolUse", label="file hook", seconds=PROMPT_SECONDS)


def hook_commands() -> tuple[HookCommand, ...]:
    """The three hooks, from the contracts the commands answer.

    One source for the event name and the deadline. A hook wired to one event
    while the command answers another is one rename away from a payload that
    arrives under a name the client does not recognise, and nothing about that
    failure looks like a failure.
    """
    pairs = (("session-start", SESSION_START), ("prompt", PROMPT), ("pretool", PRETOOL))
    return tuple(
        HookCommand(
            event=contract.event,
            command=f"{HOOK_PREFIX} {subcommand}",
            timeout=int(math.ceil(contract.seconds)) + GRACE_SECONDS,
        )
        for subcommand, contract in pairs
    )


def hooks_block() -> dict[str, list[dict[str, Any]]]:
    """The hook entries, in the shape both clients read them in.

    Event name to a list of matcher groups, each group holding the commands to
    run. It is the same object in a plugin's hook file and in a client's
    settings, which is what makes the two doors one description.
    """
    block: dict[str, list[dict[str, Any]]] = {}
    for hook in hook_commands():
        group: dict[str, Any] = {}
        if hook.event == "PreToolUse" and PRETOOL_MATCHER:
            group["matcher"] = PRETOOL_MATCHER
        group["hooks"] = [{"type": "command", "command": hook.command, "timeout": hook.timeout}]
        block.setdefault(hook.event, []).append(group)
    return block


@dataclass(frozen=True)
class Client:
    """One client, and where it keeps the two things Mabolo has to say.

    A client is data rather than a subclass, because the difference between the
    two supported ones is four paths and two spellings. The moment it is a
    class hierarchy, the thing that has to stay identical between them, the
    wiring itself, is the thing that can quietly differ.
    """

    key: str
    label: str
    #: The directory in this repository the plugin lives in.
    plugin_dir: str
    #: Where the plugin manifest sits inside that directory.
    manifest_path: str
    #: Where the plugin's hook file sits inside that directory.
    plugin_hooks_path: str
    #: Whether the manifest names the hook file. Claude Code expects the path;
    #: Codex discovers `hooks.json` at the plugin root and its validator
    #: rejects the field, so naming it there would fail the plugin outright.
    manifest_names_hooks: bool
    #: What a stdio server entry sets `type` to, or None when the client's own
    #: examples never set it. Guessing a value a client does not know is how a
    #: server ends up not starting at all.
    stdio_type: str | None
    #: The home relative file holding the user's hooks, and its format.
    hooks_file: str
    hooks_form: str
    #: The home relative file holding the user's MCP servers, its format, and
    #: the key the servers live under in it.
    server_file: str
    server_form: str
    server_key: str
    #: The directory whose existence says this client is installed at all.
    home_marker: str


CLAUDE_CODE = Client(
    key="claude-code",
    label="Claude Code",
    plugin_dir="claude-code",
    manifest_path=".claude-plugin/plugin.json",
    plugin_hooks_path="hooks/hooks.json",
    manifest_names_hooks=True,
    stdio_type="stdio",
    hooks_file=".claude/settings.json",
    hooks_form=JSON_FORM,
    #: Not `.claude/settings.json`: that file holds hooks, and a user scoped
    #: MCP server lives one level up in `.claude.json`. Writing it into the
    #: settings file is valid JSON that the client never reads.
    server_file=".claude.json",
    server_form=JSON_FORM,
    server_key="mcpServers",
    home_marker=".claude",
)

CODEX = Client(
    key="codex",
    label="Codex",
    plugin_dir="codex",
    manifest_path=".codex-plugin/plugin.json",
    #: `hooks/hooks.json`, the path the client looks in by default, and the
    #: same layout the other client uses. It was written at the plugin root
    #: first, from the CLI's embedded specification; the vendor's published
    #: packaging rules say the default is `hooks/hooks.json` and that a plugin
    #: storing them there needs no manifest entry, and a plugin whose hooks are
    #: never discovered is the silent half wiring this project is against.
    #: One file only: the client loads every hook source it finds, so a second
    #: copy would run every hook twice and write every prompt note twice.
    plugin_hooks_path="hooks/hooks.json",
    manifest_names_hooks=False,
    stdio_type=None,
    #: UNCERTAIN, and the only thing here that still is. Codex's own
    #: configuration is TOML and holds a `hooks` key, and the binary also
    #: carries the name `hooks.json` next to its Codex home paths. This is the
    #: reading that keeps the hook block identical to the plugin's. If it is
    #: wrong, this line is the whole correction: nothing else depends on it.
    hooks_file=".codex/hooks.json",
    hooks_form=JSON_FORM,
    server_file=".codex/config.toml",
    server_form=TOML_FORM,
    server_key="mcp_servers",
    home_marker=".codex",
)

CLIENTS: tuple[Client, ...] = (CLAUDE_CODE, CODEX)


def client(key: str) -> Client:
    for known in CLIENTS:
        if known.key == key:
            return known
    raise MaboloError(f"{key!r} is not a client Mabolo knows how to wire up")


def installed(home: Path | None = None) -> tuple[Client, ...]:
    """The clients that are on this machine, by the directory they keep.

    `init` wires up what it finds rather than asking about clients nobody uses,
    and a client that is not installed is not a failure to report.
    """
    where = Path(home) if home is not None else Path.home()
    return tuple(c for c in CLIENTS if (where / c.home_marker).is_dir())


def server_entry(for_client: Client) -> dict[str, Any]:
    """The MCP server entry, as that client spells a program on stdin and stdout."""
    entry: dict[str, Any] = {}
    if for_client.stdio_type:
        entry["type"] = for_client.stdio_type
    entry["command"] = EXECUTABLE
    entry["args"] = ["serve"]
    return entry


# The plugins, rendered from the same description


DISPLAY_NAME = "Mabolo Memory"
SUMMARY = "A memory for coding agents that you can read, edit and delete yourself."
LONG_SUMMARY = (
    "Keeps what your agent learned as Markdown files in a Git repository, hands a "
    "session a map of them at the start, and writes nothing without a sentence you typed."
)
KEYWORDS = ["memory", "markdown", "git", "okf", "mcp"]
STARTERS = [
    "What do you already know about this project?",
    "Remember that we deploy from main only",
    "Where did that belief come from?",
]


def _json_text(value: Any) -> str:
    """JSON as a file, not as a payload: two spaces and a final newline.

    A rendered file is compared against a file on disk byte for byte, so the
    rendering has to be the one a person's editor would leave behind.
    """
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


#: A file this name must never appear at a plugin's root. The client reads a
#: manifest there instead of the one in its own directory and silently disables
#: every hook the plugin has, which is a failure with no error message at all.
FORBIDDEN_AT_ROOT = "plugin.json"


def plugin_files(for_client: Client) -> dict[str, str]:
    """Every file of that client's plugin, as a path inside it to its text.

    Nothing here is hand written, which is the point: the manifest, the hook
    file and the server file are the same objects `init` writes, wrapped the way
    a plugin wraps them. A test compares this against `plugins/` on disk, so a
    hand edit to either side is a failing test rather than a client that is
    wired two different ways depending on how it was installed.
    """
    manifest: dict[str, Any] = {"name": SERVER_NAME}
    if for_client.manifest_names_hooks:
        manifest["displayName"] = DISPLAY_NAME
    manifest.update(
        {
            "version": __version__,
            "description": SUMMARY,
            "author": {"name": DISPLAY_NAME},
            "license": "MIT",
            "keywords": list(KEYWORDS),
        }
    )
    if for_client.manifest_names_hooks:
        manifest["hooks"] = f"./{for_client.plugin_hooks_path}"
    manifest["mcpServers"] = "./.mcp.json"
    if not for_client.manifest_names_hooks:
        # Codex reads the presentation of a plugin out of `interface`, and its
        # validator wants the block filled in rather than present.
        manifest["interface"] = {
            "displayName": DISPLAY_NAME,
            "shortDescription": "Memory you can read, edit and delete yourself",
            "longDescription": LONG_SUMMARY,
            "developerName": DISPLAY_NAME,
            "category": "Developer Tools",
            "capabilities": ["Interactive", "Read", "Write"],
            "defaultPrompt": list(STARTERS),
        }
    return {
        for_client.manifest_path: _json_text(manifest),
        for_client.plugin_hooks_path: _json_text({HOOKS_KEY: hooks_block()}),
        ".mcp.json": _json_text({"mcpServers": {SERVER_NAME: server_entry(for_client)}}),
    }


def write_plugins(root: Path | None = None) -> list[Path]:
    """Write the plugin directories out. The only writer aimed at this repository.

    It refuses anywhere that is not a checkout, because the same module is
    installed into a virtual environment on a stranger's machine, and a
    regenerator that resolves its target from `__file__` would otherwise write
    into whatever directory the package happened to be unpacked in.
    """
    where = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    if not (where / "pyproject.toml").is_file():
        raise MaboloError(f"{where} is not a checkout of this repository, nothing written")
    written = []
    for known in CLIENTS:
        for relative, text in plugin_files(known).items():
            target = where / "plugins" / known.plugin_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_atomically(target, text)
            written.append(target)
    return written


# Reading what a client's configuration holds under our keys


@dataclass(frozen=True)
class Slot:
    """One file, and the one thing of ours that belongs in it."""

    client: Client
    holds: str  # HOOKS | SERVER
    path: Path

    @property
    def label(self) -> str:
        what = "the hooks" if self.holds == HOOKS else f"the {SERVER_NAME} MCP server"
        return f"{self.client.label}: {what} in {self.path}"

    @property
    def form(self) -> str:
        return self.client.hooks_form if self.holds == HOOKS else self.client.server_form


def slots(for_client: Client, home: Path | None = None) -> tuple[Slot, ...]:
    """The files `init` would touch for that client, and nothing else."""
    where = Path(home) if home is not None else Path.home()
    return (
        Slot(client=for_client, holds=HOOKS, path=where / for_client.hooks_file),
        Slot(client=for_client, holds=SERVER, path=where / for_client.server_file),
    )


def wanted(slot: Slot) -> Any:
    """What Mabolo would have that file hold under its own keys."""
    return hooks_block() if slot.holds == HOOKS else server_entry(slot.client)


@dataclass(frozen=True)
class Finding:
    """What one file holds under our keys, next to what we would write.

    `have` is only ever the part that is ours. What else is in that file is not
    this object's business and never leaves the reader, which is the same rule
    the writer works under.
    """

    slot: Slot
    verdict: str
    want: Any
    have: Any = None
    note: str = ""

    @property
    def settled(self) -> bool:
        """True when nothing has to be written and nothing is in the way."""
        return self.verdict == SAME

    def render(self) -> list[str]:
        """The lines a person is shown, both versions when they differ."""
        if self.verdict == UNREADABLE:
            return [f"  leave   {self.slot.label}", f"          {self.note}"]
        if self.verdict == ABSENT:
            return [f"  write   {self.slot.label}"]
        if self.verdict == SAME:
            return [f"  keep    {self.slot.label}  (already {SAME})"]
        lines = [f"  leave   {self.slot.label}  ({DIFFERENT})", "          there now:"]
        lines += [f"            {line}" for line in _json_text(self.have).splitlines()]
        lines += ["          Mabolo would write:"]
        lines += [f"            {line}" for line in _json_text(self.want).splitlines()]
        lines += [f"          `{EXECUTABLE} init --rewire` replaces it. Nothing was touched."]
        return lines


def read(slot: Slot) -> Finding:
    """What that file holds under our keys, or why it could not be read.

    A file that is not valid JSON is a finding with the file named in it, never
    an exception out of a reader. The alternative is `init` dying on somebody's
    stray comma in a file it was only ever going to add a key to.
    """
    want = wanted(slot)
    try:
        document = _load(slot.path, slot.form)
    except _Unreadable as broken:
        return Finding(slot=slot, verdict=UNREADABLE, want=want, note=str(broken))
    have = _ours(document, slot)
    if have is None:
        return Finding(slot=slot, verdict=ABSENT, want=want)
    if slot.holds == HOOKS:
        mixed = _mixed_groups(document)
        if mixed:
            return Finding(
                slot=slot,
                verdict=DIFFERENT,
                want=want,
                have=have,
                note=f"{mixed} of our hooks share a group with somebody else's",
            )
    return Finding(slot=slot, verdict=compare(have, want), want=want, have=have)


def compare(have: Any, want: Any) -> str:
    """Absent, ours, or somebody's own version of ours.

    The comparison is on the parsed value and never on the text. A file
    reindented by an editor, or a TOML table written with different spacing,
    says the same thing, and calling that a difference would send a person to
    `--rewire` over whitespace.
    """
    if have is None:
        return ABSENT
    return SAME if have == want else DIFFERENT


def files_of(for_client: Client, home: Path | None = None) -> tuple[Path, ...]:
    """Every file this client keeps one of our keys in.

    Offered for `doctor`, which asks whether a wired client still points at
    this tool. It reads the files; it does not care which key is in which, and
    a second list of paths somewhere else would be a second thing to keep in
    step with the clients.
    """
    return tuple(one.path for one in slots(for_client, home))


def survey(for_client: Client, home: Path | None = None) -> tuple[Finding, ...]:
    """Read every file that client keeps our keys in. Writes nothing."""
    return tuple(read(slot) for slot in slots(for_client, home))


# Writing


def apply(finding: Finding, *, rewire: bool = False) -> bool:
    """Put our keys into that file, and touch nothing else in it.

    Returns whether the file was written. A finding that is already ours is not
    written again, so a second `init` on a wired machine leaves every mtime
    alone: a file that changes when nothing changed is a file somebody has to
    diff before they can believe it.
    """
    if finding.verdict == SAME:
        return False
    if finding.verdict == UNREADABLE:
        raise MaboloError(f"{finding.slot.path} could not be read: {finding.note}")
    if finding.verdict == DIFFERENT and not rewire:
        raise MaboloError(
            f"{finding.slot.path} holds a different version of Mabolo's own entry. "
            f"Run `{EXECUTABLE} init --rewire` to replace it."
        )
    slot = finding.slot
    try:
        if slot.form == TOML_FORM:
            text = _merge_toml_server(_text_of(slot.path), slot.client, finding.want)
        else:
            document = _load(slot.path, JSON_FORM)
            if slot.holds == HOOKS:
                _merge_hooks(document, finding.want)
            else:
                servers = document.setdefault(slot.client.server_key, {})
                if not isinstance(servers, dict):
                    raise MaboloError(
                        f"{slot.path} has a {slot.client.server_key} that is not an object"
                    )
                servers[SERVER_NAME] = finding.want
            text = _json_text(document)
    except _Unreadable as broken:
        # The file was readable when it was surveyed and is not now. Somebody
        # else is editing it, and the honest answer is the name of the file.
        raise MaboloError(str(broken)) from broken
    _write_atomically(slot.path, text)
    return True


def remove(slot: Slot) -> bool:
    """Take our own keys out of that file and leave everything else in it.

    The inverse of `apply`, and it has to be the same predicate, or uninstalling
    would leave behind exactly the entries a later `init` would refuse to
    overwrite. An entry that is not ours is not touched, a group we share with
    somebody else's command is left whole, and a file that holds nothing of
    ours is not rewritten at all.
    """
    try:
        if slot.form == TOML_FORM:
            text = _drop_toml_server(_text_of(slot.path), slot.client)
            before = _text_of(slot.path)
            if text == before:
                return False
        else:
            document = _load(slot.path, JSON_FORM)
            if _ours(document, slot) is None:
                return False
            if slot.holds == HOOKS:
                _merge_hooks(document, {})
            else:
                servers = document.get(slot.client.server_key)
                if isinstance(servers, dict):
                    servers.pop(SERVER_NAME, None)
            text = _json_text(document)
    except _Unreadable as broken:
        raise MaboloError(str(broken)) from broken
    _write_atomically(slot.path, text)
    return True


def _drop_toml_server(text: str, for_client: Client) -> str:
    """Our own table taken out of a TOML file, byte for byte elsewhere."""
    header = f"{for_client.server_key}.{SERVER_NAME}"
    lines = text.splitlines(keepends=True)
    start = None
    for number, line in enumerate(lines):
        if line.strip().replace(" ", "") == f"[{header}]":
            start = number
            break
    if start is None:
        return text
    end = len(lines)
    for number in range(start + 1, len(lines)):
        stripped = lines[number].lstrip()
        if stripped.startswith("[") and not stripped.replace(" ", "").startswith(f"[{header}."):
            end = number
            break
    kept = "".join(lines[:start]) + "".join(lines[end:])
    return kept


def _merge_hooks(document: dict[str, Any], want: dict[str, list[dict[str, Any]]]) -> None:
    """Put our groups in, take our old ones out, leave everybody else's alone.

    Ours go at the end of an event's list. A person's own hook keeps its
    position, because a hook list is run in order and moving somebody's entry
    is a behaviour change nobody asked for.
    """
    block = document.setdefault(HOOKS_KEY, {})
    if not isinstance(block, dict):
        raise MaboloError(f"the {HOOKS_KEY} key is not an object")
    for event in list(block):
        groups = block.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if _group_is_ours(group):
                if not _group_is_only_ours(group):
                    raise MaboloError(
                        f"a {event} hook of ours shares a group with another command, "
                        "so it was left alone"
                    )
                continue
            kept.append(group)
        block[event] = kept
    for event, groups in want.items():
        block[event] = list(block.get(event, [])) + [deepcopy(group) for group in groups]
    for event in [e for e, groups in block.items() if groups == [] and e not in want]:
        # An event we emptied and do not fill again was ours alone. Leaving the
        # empty list behind would read as a hook that was configured and does
        # nothing, which is the state this whole module exists to avoid.
        del block[event]


def _ours(document: dict[str, Any], slot: Slot) -> Any:
    """The part of that document that belongs to Mabolo, or None."""
    if slot.holds == SERVER:
        servers = document.get(slot.client.server_key)
        if not isinstance(servers, dict):
            return None
        return servers.get(SERVER_NAME)
    block = document.get(HOOKS_KEY)
    if not isinstance(block, dict):
        return None
    found: dict[str, list[dict[str, Any]]] = {}
    for event, groups in block.items():
        if not isinstance(groups, list):
            continue
        mine = [group for group in groups if _group_is_ours(group)]
        if mine:
            found[event] = mine
    return found or None


def _mixed_groups(document: dict[str, Any]) -> int:
    """How many groups hold one of our commands next to somebody else's."""
    block = document.get(HOOKS_KEY)
    if not isinstance(block, dict):
        return 0
    count = 0
    for groups in block.values():
        if not isinstance(groups, list):
            continue
        count += sum(1 for g in groups if _group_is_ours(g) and not _group_is_only_ours(g))
    return count


def _commands(group: Any) -> list[str]:
    if not isinstance(group, dict):
        return []
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return []
    return [h.get("command", "") for h in hooks if isinstance(h, dict)]


def _group_is_ours(group: Any) -> bool:
    return any(str(c).startswith(HOOK_PREFIX) for c in _commands(group))


def _group_is_only_ours(group: Any) -> bool:
    found = _commands(group)
    return bool(found) and all(str(c).startswith(HOOK_PREFIX) for c in found)


# Files


class _Unreadable(Exception):
    """A configuration file that cannot be parsed. Carries the sentence to show."""


def _text_of(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as problem:
        raise _Unreadable(f"{path} could not be opened: {problem}") from problem
    except UnicodeDecodeError as problem:
        raise _Unreadable(f"{path} is not UTF-8 text: {problem}") from problem


def _load(path: Path, form: str) -> dict[str, Any]:
    """A configuration file as a mapping. A missing file is an empty one."""
    text = _text_of(path)
    if not text.strip():
        return {}
    try:
        document = tomllib.loads(text) if form == TOML_FORM else json.loads(text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as problem:
        raise _Unreadable(f"{path} is not valid {form.upper()}: {problem}") from problem
    if not isinstance(document, dict):
        raise _Unreadable(f"{path} does not hold an object at the top level")
    return document


def _write_atomically(path: Path, text: str) -> None:
    """Write the whole file or none of it, keeping the mode it already had.

    A client reads these files while a session is starting. A half written one
    is a client that cannot start at all, and that is a worse outcome than
    anything this module was asked to arrange.

    A settings file is often a link into somebody's dotfiles repository. The
    replace happens on what the link points at, because replacing the link
    itself is atomic, correct, and leaves them with a file where their link
    used to be and a repository that no longer holds their settings.
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary = path.with_name(path.name + ".mabolo-tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# TOML, written by hand so that a comment somebody wrote survives a wiring


def _toml_value(value: Any) -> str:
    """One TOML value, for the two shapes a server entry is made of.

    A JSON string is a TOML basic string: the same escapes, the same quotes,
    and a control character comes out as `\\u00XX`, which TOML reads back. What
    this does not know how to write it refuses, rather than falling back on
    `str()` and producing a file that parses and means something else.
    """
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise MaboloError(f"{value!r} is not something this writes into TOML")


def _toml_table(header: str, entry: dict[str, Any]) -> str:
    lines = [f"[{header}]"]
    lines += [f"{key} = {_toml_value(value)}" for key, value in entry.items()]
    return "\n".join(lines) + "\n"


def _merge_toml_server(text: str, for_client: Client, entry: dict[str, Any]) -> str:
    """Replace our own table, or append it, and change no other byte.

    Parsing the file and writing the result back would be shorter and would
    also delete every comment and blank line the person put there. Their file
    is not ours to tidy, so the table is found as text and only its own lines
    are replaced.
    """
    header = f"{for_client.server_key}.{SERVER_NAME}"
    table = _toml_table(header, entry)
    lines = text.splitlines(keepends=True)
    start = None
    for number, line in enumerate(lines):
        stripped = line.strip()
        if stripped.replace(" ", "") == f"[{header}]":
            start = number
            break
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return f"{text}\n{table}" if text else table
    end = len(lines)
    for number in range(start + 1, len(lines)):
        stripped = lines[number].lstrip()
        if stripped.startswith("[") and not stripped.replace(" ", "").startswith(f"[{header}."):
            end = number
            break
    return "".join(lines[:start]) + table + "".join(lines[end:])


if __name__ == "__main__":  # pragma: no cover
    for one in write_plugins():
        print(one)
