"""Taste, delivered at the moment it applies.

Every other tier answers a question. This one answers no question at all: a
file is about to be read or written, and somewhere in the vault is a rule about
how that kind of file should look. Nobody asks for it, because nobody remembers
a rule they have forgotten, which is the whole reason it is written down.

So the trigger is the file, not the sentence. `mabolo.applies_to` holds the
patterns an entry is about, and a client's `PreToolUse` event carries the path
the tool is reaching for. The rules whose patterns match that path are what
this module returns.

Three things that are not obvious:

* **`instead_of` is the load bearing half.** A rule that says what to do
  competes with a habit that is already in the model's weights. A rule that
  also names the habit it replaces gives the model something to recognise, and
  gives a person something to argue with.
* **Scope decides where a rule is in force.** `scope: global` everywhere;
  `scope: project/<name>` only in that project. A rule about one product's
  wording has no business firing in another repository, and the entry says so
  itself rather than the caller guessing from where the file lives.
* **This fires far more often than anything else in the tool.** A session reads
  dozens of files. So the block is small, it is capped, it says how many rules
  it left out, and the caller is expected to remember what it has already been
  shown. Repeating the same rule on every touch of the same file is how a
  reader learns to skip the block.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Iterable

from .index import entry_line, estimate_tokens, one_line
from .schema import PROJECT_PREFIX, Entry
from .seen import key as seen_key

#: How many rules one file may raise. The same number as quiet recall, for the
#: same reason: beyond three a block stops reading as an aside.
LIMIT = 3

#: How much of one rule survives. A rule has to arrive whole enough to follow,
#: and `instead_of` rides along inside this. 240 is three lines on a phone and
#: a little over a tweet: long enough for a rule and the habit it replaces,
#: short enough that three of them do not push the person's own file out of
#: view. A rule that does not fit is left out whole rather than cut.
CHARS = 240

#: What the block calls itself. It has to be obvious that this came from the
#: vault rather than from the person, or an agent may read it as an instruction
#: the user just typed.
HEADING = "## Standing rules for this kind of file"


def _patterns_match(patterns: Iterable[str], path: str) -> str | None:
    """The first pattern this path matches, or None.

    Matched twice on purpose: against the path as given and against its last
    element. `*.css` is how a person writes "any stylesheet", and `fnmatch`
    lets `*` cross a separator, so the first form already covers a nested file.
    The second is for a pattern naming a directory's own file, such as
    `docs/*.md`, where a caller handed us an absolute path.
    """
    name = PurePosixPath(path).name
    for pattern in patterns:
        if fnmatch(path, pattern) or fnmatch(name, pattern):
            return pattern
    return None


def _in_scope(entry: Entry, project: str | None) -> bool:
    """Whether this rule is in force for the project the session is in."""
    scope = (entry.mabolo.scope or "global").strip()
    if scope == "global":
        return True
    if not scope.startswith(PROJECT_PREFIX):
        # The validator calls anything else an error. A reader that guessed
        # here would apply a rule nobody could account for.
        return False
    return project is not None and scope[len(PROJECT_PREFIX):] == project


def rules_for(
    entries: Iterable[Entry],
    path: str,
    *,
    project: str | None = None,
    seen: Iterable[str] = (),
) -> list[Entry]:
    """The design rules this path raises, most specific pattern first.

    `seen` is what the caller has already been shown, as `name@revision` keys
    from `seen.key`. The revision rides along on purpose: a rule that was
    corrected is a different rule, and a session that was shown the old one has
    not been shown the new one. The memory does not keep that itself, because
    it has no session of its own.

    Specificity is the length of the matching pattern, which is crude and
    predictable: `docs/*.md` beats `*.md`, and two patterns of the same length
    fall back to the name so that the order never depends on the order files
    came off disk.
    """
    already = set(seen)
    matched: list[tuple[int, str, Entry]] = []
    for entry in entries:
        if not entry.is_design or entry.status == "deprecated":
            continue
        if key_for(entry) in already:
            continue
        if not _in_scope(entry, project):
            continue
        pattern = _patterns_match(entry.mabolo.applies_to, path)
        if pattern is None:
            continue
        matched.append((-len(pattern), entry.path.stem if entry.path else "", entry))
    matched.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in matched]


def key_for(entry: Entry) -> str:
    """How a caller writes down that this rule was shown."""
    return seen_key(entry.path.stem if entry.path else "", entry.revision)


def line(entry: Entry) -> str:
    """One rule, as the one line a tool call can afford.

    `instead_of` is appended rather than given a line of its own: two lines per
    rule doubles the cost of the tier that fires most often, and the sentence
    is only useful next to the rule it belongs to.

    **Nothing is cut here.** A rule is executed as an instruction, and half an
    instruction reads exactly like a whole one: cutting at the limit dropped
    `instead_of` first, which is the half that carries the point, and left no
    mark that anything had gone. A rule too long for its line gets no line and
    is named instead, the same answer the core gives a rule too long for its
    seat. `fits` is what decides; this only renders.
    """
    said = entry_line(
        entry.path.stem if entry.path else "",
        entry.description or "",
        entry.title or "",
    )
    if entry.mabolo.instead_of:
        stop = "" if said.endswith((".", "!", "?")) else "."
        said += f"{stop} Instead of: {one_line(entry.mabolo.instead_of)}."
    return said


def fits(entry: Entry) -> bool:
    """Whether this rule can be said in one line at all."""
    return len(line(entry)) <= CHARS


def block(rules: list[Entry], path: str, limit: int = LIMIT) -> str:
    """The text a file opening is interrupted with, or nothing at all.

    Nothing at all is the normal answer. Most files raise no rule, and a block
    saying "no rules here" would cost every file read in the vault's life the
    tokens to say it.
    """
    if not rules:
        return ""
    sayable = [entry for entry in rules if fits(entry)]
    too_long = [entry for entry in rules if not fits(entry)]
    shown = sayable[:limit]
    if not shown and not too_long:
        return ""
    lines = [HEADING, "", f"About `{one_line(path)}`:", ""]
    lines += [line(entry) for entry in shown]
    left = len(sayable) - len(shown)
    if left:
        # Never silently. A rule that did not fit is a rule nobody is following,
        # and the count is the only way anybody finds out.
        lines.append(f"- {left} more rule(s) apply and are not shown here.")
    if too_long:
        named = ", ".join(entry.path.stem if entry.path else "" for entry in too_long)
        lines.append(
            f"- {len(too_long)} rule(s) too long for a line and left out whole: {named}. Read them."
        )
    return "\n".join(lines)


def cost(rules: list[Entry], path: str, limit: int = LIMIT) -> int:
    return estimate_tokens(block(rules, path, limit))
