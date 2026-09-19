# Measuring the memory

The tempting bug in a memory system is invisible. An entry exists, nothing ever
surfaces it, nobody notices, and the tool looks fine. A memory that quietly
forgets is indistinguishable from one that works, unless somebody measures it.

So the question set is not a test suite around the search. It is the thing the
search is built against, and it exists before the reading tier does.

```bash
mabolo eval                      # the configured vault
mabolo eval path/to/vault        # any vault
mabolo eval --case deploy-source --explain
mabolo eval --save-baseline      # write this run as the line to hold
```

## Two things get measured, and they are not the same question

A search is one link in the chain, and not the first one. Before a model can
search for an entry it has to have a reason to go looking, and that reason is
the session index: the short payload a session starts with. An entry that is no
longer offered there is still findable by name, so every search case keeps
passing while the memory gets quieter. That is the failure this tool is named
after, so it has cases of its own.

* `tier: search` is a search. A question goes in, entries come back, and the
  case says which ones and how far down. It is what a case gets when it names no
  tier at all, and `tier: index`, the name it had before, still reads.
* `tier: hint` is the session index. There is no question at all: the trigger is
  the session starting, so the case describes the state it started in and says
  what has to be in the payload, what has to stay out of it, and what it may
  cost. What that payload is and how it is chosen:
  [what a session starts with](context.md).
* `tier: recall` is the quiet block a prompt is interrupted with. Search
  shaped, like the first one, and cut to what a prompt is actually shown: three
  lines. A case cannot ask for a rank beyond that, because an entry on the
  fourth line was never shown and must not count as recalled. What it offers
  and why it usually says nothing: [quiet recall](recall.md).

## What a case looks like

One YAML file per case, in `.mabolo/eval/` inside the vault. They are versioned
with the entries they query, so any commit can be replayed later.

```yaml
id: deploy-source
query: are we allowed to cut a release from a tag?
expect:
  entries: [deploy-from-main]     # by name; an alias resolves to its entry
  rank_within: 1                  # how far down the answer may be
  must_cite: true                 # the answer has to point at the entry
tier: search
```

A hint case has no `query`. The session start is its trigger, so it carries the
state that start happened in. A `state.project` that is not a project area in
the vault fails the case: otherwise a typo is the quietest pass there is, since
the project rule simply never fires and the pinned entries still show up.

```yaml
id: session-index-atlas
tier: hint
state:
  project: atlas
  as_of: 2026-09-18
expect:
  in_payload: [working-hours, atlas-release-checklist, ci-memory-limit]
  not_in_payload: [beacon-runs-on-a-schedule, dates-are-iso]
  budget_tokens: 800
```

`as_of` is required and there is no default. The selection rule counts seven
days back from the moment the session starts, so a case that let the harness
read the clock would measure a different vault every week and report the drift
as a regression. A fixed moment is also what makes the case replayable at an old
commit.

`budget_tokens` is a statement about the result, not the budget the payload is
built with. The payload is always built to the shipped target, or a case would
only ever confirm itself.

The position a hint case records is the **cut order**: 1 is the entry furthest
from being dropped, and the number grows towards the line where the budget
stops. So a rank that grows is an entry drifting towards falling out of the
session index, which is the warning worth having before it does.

A negative case names nothing and asserts silence. It is worth as much as the
others: a search that always finds something is not a search, it is a slot
machine, and the result is injected into a prompt automatically.

```yaml
id: smalltalk
query: carry on
expect:
  entries: []
  silence: true
tier: search
```

| Key | What it means |
|---|---|
| `id` | Unique in the vault. The baseline is keyed by it, so renaming one starts it over |
| `query` | The question, as somebody would type it |
| `expect.entries` | The entries that answer it, by name or alias |
| `expect.rank_within` | How far down the list the entry may appear. Default 5 |
| `expect.must_cite` | The answer has to cite the entry. Needs a model in the loop |
| `expect.silence` | Nothing at all is the correct answer |
| `tier` | `search`, `hint`, `recall` or `design`. Defaults to `search` |
| `needs` | What this one case is waiting for. Only `meaning` so far |
| `note` | For a reader. Ignored by the run |

`needs` is for a case that is red on purpose. A tier is deferred because
nothing was built; a case is blocked because what it asks for is a decision
that was made and has not been taken back. `needs: meaning` says the entry
shares no word with the prompt, so keyword search cannot reach it and
embeddings are off by default.

Such a case is kept rather than deleted or rewritten. Deleting it deletes the
evidence that the gap exists; rewriting it into something the search can answer
turns a known limit into a green tick. It is never counted as a pass, and it
gets a line of its own in the report so that keeping it is not the same as
hiding it.

A hint case uses a different half of the format, and the two are kept apart: a
`query` on a hint case and a `state` on a search case are both refused rather
than ignored.

| Key | What it means |
|---|---|
| `state.as_of` | The moment the session starts at. Required, never the clock |
| `state.project` | The active project, or absent when there is none |
| `expect.in_payload` | Entries that have to be in the session index, by name or alias |
| `expect.not_in_payload` | Entries that have to stay out of it |
| `expect.budget_tokens` | What the whole payload may cost, estimated |

An unknown key is an error rather than something ignored, and so is a key that
belongs to the other shape: a `query` on a hint case and a `state` on a search
case are both refused by name. A case is a measurement instrument, and an
instrument that silently drops a setting reports a number about something other
than what was asked. For the same reason nothing here is coerced. An id has to
be text, a tier has to be text, and an empty name in a list of entries is an
error rather than one expectation quietly fewer than the file shows. For the same reason a case
file that sets the same key twice is refused instead of quietly keeping the
last one, and a file in `.mabolo/eval/` that is not a case is named rather than
stepped over.

## What is measured, and what is not

A memory is useful only if the whole chain works, so the links are scored
separately rather than as one number: was the entry visible at all, did the
model go and look, was the right entry near the top, did it get read, was the
answer right and cited, and what did all of that cost.

Today `mabolo eval` measures the deterministic half: which entries come back,
in which order, whether silence holds, and what a preview of the result would
cost in tokens. It runs offline in under a second and has no API bill, which is
what lets it sit in front of every commit.

The other half needs a model in the loop, and the harness does not run one. It
says so for every case it cannot decide, and never counts one as a pass:

```
search   19 cases, 19 pass, 0 fail
hint     3 cases, 3 pass, 0 fail
recall   2 cases, 2 pass, 0 fail
design   1 case not measured yet, applies_to as a load trigger is not built yet
blocked  1 case waiting on meaning: this case needs meaning rather than words…
```

Each tier is counted on its own and never added up with another. They measure
different payloads, and one number over both would hide which of them moved.

That is the same rule the rest of the tool follows. Reporting a number for the
easy half and nothing for the rest is how a measurement becomes reassurance.

## The baseline

`mabolo eval --save-baseline` writes `.mabolo/eval/baseline.json` beside the
cases, and every later run is compared against it. It is the one thing `eval`
writes, and it goes through the same door as every other change to the vault:
a lock, a revision check, a commit carrying a trailer of its own kind, and a
push with a lease. That kind is `baseline`, and it is not `write`, because
saving one changes nothing the memory knows, only what the next measurement is
held against. Saved by hand it left a commit with no trailer, and `doctor`
reports one of those for as long as the vault exists, so accepting a
measurement used to cost a finding that never went away. Saving the same
numbers twice writes no second commit.

Two things fail the run:

* **A case that passed and now fails.** The obvious one.
* **A case that still passes and slipped down the list.** The early warning. By
  the time a slip becomes a failure, the change that caused it is several
  commits back.

Getting better is reported as a note and fails nothing.

The baseline stores which tier a case was measured in, whether it passed, and at
which rank. The tier is in it because a rank means a different thing in each one:
in a search it is where the entry came back, in a hint it is how far the entry
sits from the line the budget cuts at. A case that keeps its id and changes its
tier is reported as new rather than as having slipped between two scales that
have nothing to do with each other.

The rank itself is deliberately not the BM25 score: a rank is ordinal, while a score is a float that moves whenever
anything about the corpus moves. A gate built on scores fires on noise, and a
gate that fires on noise gets switched off. Ranks are not a promise that two
different SQLite builds rank identically, only that the gate is not comparing
floating point numbers across them.

A failing case has no rank at all, so there is nothing to call better: a rank is
only compared when the case passed on both sides.

It also stores the language it was measured in, and refuses to be compared
against a run in another one. Stop words and suffixes differ by language, so
those are two measurements and not one.

`--save-baseline` writes the whole set, so it cannot be combined with `--case`:
a run over one case would replace the file with that one case, every other case
would be "new" on the next run, and "new" is not a failure. It is also the way
out of a baseline this version cannot read, so it never reads the old one.

Exit codes follow the rest of the tool: 0 when there is nothing to fix, 1 when
the run found something, 2 when the command could not do its job. An empty case
folder exits 1, because "measured nothing" printed above a green exit code is
exactly the quiet success this is built to prevent.

## Why the cases live in the vault

A case and the entries it queries have to move through history together. That is
what makes it possible later to walk the vault's history, rebuild the index at
each commit, replay a case, and name the exact change that made retrieval worse.

Three properties have to hold from the first commit, because retrofitting them
means rewriting history:

1. **Cases are versioned in the vault**, and reference entries by name and alias
   rather than by path.
2. **Ranking is a pure function** of the files at a commit and the query.
   Nothing in it looks at usage counts, at the clock, or at what was clicked
   yesterday. The session index is the same kind of function, with the moment
   passed in rather than read: `as_of` is an argument everywhere, including in
   `mabolo context`.
3. **The index rebuilds deterministically** from any commit, down to the order
   of two entries that score the same.

The vault's language is part of that, which is why it lives in the root
`index.md` and not in a configuration file on one machine. The search drops stop
words in it and cuts suffixes by its rules, so a vault whose language sat
outside it would rank differently on two clones and no commit could be replayed.

Your own questions belong in your own vault, next to your own entries. The cases
in [`examples/vault`](../examples/vault) are invented and query the invented
vault they sit in.

## When the selection rule itself changes

The baseline records the `policy` the session index was built under
(`context.POLICY`, raised by hand when the choice or the order of entries
moves). When a run uses a different one, changes that would read as
`regressed` or `slipped` are called `policy` instead:

```
policy  session-index  passed under selection policy 2 and fails under 3, which may be the rule and not the memory
```

The run still fails. "Means something different" is not "got worse", and the
word now says which one it is, but a rule change can be a rule change *and* a
regression on the same day, and nothing here can tell those apart. So the gate
stays shut until a person looks and runs `--save-baseline`, which is the same
answer a language mismatch gets.
