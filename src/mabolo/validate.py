"""The validator: what a conformant entry is, and what is merely suspicious.

Two levels, and the difference matters. An **error** means the entry cannot be
trusted to behave: it will drop out of the index, or it claims something the
format does not allow. A **warning** means a person should look at it, but
nothing breaks. A vault is edited by hand, so a warning never blocks anything,
and an error never deletes anything either: it gets named and listed.

The spec is permissive by design (only `type` is required), so the rules that
tighten it beyond OKF live in the `mabolo.*` codes and are ours, not Google's.
Where a rule comes from a decision rather than from the format, the message says
so, because a validator that cannot explain itself gets switched off.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import schema
from .errors import MaboloError
from .frontmatter import Document, FrontmatterError, read
from .schema import (
    ACTOR_HUMAN,
    ALIAS_RE,
    DEFAULT_STATUS,
    FIXED_AREAS,
    INDEX_FILE,
    KNOWN_TYPES,
    LOG_FILE,
    NAME_RE,
    OKF_VERSION,
    RESERVED_STEMS,
    STATUSES,
    Entry,
)

ERROR = "error"
WARNING = "warning"

#: The longest an entry may get. The largest real entry measured sat near 15 kB.
MAX_BODY = 200_000
#: A description is one line in an index, so it has to stay one line and short.
MAX_DESCRIPTION = 200

_MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+?)(?:\s+\"[^\"]*\")?\)")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
_FENCE = re.compile(r"^(```|~~~)", re.MULTILINE)
#: A log heading is a day, written the one way that sorts correctly as text.
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Every heading, not just a single word one: `## Two words` has to be caught by
#: the date rule as well, or the rule only ever sees what already looks right.
_LOG_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Problem:
    """One finding, addressed to the person who has to fix it."""

    level: str
    code: str
    message: str
    path: Path | None = None
    field: str | None = None

    @property
    def is_error(self) -> bool:
        return self.level == ERROR

    def render(self, root: Path | None = None) -> str:
        where = ""
        if self.path is not None:
            shown = self.path
            if root is not None:
                try:
                    shown = self.path.relative_to(root)
                except ValueError:
                    pass
            where = f"{shown}: "
        field = f" ({self.field})" if self.field else ""
        return f"{self.level:7} {where}{self.message}{field} [{self.code}]"


@dataclass
class Report:
    """The result of validating one file or a whole vault."""

    problems: list[Problem] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    checked: int = 0

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.is_error]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if not p.is_error]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, problem: Problem) -> None:
        self.problems.append(problem)

    def extend(self, problems: Iterable[Problem]) -> None:
        self.problems.extend(problems)

    def render(self, root: Path | None = None, show_warnings: bool = True) -> str:
        shown = self.problems if show_warnings else self.errors
        lines = [p.render(root) for p in sorted(shown, key=lambda p: (str(p.path), p.code))]
        lines.append(
            f"{self.checked} files checked, {len(self.errors)} errors, "
            f"{len(self.warnings)} warnings"
        )
        return "\n".join(lines)


def _problem(level: str, code: str, message: str, path: Path | None, field: str | None = None):
    return Problem(level=level, code=code, message=message, path=path, field=field)


def strip_code(text: str) -> str:
    """The text without fenced code blocks, so examples are not read as links."""
    out, fenced = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        out.append("" if fenced else line)
    return "\n".join(out)


class _Collector:
    """Collects findings for one entry. `err` breaks it, `warn` only worries."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.problems: list[Problem] = []

    def err(self, code: str, message: str, field: str | None = None) -> None:
        self.problems.append(_problem(ERROR, code, message, self.path, field))

    def warn(self, code: str, message: str, field: str | None = None) -> None:
        self.problems.append(_problem(WARNING, code, message, self.path, field))


def _check_type(meta: dict[str, Any], out: _Collector) -> None:
    entry_type = meta.get("type")
    if not isinstance(entry_type, str) or not entry_type.strip():
        out.err("okf.type.missing", "type is missing, it is the one field OKF requires", "type")
    elif entry_type.strip() not in KNOWN_TYPES:
        out.warn(
            "okf.type.unknown",
            f"type {entry_type.strip()!r} is not one of {', '.join(KNOWN_TYPES)}",
            "type",
        )


def _check_lifecycle(meta: dict[str, Any], out: _Collector) -> None:
    status = meta.get("status", DEFAULT_STATUS)
    if not isinstance(status, str) or status.strip() not in STATUSES:
        out.err("okf.status.unknown", f"status must be one of {', '.join(STATUSES)}", "status")

    if meta.get("stale_after") is None:
        return
    parsed = schema.parse_time(meta["stale_after"])
    if parsed is None:
        out.err("okf.stale_after.invalid", "stale_after is not an ISO-8601 timestamp", "stale_after")
    elif parsed.tzinfo is None:
        out.warn(
            "okf.stale_after.naive",
            "stale_after has no UTC offset, so it means different moments on different machines",
            "stale_after",
        )


def _check_generated(meta: dict[str, Any], out: _Collector) -> None:
    generated = meta.get("generated")
    if generated is None:
        return
    if not isinstance(generated, dict):
        out.err("okf.generated.incomplete", "generated must be a mapping with by and at", "generated")
        return
    by = generated.get("by")
    if not by:
        out.err("okf.generated.incomplete", "generated.by is missing", "generated.by")
    elif not isinstance(by, str) or not by.strip():
        # An unknown actor format stays a warning, because OKF allows any
        # producer. A value that is not text at all is something else: the
        # reader drops it, and then the field is gone rather than foreign.
        out.err("okf.generated.by.invalid", "generated.by must be text", "generated.by")
    elif not schema.is_actor(by):
        out.warn(
            "okf.generated.actor",
            "generated.by should be producer/version, human:<id> or process:<id>",
            "generated.by",
        )
    if not generated.get("at"):
        out.err("okf.generated.incomplete", "generated.at is missing", "generated.at")
    elif schema.parse_time(generated.get("at")) is None:
        out.err("okf.generated.at.invalid", "generated.at is not an ISO-8601 timestamp", "generated.at")


def _check_tags(meta: dict[str, Any], out: _Collector) -> None:
    """A tag is a word a person searches for, so every one of them is text."""
    if "tags" not in meta:
        return
    tags = meta.get("tags")
    if isinstance(tags, str):
        return
    if not isinstance(tags, list):
        out.warn("okf.tags.invalid", "tags should be a list of strings", "tags")
        return
    for i, tag in enumerate(tags):
        if not isinstance(tag, str) or not tag.strip():
            # The reader turns anything else into its Python repr, which is a
            # value nobody typed and nobody will ever search for.
            out.err(
                "okf.tags.item.invalid",
                f"tags[{i}] is not text, and a tag that is not text cannot be searched for",
                "tags",
            )


def _check_verified(meta: dict[str, Any], out: _Collector) -> None:
    """Only a person verifies. The whole consent gate rests on this one field."""
    verified = meta.get("verified")
    if verified is None:
        return
    items = [verified] if isinstance(verified, dict) else verified
    if not isinstance(items, list):
        out.err("okf.verified.incomplete", "verified must be a mapping or a list of them", "verified")
        return
    for i, item in enumerate(items):
        where = f"verified[{i}]"
        if not isinstance(item, dict) or not item.get("by") or not item.get("at"):
            out.err("okf.verified.incomplete", "a verification needs both by and at", where)
            continue
        if not (isinstance(item["by"], str) and ACTOR_HUMAN.match(item["by"].strip())):
            out.err(
                "mabolo.verified.not_human",
                "verified.by must be human:<id>, only a person can verify an entry",
                f"{where}.by",
            )
        if schema.parse_time(item["at"]) is None:
            out.err("okf.verified.at.invalid", "verified.at is not an ISO-8601 timestamp", f"{where}.at")


def _check_sources(meta: dict[str, Any], out: _Collector) -> list[str]:
    """Returns the ids a footnote may cite."""
    sources = meta.get("sources")
    if sources is None:
        return []
    if not isinstance(sources, list):
        out.err("okf.sources.invalid", "sources must be a list", "sources")
        return []
    ids: list[str] = []
    for i, item in enumerate(sources):
        where = f"sources[{i}]"
        if not isinstance(item, dict):
            out.err("okf.sources.resource.missing", "every source needs a resource", where)
            continue
        resource = item.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            out.err(
                "okf.sources.resource.missing",
                "every source needs a resource, and it has to be text",
                where,
            )
            continue
        if item.get("id") is not None:
            ids.append(str(item["id"]).strip())
    for duplicate in sorted({i for i in ids if ids.count(i) > 1}):
        out.err("okf.sources.id.duplicate", f"source id {duplicate!r} is used more than once", "sources")
    return ids


def _check_presentation(meta: dict[str, Any], out: _Collector) -> None:
    """The two lines that end up in an index, and later in a model's context."""
    description = meta.get("description")
    if description is None:
        out.err(
            "mabolo.description.missing",
            "description is missing, and it is the line the index and the search are built from",
            "description",
        )
    elif not isinstance(description, str):
        out.err("mabolo.description.invalid", "description must be text on one line", "description")
    elif not description.strip():
        out.err("mabolo.description.missing", "description is empty, and the index is built from it", "description")
    else:
        if "\n" in description.strip():
            out.err("mabolo.description.multiline", "description must be a single line", "description")
        if len(description) > MAX_DESCRIPTION:
            out.warn(
                "mabolo.description.long",
                f"description is {len(description)} characters, budget is {MAX_DESCRIPTION}",
                "description",
            )

    title = meta.get("title")
    if title is None:
        out.warn("mabolo.title.missing", "title is missing, so lists have nothing but the file name to show", "title")
    elif not isinstance(title, str) or not title.strip():
        out.err("mabolo.title.invalid", "title must be text", "title")
    elif "\n" in title.strip():
        out.err("mabolo.title.multiline", "title must be a single line, the index is built from it", "title")


def _check_block(block: dict[str, Any], out: _Collector, expected_area: str | None,
                 areas: Iterable[str]) -> str:
    """The namespaced part, and the area that decides where the entry belongs."""
    area = block.get("area")
    if not isinstance(area, str) or not area.strip():
        out.err("mabolo.area.missing", "mabolo.area is missing", "mabolo.area")
        area = ""
    else:
        area = area.strip()
        if not schema.area_is_known(area, tuple(areas)):
            out.err(
                "mabolo.area.unknown",
                f"area {area!r} is not configured and is not project/<name>",
                "mabolo.area",
            )
        elif expected_area is not None and area != expected_area:
            out.err(
                "mabolo.area.mismatch",
                f"area {area!r} does not match the folder, which says {expected_area!r}",
                "mabolo.area",
            )

    if "pin" in block and not isinstance(block.get("pin"), bool):
        out.err("mabolo.pin.invalid", "mabolo.pin must be true or false", "mabolo.pin")

    anchor = block.get("anchor")
    if anchor is not None:
        if not isinstance(anchor, str):
            out.err("mabolo.anchor.invalid", "mabolo.anchor must be a path as text", "mabolo.anchor")
        elif anchor.startswith("/") or anchor.startswith("~"):
            out.err("mabolo.anchor.absolute", "mabolo.anchor must be a path inside the project, not absolute", "mabolo.anchor")
        elif ".." in Path(anchor).parts:
            out.err("mabolo.anchor.traversal", "mabolo.anchor must not step out of the project", "mabolo.anchor")

    aliases = block.get("aliases")
    if aliases is not None:
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            out.warn("mabolo.aliases.invalid", "mabolo.aliases should be a list of names", "mabolo.aliases")
        else:
            for alias in aliases:
                name = schema.normalise_name(alias)
                if not ALIAS_RE.match(name) or name.lower() in RESERVED_STEMS:
                    out.err(
                        "mabolo.alias.invalid",
                        f"the alias {alias!r} cannot be resolved: no path separators, no reserved names",
                        "mabolo.aliases",
                    )
    return area


def _check_design(block: dict[str, Any], out: _Collector, area: str) -> None:
    """Taste is loaded when a matching file is touched, so it needs a trigger."""
    if area != "design":
        if block.get("applies_to"):
            out.warn(
                "mabolo.applies_to.outside_design",
                "applies_to only triggers loading for entries in the design area",
                "mabolo.applies_to",
            )
        return
    if not block.get("applies_to"):
        out.err(
            "mabolo.design.applies_to.missing",
            "a design entry needs applies_to, otherwise it is never loaded when it matters",
            "mabolo.applies_to",
        )
    if not block.get("instead_of"):
        out.warn(
            "mabolo.design.instead_of.missing",
            "instead_of names the default this rule overrides, which is what makes it checkable",
            "mabolo.instead_of",
        )
    scope = block.get("scope")
    if scope is not None and not (scope == "global" or schema.is_project_area(str(scope))):
        out.err("mabolo.design.scope.invalid", "mabolo.scope must be global or project/<name>", "mabolo.scope")


def _check_body(entry: Entry, body: str, source_ids: list[str], out: _Collector) -> None:
    """The prose, and the footnotes that carry what somebody actually said."""
    if len(body) > MAX_BODY:
        out.err("mabolo.body.too_large", f"the entry has {len(body)} characters, the limit is {MAX_BODY}", None)
    if not body.strip():
        out.warn("mabolo.body.empty", "the entry has a frontmatter and no text", None)

    ids = set(source_ids)
    for ref in sorted(entry.footnote_refs()):
        out.warn("mabolo.footnote.undefined", f"footnote [^{ref}] is used but never defined", None)
    for note in sorted(entry.footnote_ids() - ids):
        out.warn("mabolo.footnote.unsourced", f"footnote [^{note}] has no matching entry in sources", "sources")
    for unused in sorted(ids - entry.footnote_ids()):
        out.warn("mabolo.source.uncited", f"source {unused!r} is never cited in the text", "sources")

    for match in _WIKILINK.finditer(strip_code(body)):
        out.warn(
            "mabolo.link.wikilink",
            f"[[{match.group(1)}]] is not a Markdown link, so it breaks outside Obsidian",
            None,
        )


def validate_meta(
    meta: dict[str, Any],
    *,
    path: Path | None = None,
    body: str = "",
    expected_area: str | None = None,
    areas: Iterable[str] = FIXED_AREAS,
) -> list[Problem]:
    """Validate one frontmatter mapping. The file does not have to exist."""
    out = _Collector(path)
    _check_type(meta, out)
    _check_lifecycle(meta, out)
    _check_generated(meta, out)
    _check_verified(meta, out)
    source_ids = _check_sources(meta, out)
    _check_tags(meta, out)
    for key in [k for k in meta if k not in Entry.KNOWN]:
        out.warn("okf.field.unknown", f"unknown top level field {key!r}, Mabolo will not read it", key)

    block = meta.get("mabolo")
    if not isinstance(block, dict):
        out.err(
            "mabolo.block.missing",
            "the mabolo block is missing, so the entry has no area" if block is None else "mabolo must be a mapping",
            "mabolo",
        )
        block = {}
    area = _check_block(block, out, expected_area, areas)
    _check_presentation(meta, out)
    _check_design(block, out, area)
    _check_body(Entry.from_meta(meta, body=body, path=path), body, source_ids, out)
    return out.problems


def validate_document(
    doc: Document,
    *,
    root: Path | None = None,
    expected_area: str | None = None,
    areas: Iterable[str] = FIXED_AREAS,
) -> list[Problem]:
    """Validate a document that has already been read."""
    name = doc.path.name
    problems: list[Problem] = []
    if doc.crlf:
        problems.append(
            _problem(
                WARNING,
                "mabolo.eol.crlf",
                "the file uses CRLF line endings, and Mabolo writes LF, so the first write shows as a full rewrite",
                doc.path,
            )
        )
    if name in (INDEX_FILE, LOG_FILE):
        return problems + _validate_reserved(doc, root)
    if doc.meta is None:
        return problems + [
            _problem(
                ERROR,
                "okf.frontmatter.missing",
                "a concept document needs a frontmatter block",
                doc.path,
            )
        ]
    if root is not None and doc.path.parent == root:
        problems.append(
            _problem(
                ERROR,
                "mabolo.entry.at_root",
                "an entry lives in an area folder, not in the vault root",
                doc.path,
            )
        )
    problems += validate_meta(
        doc.meta, path=doc.path, body=doc.body, expected_area=expected_area, areas=areas
    )
    stem = doc.path.stem
    if stem in RESERVED_STEMS:
        problems.append(
            _problem(
                ERROR,
                "mabolo.name.reserved",
                f"{stem!r} is reserved: the index and the log are rebuilt from the entries",
                doc.path,
            )
        )
    elif not NAME_RE.match(stem):
        problems.append(
            _problem(
                ERROR,
                "mabolo.name.invalid",
                f"{stem!r} is not a usable entry name, use lower case, digits, dot, dash, underscore",
                doc.path,
            )
        )
    return problems


def _validate_reserved(doc: Document, root: Path | None) -> list[Problem]:
    """`index.md` and `log.md` have their own rules in the spec."""
    out: list[Problem] = []
    if doc.path.name == LOG_FILE:
        if doc.meta is not None:
            out.append(_problem(ERROR, "okf.log.frontmatter", "log.md must have no frontmatter", doc.path))
        return out + _validate_log_body(doc)

    at_root = root is not None and doc.path.parent == root
    if doc.meta is None:
        # A folder index carries no frontmatter, that is the rule. The root one
        # is where the format is declared, so there its absence is the finding.
        return out + (_check_okf_version(None, doc.path) if at_root else [])
    if not at_root:
        out.append(
            _problem(
                ERROR,
                "okf.index.frontmatter",
                "only the index.md in the vault root may carry frontmatter",
                doc.path,
            )
        )
        return out
    extra = [k for k in doc.meta if k != "okf_version"]
    if extra:
        out.append(
            _problem(
                ERROR,
                "okf.index.frontmatter",
                "index.md may only carry okf_version",
                doc.path,
            )
        )
    out += _check_okf_version(doc.meta.get("okf_version"), doc.path)
    return out


def _check_okf_version(version: Any, path: Path | None) -> list[Problem]:
    """The root index declares the format, and every other rule reads it that way.

    Without the declaration there is nothing that says the fields in this vault
    mean what this validator assumes, so a missing version is an error and not a
    detail. A version from a different line of the format is an error too: the
    same field name meant something else in v0.1.
    """
    if version is None:
        return [
            _problem(
                ERROR,
                "okf.version.missing",
                f"the root index.md declares no okf_version, and Mabolo reads this vault as OKF {OKF_VERSION}",
                path,
            )
        ]
    if not isinstance(version, (str, float, int)) or isinstance(version, bool):
        return [_problem(ERROR, "okf.version.invalid", "okf_version must be text", path)]
    if str(version) != OKF_VERSION:
        return [
            _problem(
                ERROR,
                "okf.version.mismatch",
                f"this vault declares OKF {version}, Mabolo reads OKF {OKF_VERSION}",
                path,
            )
        ]
    return []


def _validate_log_body(doc: Document) -> list[Problem]:
    """The spec wants ISO date headings, newest first, one group per day."""
    out: list[Problem] = []
    days: list[dt.date] = []
    for match in _LOG_HEADING.finditer(strip_code(doc.body)):
        heading = match.group(1)
        # `date.fromisoformat` also accepts `20260814`, which sorts differently
        # from the dashed form, so the shape is checked before the value.
        if not _ISO_DAY.match(heading):
            out.append(
                _problem(
                    ERROR,
                    "okf.log.heading",
                    f"{heading!r} is not an ISO date (YYYY-MM-DD), and log.md groups by date",
                    doc.path,
                )
            )
            continue
        try:
            days.append(dt.date.fromisoformat(heading))
        except ValueError:
            out.append(
                _problem(
                    ERROR,
                    "okf.log.heading",
                    f"{heading!r} is not a real date, and log.md groups by date",
                    doc.path,
                )
            )
    for day in sorted({d for d in days if days.count(d) > 1}):
        out.append(
            _problem(ERROR, "okf.log.duplicate", f"{day.isoformat()} has more than one group", doc.path)
        )
    if days != sorted(days, reverse=True):
        out.append(_problem(ERROR, "okf.log.order", "log.md has to run newest first", doc.path))
    return out


@dataclass(frozen=True)
class Inventory:
    """Every Markdown file of a vault, and what could not be looked at.

    One definition of "a file in this vault", used by the validator and by the
    index builder alike. Two definitions produce a vault where an entry is valid
    and missing from its own index at the same time.

    Symlinks are not followed: a vault that reads through a link reads whatever
    somebody else put at the other end.
    """

    files: list[Path] = field(default_factory=list)
    links: list[Path] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.problems


def iter_markdown(root: Path) -> Inventory:
    """Walk a vault. A folder that cannot be read becomes a finding, not a gap."""
    files: list[Path] = []
    links: list[Path] = []
    problems: list[Problem] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(directory.iterdir())
        except OSError as exc:
            # Skipping it quietly is how a run over half a vault reports success.
            problems.append(
                _problem(
                    ERROR,
                    "vault.dir.unreadable",
                    f"this folder cannot be read ({exc.strerror}), so the run did not cover it",
                    directory,
                )
            )
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            if child.is_symlink():
                links.append(child)
                continue
            if child.is_dir():
                stack.append(child)
            elif child.suffix.lower() == ".md":
                files.append(child)
    return Inventory(files=sorted(files), links=sorted(links), problems=problems)


def markdown_files(root: Path) -> list[Path]:
    """Every Markdown file of a vault, or an error if any folder was unreadable.

    A bare list means the list is complete. A caller that wants to carry on with
    a partial answer has to say so by using `iter_markdown` and looking at what
    it could not reach.
    """
    inventory = iter_markdown(root)
    if not inventory.complete:
        raise MaboloError(inventory.problems[0].message.replace("this folder", str(inventory.problems[0].path)))
    return inventory.files


def area_of(root: Path, path: Path) -> str | None:
    """The area a file sits in, derived from its folder."""
    parts = path.relative_to(root).parts[:-1]
    if not parts:
        return None
    if parts[0] == "project" and len(parts) >= 2:
        return f"project/{parts[1]}"
    return parts[0]


def validate_vault(root: Path, areas: Iterable[str] = FIXED_AREAS) -> Report:
    """Validate a whole vault, including the checks that need every file at once."""
    report = Report()
    names: dict[str, Path] = {}
    aliases: dict[str, Path] = {}
    inventory = iter_markdown(root)
    files, links = inventory.files, inventory.links
    report.extend(inventory.problems)

    for link in links:
        report.add(
            _problem(
                WARNING,
                "vault.symlink",
                "this is a symlink, and Mabolo neither reads nor writes through those",
                link,
            )
        )

    for path in files:
        report.checked += 1
        try:
            doc = read(path)
        except (FrontmatterError, MaboloError) as exc:
            report.add(_problem(ERROR, "okf.frontmatter.broken", str(exc), path))
            continue
        except UnicodeDecodeError:
            report.add(_problem(ERROR, "vault.file.unreadable", "the file is not valid UTF-8", path))
            continue
        except OSError as exc:
            report.add(_problem(ERROR, "vault.file.unreadable", f"the file cannot be read: {exc.strerror}", path))
            continue
        problems = validate_document(
            doc, root=root, expected_area=area_of(root, path), areas=areas
        )
        report.extend(problems)
        if doc.path.name in (INDEX_FILE, LOG_FILE) or doc.meta is None:
            continue
        entry = Entry.from_meta(doc.meta, body=doc.body, path=path, revision=doc.revision)
        report.entries.append(entry)
        if entry.name in names:
            report.add(
                _problem(
                    ERROR,
                    "vault.name.duplicate",
                    f"the name {entry.name!r} is already used by {names[entry.name].relative_to(root)}",
                    path,
                )
            )
        else:
            names[entry.name] = path
        for alias in entry.mabolo.aliases:
            if alias in aliases:
                report.add(
                    _problem(
                        ERROR,
                        "vault.alias.duplicate",
                        f"the alias {alias!r} is already used by {aliases[alias].relative_to(root)}",
                        path,
                    )
                )
            else:
                aliases[alias] = path

    for alias, path in aliases.items():
        if alias in names:
            report.add(
                _problem(
                    ERROR,
                    "vault.alias.collision",
                    f"the alias {alias!r} is also the name of an entry",
                    path,
                )
            )

    report.extend(_check_links(root, files))
    try:
        has_index = (root / INDEX_FILE).exists()
    except OSError as exc:
        report.add(
            _problem(ERROR, "vault.dir.unreadable", f"the vault root cannot be read: {exc.strerror}", root)
        )
        has_index = True
    if not has_index:
        report.add(
            _problem(WARNING, "vault.index.missing", "the vault has no root index.md", root / INDEX_FILE)
        )
    return report


def _check_links(root: Path, files: list[Path]) -> list[Problem]:
    """Links that point at nothing, and links that point out of the vault."""
    out: list[Problem] = []
    resolved_root = root.resolve()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in _MD_LINK.finditer(strip_code(text)):
            target = match.group(1)
            if "://" in target or target.startswith("#") or target.startswith("mailto:"):
                continue
            relative = target.split("#")[0]
            if not relative:
                continue
            # A leading slash is bundle relative in OKF, everything else is file relative.
            base = root if relative.startswith("/") else path.parent
            destination = base / relative.lstrip("/")
            try:
                inside = destination.resolve().is_relative_to(resolved_root)
            except OSError:
                inside = False
            if not inside:
                # A warning, not an error: pointing at a file in your project is
                # a reasonable thing to write down. It gets its own code because
                # what is at the other end is not vault knowledge, and Mabolo
                # never follows it to read anything.
                out.append(
                    _problem(
                        WARNING,
                        "mabolo.link.outside",
                        f"the link {target!r} leads out of the vault, and Mabolo never follows it",
                        path,
                    )
                )
                continue
            try:
                missing = not destination.exists()
            except OSError:
                # Unreadable is not the same as absent, and the folder that
                # cannot be read is already an error of its own.
                continue
            if missing:
                out.append(
                    _problem(WARNING, "mabolo.link.broken", f"the link {target!r} points at nothing", path)
                )
    return out
