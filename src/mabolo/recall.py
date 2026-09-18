"""Quiet recall: the memory speaking up on a prompt nobody addressed to it.

The session index answers "what is in this vault". This answers something
harder: the person wrote a sentence to their agent, and somewhere in the vault
is an entry that changes what the right answer is. Nobody asked for it. The
entry is offered anyway, because the whole failure this project is about is a
memory that holds the answer and stays quiet.

**Which makes silence the feature, not the fallback.** This runs on every
prompt, ahead of the prompt, and the person did not ask for it: an offer that
is merely plausible is noise, and noise on every prompt teaches the reader to
stop looking at the block entirely. So the relevance floor of the search is
what decides, three lines is the ceiling, and nothing at all is the normal
result. A block that appears on one prompt in twenty is doing its job.

**It is a pointer, like everything else at this tier.** One line per entry,
capped, with the name first: enough to recognise that something is known, and
the name to ask with. The full text is one `read` away and costs nothing until
somebody wants it.
"""

from __future__ import annotations

from typing import Iterable

from .index import Hit, estimate_tokens

#: How many entries a prompt may be interrupted with. Three is not a budget
#: decision: it is the point where a block stops reading as an aside and starts
#: reading as a second opinion nobody asked for.
LIMIT = 3

#: How much of one entry's line survives. Long enough for a description to make
#: its point, short enough that three of them do not push the person's own
#: sentence down the screen.
CHARS = 200

#: What the block calls itself. It has to be obvious that this came from the
#: memory rather than from the person, or the agent may read it as instruction.
HEADING = "## From memory, not from the prompt"

#: The mark a cut line ends with, so that a truncated description is visibly
#: truncated rather than quietly wrong.
ELLIPSIS = "…"


def line_for(hit: Hit) -> str:
    """One entry, as a prompt is allowed to see it: one line, capped, printable.

    The folding belongs to `entry_line`, which every reader of a vault shares,
    and the cap is applied to what it returns. Folding again here read like a
    second defence and was not one: it could only ever see text that was
    already flat, so no mutation of it changed any result, and the next person
    would have trusted a guard that guards nothing.
    """
    line = hit.line()
    if len(line) <= CHARS:
        return line
    return line[: CHARS - len(ELLIPSIS)].rstrip() + ELLIPSIS


def block(hits: Iterable[Hit], limit: int = LIMIT) -> str:
    """The text a prompt is interrupted with, or an empty string for silence.

    An empty string is the normal answer and the one the caller must be able to
    act on without checking anything else: no heading, no blank line, nothing
    to strip. A block that announced "nothing found" would cost every prompt in
    the vault's life a line to say that the memory had nothing to add.
    """
    lines = [line_for(hit) for hit in list(hits)[:limit]]
    if not lines:
        return ""
    return "\n".join([HEADING, *lines])


def cost(hits: Iterable[Hit], limit: int = LIMIT) -> int:
    """What that block costs, estimated. Zero when there is no block."""
    return estimate_tokens(block(hits, limit))
