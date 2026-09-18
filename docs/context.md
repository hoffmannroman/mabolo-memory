# What a session starts with

A memory that preloads everything is a memory you pay for on every prompt,
whether or not it had anything to say. A memory that preloads a truncated
version of everything is worse: it looks complete and is not.

So a session is handed a map. Every area that holds entries, with how many it
holds, the few entries this session is most likely to need, and a last line
saying how many were left out. An empty vault therefore shows only that last
line: the list of areas comes from the entries themselves and never from the
configuration on one machine, because two clones of one commit have to produce
the same payload.

```bash
mabolo context                                  # the configured vault, as of now
mabolo context path/to/vault --project atlas    # as a session in that project sees it
mabolo context --as-of 2026-09-18               # what it looked like on that day
mabolo context --no-clock                       # nothing counts as recent
```

```
## design (3 entries)

## hosts (2 entries)
- build-server: Four cores, 4 GB of RAM, runs the nightly build

## infra (5 entries)
- ci-memory-limit: The CI image caps at 4 GB, so more than -j4 gets the runner killed

## persona (2 entries)
- working-hours: Deep work after 20:00, reviews and replies before noon

## project/atlas (4 entries)
- atlas: The example project this vault belongs to
- atlas-release-checklist: Migrations applied, changelog written, staging green for a full day
- atlas-tone: Product copy stays flat and factual, and never shouts
- atlas-uses-postgres: One Postgres database, no second store and no cache that outlives a request

## project/beacon (2 entries)

11 entries not shown. Search the memory by name or topic to reach them.
```

The two empty headings are the point. Nothing in `design` was picked for this
session, and a reader can still see that three rules exist there and go and ask
for them.

## What gets in

An entry is offered when one of three things is true, and they are checked in
this order:

1. **It is pinned.** `mabolo.pin: true` in the entry.
2. **It belongs to the active project.**
3. **It was touched in the last seven days**, according to the entry itself: the
   later of `generated.at` and any `verified.at`. Both ends count: a timestamp
   in the future is not recent either, or one wrong clock would keep an entry at
   the front of every session for years.

That order is also the order entries survive in. When the payload does not fit
the budget the least safe line goes first, and inside one class the oldest goes
before the newest. A pinned entry of the active project is held by the pin, so
leaving a project never takes it away.

## What is deliberately not in the rule

**Nothing is learned.** No usage counts, no "you read this yesterday", no
ranking that improves while you work. Those would all make the payload depend on
something that is not in the repository, and then it could not be rebuilt at an
old commit. Rebuilding it at an old commit is what turns "the memory got quieter"
into a commit you can name.

**The clock is passed in, not read.** `--as-of` is an argument everywhere,
including in the harness that measures this tier. A rule that read the clock
itself would answer differently every week, and no measurement of it would hold.

**The selection is not a model.** One line per entry does not fit in the budget
once a vault has a few thousand entries, and silently trimming the tail is the
failure this whole design is against. A rule can be read, argued with and
replayed. That matters more here than being clever.

## Nothing in an entry can forge the shape of it

The heading, the entry lines and the last line are what a reader trusts, and all
three are built from files a person or an agent wrote. So every value that goes
into the payload is reduced to one printable line first. A description holding a
newline would otherwise write its own heading and its own closing sentence into
the middle of a payload that is injected into a prompt automatically. The words
survive, the structure does not bend.

## The budget

The target is about 800 tokens, estimated, and it is a target rather than a
limit: the hard ceiling belongs to whichever client receives the payload, and
clients disagree about it. `--budget` changes it for one run, which is the
quickest way to see what a smaller session would lose.

An entry the budget cut is counted separately from one the rule never chose.
They are different problems: the first is fixed by a larger budget, the second
by a pin.

## It is measured

An entry that stops being offered here is still findable by name, so every
search case keeps passing while the memory gets quieter. That is why the
question set has cases about this payload too, with `tier: hint`, and why they
record how close an entry is to being cut rather than only whether it is there.
See [measuring the memory](eval.md).
