"""Renaming an area, which is what happens when a project is renamed.

A project keeps its memory in `project/<name>`, and a session finds that memory
by the folder it is running in: the working directory's name is matched against
the areas that exist. Rename the directory and the repository, leave the area
alone, and the match stops. The entries are all still there and none of them
arrives, which is the worst shape a memory can fail in, because nothing reports
it.

Three things this does and one it refuses.

**It moves the files and says where they went.** Every entry of the area, its
`mabolo.area`, and the one entry named after the area itself, which is renamed
with it because that name *is* the area's name.

**It repairs the links that pointed into the folder.** A link is an address, so
an address that moved is a link that has to move with it; this is the one part
that would otherwise be found weeks later as a dead link.

**It goes through `write.apply`,** in one commit with a kind of its own. One
commit because an area half moved is a vault that neither name finds, and its
own kind because a reader of the history has to be able to tell a commit that
renamed a folder from one that changed what the memory knows.

**It does not rewrite prose, and it does not touch the journal.** The journal is
the one file written in the order things happened, and what it says was true on
the day it was written: the project really was called that then. The same goes
for an import's provenance, which records the address a file came from and not a
claim about today. Both are reported and left alone, because deciding that a
sentence is now wrong is a person's judgement and not a rename's.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from . import frontmatter, lint, validate, write
from .errors import MaboloError
from .schema import PROJECT_PREFIX, Entry, is_project_area

#: The kind of commit a move makes. In `write.KINDS` for the usual reason.
KIND = "move"

#: A name standing alone, for finding the mentions this command leaves behind.
#: The same boundary `lint` hunts names within, so the two agree about what
#: counts as a mention and a person reading both reports is not told two things.
_EDGE = lint._EDGE


@dataclass(frozen=True)
class Plan:
    """What a move would do, and what it deliberately would not."""

    old: str
    new: str
    changes: list[write.Change]
    #: Vault relative paths, `old -> new`, for the entries that move.
    moved: list[tuple[str, str]]
    #: Entries outside the area whose links had to be readdressed.
    relinked: list[str]
    #: Places that name the old area and are left exactly as they are.
    left: list[str]

    @property
    def empty(self) -> bool:
        return not self.changes

    def render(self) -> str:
        lines = [f"{self.old} -> {self.new}", ""]
        lines += [f"  {was} -> {now}" for was, now in self.moved]
        if self.relinked:
            lines += ["", "links readdressed in:"]
            lines += [f"  {where}" for where in self.relinked]
        if self.left:
            lines += [
                "",
                "left alone, because a rename does not get to decide that a "
                "sentence is now wrong:",
            ]
            lines += [f"  {where}" for where in self.left]
        return "\n".join(lines)


def plan(vault, old: str, new: str) -> Plan:
    """Everything the move would write, without writing any of it.

    Both areas have to be project areas. The fixed areas are fixed on purpose,
    and a move between two depths would silently break every relative link in
    the folder: `../../hosts/x.md` counts parents, and the count only stays
    right because `project/a` and `project/b` sit at the same depth.
    """
    for area in (old, new):
        if not is_project_area(area):
            raise MaboloError(
                f"{area!r} is not a project area. Only those are renamed: the "
                f"fixed areas are fixed, and a move across depths would break "
                f"every relative link in the folder."
            )
    if old == new:
        raise MaboloError("those are the same area")
    old_dir, new_dir = vault.area_dir(old), vault.area_dir(new)
    if not old_dir.is_dir():
        raise MaboloError(f"there is no {old_dir.relative_to(vault.root)} in this vault")
    if new_dir.exists():
        raise MaboloError(
            f"{new_dir.relative_to(vault.root)} already exists. Merging two areas is a "
            f"different decision from renaming one, and this command does not make it."
        )

    entries = vault.entries()
    moving = [e for e in entries if e.path is not None and e.path.parent == old_dir]
    if not moving:
        raise MaboloError(f"{old} holds no entries, so there is nothing to move")

    old_name, new_name = old[len(PROJECT_PREFIX):], new[len(PROJECT_PREFIX):]
    # The entry named after the area is renamed with it: that name *is* the
    # area's name, and `lint` reads it to tell an entry's own overview from a
    # reference to somewhere else. Every other name travels unchanged, because
    # renaming an entry changes its identity, and its identity is what the eval
    # baseline, the inbox and every link elsewhere are keyed by.
    destination = {
        e.path: new_dir / f"{new_name if e.path.stem == old_name else e.path.stem}.md"
        for e in moving
    }

    changes: list[write.Change] = []
    moved: list[tuple[str, str]] = []
    for entry in moving:
        target = destination[entry.path]
        moved_entry = replace(
            entry,
            mabolo=replace(entry.mabolo, area=new),
            title=new_name if entry.title == old_name else entry.title,
            body=_readdressed(entry.body, entry.path.parent, target.parent, destination),
            # The mapping as it was read is validated too, on purpose: its
            # errors are the ones a rewrite would tidy away unseen. So the area
            # has to move there as well, or the entry is refused for still
            # saying, in the copy nobody edits, where it used to live.
            raw=_raw_with_area(entry.raw, new),
        )
        _, data = vault.render_entry(moved_entry, target)
        changes.append(write.Change(path=_rel(vault, entry.path), data=None,
                                    expect=entry.revision))
        changes.append(write.Change(path=_rel(vault, target), data=data, expect=None))
        moved.append((_rel(vault, entry.path), _rel(vault, target)))

    # The old folder's index describes a folder that will be empty. Named here
    # rather than left to the derivation, which only ever writes an index and
    # never removes one.
    stale_index = old_dir / "index.md"
    if stale_index.is_file():
        changes.append(write.Change(
            path=_rel(vault, stale_index), data=None,
            expect=frontmatter.revision(stale_index.read_bytes())))

    relinked: list[str] = []
    for entry in entries:
        if entry.path is None or entry.path in destination:
            continue
        body = _readdressed(entry.body, entry.path.parent, entry.path.parent, destination)
        if body == entry.body:
            continue
        _, data = vault.render_entry(replace(entry, body=body), entry.path)
        changes.append(write.Change(path=_rel(vault, entry.path), data=data,
                                    expect=entry.revision))
        relinked.append(_rel(vault, entry.path))

    return Plan(old=old, new=new, changes=changes, moved=moved, relinked=relinked,
                left=_mentions(vault, entries, destination, old_name))


def _raw_with_area(raw: dict, area: str) -> dict:
    """The entry's original mapping with the area it now lives in."""
    if not raw or not isinstance(raw.get("mabolo"), dict):
        return raw
    return {**raw, "mabolo": {**raw["mabolo"], "area": area}}


def _rel(vault, path: Path) -> str:
    return path.relative_to(vault.root).as_posix()


def _readdressed(body: str, was: Path, now: Path, destination: dict[Path, Path]) -> str:
    """The body with every link to a moved file pointing at where it moved to.

    Resolved against the folder the link was written in and recomputed from the
    folder it will be read in, so a link that survives the move untouched comes
    back byte for byte. Built on the validator's own idea of what a link is: a
    second one here would mean two answers to "does this line contain a link",
    and then the report and the repair disagree about the same file.
    """
    def moved_to(match: re.Match[str]) -> str:
        whole, target = match.group(0), match.group(1)
        if "://" in target or target.startswith("#"):
            return whole
        try:
            landed = (was / target).resolve()
        except (OSError, ValueError, RuntimeError):
            return whole
        if landed not in destination:
            return whole
        fresh = os.path.relpath(destination[landed], now)
        return whole[: whole.index("](") + 2] + fresh + whole[whole.index("](") + 2 + len(target):]

    return validate._MD_LINK.sub(moved_to, body)


def _mentions(vault, entries, destination: dict[Path, Path], name: str) -> list[str]:
    """Every place that still says the old name once the addresses are right.

    Reported and not rewritten. A mention is a sentence somebody wrote, and
    whether it became wrong when the project was renamed is a judgement about
    the sentence: "the older product was folded into atlas in September" is
    still true, and "atlas is only a working title" is not. A rename that
    guessed would be wrong in one of those two directions every time.
    """
    standing = re.compile(rf"(?<!{_EDGE}){re.escape(name)}(?!{_EDGE})", re.IGNORECASE)
    out: list[str] = []
    for entry in entries:
        if entry.path is None:
            continue
        # The moved entries too, under the names they will have. They were the
        # gap in the first version of this report: an entry that travels with
        # the area is the likeliest of all to still say the old name, and the
        # area's own overview is both the likeliest and the most read.
        where = destination.get(entry.path, entry.path)
        # The frontmatter as well as the prose: a description is the line a
        # session is shown first, and it is exactly where a stale name does the
        # most damage while being the hardest to see.
        text = f"{entry.description or ''}\n{entry.title or ''}\n{entry.body}"
        if standing.search(lint._prose(text)):
            out.append(_rel(vault, where))
    if vault.log_file.is_file():
        found = standing.findall(vault.log_file.read_text(encoding="utf-8"))
        if found:
            out.append(f"{_rel(vault, vault.log_file)} ({len(found)} times, and it is a record)")
    return sorted(out)


def carry_out(vault, made: Plan, *, actor: str, remote: str | None = None,
              branch: str | None = None) -> write.Result:
    """Write the move, in one commit, through the door everything else uses."""
    result = write.apply(
        vault.root,
        made.changes + vault.index_changes(made.changes),
        f"rename {made.old} to {made.new}",
        actor=actor,
        kind=KIND,
        remote=remote,
        branch=branch,
    )
    if result.ok:
        # A folder is not part of a commit, so nothing above removes the one
        # left behind, and a vault with both names in it reads as though the
        # move stopped halfway. Only when it is empty: anything still in there
        # is somebody's file and not this command's to delete.
        try:
            vault.area_dir(made.old).rmdir()
        except OSError:
            pass
    return result
