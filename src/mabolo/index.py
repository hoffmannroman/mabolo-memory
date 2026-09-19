"""The index: BM25 over SQLite FTS5, rebuilt from the files every time.

The index is derived and disposable. It is built in memory from the entries as
they are on disk, in a fixed order, so the same commit always produces the same
index and the same ranking. That is not tidiness, it is the precondition for
`bisect-memory`: walking the vault's history is only meaningful if an old commit
can be replayed exactly.

FTS5 ships with the standard library's `sqlite3`, so this costs no dependency.
What it does cost is a second tokeniser, and two tokenisers that disagree are a
search that cannot find the word that was typed. So FTS5 never sees prose: it
sees the output of `query.tokenise`, one word per token, and `remove_diacritics`
is switched off so that a word keeps its accents on both sides. That last part
was missed once: FTS5 folded the accents away, the relevance check did not, and
a question spelled without them matched inside SQLite and was thrown away again
by the floor.

**Relevance is absolute, and the evidence belongs to one entry.** A question
gets an entry back when it names that entry, or when two of the question's words
are in it. Naming counts for the entry that was named and for nothing else: a
global "some name was mentioned somewhere" let a question about one project drag
in an unrelated entry that happened to share a single common word.

Two things are deliberately not here yet:

* **Ranking ignores `status`.** A deprecated entry is found like any other. What
  should happen to it is a question about staleness, and staleness has its own
  phase.
* **No usage counts, no recency, no learning.** Ranking is a pure function of
  the files and the query. Anything that remembers what was clicked yesterday
  cannot be replayed at an old commit.
"""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import query as q
from .errors import MaboloError
from .schema import PROJECT_PREFIX, Entry

#: The searchable fields and their BM25 weight, in one place. The column list,
#: the insert statement and the weight arguments are all built from this, so a
#: field cannot end up carrying the weight of its neighbour. A name and a title
#: are what a person would type; the body is context and counts least.
FIELDS: tuple[tuple[str, float], ...] = (
    ("name", 8.0),
    ("title", 6.0),
    ("aliases", 5.0),
    ("description", 3.0),
    ("body", 1.0),
)
COLUMNS = tuple(name for name, _ in FIELDS)
WEIGHTS = tuple(weight for _, weight in FIELDS)

#: How many lines a search returns unless the caller says otherwise. The number
#: is the one the eval measures against, and five lines is what fits in a
#: preview without crowding out the answer.
DEFAULT_LIMIT = 5

#: Characters per token, for the cost estimate. A rough constant on purpose:
#: the real count depends on the tokeniser of whichever model reads it, and
#: every number this produces is presented as an estimate.
CHARS_PER_TOKEN = 4

#: The score of an entry that was named and matched no word. Worse than any
#: BM25 score, which is negative, so a named entry never pushes a real match
#: down; it exists so that `ci` finds the entry called `ci`.
NAMED_ONLY_SCORE = 0.0

_SCHEMA = (
    f"CREATE VIRTUAL TABLE entries USING fts5({', '.join(COLUMNS)}, "
    # Accents stay. The relevance check below compares Python's folded words,
    # and Python does not strip them either.
    "tokenize = 'unicode61 remove_diacritics 0');"
)
_INSERT = (
    f"INSERT INTO entries(rowid, {', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('?' * (len(COLUMNS) + 1))})"
)


def estimate_tokens(text: str) -> int:
    """A rough token count for a piece of text. Always called an estimate."""
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def one_line(text: str) -> str:
    """Text reduced to a single printable line.

    Every payload built from entries has a structure a reader is meant to trust,
    and every part of that structure comes from a file a person or an agent
    wrote. A description holding a newline can therefore write its own heading
    and its own closing line into the middle of a payload that is injected into
    a prompt automatically. The validator calls a multi line description an
    error, but the readers here render whatever is on disk, so the structure is
    defended at the point of rendering rather than upstream. Control characters
    go the same way: they cannot be seen, and what cannot be seen cannot be
    checked.
    """
    collapsed = " ".join(text.split())
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in collapsed)


def entry_line(name: str, description: str, title: str) -> str:
    """The one line that stands for an entry, wherever one is listed.

    Both readers of a vault show an entry the same way, so they say it in one
    place. They were two copies for exactly one commit, and the copies were
    already apart: the session index put every value through `one_line` and the
    search preview did not, which made the second one a way back in for the
    forged heading the first one had just been taught to refuse.
    """
    return f"- {one_line(name)}: {one_line(description or title or name)}"


@dataclass(frozen=True)
class Document:
    """One entry, reduced to what the search and the session index need.

    `pin` and `at` are not searched. They belong to the other reader of this
    list: the session index picks its lines by them, and building a second view
    of the same files to carry two fields would be a second place that can
    disagree with this one about what is in the vault.
    """

    name: str
    title: str
    description: str
    area: str
    path: Path | None
    aliases: tuple[str, ...]
    #: The entry asked to be in every session.
    pin: bool = False
    #: The day it asked, when it says so. The core has a fixed number of seats
    #: and the newest pin is the one that loses one, so this is what decides.
    #: None means the pin named no day and `at` stands in for it.
    pinned_at: dt.date | None = None
    #: When the entry was last touched, in UTC. See `Entry.touched_at`, which is
    #: where that is decided; nothing here interprets a timestamp of its own.
    at: dt.datetime | None = None
    #: Every word of the entry, folded and split the way a query is, sorted so
    #: that a prefix can be found without walking the list.
    words: tuple[str, ...] = field(default_factory=tuple)
    fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        """The folded name, which is the spelling a question is compared against."""
        return q.fold(self.name)

    def has_prefix(self, stem: str) -> bool:
        position = bisect_left(self.words, stem)
        return position < len(self.words) and self.words[position].startswith(stem)


@dataclass(frozen=True)
class Hit:
    """One entry the search returned, with where it landed and why."""

    document: Document
    rank: int
    score: float
    #: The query stems this entry actually contains.
    matched: tuple[str, ...] = ()
    #: The names of this entry that the question used, if it used any.
    named: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.document.name

    @property
    def title(self) -> str:
        return self.document.title

    @property
    def description(self) -> str:
        return self.document.description

    @property
    def area(self) -> str:
        return self.document.area

    @property
    def path(self) -> Path | None:
        return self.document.path

    def line(self) -> str:
        """The one line a preview of a search result would show for this entry."""
        return entry_line(self.name, self.description, self.title)


@dataclass(frozen=True)
class SearchResult:
    """What one search did: the question as parsed, and what came back.

    One object rather than a bare list, because `--explain` used to rebuild the
    query and re-derive the name evidence from a second copy of the rule. Two
    copies of a rule produce a tool that explains something other than what it
    did.
    """

    query: q.Query
    hits: tuple[Hit, ...] = ()
    #: Every name the question used that this vault knows, sorted.
    named: tuple[str, ...] = ()

    def __iter__(self):
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def __getitem__(self, item):
        return self.hits[item]

    def __bool__(self) -> bool:
        return bool(self.hits)


def _searchable(text: str) -> str:
    """Text as the index stores it: the words of `query.words_of`, and nothing else."""
    return " ".join(q.words_of(text))


def documents_of(entries: Iterable[Entry]) -> list[Document]:
    """A vault's entries as everything downstream sees them.

    The one projection from files to what is read: the search builds its index
    from this, and the session index picks its lines from the same list. A
    second way in would be a second opinion about what is in the vault.

    Two entries that answer to one name are refused rather than returned. One of
    them could never be reached by that name, and which one that is would depend
    on the order the files came back in. The comparison is on the folded name,
    because folded is what a question is compared against: the validator's own
    duplicate rule looks at the spelling as written and would let `Foo` and
    `foo` through.

    One entry claiming a name twice is not that. It happens the moment an alias
    folds onto its own entry's name, `Beacon` next to `beacon`, which is what a
    rename that only changed a letter's case leaves behind. It costs nobody
    anything: the name still leads to exactly one entry. Counting the claims
    rather than the claimants would refuse the whole vault over it, with a
    message that says two entries and then names one.
    """
    documents = [_document(e) for e in entries]
    #: Keyed by which entry claims it, not by what that entry is called: two
    #: different files can carry the same name, and that is the clash this
    #: refuses. Position stands in for identity because a document need not
    #: have a path.
    claims: dict[str, dict[int, str]] = {}
    for position, doc in enumerate(documents):
        for name in (doc.key, *doc.aliases):
            claims.setdefault(name, {})[position] = doc.name
    clashes = sorted(
        f"{name} ({', '.join(sorted(owners.values()))})"
        for name, owners in claims.items()
        if len(owners) > 1
    )
    if clashes:
        raise MaboloError(
            "these names belong to more than one entry, so reading this vault "
            f"cannot be trusted: {'; '.join(clashes)}. Run `mabolo validate`."
        )
    return documents


def _document(entry: Entry) -> Document:
    """One entry as the index sees it."""
    values = {
        "name": entry.name,
        "title": entry.title or "",
        "aliases": " ".join(entry.mabolo.aliases),
        "description": entry.description or "",
        "body": entry.body,
    }
    fields = tuple(_searchable(values[column]) for column in COLUMNS)
    words = sorted({word for text in fields for word in text.split()})
    return Document(
        name=entry.name,
        title=entry.title or "",
        description=entry.description or "",
        area=entry.area,
        path=entry.path,
        aliases=tuple(q.fold(a) for a in entry.mabolo.aliases),
        pin=bool(entry.mabolo.pin),
        pinned_at=entry.mabolo.pin if isinstance(entry.mabolo.pin, dt.date) else None,
        at=entry.touched_at(),
        words=tuple(words),
        fields=fields,
    )


class Index:
    """A searchable view of a vault. Built from entries, never from a database file."""

    def __init__(self, documents: Iterable[Document], language: str = "en") -> None:
        # Sorted by name so that two machines with the same files build the same
        # index, down to the rowids that break a tie in the ranking.
        self.documents = tuple(sorted(documents, key=lambda d: d.name))
        self.language = language
        self.targets, self.entry_targets = self._name_targets()
        self.names = frozenset(self.targets)
        self._by_key = {doc.key: doc for doc in self.documents}
        self._db = sqlite3.connect(":memory:")
        try:
            self._db.executescript(_SCHEMA)
        except sqlite3.OperationalError as exc:
            # FTS5 is compiled into SQLite or it is not, and a Python built
            # without it fails here with three words and a traceback. The
            # session start hook swallows everything and would simply go quiet,
            # so a person would see a memory that knows nothing rather than a
            # tool that cannot search. One sentence is the difference.
            self._db.close()
            raise MaboloError(
                "this Python's SQLite was built without the FTS5 extension, so the search "
                f"index cannot be created ({exc})."
            ) from exc
        self._db.executemany(_INSERT, [(i, *doc.fields) for i, doc in enumerate(self.documents)])

    @classmethod
    def build(cls, entries: list[Entry], language: str = "en") -> Index:
        """The index for a list of entries."""
        return cls(documents_of(entries), language=language)

    def _name_targets(self) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
        """Every name a question could use, and the entries it points at.

        Two maps, because two callers mean different things by "name". A
        question may name a project, and naming a project is a statement about
        all of its entries. Someone naming an entry means that one entry, so the
        second map leaves projects out; see `resolve_entry`.
        """
        targets: dict[str, set[str]] = {}
        entries: dict[str, set[str]] = {}
        for doc in self.documents:
            for name in (doc.key, *doc.aliases):
                targets.setdefault(name, set()).add(doc.name)
                entries.setdefault(name, set()).add(doc.name)
            if doc.area.startswith(PROJECT_PREFIX):
                project = q.fold(doc.area[len(PROJECT_PREFIX) :])
                targets.setdefault(project, set()).add(doc.name)
        return (
            {name: frozenset(owners) for name, owners in targets.items()},
            {name: frozenset(owners) for name, owners in entries.items()},
        )

    def resolve(self, name: str) -> Document | None:
        """The entry a name, an alias or a one entry project points at, or None.

        This is the search's idea of a name. Whoever names an entry and means
        that entry wants `resolve_entry`.
        """
        return self._resolve_in(self.targets, name)

    def resolve_entry(self, name: str) -> Document | None:
        """The entry a name or an alias points at, and never a project.

        An eval case names the entry it expects. Letting a project name resolve
        here meant that a case naming `atlas` passed against the single entry
        that happened to live in `project/atlas`: a green case measuring
        something other than what its file says.
        """
        return self._resolve_in(self.entry_targets, name)

    def _resolve_in(self, targets: dict[str, frozenset[str]], name: str) -> Document | None:
        wanted = q.fold(name)
        found = self._by_key.get(wanted)
        if found is not None:
            return found
        owners = targets.get(wanted) or frozenset()
        if len(owners) == 1:
            return self._by_key.get(q.fold(next(iter(owners))))
        return None

    def names_in(self, parsed: q.Query) -> dict[str, frozenset[str]]:
        """The names this question uses, and the entries each one points at.

        A name is a run of words in the question, not a substring of its text.
        As a substring, the alias `password store` counted as named inside
        `keypassword storefront`.
        """
        found: dict[str, frozenset[str]] = {}
        words = list(parsed.words)
        for name, owners in self.targets.items():
            wanted = q.words_of(name)
            span = len(wanted)
            if not span:
                continue
            if any(words[i : i + span] == wanted for i in range(len(words) - span + 1)):
                found[name] = owners
        return found

    def search(self, text: str | q.Query, limit: int = DEFAULT_LIMIT) -> SearchResult:
        """The entries that answer a question, best first, or nothing at all.

        Nothing at all is a real answer here and the reason for the relevance
        floor. The MATCH expression joins its terms with OR, so almost any
        question matches almost any vault somewhere in a body. Returning that is
        worse than returning nothing when the result is injected into a prompt
        automatically: it teaches the reader that the memory is noise.
        """
        parsed = text if isinstance(text, q.Query) else q.build(text, self.language)
        named = self.names_in(parsed)
        named_documents = {name for owners in named.values() for name in owners}

        scores: dict[int, float] = {}
        match = q.to_match(parsed)
        if match:
            scores = dict(
                self._db.execute(
                    f"SELECT rowid, bm25(entries, {', '.join(str(w) for w in WEIGHTS)}) "
                    "FROM entries WHERE entries MATCH ?",
                    (match,),
                ).fetchall()
            )
        # A named entry is a candidate even when no word of the question is in
        # it, or the entry called `ci` could be named outright and still not be
        # found: its name is too short to become a stem.
        for position, doc in enumerate(self.documents):
            if doc.name in named_documents:
                scores.setdefault(position, NAMED_ONLY_SCORE)

        scored = []
        for rowid, score in scores.items():
            doc = self.documents[rowid]
            matched = tuple(s for s in parsed.stems if doc.has_prefix(s))
            mentions = tuple(sorted(n for n, owners in named.items() if doc.name in owners))
            if not self._is_relevant(matched, bool(mentions)):
                continue
            scored.append((score, doc.name, doc, matched, mentions))
        # BM25 in SQLite is negative and better the smaller it is. The name
        # breaks a tie, so the order does not depend on how SQLite happened to
        # walk the table.
        scored.sort(key=lambda row: (row[0], row[1]))
        hits = tuple(
            Hit(document=doc, rank=position, score=score, matched=matched, named=mentions)
            for position, (score, _, doc, matched, mentions) in enumerate(scored[:limit], start=1)
        )
        return SearchResult(query=parsed, hits=hits, named=tuple(sorted(named)))

    @staticmethod
    def _is_relevant(matched: tuple[str, ...], named: bool) -> bool:
        """Relevance is absolute, not relative.

        This entry was named, or two of the question's words are in it. Without
        a rule of this shape a search always returns its best of a bad lot, and
        "the best of nothing" reads exactly like an answer. The name has to name
        *this* entry: counting any name anywhere in the question let a question
        about one project return an unrelated entry that only shared the word
        "does".
        """
        return named or len(matched) >= 2

    def preview(self, hits: Iterable[Hit]) -> str:
        """The lines a preview would consist of, for measuring what it costs."""
        return "\n".join(hit.line() for hit in hits)

    def preview_cost(self, hits: Iterable[Hit]) -> int:
        return estimate_tokens(self.preview(hits))

    def close(self) -> None:
        """Let go of the database. An index is disposable, so saying so is cheap."""
        self._db.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __len__(self) -> int:
        return len(self.documents)
