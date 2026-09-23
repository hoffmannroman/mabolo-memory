# Writing: the gate, the transaction, and what a tool may claim

Reading a memory is a convenience. Writing to one is a promise, and this is the
page that says exactly how far the promise reaches.

One rule, written as a state machine rather than as three slogans that
contradict each other:

> Automation may produce disposable proposals. Only human consent commits
> anything to the vault. The execution of that commit may be automatic, but it
> stays atomic and revertible.

## The gate

Every write tool takes a `quote`: the sentence that authorised the change, word
for word. The model supplies it, so on its own it proves nothing. A model can
write a sentence you never said.

So the server checks it. The `UserPromptSubmit` hook writes down every prompt it
is handed, and a quote is only accepted when it appears in one of those notes.
A match produces `verified.by: human:<id>` on the entry. No match, no write, and
the tool says why.

**What the notes are.** One JSON line per prompt, under
`$XDG_STATE_HOME/mabolo/prompts/<session>.jsonl`, outside the vault because
they are disposable and nobody asked to keep them. Obvious secrets are redacted
before the line is written, a session file is capped at 500 lines, and a file
nothing can be verified against any more is deleted after seven days.
`mabolo hook prompt --no-record` turns the whole thing off, at the price of
every write tool refusing afterwards.

**What the match is.** Whitespace is collapsed and capitalisation is ignored:
a model relaying a sentence may start it with a capital where you did not, and
refusing over the shift key teaches callers to pad the quote until something
matches. The words have to be yours. A quote shorter than twelve characters is
refused outright, because "yes" appears in half of all prompts and there is
nothing there to recognise.

**What the gate holds against, exactly.** A model that has only the tool call:
a quote has to match a note the hook wrote. Anything that can write into the
state directory, which includes an agent with a shell, can write a note and
then quote it. There is no lock here that would hold against that: a key in a
file the same user can read is a lock with the key taped to it. What remains is
detection rather than prevention, and it is the same thing that protects you
from everything else here: every write is one commit, carrying the sentence in
its message and in a footnote, so a forged sentence is one you can read and did
not say, and `git revert` is the way back.

**A message from another agent is not written down.** One agent can hand a
message to another, and it arrives at the prompt hook by the same road a typed
sentence does. Recorded, it became quotable, and the commit would have said
that you approved a sentence no human wrote; this was measured in
a real session where four of ten notes were another agent's. The client wraps
such a message before the hook sees it, so where it came from is evidence
rather than something its sender chose to include, and a prompt carrying that
wrapper is not recorded at all. A quote from one then fails to verify, the
write is refused, and the model has to go and ask. **No session can manufacture
consent for another one**, however plainly it reports what you told it.

**What it can claim, and what it falls back to.** The strongest claim is "this
sentence, in this conversation": that session's notes are the evidence, and the
window does not apply. The server gets the name from `MABOLO_SESSION_ID` if the
client sets it, and otherwise from the host — Claude Code puts its own session
id into the environment of every MCP server it starts
(`ENV_SESSION_FALLBACKS`). A name the caller gives is a claim and is honoured
even when it finds nothing; a name merely read off the host is a guess, so if
that session has no notes yet the server falls back rather than refusing
everything.

The fallback is the older, weaker claim: "this sentence, typed in this
directory, within the last twelve hours". Notes from another directory do not
count, and notes older than the window do not either.

Until 2026-09-22 this document said a client starts the server without telling
it which session it belongs to. That was true of the protocol and false of the
client in front of us, and the cost was real: an agent that `cd`s into a
project — which is most of an afternoon's work — left the directory the
sentence had been typed in, and writes were refused for sentences the person
really had typed. Reading the host's session id makes the gate **narrower**,
not kinder: once a session has notes, no other session's sentence counts for
it, which the directory rule alone never guaranteed.

This is written down rather than glossed over, because the difference matters:
the gate proves a person typed the sentence, not that they typed it at the
moment the tool ran.

**The permission dialog is the second barrier, not the evidence.** Your client
asks before a tool runs. That is worth having and it is not proof of anything:
the question it asks is about the tool, not about the sentence.

## The transaction

Several sessions on several machines write into the same vault, and Git is the
database. Checking the file locally is not enough: two machines can read the
same state, both decide they are up to date, and both commit. The first push
wins and the second is left with a diverged branch. So the comparison happens on
the remote, where Git has the primitive for it.

```
  take the lock              one mutation at a time on this machine
        |
  Git already busy? ---yes--> blocked, nothing is touched
        |
  fetch, remember the remote commit           this is the lease
        |
  commit hand made changes   "edit outside mabolo by human:<id>"
        |
  line up with the remote    fast forward, or report a divergence
        |
  revision still yours? --no--> stale, read it again and decide
        |
  build the commit           straight into the object store
        |
  push with the lease -----refused--> conflict, nothing local moved
        |
  move the branch, write the files, line the index up
```

**Nothing is written where you can see it until the commit is safe.** The bytes
go into Git's object store, the tree is assembled in an index of its own, and
the working tree is only touched after the push succeeded. A refused push costs
nothing: the commit object is unreferenced and Git collects it.

**Your working tree is never reset and never cleaned.** The only files touched
are the ones the change names. Whatever you changed by hand, staged or not, is
committed first in a commit of its own, so the diff of a write holds only what
the write did. Staging is not a hiding place: a file Mabolo looked away from
would never reach your other machines, and the next write of that path would
overwrite it.

**Three things stop a mutation outright**, and each is a state where somebody
is in the middle of something a memory tool should not decide for them: a
merge, rebase, cherry-pick, revert or bisect already running; a detached HEAD,
or a branch other than the one that syncs; and a file you have staged and then
edited again, where Git holds two versions and committing the one on disk would
throw away the one you put there on purpose.

### What a tool can answer

| Outcome | What happened |
|---|---|
| `written` | committed and on the remote |
| `local` | committed, and this vault has no remote |
| `offline` | committed, and the remote could not be reached, so it is only here |
| `conflict` | the remote moved, nothing was written, read it again |
| `stale` | the entry changed since you read it, nothing was written |
| `blocked` | somebody is mid change in the vault, nothing was touched |
| `nothing` | the vault already says exactly that |

`offline` is a deliberate answer rather than a failure. Refusing to remember
something because a laptop is on a train would be the worse bargain, and a
message that said "written" would be a guess. The next mutation fetches, fast
forwards and carries it along.

The way back from any change is `git revert` of a single commit.

## The tools

A client that speaks MCP gets these. `mabolo serve --read-only` registers only
the three marked always, which is what an unattended agent, a lint pass or a
subagent is started with: the write tools are not refused there, they do not
exist.

| Tool | Mode | What it does |
|---|---|---|
| `mabolo_search` | always | one line per entry, with what reading them all would cost |
| `mabolo_read` | always | entries in full, each with its revision |
| `mabolo_propose` | always | files a suggestion for you to answer later |
| `mabolo_write` | full | a new entry, from a sentence you typed |
| `mabolo_edit` | full | replaces one passage, leaving the rest of the prose alone |
| `mabolo_describe` | full | replaces the description, the line a session is shown |
| `mabolo_status` | full | sets whether an entry is current: draft, stable or deprecated |
| `mabolo_forget` | full | removes an entry; the history keeps it |
| `mabolo_decide` | full | answers a proposal you named yourself |
| `journal_add` | full | one line under today's date in `log.md` |
| `mabolo_reindex` | full | rebuilds an index that no longer matches its entries |

**A search says when an entry is no longer current.** A description describes
what a thing *is*, so "self-hosted messenger, Python and WebSocket" stays true
of a project abandoned in September, and the field that knows it was abandoned
is `status`. A preview is the one place a reader meets a description with none
of the frontmatter around it, so the line carries `[deprecated]` there. Ranking
is untouched: a deprecated entry is found like any other, because what it
records still happened. The mark is a caveat on the answer, not a thumb on the
scale.

`mabolo_search` and `mabolo_read` are two calls on purpose. A single call cannot
announce the price of its own payload: by the time the answer arrives, the
payload is already in the context.

**An edit is targeted, never a rewrite.** The passage has to occur exactly once,
and an entry whose passage is ambiguous is refused rather than guessed at. A
rewritten entry produced by a model silently changes sentences that were
somebody's decision. The prose outside the passage is left byte for byte; the
frontmatter is re-rendered, because that part is structured data the tool owns.

**An edit that only adds or removes a link records no verification.** A
verification says you stand behind what the entry claims, and the session index
reads the newest one as "this moved recently". Wrapping a word in a link claims
nothing, so counting it as a yes let a pass over the wiring push the standing
rules off the front page of every session for a week. Which kind of edit it is
comes from the passage and never from the call: there is no parameter for it,
because a caller saying "this one does not count" is the sort of claim the
whole gate exists not to believe. The words either differ or they do not.

**`journal_add` takes no quote.** It records what a session did, it makes no
claim about you, and it is never read as a rule. Requiring a quote there would
mean approving a sentence you never said.

**Nor does `mabolo_reindex`, for the same reason.** An index holds nothing but
what the entries beside it already say, so a rebuild asserts nothing you have
not approved once. It is still a write, so it is not registered in read mode
and its commit carries a trailer of its own kind. What it is for, and the one
case nothing else watches, is in [when something has gone wrong](recovery.md).

**A description has its own tool, because it is not a passage.** `mabolo_edit`
splits the frontmatter off before it looks for the words you gave it, so the
line a reader sees first used to be the one thing an agent could not correct,
however plainly you said it. `mabolo_describe` takes the same gate and the same
revision check, refuses a description that would not fit one line of an index
rather than letting it be cut there, and does record a verification: unlike a
link, a description is a claim about what the entry holds.

**So does the status, for the same reason.** It sits in the frontmatter too,
so a project brought back from the archive kept `deprecated`, and every search
went on marking it as dead however plainly you said it was back. `mabolo_status`
takes the gate and the revision check, accepts `draft`, `stable` and
`deprecated` and nothing else (active is `stable`), and records a verification.
Its answer repeats the description, because the line written when an entry was
retired often says so, and then that wants `mabolo_describe` as well.

**A body keeps its paragraphs.** Trailing whitespace goes, a run of blank lines
becomes one, an indent the whole body shares is dropped, and nothing else is
touched: a rule and its reason stay two paragraphs, and a table, a list and an
indented code block survive. The sentence that authorised the entry is added
under it as a footnote, with the marker at the end of the last paragraph, or on
a line of its own when that paragraph ends in a fence or a table row.

## Proposals: what an agent may do alone

An agent with nothing of yours to quote cannot write. It can suggest.

```
                   dropped after 30 days
                 +----------------------+
                 v                      |
 automation -> proposed --approved--> committed --git revert--> gone
                 |                      |
                 +-- refused            +-- conflict
                     id stays on file       push refused, read again
```

**A proposal waits on a branch that is never merged.** An orphan branch called
`inbox` in the vault's own repository, one JSON file per suggestion, rebuilt as
a single parentless commit from the union of what this clone and the remote
hold. So an unapproved suggestion never enters the history of the vault, it is
still visible on every machine, and one filed while you were offline goes out
with the next thing that touches the branch. The branch is never checked out.

**The id is derived, not invented.** A hash of the action, the target and the
sentence, folded the way the consent check folds it. The same suggestion made
twice is one id twice, so an extraction pass that runs over a transcript again
does not turn one thought into two things to read, and a suggestion you refused
in March is recognised when it comes back in June.

**You answer, and the answer is written down.** At a terminal:

```bash
mabolo inbox                 # what is waiting
mabolo inbox a3f2 yes 7c01 no
```

A yes writes the entry and the line recording it in one commit, because two
commits would leave a window in which the vault holds a claim nothing accounts
for. A no writes only the line. Both go into `.mabolo/decided.md`, which lives
in the vault beside the entries so that the answer travels with them. The
sentence a proposal quoted is never written there: a refusal is often precisely
"I do not want this remembered".

Through MCP the same answer needs the sentence you typed, and the answer is read
out of that sentence rather than taken from the call. The word has to stand next
to the id, so "a3f2 yes" answers and "a3f2 yesterday we looked at it" does not,
and one sentence answering two proposals answers each the way you wrote it: a
call claiming yes for the one you refused is turned down naming what you said.
An id you answered both ways is refused rather than resolved. A machine
generated id in a prompt is a signature of having looked at the inbox; "yes" is
a signature of nothing, which is why this is the one place the twelve character
floor is lowered and the shape stands in for the length.

## Extraction: where proposals come from

Proposals are mined from finished transcripts, behind gates that each exist
because something went wrong without them.

1. **Signal words only.** No signal, no model call, no cost.
2. **A window** of the signal message and three exchanges before it, with tool
   output stripped.
3. **Redact, then shorten.** In that order, so that shortening cannot cut a
   secret in half and leave the half that matters.
4. **An isolated model run.** No tools, a byte cap, a timeout, and the window
   counted as data rather than as instruction. The model is a command line from
   your configuration, not a dependency: Mabolo ships no vendor and no API key
   handling, and with nothing configured the pass says so instead of doing
   nothing quietly.
5. **The quote must match verbatim** a real message of the window, or the
   proposal is discarded and counted.
6. **The validator runs** on the finished entry before a proposal is built at
   all, and again when you approve it, because the vault has moved in between.

The pass reports how many candidates there were and where each one went. A run
that silently produced nothing is indistinguishable from one that never
happened, and this project treats that as the failure to avoid.
