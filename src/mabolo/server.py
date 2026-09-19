"""The MCP server: the memory as tools a model can call.

Three things happen here and nowhere else, and everything else is delegated.

**The read side is the tier the session index cannot pay for.** `mabolo_search`
returns the lines, `mabolo_read` returns the entries that were picked. Two
calls, not one, because a single call cannot announce the price of its own
payload: by the time the answer arrives the payload is already in the context.

**The write side is the gate.** Every write tool takes a `quote`, the sentence
that authorised it, and the sentence is checked against what the prompt hook
actually saw a person type. A server started in read mode does not register the
write tools at all, which is what "an agent acting on its own never writes"
means in code rather than in a docstring.

**What the gate holds against, exactly.** A model that has only the tool call:
a quote has to match a note the hook wrote. Anything that can write into the
state directory, which includes an agent with a shell, can write a note and
then quote it. What remains against that is not prevention but detection:
every write is one commit carrying the sentence in its message and in a
footnote, so a forged sentence is a sentence the person can read and did not
say, and `git revert` is the way back. A barrier this could not keep would be
worse than none.

**Nothing here talks to Git.** A tool builds the bytes of a file and hands them
to `write.apply`, which owns the lock, the revision check, the commit and the
push. The tools are the vocabulary; the transaction is the guarantee.

The answers are sentences rather than JSON. Everything a tool returns is read
by a model, a sentence is cheaper than an object, and the one value a caller
has to feed back into the next call, the revision, is easier to see in a line
than in a nested field.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from mcp.server import MCPServer

from . import (
    PRODUCER,
    __version__,
    consent,
    decide,
    drift,
    frontmatter,
    inbox,
    journal,
    lint,
    proposal,
    session,
    validate,
    write,
)
from .errors import MaboloError
from .index import Index, estimate_tokens, one_line
from .schema import (
    QUOTE_ID,
    DEFAULT_STATUS,
    KNOWN_TYPES,
    Entry,
    Generated,
    MaboloBlock,
    Source,
    Verification,
    iso,
    now,
    quoted_body,
)
from .vault import PassageNotUnique, Vault

#: What the client is told this server is for. Short on purpose: it is injected
#: into a context that the whole project exists to keep small.
INSTRUCTIONS = (
    "A memory made of Markdown and Git. Search for what is known, read what "
    "looks relevant, and write only what the person just said, quoting them."
)

# The id a quote gets lives in `schema`, with the function that writes it.

#: How many lines a search offers before it starts costing more than it saves.
DEFAULT_LIMIT = 5
MAX_LIMIT = 25

#: How many entries one read may fetch. A read is the expensive tier, and a
#: caller asking for twenty at once has stopped choosing.
MAX_READ = 8


@dataclass(frozen=True)
class Settings:
    """Everything the tools need that is not the vault itself."""

    actor: str
    remote: str | None = None
    branch: str | None = None
    read_only: bool = False
    #: Where the client started this server, which is how a quote is tied to
    #: the project it was typed in.
    cwd: Path | None = None
    #: The session the client named, when it names one at all.
    session: str | None = None
    #: Where the prompt notes live. Only a test points this somewhere else.
    prompts: Path | None = None


def _sentence(function: Callable[..., str]) -> Callable[..., str]:
    """Turn a refusal into a sentence the model can act on.

    An exception becomes a protocol level error, and a model that gets one
    tends to try the same call again. A sentence saying what was wrong is the
    difference between a retry loop and a corrected second attempt.
    """

    @wraps(function)
    def guarded(*args: Any, **kwargs: Any) -> str:
        try:
            return function(*args, **kwargs)
        except MaboloError as exc:
            return f"refused: {exc}"
        except (OSError, ValueError) as exc:
            return f"refused: {exc}"

    return guarded


def build(vault: Vault, settings: Settings) -> MCPServer:
    """The server, with the tools this mode is allowed to offer."""
    server = MCPServer(name="mabolo", version=__version__, instructions=INSTRUCTIONS)

    def index() -> Index:
        return Index.build(vault.entries(), language=vault.declared_language())

    def entry_path(name: str) -> Path:
        with index() as built:
            found = built.resolve_entry(name)
        if found is None or found.path is None:
            raise MaboloError(f"there is no entry called {name!r}. Search for it first.")
        return found.path

    def relative(path: Path) -> str:
        return path.relative_to(vault.root).as_posix()

    def consent_for(quote: str) -> consent.Consent:
        return consent.check(
            quote, cwd=settings.cwd, session=settings.session, directory=settings.prompts
        )

    def commit(changes: list[write.Change], message: str, kind: str = "write") -> write.Result:
        # The area index is derived from the entries, so it belongs to the same
        # commit as the entry that changed it. See `Vault.index_changes`.
        return write.apply(
            vault.root,
            changes + vault.index_changes(changes),
            message,
            actor=settings.actor,
            kind=kind,
            remote=settings.remote,
            branch=settings.branch,
        )

    @server.tool()
    @_sentence
    def mabolo_search(query: str, limit: int = DEFAULT_LIMIT) -> str:
        """Ask the memory what it knows about something.

        Returns one line per entry: its name and its description, with what
        reading them all would cost. Nothing is read yet. Pick the ones that
        matter and pass their names to mabolo_read.

        An empty answer is a real answer. This memory would rather say nothing
        than offer something that merely looks related.
        """
        wanted = max(1, min(int(limit), MAX_LIMIT))
        with index() as built:
            found = built.search(query, limit=wanted)
            if not found.hits:
                return "nothing in the memory matches that."
            lines = built.preview(found.hits)
            cost = sum(estimate_tokens(_text_of(hit.path)) for hit in found.hits if hit.path)
        return f"{lines}\n\nreading all of them costs about {cost} tokens."

    @server.tool()
    @_sentence
    def mabolo_read(names: list[str]) -> str:
        """Read entries in full, by the names a search returned.

        Each one arrives with its revision. Keep the revision: it is what
        mabolo_edit and mabolo_forget need in order to refuse a change that
        would overwrite somebody else's, and it changes whenever the entry
        does.
        """
        if not names:
            return "name at least one entry."
        if len(names) > MAX_READ:
            return f"that is {len(names)} entries. Read at most {MAX_READ} at a time, and choose."
        out: list[str] = []
        for name in names:
            path = entry_path(str(name))
            document = frontmatter.read(path)
            out.append(
                f"## {path.stem} ({relative(path)}, revision {document.revision})\n\n"
                f"{path.read_text(encoding='utf-8').strip()}{_moved(vault, path, settings)}"
            )
        return "\n\n".join(out)

    @server.tool()
    @_sentence
    def mabolo_propose(action: str, target: str, quote: str, entry: str = "", old: str = "",
                       new: str = "", note: str = "") -> str:
        """File a suggestion for the person to answer later, changing nothing.

        This is what an agent may do on its own. It writes nothing into the
        vault: the proposal waits on a branch that is never merged, and it
        becomes an entry only when the person says yes.

        `quote` is still the person's own sentence, and it is not checked here,
        because a proposal is not a claim about them yet. It is checked when
        they answer.
        """
        if action not in proposal.ACTIONS:
            return f"refused: a proposal asks for one of {', '.join(proposal.ACTIONS)}."
        made = proposal.Proposal(
            action=action,
            target=target.strip(),
            quote=quote,
            entry=entry,
            old=old,
            new=new,
            note=note,
            source=PRODUCER,
            filed_at=iso(now()) or "",
        )
        result = inbox.file(vault.root, made, remote=settings.remote)
        if result.outcome == inbox.ALREADY:
            return f"{made.id} was already waiting, so nothing was added."
        if not result.ok:
            return f"{result.outcome}: {result.message}"
        return f"{made.id} is waiting for an answer: {result.message}."

    if not settings.read_only:
        _register_writing(server, vault, settings, entry_path, relative, consent_for, commit, index)
    return server


def _moved(vault: Vault, path: Path, settings: Settings) -> str:
    """One line under an entry when the file it watches has moved.

    Here and not in the session index. The map is paid for at every session
    start, and a mark beside a one line description is a verdict a reader
    cannot act on there. At read time they are about to rely on the text, which
    is the moment the question matters. Silence means the anchor has not moved;
    an anchor that could not be checked says so, because "not checked" and "has
    not moved" are different answers.
    """
    entry = next((e for e in vault.entries() if e.path == path), None)
    if entry is None or not entry.mabolo.anchor:
        return ""
    where, project = session.standing(vault, settings.cwd)
    judged = drift.judge(entry, project=project, repository=where, moment=now())
    if judged.state == drift.FRESH:
        return ""
    return f"\n\n> {judged.reason} ({one_line(entry.mabolo.anchor)})"


def _text_of(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _register_writing(
    server: MCPServer,
    vault: Vault,
    settings: Settings,
    entry_path: Callable[[str], Path],
    relative: Callable[[Path], str],
    consent_for: Callable[[str], consent.Consent],
    commit: Callable[..., write.Result],
    index: Callable[[], Index],
) -> None:
    """The tools that change the vault, and the gate in front of most of them.

    Four of them make a claim about the person and pass through `consent`.
    Two do not: `journal_add` records what a session did, and `mabolo_reindex`
    rebuilds a file that is derived from entries the person already approved.
    Both still write, so neither is registered in read mode.
    """

    def refuse(given: consent.Consent) -> str:
        return (
            f"nothing was written: {given.reason}. "
            "Only a sentence the person typed themselves can authorise a change. "
            "If they have not said it yet, ask."
        )

    def verification(given: consent.Consent) -> Verification:
        return Verification(by=settings.actor, at=iso(given.at or now()) or "")

    def source_for(given: consent.Consent) -> Source:
        day = (given.at or now()).date().isoformat()
        return Source(id=QUOTE_ID, resource=f"session://{day}")

    def message(said: str, given: consent.Consent) -> str:
        return f"{said}\n\n{given.source_note()}, approved by {settings.actor}"

    def answer(result: write.Result, where: str) -> str:
        """What a write tool says, in one shape for all four of them."""
        if not result.ok:
            return f"{result.outcome}: {result.message}"
        revision = result.revisions.get(where)
        at = f", revision {revision}" if revision else ""
        return f"{where}{at}: {result.message}."

    @server.tool()
    @_sentence
    def mabolo_write(
        area: str,
        name: str,
        description: str,
        body: str,
        quote: str,
        type: str = "reference",
        title: str = "",
    ) -> str:
        """Write a new entry, because the person just said something worth keeping.

        `quote` is the sentence they typed, word for word. It is checked
        against what they actually wrote, it is stored with the entry as its
        source, and without a match nothing is written at all. Do not
        paraphrase it and do not write one yourself.

        `area` is one of the vault's areas, or project/<name>. `description` is
        one line and is what every future session sees first, so it carries the
        point rather than the topic.
        """
        given = consent_for(quote)
        if not given.verified:
            return refuse(given)
        if type not in KNOWN_TYPES:
            return f"refused: type has to be one of {', '.join(KNOWN_TYPES)}."
        entry = Entry(
            type=type,
            title=title.strip() or None,
            description=one_line(description),
            sources=[source_for(given)],
            generated=Generated(by=PRODUCER, at=iso(now()) or ""),
            verified=[verification(given)],
            status=DEFAULT_STATUS,
            mabolo=MaboloBlock(area=area.strip()),
            body=quoted_body(body, given.quote),
        )
        entry.path = vault.path_for(entry.mabolo.area, name.strip())
        target, data = vault.render_entry(entry)
        path = relative(target)
        result = commit(
            [write.Change(path=path, data=data, expect=None)], message(f"write {path}", given)
        )
        return answer(result, path)

    @server.tool()
    @_sentence
    def mabolo_edit(name: str, old: str, new: str, quote: str, revision: str) -> str:
        """Replace one passage of an entry, leaving the rest of the prose alone.

        `old` has to appear exactly once in the entry, and `revision` has to be
        the one the last read returned. Both refusals mean the same thing: read
        the entry again, because what is there is not what you think.

        Use this rather than rewriting an entry. A rewrite changes sentences
        nobody asked about, and those sentences were somebody's decision.

        An edit that only adds or removes a link does not record a
        verification. A verification says the person stands behind what the
        entry claims, the session index reads the newest one as "this moved
        recently", and a tidying pass over the wiring would otherwise push the
        standing rules off the front page of every session for a week. Whether
        it is one is read from the change itself, never from the call: the
        words either differ or they do not.
        """
        given = consent_for(quote)
        if not given.verified:
            return refuse(given)
        path = entry_path(name)
        document = frontmatter.read(path)
        if document.revision != revision:
            return (
                f"refused: {path.stem} is at revision {document.revision}, not {revision}. "
                "Read it again and decide."
            )
        try:
            entry = vault.replace_once(path, old, new)
        except PassageNotUnique as ambiguous:
            more = " Quote more of it." if ambiguous.seen > 1 else ""
            return f"refused: {ambiguous}.{more}"
        wording = validate.without_links(old) != validate.without_links(new)
        if wording:
            entry.approve(settings.actor, iso(given.at or now()) or "")
        target, data = vault.render_entry(entry)
        where = relative(target)
        result = commit(
            [write.Change(path=where, data=data, expect=document.revision)],
            message(f"edit {where}" if wording else f"link {where}", given),
            "edit",
        )
        said = answer(result, where)
        if result.ok and not wording:
            said += " Only the link changed, so nothing was verified."
        return said

    @server.tool()
    @_sentence
    def mabolo_forget(name: str, quote: str, revision: str, reason: str = "") -> str:
        """Remove an entry, because the person said it is no longer true.

        The file goes; the history keeps it, and one `git revert` brings it
        back. Entries that link to it are named in the answer, because a
        removal leaves their links pointing at nothing.
        """
        given = consent_for(quote)
        if not given.verified:
            return refuse(given)
        path = entry_path(name)
        document = frontmatter.read(path)
        if document.revision != revision:
            return (
                f"refused: {path.stem} is at revision {document.revision}, not {revision}. "
                "Read it again and decide."
            )
        where = relative(path)
        said = f"forget {where}"
        if reason.strip():
            said += f" ({one_line(reason)})"
        linking = _linking_to(vault, path.stem)
        result = commit(
            [write.Change(path=where, data=None, expect=document.revision)],
            message(said, given),
            "forget",
        )
        if not result.ok:
            return f"{result.outcome}: {result.message}"
        note = f" Still linked to from: {', '.join(linking)}." if linking else ""
        return f"{where} is gone: {result.message}.{note}"

    @server.tool()
    @_sentence
    def mabolo_decide(id: str, verdict: str, quote: str) -> str:
        """Answer a proposal the person has just decided about, by its id.

        Only for a sentence in which the person names the proposal themselves,
        for example "a3f2 yes" or "reject 7c01". The id is the evidence: a hex
        string a machine generated is a signature of having looked at the
        inbox, where a bare "yes" is a signature of nothing. Do not shorten,
        expand or tidy what they wrote, and do not answer a proposal they did
        not name.

        A yes writes the entry and the answer in one commit. A no writes only
        the answer, and the sentence the proposal quoted is never recorded.
        """
        wanted = verdict.strip().lower()
        if wanted not in consent.VERDICTS:
            return f"refused: a verdict is {' or '.join(consent.VERDICTS)}."
        waiting = inbox.read(vault.root).proposals
        one = proposal.resolve(id, waiting)
        given = consent.check(
            quote,
            cwd=settings.cwd,
            session=settings.session,
            directory=settings.prompts,
            minimum=proposal.MIN_PREFIX,
        )
        if not given.verified:
            return refuse(given)
        # The answer is read out of what the person typed, never taken from
        # this call. A check that the id occurs and the word occurs let one
        # sentence answering two proposals approve the one that was refused,
        # and let "a3f2 yesterday we reviewed it" pass for a yes.
        said = consent.verdict_for(
            one.id,
            cwd=settings.cwd,
            session=settings.session,
            directory=settings.prompts,
        )
        if not said.given:
            return f"nothing was written: {said.reason}"
        if said.answer != wanted:
            return (
                f"nothing was written: the person said {said.answer!r} about {one.id} "
                f"and this call says {wanted!r}. Pass on what they said."
            )
        answered = decide.answer(
            vault,
            one,
            approved=said.answer == "yes",
            by=settings.actor,
            remote=settings.remote,
            branch=settings.branch,
        )
        return answered.line()

    @server.tool()
    @_sentence
    def journal_add(line: str, project: str = "") -> str:
        """Note in one line what this session did, under today's date.

        The journal is the one file written in the order things happened, and a
        session that does not write to it leaves the next one guessing what was
        decided. A line names its project, or it belongs to no project and no
        session will be shown it.

        This needs no quote: it records what happened, it makes no claim about
        the person, and it is never read as a rule.
        """
        bullet = journal.line_for(line, project.strip() or None)
        target = vault.log_file
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        revision = frontmatter.revision(text.encode("utf-8")) if text else None
        today = now().date()
        data = journal.add_line(text, today, bullet).encode("utf-8")
        where = relative(target)
        result = commit(
            [write.Change(path=where, data=data, expect=revision)],
            f"journal {today.isoformat()}",
            "journal",
        )
        if not result.ok:
            return f"{result.outcome}: {result.message}"
        return f"noted under {today.isoformat()}: {result.message}."

    @server.tool()
    @_sentence
    def mabolo_reindex() -> str:
        """Rebuild the area indexes that no longer match the entries beside them.

        The repair for the one way an index goes stale that no tool sees. Every
        write derives the indexes it made wrong and commits them with the entry,
        so the tools keep themselves straight. A change that did not come
        through them -- a file written in an editor, a merge, a commit made by
        hand -- derives nothing, and the folder's index goes on describing a
        vault that no longer exists. `mabolo doctor` is what reports that.

        Call it when something reports a stale index, not on a hunch: it says
        so and changes nothing when every index already matches.

        This needs no quote. An index holds nothing but what the entries beside
        it already say, so a rebuild makes no claim about the person that they
        have not approved once already. It is still a write, so it is not
        offered in read mode, and the commit carries a trailer of its own kind.
        """
        changes = vault.stale_indexes()
        if not changes:
            return "every index matches the entries beside it. Nothing was written."
        where = ", ".join(change.path for change in changes)
        result = commit(changes, f"rebuild {len(changes)} index file(s)", "reindex")
        if not result.ok:
            return f"{result.outcome}: {result.message}"
        return f"rebuilt {where}: {result.message}."


def _linking_to(vault: Vault, name: str) -> list[str]:
    """The entries whose text points at this one.

    Through the same reader `lint` uses, and not a substring test of its own.
    Two readers of what a link is means `forget` and `lint` can disagree about
    who points at a removed entry, and then one report contradicts the other
    about the same vault.
    """
    out: list[str] = []
    for entry in vault.entries():
        if entry.path is None or entry.path.stem == name:
            continue
        targets = {Path(target).name for _, target in lint.links_of(entry.body)}
        if f"{name}.md" in targets or f"[[{name}]]" in entry.body:
            out.append(entry.path.stem)
    return sorted(out)
