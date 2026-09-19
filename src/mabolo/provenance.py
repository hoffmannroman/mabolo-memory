"""Where one entry came from: who wrote it, who approved it, what it quotes, what moved it.

This is the library behind `mabolo why <entry>`, one of the two commands that
turn the auditability claim into something a person can operate. The claim is
cheap to make and worth nothing until somebody can sit down with one entry and
check it, so this module answers exactly that: what the evidence is, and where
the holes in it are.

Four rules that are not obvious:

* **A mismatch is the answer, not an error.** A source nothing cites and a
  footnote no source backs are both printed, side by side with the ones that
  line up. That pair is the shape of a claim losing its evidence: somebody
  rewrote the prose and the frontmatter stayed behind, or the other way round.
  Leaving either out because it is malformed would hide the one thing in the
  report worth looking at.
* **No history is an answer too.** A vault is a folder of Markdown that happens
  to live in a Git repository, and it is allowed not to. Raising here would
  make provenance a property of Git rather than of the entry, and the entry is
  what the person asked about.
* **Everything printed comes off disk or out of Git**, so every value goes
  through `index.one_line`. A resource or an actor holding a newline would
  otherwise write its own section heading into a report whose whole value is
  that its structure can be trusted. The validator calls such a value a
  finding; this reader renders what is really there, which is why the defence
  sits at the point of rendering.
* **Footnotes are read the way the validator reads them**, definitions at the
  start of a line, fenced blocks and all. Two readings of the same body would
  mean `mabolo why` and `mabolo validate` disagreeing about which footnotes
  exist, and then neither of them is evidence.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from . import git
from .errors import MaboloError
from .index import one_line
from .schema import FOOTNOTE_DEFINITION, Entry, as_utc, normalise_name, parse_time
from .vault import Vault

#: What happened to the entry before Git ever saw it.
GENERATED = "generated"
VERIFIED = "verified"

#: How a source and the body agree, or fail to.
QUOTED = "quoted"
#: A source with an id that no footnote in the body cites.
UNCITED = "uncited"
#: A footnote citing an id that no source declares.
UNBACKED = "unbacked"
#: A source with no id at all, which no footnote can cite even in principle.
#: The importer writes these: it knows the file a claim came from and has no
#: sentence to quote from it.
UNQUOTED = "unquoted"

#: What a commit did to the file.
ADDED = "added"
CHANGED = "changed"
RENAMED = "renamed"
DELETED = "deleted"
#: A commit that is in the file's history and says nothing about how, which is
#: what a merge looks like. Naming it beats calling it a change it may not be.
TOUCHED = "touched"

_STATUS = {"A": ADDED, "M": CHANGED, "R": RENAMED, "D": DELETED, "C": ADDED, "T": CHANGED}

#: A footnote definition, anchored the way `Entry.footnote_ids` anchors it.
_DEFINITION = FOOTNOTE_DEFINITION

#: `git log` output, cut with bytes no commit message can contain. A commit
#: object may not hold a NUL, so a subject cannot forge a record boundary.
#: The format asks Git for them by escape rather than carrying them itself: a
#: NUL cannot travel through an argument list, and a format string holding one
#: never reaches Git at all.
_RECORD = "\x00"
_FIELD = "\x1f"
_LOG_FORMAT = "%x00%h%x1f%ad%x1f%s"

#: Where an act with an unreadable timestamp sorts: last, and without raising.
_FLOOR = dt.datetime.min.replace(tzinfo=dt.timezone.utc)

#: The line under a quote when there is no quote to print. Keyed by status so
#: that every row of the evidence block has the same two line shape, whether
#: the evidence is there or missing.
_INSTEAD = {
    QUOTED: "the footnote defines this id and says nothing",
    UNCITED: "no footnote in the body cites this source",
    UNBACKED: "the footnote defines this id and says nothing",
    UNQUOTED: "this source carries no id, so no footnote can cite it",
}


@dataclass(frozen=True)
class Act:
    """One thing a person or a producer did to the entry, and when.

    `at` is the timestamp as the file spells it, because that is what a person
    is checking. `when` is the same moment in UTC and only exists to order the
    acts, since the two halves of a provenance are often written in different
    zones by different machines. It is None when the timestamp cannot be read,
    which is a finding for `validate` and no reason to drop the act.
    """

    kind: str
    by: str
    at: str
    when: dt.datetime | None = None


@dataclass(frozen=True)
class Quote:
    """One piece of evidence, or one half of a piece that lost its other half.

    `id` is None only for a source that declares none. `resource` is None for a
    footnote no source backs, and `text` is None for a source no footnote
    cites. The row exists either way: the gap is the finding.
    """

    id: str | None
    resource: str | None
    text: str | None
    status: str


@dataclass(frozen=True)
class Commit:
    """One commit that touched the file, as `git log --follow` reports it."""

    revision: str
    at: str
    subject: str
    change: str
    #: The path the file was called before this commit renamed it.
    previous: str | None = None


def _in_quotes(said: str) -> str:
    """The sentence in one pair of quotation marks, however the file spells it.

    An entry Mabolo wrote stores the sentence quoted, so wrapping it again
    printed `""like this""`, which reads like a transcription error in the
    evidence that is meant to be the trustworthy part. A footnote somebody
    added by hand without quotation marks still gets them here.
    """
    if len(said) > 1 and said.startswith('"') and said.endswith('"'):
        return said
    return f'"{said}"'


@dataclass(frozen=True)
class Provenance:
    """Everything `mabolo why` knows about one entry, ready to be printed."""

    name: str
    #: The file on disk, for a caller that has to open it again.
    path: Path
    #: The same file spelled inside the vault, which is what a person reads and
    #: what Git was asked about.
    where: str
    revision: str
    #: Production and approval, oldest first.
    acts: tuple[Act, ...] = ()
    #: Sources in the order the frontmatter declares them, then the footnotes
    #: nothing declares, in body order.
    quotes: tuple[Quote, ...] = ()
    #: Newest first, the order Git reports and the order a person reads.
    commits: tuple[Commit, ...] = ()
    #: Why there are no commits, when there are none. None when there are.
    history_note: str | None = None
    #: `mabolo.anchor` as the entry carries it.
    anchor: str | None = None
    #: Whether that anchor still points at something that has not moved.
    #: **Nothing in this module fills this in.** Drift detection lives in
    #: `mabolo.drift`, and the caller that owns both, the `why` command, sets
    #: this before calling `render`. None means the question was never asked,
    #: and `render` says so rather than implying the anchor is fine.
    anchor_note: str | None = None

    def render(self) -> str:
        """The report a person reads, four blocks, narrow enough for a phone."""
        lines = [
            one_line(self.name),
            f"  file      {one_line(self.where)}",
            f"  revision  {one_line(self.revision)}",
        ]
        for block in (self._who(), self._evidence(), self._anchor(), self._history()):
            lines.append("")
            lines.extend(block)
        return "\n".join(lines)

    def _who(self) -> list[str]:
        if not self.acts:
            return ["who and when", "  nobody is named: the entry says neither who produced it nor who approved it"]
        kind = max(len(a.kind) for a in self.acts)
        by = max(len(one_line(a.by)) for a in self.acts)
        return ["who and when"] + [
            f"  {a.kind:<{kind}}  {one_line(a.by):<{by}}  {one_line(a.at)}" for a in self.acts
        ]

    def _evidence(self) -> list[str]:
        if not self.quotes:
            return ["evidence", "  nothing: this entry names no source and quotes nothing"]
        width = max(len(one_line(q.id or "-")) for q in self.quotes)
        status = max(len(q.status) for q in self.quotes)
        out = ["evidence"]
        for quote in self.quotes:
            head = f"  {one_line(quote.id or '-'):<{width}}  {quote.status:<{status}}"
            resource = one_line(quote.resource) if quote.resource else ""
            out.append(f"{head}  {resource}".rstrip())
            said = _in_quotes(one_line(quote.text)) if quote.text else _INSTEAD[quote.status]
            out.append(f"      {said}")
        return out

    def _anchor(self) -> list[str]:
        if self.anchor is None:
            note = self.anchor_note or "none, so nothing about this entry is watched for movement"
            return ["anchor", f"  {one_line(note)}"]
        note = self.anchor_note or "whether it has moved was not checked"
        return ["anchor", f"  {one_line(self.anchor)}", f"      {one_line(note)}"]

    def _history(self) -> list[str]:
        if not self.commits:
            return ["history", f"  {one_line(self.history_note or 'no commit touched this file')}"]
        revision = max(len(c.revision) for c in self.commits)
        change = max(len(c.change) for c in self.commits)
        out = ["history"]
        for commit in self.commits:
            out.append(
                f"  {commit.revision:<{revision}}  {one_line(commit.at)}  "
                f"{commit.change:<{change}}  {one_line(commit.subject)}"
            )
            if commit.previous:
                out.append(f"      from {one_line(commit.previous)}")
        return out


def of(vault: Vault, name: str) -> Provenance:
    """Everything known about the entry of this name, or an error naming it.

    Reading the file is enough for three of the four blocks. Only the history
    needs Git, and only the history is allowed to come back empty.
    """
    entry = find(vault, name)
    if entry.path is None:  # pragma: no cover - `find` only ever returns read entries
        raise MaboloError(f"{name!r} was not read from a file, so it has no provenance")
    where = entry.path.relative_to(vault.root).as_posix()
    commits, note = history(vault.root, where)
    return Provenance(
        name=entry.path.stem,
        path=entry.path,
        where=where,
        revision=entry.revision,
        acts=acts(entry),
        quotes=quotes(entry),
        commits=tuple(commits),
        history_note=note,
        anchor=entry.mabolo.anchor,
    )


def find(vault: Vault, name: str) -> Entry:
    """The entry this name means, by file name first and by alias after.

    The file name is tried on its own, without reading anything else. An entry
    somebody renamed keeps the old name as an alias, and `why` is precisely the
    command reached for after a rename, so the alias has to work; but a vault
    holding one unreadable file must not stop `why` answering about a file that
    reads perfectly. Hence the two passes, and hence the count of unreadable
    files in the message when nothing matched: a name that is missing and a
    name that is in a file nobody can parse are different problems.
    """
    wanted = normalise_name(str(name))
    if not wanted:
        raise MaboloError("name the entry you want to know about")

    paths = [p for p in vault.entry_paths() if p.stem == wanted]
    if len(paths) > 1:
        found = ", ".join(sorted(p.relative_to(vault.root).as_posix() for p in paths))
        raise MaboloError(f"{wanted!r} is the name of more than one file here: {found}")
    if paths:
        return vault.read_entry(paths[0])

    entries, unreadable = vault.readable_entries()
    matches = [e for e in entries if wanted in e.mabolo.aliases]
    if len(matches) > 1:
        found = ", ".join(sorted(e.name for e in matches))
        raise MaboloError(f"{wanted!r} is an alias of more than one entry: {found}")
    if matches:
        return matches[0]

    trailer = ""
    if unreadable:
        trailer = (
            f", and {len(unreadable)} file(s) here could not be read. "
            "Run `mabolo validate` to see them by name."
        )
    raise MaboloError(f"there is no entry called {wanted!r} in this vault{trailer}")


def acts(entry: Entry) -> tuple[Act, ...]:
    """Production and approval, oldest first, in the order they really happened.

    Not in the order the frontmatter lists them. The two halves are written by
    two different machines, often in two different zones, and a verification is
    routinely added to an entry that was generated somewhere else; sorting by
    the moment is the only reading that survives that. `as_utc` is what makes a
    timestamp without an offset comparable with one that has one at all.
    """
    found: list[Act] = []
    if entry.generated:
        found.append(_act(GENERATED, entry.generated.by, entry.generated.at))
    for verification in entry.verified:
        found.append(_act(VERIFIED, verification.by, verification.at))
    found.sort(key=lambda a: (a.when is None, a.when or _FLOOR))
    return tuple(found)


def _act(kind: str, by: str, at: str) -> Act:
    parsed = parse_time(at)
    return Act(kind=kind, by=by, at=at, when=as_utc(parsed) if parsed is not None else None)


def quotes(entry: Entry) -> tuple[Quote, ...]:
    """Every source and every footnote, including the ones with no counterpart.

    Sources first, in the order the entry declares them, then the footnotes
    nothing declares. A reader looking for what an entry rests on reads the
    frontmatter's order; a reader looking for what went wrong needs the
    leftovers to be somewhere, and the end of the same list is where they are
    impossible to miss.
    """
    notes = footnotes(entry.body)
    found: list[Quote] = []
    for source in entry.sources:
        if source.id is None:
            found.append(Quote(None, source.resource, None, UNQUOTED))
        elif source.id in notes:
            found.append(Quote(source.id, source.resource, notes[source.id], QUOTED))
        else:
            found.append(Quote(source.id, source.resource, None, UNCITED))
    declared = {s.id for s in entry.sources if s.id}
    for note, text in notes.items():
        if note not in declared:
            found.append(Quote(note, None, text, UNBACKED))
    return tuple(found)


def footnotes(body: str) -> dict[str, str]:
    """Every footnote definition in the body, by id, with the sentence it holds.

    **A definition runs until the next one.** A quote is a sentence somebody
    said, it wraps in the file and in every editor that touches it, and a
    reader that took only the first physical line would print half a quote that
    looks like a whole one. So indented continuation lines are folded back in,
    which is what Markdown itself does with them, and a blank or unindented
    line ends the definition.

    A second definition of the same id keeps the first, the way a Markdown
    renderer does. The vault shows what a reader of the file would see.
    """
    found: dict[str, str] = {}
    current: str | None = None
    parts: list[str] = []

    def close() -> None:
        nonlocal current
        if current is not None and current not in found:
            found[current] = " ".join(" ".join(parts).split())
        current = None
        parts.clear()

    for raw in body.splitlines():
        definition = _DEFINITION.match(raw)
        if definition:
            close()
            current = definition.group(1)
            parts.append(definition.group(2))
            continue
        if current is None:
            continue
        if raw.strip() and raw[:1] in (" ", "\t"):
            parts.append(raw.strip())
            continue
        close()
    close()
    return found


def history(root: Path, where: str) -> tuple[list[Commit], str | None]:
    """Every commit that touched this one file, newest first, and why there is none.

    `--follow` is the whole point: an entry that was renamed keeps the commits
    from before the rename, and without it the history of a renamed entry
    starts at the rename and looks like the entry was invented there.

    A vault with no repository, a repository with no commit and a file nobody
    has committed yet are three different sentences and none of them is a
    failure. Git is how a vault syncs, not what makes an entry an entry.
    """
    try:
        if git.repository_dir(root) is None:
            return [], "this vault is not a Git repository, so there is no history to read"
        if git.rev(root, "HEAD") is None:
            return [], "this vault has no commit yet, so there is no history to read"
        result = git.run(
            root,
            "log",
            "--follow",
            "--name-status",
            "--date=short",
            f"--format={_LOG_FORMAT}",
            "--",
            where,
            check=False,
        )
    except MaboloError as exc:
        return [], str(exc)
    if result.returncode != 0:
        return [], "git could not read the history of this file"
    found = parse_log(result.stdout)
    if not found:
        return [], "this file is in no commit yet, so Git has nothing to say about it"
    return found, None


def parse_log(text: str) -> list[Commit]:
    """`git log --follow --name-status` turned into commits, in the order given.

    The order is Git's, not ours: it already runs newest first, and re-sorting
    here would hide a caller that asked for something else.
    """
    found: list[Commit] = []
    for record in text.split(_RECORD):
        if not record.strip():
            continue
        lines = record.splitlines()
        head = lines[0].split(_FIELD)
        if len(head) < 3:
            continue
        change, previous = TOUCHED, None
        for line in lines[1:]:
            if not line.strip():
                continue
            fields = line.split("\t")
            change = _STATUS.get(fields[0][:1], TOUCHED)
            if change == RENAMED and len(fields) >= 3:
                previous = fields[1]
            break
        found.append(
            Commit(
                revision=head[0],
                at=head[1],
                subject=_FIELD.join(head[2:]),
                change=change,
                previous=previous,
            )
        )
    return found
