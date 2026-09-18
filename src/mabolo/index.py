"""The index: BM25 over SQLite FTS5, rebuilt from the files every time.

The index is derived and disposable. It is built in memory from the entries as
they are on disk, in a fixed order, so the same commit always produces the same
index and the same ranking. That is not tidiness, it is the precondition for
`bisect-memory`: walking the vault's history is only meaningful if an old commit
can be replayed exactly.

FTS5 ships with the standard library's `sqlite3`, so this costs no dependency.
What it does cost is a second tokeniser, and two tokenisers that disagree are a
search that cannot find the word that was typed. So FTS5 never sees prose: it
sees the output of `query.tokenise`, one word per token, and its own tokeniser
has nothing left to do. Both sides of the search are then spelled the same way.

Two things are deliberately not here yet:

* **Ranking ignores `status`.** A deprecated entry is found like any other. What
  should happen to it is a question about staleness, and staleness has its own
  phase.
* **No usage counts, no recency, no learning.** Ranking is a pure function of
  the files and the query. Anything that remembers what was clicked yesterday
  cannot be replayed at an old commit.
"""

from __future__ import annotations

import math
import sqlite3
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path

from . import query as q
from .errors import MaboloError
from .schema import Entry, PROJECT_PREFIX

#: Field weights for BM25, in the column order of the table below. A name and a
#: title are what a person would type; the body is context and counts least.
WEIGHTS = (8.0, 6.0, 5.0, 3.0, 1.0)

#: How many lines a search returns unless the caller says otherwise. The number
#: is the one the eval measures against, and five lines is what fits in a
#: preview without crowding out the answer.
DEFAULT_LIMIT = 5

#: Characters per token, for the cost estimate. A rough constant on purpose:
#: the real count depends on the tokeniser of whichever model reads it, and
#: every number this produces is presented as an estimate.
CHARS_PER_TOKEN = 4

_SCHEMA = """
CREATE VIRTUAL TABLE entries USING fts5(
    name, title, aliases, description, body, tokenize = 'unicode61'
);
"""


def estimate_tokens(text: str) -> int:
    """A rough token count for a piece of text. Always called an estimate."""
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


@dataclass(frozen=True)
class Hit:
    """One entry the search returned, with where it landed and why."""

    name: str
    title: str
    description: str
    area: str
    path: Path | None
    rank: int
    score: float
    #: The query stems this entry actually contains. Empty is impossible: an
    #: entry with no matching stem cannot pass the relevance floor.
    matched: tuple[str, ...] = ()

    def line(self) -> str:
        """The one line a preview would show for this entry.

        Provisional, and only used to estimate what a preview costs. What the
        reading tier actually sends is decided where the reading tier is built.
        """
        shown = self.description or self.title or self.name
        return f"- {self.name}: {shown}"


@dataclass(frozen=True)
class Document:
    """One entry, reduced to what the search needs."""

    name: str
    title: str
    description: str
    area: str
    path: Path | None
    aliases: tuple[str, ...]
    #: Every word of the entry, folded and split the way a query is, sorted so
    #: that a prefix can be found without walking the list.
    words: tuple[str, ...] = field(default_factory=tuple)
    fields: tuple[str, ...] = field(default_factory=tuple)

    def has_prefix(self, stem: str) -> bool:
        position = bisect_left(self.words, stem)
        return position < len(self.words) and self.words[position].startswith(stem)


def _searchable(text: str) -> str:
    """Text as the index stores it: the words of `query.tokenise`, and nothing else."""
    words: list[str] = []
    for token in q.tokenise(text):
        words.extend(q.parts(token))
    return " ".join(words)


def _document(entry: Entry) -> Document:
    """One entry as the index sees it."""
    aliases = tuple(q.fold(a) for a in entry.mabolo.aliases)
    fields = (
        _searchable(entry.name),
        _searchable(entry.title or ""),
        _searchable(" ".join(entry.mabolo.aliases)),
        _searchable(entry.description or ""),
        _searchable(entry.body),
    )
    words = sorted({word for text in fields for word in text.split()})
    return Document(
        name=entry.name,
        title=entry.title or "",
        description=entry.description or "",
        area=entry.area,
        path=entry.path,
        aliases=aliases,
        words=tuple(words),
        fields=fields,
    )


class Index:
    """A searchable view of a vault. Built from entries, never from a database file."""

    def __init__(self, documents: list[Document], language: str = "en") -> None:
        # Sorted by name so that two machines with the same files build the same
        # index, down to the rowids that break a tie in the ranking.
        self.documents = sorted(documents, key=lambda d: d.name)
        self.language = language
        self.names = self._known_names()
        #: The names made of more than one word. A token can never equal one, so
        #: an alias like `password store` would otherwise be a name nobody can
        #: name, which is the opposite of what an alias is for.
        self.phrase_names = tuple(sorted(n for n in self.names if " " in n))
        self._db = sqlite3.connect(":memory:")
        self._db.executescript(_SCHEMA)
        self._db.executemany(
            "INSERT INTO entries(rowid, name, title, aliases, description, body) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(i, *doc.fields) for i, doc in enumerate(self.documents)],
        )

    @classmethod
    def build(cls, entries: list[Entry], language: str = "en") -> Index:
        """The index for a list of entries.

        Duplicate names are refused rather than ranked. Two entries with one
        name mean one of them can never be reached by name, and a search that
        silently drops an entry is the failure this whole project is about.
        """
        documents = [_document(e) for e in entries]
        seen: dict[str, int] = {}
        for doc in documents:
            seen[doc.name] = seen.get(doc.name, 0) + 1
        duplicates = sorted(name for name, count in seen.items() if count > 1)
        if duplicates:
            raise MaboloError(
                f"the vault has more than one entry called {', '.join(duplicates)}, "
                "so an index over it cannot be trusted. Run `mabolo validate`."
            )
        return cls(documents, language=language)

    def _known_names(self) -> frozenset[str]:
        """Every name a person could reasonably type and mean one entry.

        Entry names, their aliases, and the project a project area is named
        after. The relevance floor treats one of these as worth as much as a
        second matching word, because naming a thing is not a coincidence.
        """
        names: set[str] = set()
        for doc in self.documents:
            names.add(q.fold(doc.name))
            names.update(doc.aliases)
            if doc.area.startswith(PROJECT_PREFIX):
                names.add(q.fold(doc.area[len(PROJECT_PREFIX) :]))
        return frozenset(names)

    def resolve(self, name: str) -> Document | None:
        """The entry a name or an alias points at, or None."""
        wanted = q.fold(name)
        for doc in self.documents:
            if q.fold(doc.name) == wanted:
                return doc
        for doc in self.documents:
            if wanted in doc.aliases:
                return doc
        return None

    def search(self, text: str, limit: int = DEFAULT_LIMIT) -> list[Hit]:
        """The entries that answer a question, best first, or nothing at all.

        Nothing at all is a real answer here and the reason for the relevance
        floor below. The MATCH expression joins its terms with OR, so almost any
        question matches almost any vault somewhere in a body. Returning that is
        worse than returning nothing when the result is injected into a prompt
        automatically: it teaches the reader that the memory is noise.
        """
        parsed = q.build(text, self.language)
        if parsed.is_empty:
            return []
        match = q.to_match(parsed)
        rows = self._db.execute(
            f"SELECT rowid, bm25(entries, {', '.join(str(w) for w in WEIGHTS)}) "
            "FROM entries WHERE entries MATCH ?",
            (match,),
        ).fetchall()
        named = self.names_something(parsed)
        scored: list[tuple[float, str, Document, tuple[str, ...]]] = []
        for rowid, score in rows:
            doc = self.documents[rowid]
            matched = tuple(s for s in parsed.stems if doc.has_prefix(s))
            if not self._is_relevant(matched, named):
                continue
            scored.append((score, doc.name, doc, matched))
        # BM25 in SQLite is negative and better the smaller it is. The name
        # breaks a tie, so the order does not depend on how SQLite happened to
        # walk the table.
        scored.sort(key=lambda row: (row[0], row[1]))
        return [
            Hit(
                name=doc.name,
                title=doc.title,
                description=doc.description,
                area=doc.area,
                path=doc.path,
                rank=position,
                score=score,
                matched=matched,
            )
            for position, (score, _, doc, matched) in enumerate(scored[:limit], start=1)
        ]

    def names_something(self, parsed: q.Query) -> bool:
        """True when the question names an entry, an alias or a project."""
        if any(token in self.names for token in parsed.tokens):
            return True
        folded = q.fold(parsed.text)
        return any(name in folded for name in self.phrase_names)

    @staticmethod
    def _is_relevant(matched: tuple[str, ...], named: bool) -> bool:
        """Relevance is absolute, not relative.

        Two matching words, or one matching word in a question that named
        something this vault knows. Without this rule a search always returns
        its best of a bad lot, and "the best of nothing" reads exactly like an
        answer.
        """
        if named and len(matched) >= 1:
            return True
        return len(matched) >= 2

    def preview(self, hits: list[Hit]) -> str:
        """The lines a preview would consist of, for measuring what it costs."""
        return "\n".join(hit.line() for hit in hits)

    def preview_cost(self, hits: list[Hit]) -> int:
        return estimate_tokens(self.preview(hits))

    def __len__(self) -> int:
        return len(self.documents)
