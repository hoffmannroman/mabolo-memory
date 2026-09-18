"""The query pipeline: a question in, search terms out, and nothing else.

This is a pure function of its input, and it has to stay one. `bisect-memory`
walks the vault's history, rebuilds the index at each commit and replays a case;
a pipeline that looked at usage counts, at the clock or at anything else outside
its arguments would answer differently on the second run and make the whole
exercise meaningless.

The steps are the ones that were measured in the predecessor and carried over:
fold case, drop stop words in the vault language and in English, drop anything
shorter than three characters, cut a suffix while at least four characters
remain, and search each stem as a prefix. A hyphenated name is additionally
searched as a phrase, because `deploy-from-main` is one thing a person typed and
not three words that happen to be adjacent.

The stemmer is deliberately crude. It cuts a suffix off the end and never
consults a dictionary, so `working` and `works` meet at `work` while `went` and
`go` never meet at all. A real stemmer is a dependency and a language decision;
this one is twenty lines and its failures are the obvious kind.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Shorter than this, a word is not searched at all: `is`, `an`, `-j4`.
MIN_TOKEN = 3
#: A suffix is only cut while at least this much word remains.
MIN_STEM = 4

#: Stop words, per language. Short lists on purpose: every word in here is a
#: word the search can no longer find, so the bar is "carries no meaning
#: anywhere", not "is common".
STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        """
        the and but for not are was were with this that from they them then than
        have has had you your yours our ours his her hers its their theirs
        what when where which who whom why how all any both each few more most
        other some such only own same too very can will just should now
        about above after again against because been before being below between
        down during into once out over under until while
        """.split()
    ),
    "de": frozenset(
        """
        der die das den dem des ein eine einer eines einem einen und oder aber
        nicht sind war waren mit von aus auf bei nach vor über unter durch für
        ist sein seine ihr ihre wir uns ihr euch sie man
        was wann wobei welche welcher welches wer wem wen warum wie alle
        auch noch nur schon sehr dann denn damit dass dies diese dieser
        """.split()
    ),
}

#: Suffixes, per language, tried longest first and only ever once per word.
#: Cutting repeatedly turns `addresses` into `addr` and then matches half the
#: vault, which is worse than missing a plural.
SUFFIXES: dict[str, tuple[str, ...]] = {
    "en": ("ingly", "ings", "edly", "ing", "ies", "ers", "est", "ed", "es", "ly", "er", "s"),
    "de": ("ungen", "ung", "heit", "keit", "lich", "isch", "ern", "end", "en", "er", "es", "em", "e", "n"),
}

#: A token keeps the three characters that appear inside names, because
#: `deploy-from-main`, `example.dev` and `build_server` are each one name a
#: person would type, and splitting them here loses the phrase.
_SPLIT = re.compile(r"[^\w.\-]+", re.UNICODE)
_INNER = re.compile(r"[.\-_]+")
_EDGES = ".-_"


@dataclass(frozen=True)
class Query:
    """What a search is run with, after the pipeline and before SQLite.

    Every field is kept rather than folded into one string, because the parts
    answer different questions later: `stems` drive the ranking, `tokens` decide
    whether a known name was named, and `phrases` keep hyphenated names whole.
    """

    text: str
    #: The stems, deduplicated, in the order they appeared.
    stems: tuple[str, ...] = ()
    #: The words as typed, folded but neither shortened nor dropped. A name is
    #: matched against these: `deploy-from-main` never survives stemming as one
    #: piece, and the relevance rule needs to see that a name was named.
    tokens: tuple[str, ...] = ()
    #: Hyphenated or dotted names, as the phrase their parts form.
    phrases: tuple[str, ...] = ()
    #: Every word of the question in order, with names broken into their parts.
    #: A name is recognised as a run of words in here rather than as a substring
    #: of the text, so `password store` is named in "the password store" and not
    #: in "keypassword storefront".
    words: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when there is nothing worth searching for.

        A search with nothing to search for returns nothing. Falling back to
        "return the newest few" would make every negative case pass by accident.
        """
        return not self.stems and not self.phrases


def fold(text: str) -> str:
    """One spelling for text that means the same. NFKC, then case folded."""
    return unicodedata.normalize("NFKC", str(text)).casefold()


def tokenise(text: str) -> list[str]:
    """The words of a text, folded, with the characters names are made of kept."""
    out = []
    for piece in _SPLIT.split(fold(text)):
        piece = piece.strip(_EDGES)
        if piece:
            out.append(piece)
    return out


def parts(token: str) -> list[str]:
    """The pieces of a name: `deploy-from-main` is also `deploy`, `from`, `main`."""
    return [part for part in _INNER.split(token) if part]


def stem(word: str, language: str = "en") -> str:
    """The word with at most one suffix cut off, and never below `MIN_STEM`."""
    for suffix in sorted(SUFFIXES.get(language, ()), key=len, reverse=True):
        if word.endswith(suffix) and len(word) - len(suffix) >= MIN_STEM:
            return word[: -len(suffix)]
    return word


def _languages(language: str) -> tuple[str, ...]:
    """The vault language and English, which is what the code around it is in."""
    return (language, "en") if language != "en" else ("en",)


def is_stopword(word: str, language: str = "en") -> bool:
    return any(word in STOPWORDS.get(code, frozenset()) for code in _languages(language))


def words_of(text: str) -> list[str]:
    """Every word of a text in order, with names broken into their parts.

    One definition of "the words of this text", used for the query and for a
    name alike. Comparing a name against the text itself instead would make
    `password store` a name that was mentioned in `keypassword storefront`.
    """
    return [part for token in tokenise(text) for part in parts(token)]


def stems_of(text: str, language: str = "en") -> list[str]:
    """Every stem of a text, deduplicated, in order.

    Used for the query and for the entries alike, so that a word is spelled the
    same on both sides. Two spellings is how a search stops finding the entry
    that contains the very word that was typed.
    """
    return _stems(tokenise(text), language)


def _stems(tokens: list[str], language: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for part in parts(token):
            if len(part) < MIN_TOKEN or is_stopword(part, language):
                continue
            key = stem(part, language)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def build(text: str, language: str = "en") -> Query:
    """The query pipeline, end to end.

    The text is tokenised once and everything else is derived from that one
    list. Tokenising twice is how `tokens` and `stems` come to disagree about
    what a word is after somebody changes the tokeniser.
    """
    tokens = tokenise(text)
    phrases: list[str] = []
    words: list[str] = []
    for token in tokens:
        pieces = parts(token)
        words.extend(pieces)
        if len(pieces) > 1:
            phrase = " ".join(pieces)
            if phrase not in phrases:
                phrases.append(phrase)
    return Query(
        text=str(text),
        stems=tuple(_stems(tokens, language)),
        tokens=tuple(dict.fromkeys(tokens)),
        phrases=tuple(phrases),
        words=tuple(words),
    )


def _quote(term: str) -> str:
    """One FTS5 string literal. Everything goes through here.

    FTS5 reads its argument as an expression, so a query containing `OR`, `*`,
    a colon or a bracket would otherwise be syntax rather than words, and a
    person's question is words.
    """
    return '"' + term.replace('"', '""') + '"'


def to_match(query: Query) -> str:
    """The FTS5 MATCH expression for a query, or an empty string for nothing.

    Every stem is a prefix and the terms are joined with OR: a question uses
    words the entry does not, and requiring all of them finds nothing. What
    keeps OR from matching everything is not the expression but the relevance
    floor in `index`, which is applied to the results.
    """
    terms = [f"{_quote(s)}*" for s in query.stems]
    terms += [_quote(p) for p in query.phrases]
    return " OR ".join(terms)
