"""The entry schema: Open Knowledge Format v0.2, plus a namespaced `mabolo` block.

The format is young, v0.2 replaced v0.1 within weeks and both changes were
breaking, so this file is the only place that knows what an entry looks like and
there is no runtime dependency on an OKF library.

Reading is deliberately tolerant: a malformed value becomes `None` here and a
problem in `validate`, because a vault that can be edited by hand in Obsidian
will contain malformed values, and crashing on them helps nobody. Writing is
strict: `to_meta` only ever emits a conformant block.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The version of the format this schema targets.
OKF_VERSION = "0.2"

#: OKF lifecycle values.
STATUSES = ("draft", "stable", "deprecated")
DEFAULT_STATUS = "stable"

#: Entry types Mabolo knows about. OKF allows any descriptive string, so an
#: unknown type is a warning and not an error.
KNOWN_TYPES = ("user", "feedback", "project", "reference")

#: Areas that exist in every vault. Project areas are `project/<name>`.
FIXED_AREAS = ("persona", "hosts", "infra", "design")
PROJECT_PREFIX = "project/"

#: Actor conventions from the spec: an agent, a person, an automated process.
ACTOR_PRODUCER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*/[A-Za-z0-9][A-Za-z0-9._+-]*$")
ACTOR_HUMAN = re.compile(r"^human:[a-z0-9][a-z0-9._-]{0,63}$")
ACTOR_PROCESS = re.compile(r"^process:[a-z0-9][a-z0-9._-]{0,63}$")

#: Entry names are file names, so they are kebab-case and unique in the vault.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
#: Project names come from directory names elsewhere and keep their own shape.
#: A project name is an entry name. It was allowed capitals once, so that a
#: folder called `Atlas` could be a project spelled the way its repository is.
#: That put a capital into a path, and a file system that folds case then turns
#: `project/Atlas` and `project/atlas` into one folder holding two areas: on a
#: clone, entries land together and one of the two areas quietly disappears.
#: The folder name is folded when it is matched instead, which gives the same
#: answer without the path ever carrying the capital.
PROJECT_NAME_RE = NAME_RE
#: An alias is a search key, not a file name, so it keeps the spelling a person
#: would actually type. It may not contain a path separator or a control
#: character, because a search resolves it and a resolver must not walk.
ALIAS_RE = re.compile(r"^[^\x00-\x1f/\\]{1,80}$")

#: Reserved file names, with their own rules in the spec.
INDEX_FILE = "index.md"
LOG_FILE = "log.md"
#: An entry may not take one of those names: the index is rebuilt from the
#: entries, so an entry called `index` would be overwritten by its own folder.
RESERVED_STEMS = frozenset({"index", "log"})

#: A configured area is one plain folder name. Anything else is a path, and a
#: path in a configuration file is a way out of the vault.
AREA_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")

#: The language the entries are written in. It belongs to the vault and not to
#: the machine: the search drops stop words in it and cuts suffixes by its
#: rules, so the same commit and the same question rank differently under
#: another one. A vault that carried its language in a local configuration file
#: would rank differently on two machines, and then no history could be replayed.
DEFAULT_LANGUAGE = "en"
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$")


def is_language(value: Any) -> bool:
    """True for a short language code. An unknown one is fine, it only means no
    stop words beyond the English ones."""
    return isinstance(value, str) and bool(LANGUAGE_RE.match(value))


def now() -> dt.datetime:
    """The current local time, with its offset and without microseconds."""
    return dt.datetime.now().astimezone().replace(microsecond=0)


def iso(value: dt.datetime | dt.date | str | None) -> str | None:
    """An ISO-8601 string for anything YAML may have handed us."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def as_utc(when: dt.datetime | dt.date) -> dt.datetime:
    """Any date or datetime as an aware UTC datetime, and the only place that rule lives.

    Two halves, and both were a bug before this was one function. A value
    without an offset is read as UTC rather than as local time, or the same
    commit would sort differently in two time zones. A value *with* an offset
    is converted, not passed through: arithmetic on an aware datetime moves
    wall clock time, so subtracting seven days from the same moment expressed
    in Athens and in UTC crosses a daylight saving boundary differently and
    lands on two different instants.
    """
    if not isinstance(when, dt.datetime):
        when = dt.datetime.combine(when, dt.time.min)
    if when.tzinfo is None:
        return when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc)


def parse_time(value: Any) -> dt.datetime | None:
    """A datetime from a string, a date or a datetime, or None if it is none of those."""
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def is_bare_date(value: Any) -> bool:
    """True when this says a day and nothing about the time of day."""
    if isinstance(value, dt.datetime):
        return False
    if isinstance(value, dt.date):
        return True
    return isinstance(value, str) and ":" not in value and "T" not in value.strip().upper()


def parse_moment(value: Any) -> dt.datetime | None:
    """The moment a session starts at, from what a person or a case wrote.

    Same as `parse_time`, except that a bare date means the **whole** of that
    day and therefore reads as its last instant rather than its first.

    That is the reading the rest of the payload already used. A journal day is
    a calendar day, so `--as-of 2026-09-18` shows a decision written that
    morning; an entry touched the same morning was compared against midnight
    and came out as "not touched this week", because it lay in the future. One
    payload, two readings of one date, and they disagreed about the same
    morning. A day is the unit a person means when they write one.

    `parse_time` keeps the old reading, and an entry's own `generated.at` still
    uses it: a bare date there says when an entry came into being, and the
    earliest instant of that day is the careful answer.
    """
    when = parse_time(value)
    if when is None:
        return None
    if is_bare_date(value):
        return when.replace(hour=23, minute=59, second=59, microsecond=999999)
    return when


def _as_pin(value: Any) -> bool | dt.date:
    """`true`, a day, or not pinned at all.

    A string that looks like a date becomes one; anything else that is truthy
    is a plain pin. Refusing an unreadable value would make a typo in one
    entry's `pin` field cost a session its whole payload, and the readers here
    render what is on disk rather than what a validator wishes were there.
    `validate` is where a malformed pin gets reported.
    """
    if value is True:
        return True
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            return bool(value.strip())
    return False


def is_actor(value: Any) -> bool:
    """True for `producer/version`, `human:<id>` or `process:<id>`."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(ACTOR_HUMAN.match(text) or ACTOR_PROCESS.match(text) or ACTOR_PRODUCER.match(text))


def normalise_name(name: str) -> str:
    """The canonical file stem for an entry name."""
    return unicodedata.normalize("NFC", name.strip())


def is_project_area(area: str) -> bool:
    name = area[len(PROJECT_PREFIX) :]
    return (
        area.startswith(PROJECT_PREFIX)
        and bool(PROJECT_NAME_RE.match(name))
        and name not in (".", "..")
    )


def is_safe_area(area: str) -> bool:
    """True for an area that cannot leave the vault when turned into a path."""
    return bool(AREA_RE.match(area)) and area not in (".", "..")


def area_is_known(area: str, areas: tuple[str, ...] | list[str] = FIXED_AREAS) -> bool:
    return area in tuple(areas) or is_project_area(area)


def area_to_dir(area: str) -> Path:
    """`project/atlas` lives in `project/atlas/`, everything else in one folder."""
    return Path(*area.split("/"))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


@dataclass
class Generated:
    """Who produced an entry, and when. Both halves are required by the spec."""

    by: str
    at: str

    @classmethod
    def from_meta(cls, value: Any) -> Generated | None:
        if not isinstance(value, dict):
            return None
        by = value.get("by")
        at = iso(value.get("at"))
        if not isinstance(by, str) or not at:
            return None
        return cls(by=by.strip(), at=at)

    def to_meta(self) -> dict[str, str]:
        return {"by": self.by, "at": self.at}


@dataclass
class Verification:
    """One act of verification. In Mabolo this is only ever written for a person."""

    by: str
    at: str

    @classmethod
    def from_meta(cls, value: Any) -> Verification | None:
        if not isinstance(value, dict):
            return None
        by = value.get("by")
        at = iso(value.get("at"))
        if not isinstance(by, str) or not at:
            return None
        return cls(by=by.strip(), at=at)

    def to_meta(self) -> dict[str, str]:
        return {"by": self.by, "at": self.at}


@dataclass
class Source:
    """Where a claim came from. `id` is what a footnote in the body points at."""

    resource: str
    id: str | None = None
    title: str | None = None
    author: str | None = None
    usage_count: int | None = None
    last_modified: str | None = None
    #: Anything else the source carried, kept so a round trip loses nothing.
    extra: dict[str, Any] = field(default_factory=dict)

    KNOWN = ("id", "resource", "title", "author", "usage_count", "last_modified")

    @classmethod
    def from_meta(cls, value: Any) -> Source | None:
        if not isinstance(value, dict):
            return None
        resource = value.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            return None
        count = value.get("usage_count")
        return cls(
            resource=resource.strip(),
            id=str(value["id"]).strip() if value.get("id") is not None else None,
            title=str(value["title"]).strip() if value.get("title") is not None else None,
            author=str(value["author"]).strip() if value.get("author") is not None else None,
            usage_count=count if isinstance(count, int) else None,
            last_modified=iso(value.get("last_modified")),
            extra={k: v for k, v in value.items() if k not in cls.KNOWN},
        )

    def to_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if self.id:
            meta["id"] = self.id
        meta["resource"] = self.resource
        for key in ("title", "author", "usage_count", "last_modified"):
            value = getattr(self, key)
            if value is not None:
                meta[key] = value
        meta.update(self.extra)
        return meta


@dataclass
class MaboloBlock:
    """Everything Mabolo adds, namespaced so that OKF stays untouched."""

    area: str = ""
    anchor: str | None = None
    #: `true`, or the day the entry was pinned. A standing rule competes for a
    #: seat in the core, and when there is one seat too few the newest pin is
    #: the one that loses it, so the day has to travel with the pin. `true`
    #: stays valid and falls back to `generated.at`.
    pin: bool | dt.date = False
    aliases: list[str] = field(default_factory=list)
    # Only the design area uses these three.
    scope: str | None = None
    applies_to: list[str] = field(default_factory=list)
    instead_of: str | None = None
    #: Anything else found under `mabolo:`, kept so a round trip loses nothing.
    extra: dict[str, Any] = field(default_factory=dict)

    KNOWN = ("area", "anchor", "pin", "aliases", "scope", "applies_to", "instead_of")

    @classmethod
    def from_meta(cls, value: Any) -> MaboloBlock:
        if not isinstance(value, dict):
            return cls()
        area = value.get("area")
        anchor = value.get("anchor")
        instead_of = value.get("instead_of")
        scope = value.get("scope")
        return cls(
            area=area.strip() if isinstance(area, str) else "",
            anchor=anchor.strip() if isinstance(anchor, str) and anchor.strip() else None,
            pin=_as_pin(value.get("pin")),
            aliases=_string_list(value.get("aliases")),
            scope=scope.strip() if isinstance(scope, str) and scope.strip() else None,
            applies_to=_string_list(value.get("applies_to")),
            instead_of=(
                instead_of.strip() if isinstance(instead_of, str) and instead_of.strip() else None
            ),
            extra={k: v for k, v in value.items() if k not in cls.KNOWN},
        )

    def to_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {"area": self.area}
        if self.anchor:
            meta["anchor"] = self.anchor
        # Optional in the format, so only a pinned entry says so. Writing
        # `pin: false` into every entry is a line of noise in every file.
        if self.pin:
            meta["pin"] = self.pin
        if self.aliases:
            meta["aliases"] = list(self.aliases)
        if self.scope:
            meta["scope"] = self.scope
        if self.applies_to:
            meta["applies_to"] = list(self.applies_to)
        if self.instead_of:
            meta["instead_of"] = self.instead_of
        meta.update(self.extra)
        return meta


#: The id the person's own sentence gets when it is written under an entry.
#: One id, because two writers used two (`q1` and `s1`) for the same thing, and
#: `mabolo why` reads a footnote by its source rather than by its name only
#: because nothing had yet written the two forms into one vault.
QUOTE_ID = "q1"

#: A footnote definition at the start of a line, which is where an entry keeps
#: the sentence it rests on. One pattern, because `why` and the validator read
#: the same footnotes and a second spelling is how the two come to disagree
#: about whether an entry has its evidence.
FOOTNOTE_DEFINITION = re.compile(r"^\[\^([^\]]+)\]:[ \t]*(.*)$", re.MULTILINE)


def quoted_body(prose: str, quote: str) -> str:
    """The prose with the sentence that authorised it in a footnote under it.

    In a footnote and not in the text, because that is what makes the entry
    checkable later: `why` reads the footnote, and an entry whose evidence was
    paraphrased into its own prose has no evidence.

    The marker is set off by a space rather than glued to the last word, which
    is the usual Markdown habit. Glued, it extends that word into a run of
    non-space characters, and a body ending in "the password store" became "the
    password ***" the moment the marker was attached: the redactor saw a
    keyword followed by eight characters and did its job, and the entry was
    refused for a secret that was never there.
    """
    text = " ".join(str(prose).split()) or " ".join(str(quote).split())
    said = " ".join(str(quote).split()).replace('"', "'")
    return f"{text} [^{QUOTE_ID}]\n\n[^{QUOTE_ID}]: \"{said}\"\n"


@dataclass
class Entry:
    """One concept document: OKF frontmatter plus the prose below it."""

    type: str = "reference"
    title: str | None = None
    description: str | None = None
    resource: str | None = None
    tags: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    usage_window: dict[str, Any] | None = None
    generated: Generated | None = None
    verified: list[Verification] = field(default_factory=list)
    status: str = DEFAULT_STATUS
    stale_after: str | None = None
    mabolo: MaboloBlock = field(default_factory=MaboloBlock)
    #: Unknown top level keys, kept so that hand written additions survive.
    extra: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    path: Path | None = None
    revision: str = ""
    #: The mapping exactly as it was read, before any of the tidying above.
    #: Empty for an entry that was built rather than read. `to_meta` falls back
    #: to it for every field the tolerant read dropped, and `Vault.write_entry`
    #: validates it, so that fixing one line of an entry never quietly repairs
    #: the rest of it.
    raw: dict[str, Any] = field(default_factory=dict)

    KNOWN = (
        "type",
        "title",
        "description",
        "resource",
        "tags",
        "sources",
        "usage_window",
        "generated",
        "verified",
        "status",
        "stale_after",
        "mabolo",
    )

    @property
    def name(self) -> str:
        return self.path.stem if self.path else ""

    @property
    def area(self) -> str:
        return self.mabolo.area

    @property
    def is_design(self) -> bool:
        return self.mabolo.area == "design"

    def touched_at(self) -> dt.datetime | None:
        """When this entry was last touched, according to the entry itself.

        The latest of `generated.at` and every `verified.at`, in UTC. A
        verification is a touch: reading only `generated` would make an entry
        somebody confirmed yesterday look years old to the session index.

        Taken from the frontmatter and never from the filesystem. An mtime is
        not part of a commit, so a fresh clone would order the session index
        differently from the machine the vault was written on.
        """
        stamps = [self.generated.at] if self.generated else []
        stamps += [v.at for v in self.verified]
        times = [t for t in (parse_time(s) for s in stamps) if t is not None]
        return max(as_utc(t) for t in times) if times else None

    @classmethod
    def from_meta(cls, meta: dict[str, Any], body: str = "", path: Path | None = None,
                  revision: str = "") -> Entry:
        """A tolerant read. Whatever does not fit becomes a problem in `validate`."""
        raw_verified = meta.get("verified")
        if isinstance(raw_verified, dict):
            raw_verified = [raw_verified]
        verified = [
            v for v in (Verification.from_meta(x) for x in raw_verified or [])
            if v is not None
        ] if isinstance(raw_verified, list) else []
        raw_sources = meta.get("sources")
        sources = [
            s for s in (Source.from_meta(x) for x in raw_sources or []) if s is not None
        ] if isinstance(raw_sources, list) else []
        status = meta.get("status")
        raw_type = meta.get("type")
        return cls(
            # Only a string is a type. Turning `null` into the text "None" would
            # invent a value and then write it back as if a person had chosen it.
            type=raw_type.strip() if isinstance(raw_type, str) else "",
            title=str(meta["title"]).strip() if meta.get("title") is not None else None,
            description=(
                str(meta["description"]).strip() if meta.get("description") is not None else None
            ),
            resource=str(meta["resource"]).strip() if meta.get("resource") is not None else None,
            tags=_string_list(meta.get("tags")),
            sources=sources,
            usage_window=meta.get("usage_window") if isinstance(meta.get("usage_window"), dict) else None,
            generated=Generated.from_meta(meta.get("generated")),
            verified=verified,
            status=status.strip() if isinstance(status, str) and status.strip() else DEFAULT_STATUS,
            stale_after=iso(meta.get("stale_after")),
            mabolo=MaboloBlock.from_meta(meta.get("mabolo")),
            extra={k: v for k, v in meta.items() if k not in cls.KNOWN},
            body=body,
            path=path,
            revision=revision,
            raw=dict(meta),
        )

    def to_meta(self) -> dict[str, Any]:
        """The frontmatter mapping, in the order `frontmatter.KEY_ORDER` wants."""
        meta: dict[str, Any] = {"type": self.type}
        if self.title:
            meta["title"] = self.title
        if self.description:
            meta["description"] = self.description
        if self.resource:
            meta["resource"] = self.resource
        if self.tags:
            meta["tags"] = list(self.tags)
        if self.sources:
            meta["sources"] = [s.to_meta() for s in self.sources]
        if self.usage_window:
            meta["usage_window"] = dict(self.usage_window)
        if self.generated:
            meta["generated"] = self.generated.to_meta()
        if self.verified:
            meta["verified"] = [v.to_meta() for v in self.verified]
        meta["status"] = self.status
        if self.stale_after:
            meta["stale_after"] = self.stale_after
        meta["mabolo"] = self.mabolo.to_meta()
        meta.update(self.extra)
        # Whatever the tolerant read refused to interpret stays as it was found.
        # `from_meta` turns an incomplete `generated` into None and a `tags: 7`
        # into an empty list; writing that back would delete the very thing the
        # validator is about to complain about, and provenance is the first
        # casualty. A malformed entry is refused by `Vault.write_entry`, not
        # tidied up here.
        for key in self.KNOWN:
            if key not in meta and key in self.raw:
                meta[key] = self.raw[key]
        return meta

    def approve(self, by: str, at: str) -> None:
        """Record that a person said yes, unless this same yes is already here.

        The same sentence approving two changes in the same second would
        otherwise be recorded twice, and a list of identical approvals says
        nothing that one of them does not. It was kept by one writer and not by
        the other, which is how two entries in one vault end up disagreeing
        about what an approval looks like.
        """
        if not any(v.by == by and v.at == at for v in self.verified):
            self.verified = [*self.verified, Verification(by=by, at=at)]

    def footnote_ids(self) -> set[str]:
        """Footnote definitions in the body, which is where quotes live."""
        return {match.group(1) for match in FOOTNOTE_DEFINITION.finditer(self.body)}

    def footnote_refs(self) -> set[str]:
        """Footnote references in the body, minus the definitions."""
        refs = set(re.findall(r"\[\^([^\]]+)\]", self.body))
        return refs - self.footnote_ids()
