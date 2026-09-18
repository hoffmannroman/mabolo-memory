"""Reading and writing Markdown files with a YAML frontmatter block.

Four rules the rest of the package depends on:

* A file either opens with a frontmatter block or has none at all. A block that
  opens and never closes is an error, not a file without frontmatter, because
  silently treating half a schema as prose is how garbage enters a vault.
* A duplicate key is an error. YAML keeps the last value and drops the first
  without a word, which is exactly the kind of quiet loss this project exists to
  prevent, and it is a common mistake when a person edits frontmatter by hand.
* Reading is bounded. A file is measured before it is parsed, so one accidental
  export cannot take the whole run down with it.
* Writing normalises the file name, never the folders above it. Normalising a
  whole path silently moves a write into a different directory, which on a
  filesystem that keeps both spellings can be somebody else's.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import MaboloError

#: Line that opens a frontmatter block, and the two lines that may close it.
_OPEN = re.compile(r"^---[ \t]*$")
_CLOSE = re.compile(r"^(?:---|\.\.\.)[ \t]*$")

#: A single entry is prose. These are not budgets, they are the point where a
#: file stops being an entry and becomes an accident.
MAX_FILE_BYTES = 4_000_000
MAX_FRONTMATTER_BYTES = 256_000

#: Frontmatter key order. Anything not listed keeps its own order behind these.
KEY_ORDER = (
    "okf_version",
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


class FrontmatterError(MaboloError):
    """The frontmatter block is missing its end, duplicated, or not valid YAML."""


class _StrictLoader(yaml.SafeLoader):
    """A safe loader that refuses to drop a duplicated key on the floor."""


def _no_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise FrontmatterError(
                f"the frontmatter sets {key!r} twice, and YAML would keep only the last one"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


@dataclass(frozen=True)
class Document:
    """A Markdown file split into its frontmatter and its body."""

    path: Path
    meta: dict[str, Any] | None
    body: str
    revision: str
    #: True when the file on disk used CRLF. Writing always produces LF, so a
    #: caller that cares has to say so rather than let it happen quietly.
    crlf: bool = False

    @property
    def has_frontmatter(self) -> bool:
        return self.meta is not None


def revision(data: bytes) -> str:
    """Short content revision of a file, used to detect a lost update."""
    return hashlib.sha256(data).hexdigest()[:12]


def split(text: str) -> tuple[str | None, str]:
    """Split raw text into the YAML source of the frontmatter and the body."""
    text = text.removeprefix("﻿")
    lines = text.splitlines(keepends=True)
    if not lines or not _OPEN.match(lines[0].rstrip("\r\n")):
        return None, text
    for i in range(1, len(lines)):
        if _CLOSE.match(lines[i].rstrip("\r\n")):
            head = "".join(lines[1:i])
            body = "".join(lines[i + 1 :])
            return head, body.lstrip("\r\n")
    raise FrontmatterError("the frontmatter block opens with --- and never closes")


def parse(text: str) -> tuple[dict[str, Any] | None, str]:
    """Parse raw text into a frontmatter mapping and the body below it."""
    head, body = split(text)
    if head is None:
        return None, body
    if len(head.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise FrontmatterError(
            f"the frontmatter is larger than {MAX_FRONTMATTER_BYTES} bytes, which no entry needs"
        )
    try:
        meta = yaml.load(head, Loader=_StrictLoader)
    except FrontmatterError:
        raise
    except (yaml.YAMLError, ValueError) as exc:  # ValueError: a date like 2026-02-30
        raise FrontmatterError(f"the frontmatter is not valid YAML: {_terse(exc)}") from exc
    if meta is None:
        return {}, body
    if not isinstance(meta, dict):
        raise FrontmatterError("the frontmatter is not a mapping of keys to values")
    return meta, body


def _terse(exc: Exception) -> str:
    """A YAML error without the offending line.

    The line is the one place a secret could sit, and these messages travel into
    reports and, later, into an agent's context.
    """
    problem = getattr(exc, "problem", None)
    mark = getattr(exc, "problem_mark", None)
    if problem and mark is not None:
        return f"{problem} at line {mark.line + 1}, column {mark.column + 1}"
    if problem:
        return str(problem)
    return type(exc).__name__


def read(path: Path) -> Document:
    """Read one Markdown file. Raises `FrontmatterError` on a broken block."""
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise FrontmatterError(
            f"the file is {size} bytes, above the {MAX_FILE_BYTES} byte limit for an entry"
        )
    data = path.read_bytes()
    text = data.decode("utf-8")
    meta, body = parse(text)
    return Document(
        path=path,
        meta=meta,
        body=body,
        revision=revision(data),
        crlf="\r\n" in text,
    )


def order_keys(meta: dict[str, Any]) -> dict[str, Any]:
    """Known keys in a fixed order, unknown ones after them in their own order."""
    ordered = {k: meta[k] for k in KEY_ORDER if k in meta}
    ordered.update({k: v for k, v in meta.items() if k not in ordered})
    return ordered


def dump(meta: dict[str, Any] | None, body: str) -> str:
    """Render frontmatter and body back into one document."""
    text = body.strip("\r\n")
    text = f"{text}\n" if text else ""
    if meta is None:
        return text
    head = yaml.safe_dump(
        order_keys(meta),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        # No wrapping: a wrapped description is still one value, but it stops
        # being greppable, and a vault is meant to be read with ordinary tools.
        width=10**6,
    ).strip("\n")
    return f"---\n{head}\n---\n\n{text}"


def normalise_target(path: Path) -> Path:
    """The path to write to, with only the file name normalised to NFC.

    The folders above it are left exactly as they were. Normalising them would
    move the write into a directory nobody checked, and on Linux the two
    spellings are two different directories.
    """
    return path.parent / unicodedata.normalize("NFC", path.name)


def write(path: Path, meta: dict[str, Any] | None, body: str) -> str:
    """Write a document and return its revision."""
    target = normalise_target(Path(path))
    if target.is_symlink() or target.parent.is_symlink():
        raise FrontmatterError(f"{target} is a symlink, and the vault does not write through those")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = dump(meta, body).encode("utf-8")
    target.write_bytes(data)
    return revision(data)
