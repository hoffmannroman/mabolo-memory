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

**What it cannot claim.** A client starts the MCP server without telling it
which session it belongs to. So unless the client sets `MABOLO_SESSION_ID`, the
server cannot honestly say "this sentence, in this conversation". What it can
say is "this sentence, typed in this directory, within the last twelve hours",
and that is what it does: notes from another directory do not count, and notes
older than the window do not either. With a session named, that session's notes
are the evidence and the window does not apply.

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
are the ones the change names. A change you made by hand in an editor is
committed first, in a commit of its own, so that the diff of a write holds only
what the write did. Your staged work stays staged.

**The only thing that stops a mutation outright** is a merge, rebase,
cherry-pick, revert or bisect already running in the vault, or a detached HEAD.
Neither is something a memory tool should decide for you.

### What a tool can answer

| Outcome | What happened |
|---|---|
| `written` | committed and on the remote |
| `local` | committed, and this vault has no remote |
| `offline` | committed, and the remote could not be reached, so it is only here |
| `conflict` | the remote moved, nothing was written, read it again |
| `stale` | the entry changed since you read it, nothing was written |
| `blocked` | Git is busy in the vault, nothing was touched |
| `nothing` | the vault already says exactly that |

`offline` is a deliberate answer rather than a failure. Refusing to remember
something because a laptop is on a train would be the worse bargain, and a
message that said "written" would be a guess. The next mutation fetches, fast
forwards and carries it along.

The way back from any change is `git revert` of a single commit.

## The tools

A client that speaks MCP gets these. `mabolo serve --read-only` registers only
the first two, which is what an unattended agent, a lint pass or a subagent is
started with: the write tools are not refused there, they do not exist.

| Tool | What it does |
|---|---|
| `mabolo_search` | one line per entry, with what reading them all would cost |
| `mabolo_read` | entries in full, each with its revision |
| `mabolo_write` | a new entry, from a sentence you typed |
| `mabolo_edit` | replaces one passage, leaving the rest of the prose alone |
| `mabolo_forget` | removes an entry; the history keeps it |
| `journal_add` | one line under today's date in `log.md` |

`mabolo_search` and `mabolo_read` are two calls on purpose. A single call cannot
announce the price of its own payload: by the time the answer arrives, the
payload is already in the context.

**An edit is targeted, never a rewrite.** The passage has to occur exactly once,
and an entry whose passage is ambiguous is refused rather than guessed at. A
rewritten entry produced by a model silently changes sentences that were
somebody's decision. The prose outside the passage is left byte for byte; the
frontmatter is re-rendered, because that part is structured data the tool owns.

**`journal_add` takes no quote.** It records what a session did, it makes no
claim about you, and it is never read as a rule. Requiring a quote there would
mean approving a sentence you never said.

## What is not built yet

Proposals. The state machine has a `proposed` state, an inbox on an orphan
branch and an extraction pass that mines finished transcripts behind eight
gates. None of that exists yet. Today the shape is simpler and the claim is
narrower: a tool call carrying a sentence you just typed writes; everything
else reads.
