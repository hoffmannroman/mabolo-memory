"""Configuration: one small TOML file, read with the standard library.

It holds the four things `init` asks for, plus the answers it does not ask for
and has to write somewhere. Anything that can be derived from the vault is not
configuration, because two places that can disagree eventually do: the vault's
language lives in the vault, and what is here is only the value a new one starts
with.

Three rules that are not obvious:

* **An area is one plain folder name.** It becomes a directory, so a path here
  would be a way out of the vault.
* **Unknown keys survive a rewrite.** A newer Mabolo may have written the file;
  an older one must not delete what it does not understand.
* **An environment override is for one run.** A vault named by the environment
  is never written back into the file as if somebody had chosen it.

There is no TOML writer in the standard library, so this module writes the
handful of shapes it uses itself rather than adding a dependency for it.
"""

from __future__ import annotations

import getpass
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import MaboloError
from .schema import (
    ACTOR_HUMAN,
    DEFAULT_LANGUAGE,
    FIXED_AREAS,
    is_language,
    is_project_area,
    is_safe_area,
)

CONFIG_VERSION = 1
DEFAULT_VAULT_DIRNAME = "mabolo-data"
DEFAULT_BRANCH = "main"

#: Environment overrides, so a second vault can be driven without touching the file.
ENV_CONFIG = "MABOLO_CONFIG"
ENV_VAULT = "MABOLO_VAULT"

_SAFE_ID = re.compile(r"[^a-z0-9._-]+")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def default_config_path() -> Path:
    override = os.environ.get(ENV_CONFIG)
    return Path(override).expanduser() if override else config_home() / "mabolo" / "config.toml"


def default_vault_path() -> Path:
    override = os.environ.get(ENV_VAULT)
    return Path(override).expanduser() if override else Path.home() / DEFAULT_VAULT_DIRNAME


def default_actor() -> str:
    """`human:<id>` from the login name, which is a guess `init` lets you correct."""
    try:
        login = getpass.getuser()
    except Exception:  # no password entry for this uid, which happens in containers
        login = "me"
    return f"human:{_SAFE_ID.sub('-', login.strip().lower()) or 'me'}"


def is_approver(value: str) -> bool:
    """True only for `human:<id>`.

    The general actor rule also allows `producer/version` and `process:<id>`,
    which are fine for who *made* an entry. Who *approves* one is narrower: the
    validator refuses any `verified.by` that is not a person, so accepting one
    here would configure a vault whose every approval is rejected later.
    """
    return bool(ACTOR_HUMAN.match(str(value).strip()))


def _toml_string(value: str) -> str:
    out: list[str] = []
    for char in value:
        if char in ('"', "\\"):
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            # A raw control character produces a file that cannot be read back.
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


class UnwritableValue(MaboloError):
    """A value this version cannot put back into TOML without changing it."""


def _toml_value(value: Any) -> str:
    """One TOML value, or an error rather than a value that reads back differently.

    `str()` as a fallback looks harmless and is not: a date becomes text, a
    float becomes text, a nested table becomes its Python repr. The file still
    parses, so nothing complains, and a setting written by a newer version has
    quietly changed type. Refusing is the honest answer, and the comment above
    the unknown keys promises exactly that.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise UnwritableValue(
        f"a value of type {type(value).__name__} cannot be written back without changing it"
    )


def _named(key: str, value: Any) -> str:
    """A TOML value, with the key in the error when it cannot be written."""
    try:
        return _toml_value(value)
    except UnwritableValue as exc:
        raise MaboloError(
            f"{key}: {exc}. It was written by a newer Mabolo, and this one will not "
            "rewrite the file rather than change what that setting means."
        ) from None


@dataclass
class Config:
    """What Mabolo needs to know before it can do anything."""

    vault: Path
    actor: str
    areas: list[str] = field(default_factory=lambda: list(FIXED_AREAS))
    #: The language `init` writes into a new vault. The vault itself is what
    #: the search reads, in its root index.md; this is only the answer given
    #: when one is created, so that a second vault on this machine starts the
    #: same way. `init` does not ask for it: four questions are the budget.
    language: str = DEFAULT_LANGUAGE
    remote_url: str | None = None
    remote_branch: str = DEFAULT_BRANCH
    version: int = CONFIG_VERSION
    path: Path | None = None
    #: Keys the file carried that this version does not know. Kept so that an
    #: older Mabolo cannot quietly delete a setting a newer one wrote.
    unknown: dict[str, Any] = field(default_factory=dict)
    #: The same, for keys inside [remote].
    unknown_remote: dict[str, Any] = field(default_factory=dict)
    #: True when the vault came from the environment, so `save` refuses to make
    #: a one-run override permanent.
    vault_from_environment: bool = False

    @classmethod
    def default(cls, vault: Path | None = None, actor: str | None = None) -> Config:
        return cls(
            vault=Path(vault).expanduser() if vault else default_vault_path(),
            actor=actor or default_actor(),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> Config:
        where = path or "the configuration"
        vault = data.get("vault")
        if not isinstance(vault, str) or not vault.strip():
            raise MaboloError(f"{where} names no vault")

        remote = data.get("remote", {})
        if not isinstance(remote, dict):
            raise MaboloError(f"{where}: [remote] must be a table")
        branch = str(remote.get("branch") or DEFAULT_BRANCH)
        if not _BRANCH_RE.match(branch):
            raise MaboloError(f"{where}: {branch!r} is not a usable branch name")

        if "areas" in data:
            areas = data["areas"]
            if not isinstance(areas, list) or not all(isinstance(a, str) for a in areas):
                raise MaboloError(f"{where}: areas must be a list of names")
        else:
            areas = list(FIXED_AREAS)
        unusable = [a for a in areas if not is_safe_area(a)]
        if unusable:
            raise MaboloError(
                f"{where}: an area is one plain folder name, so {unusable} cannot be used. "
                "Project areas are created on demand and do not belong in this list."
            )

        language = str(data.get("language") or DEFAULT_LANGUAGE).strip().lower()
        if not is_language(language):
            raise MaboloError(
                f"{where}: language {language!r} is a short code such as en or de. "
                "An unknown code is fine, it simply means no stop words beyond the English ones."
            )

        actor = str(data.get("actor") or default_actor())
        if not is_approver(actor):
            raise MaboloError(
                f"{where}: actor {actor!r} has to look like human:<id>, "
                "because a person is what approves an entry"
            )

        raw_version = data.get("version", CONFIG_VERSION)
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise MaboloError(f"{where}: version must be a whole number")
        if raw_version > CONFIG_VERSION:
            raise MaboloError(
                f"{where} was written by a newer Mabolo (version {raw_version}). "
                "Update this one rather than let it rewrite the file."
            )

        override = os.environ.get(ENV_VAULT)
        known = {"version", "vault", "actor", "areas", "language", "remote"}
        known_remote = {"url", "branch"}
        return cls(
            vault=Path(override or vault).expanduser(),
            actor=actor,
            areas=list(areas),
            language=language,
            remote_url=str(remote["url"]) if remote.get("url") else None,
            remote_branch=branch,
            version=raw_version,
            path=path,
            unknown={k: v for k, v in data.items() if k not in known},
            unknown_remote={k: v for k, v in remote.items() if k not in known_remote},
            vault_from_environment=bool(override),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        target = Path(path).expanduser() if path else default_config_path()
        if not target.exists():
            raise MaboloError(
                f"no configuration at {target}. Run `mabolo init` to create a vault and one."
            )
        try:
            data = tomllib.loads(target.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise MaboloError(f"{target} is not readable TOML: {exc}") from exc
        except OSError as exc:
            raise MaboloError(f"{target} cannot be read: {exc.strerror}") from exc
        return cls.from_dict(data, path=target)

    @classmethod
    def load_if_present(cls, path: Path | None = None) -> Config | None:
        target = Path(path).expanduser() if path else default_config_path()
        return cls.load(target) if target.exists() else None

    def known_areas(self) -> tuple[str, ...]:
        return tuple(self.areas)

    def area_is_known(self, area: str) -> bool:
        return area in self.areas or is_project_area(area)

    def to_toml(self) -> str:
        lines = [
            "# Mabolo Memory. Written by `mabolo init`, safe to edit by hand.",
            f"version = {self.version}",
            "",
            "# The vault is a Git repository of Markdown files and belongs to you,",
            "# not to this tool. Uninstalling Mabolo leaves it exactly as it is.",
            f"vault = {_toml_value(str(self.vault))}",
            "",
            "# Who approves entries. This is what lands in the OKF `verified` field.",
            f"actor = {_toml_value(self.actor)}",
            "",
            "# Delete what you do not need, add what you do. One plain folder name",
            "# each. Project areas are created on demand as project/<name> and are",
            "# not listed here.",
            f"areas = {_toml_value(self.areas)}",
            "",
            "# The language a vault created from here is written in, as a short",
            "# code. What the search reads is the vault's own root index.md.",
            f"language = {_toml_value(self.language)}",
            "",
        ]
        if self.unknown:
            lines += [
                "# Written by a version of Mabolo that knew more than this one.",
                "# Kept untouched rather than dropped.",
            ]
            lines += [
                f"{key} = {_named(key, value)}"
                for key, value in self.unknown.items()
                if not isinstance(value, dict)
            ]
            lines.append("")
        lines += [
            "[remote]",
            "# A bare Git repository over SSH, or empty for a vault that stays local.",
            f"url = {_toml_value(self.remote_url or '')}",
            f"branch = {_toml_value(self.remote_branch)}",
        ]
        lines += [f"{key} = {_named(key, value)}" for key, value in self.unknown_remote.items()]
        lines.append("")
        for key, value in self.unknown.items():
            if isinstance(value, dict):
                lines.append(f"[{key}]")
                lines += [f"{k} = {_named(f'{key}.{k}', v)}" for k, v in value.items()]
                lines.append("")
        return "\n".join(lines)

    def save(self, path: Path | None = None) -> Path:
        if self.vault_from_environment:
            raise MaboloError(
                f"the vault came from {ENV_VAULT} and is meant for this run only. "
                "Unset it, or pass the path you want written."
            )
        target = Path(path).expanduser() if path else (self.path or default_config_path())
        # Rendered before anything is opened. Building the text inside the open
        # file meant that a value this version cannot write left the existing
        # configuration truncated to nothing, which is a worse outcome than the
        # rewrite it was refusing.
        text = self.to_toml()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise MaboloError(f"{target} is a symlink, and the configuration is not written through one")
        temporary = target.parent / f".{target.name}.mabolo-tmp"
        try:
            # Created with tight permissions from the start, rather than fixed
            # afterwards, after a moment in which the file was world readable.
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.path = target
        return target
