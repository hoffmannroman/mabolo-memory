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
mabolo context --no-project                     # no project at all
```

```
Active project: atlas

## Always
- working-hours: Deep work after 20:00, reviews and replies before noon

## Lately
- 2026-09-16: The release moved to Friday, because the migration did not run through on staging twice in a row. See [atlas-release-checklist](project/atlas/atlas-release-checklist.md).
- 2026-09-16: Decided against a second store for the session cache, so nothing outlives a request: [atlas-uses-postgres](project/atlas/atlas-uses-postgres.md).
- 2026-08-14: Wrote down where releases are cut from, [atlas-release-checklist](project/atlas/atlas-release-checklist.md).

## design (3 entries)
also here: dates-are-iso, errors-name-the-fix, no-centred-layouts

## hosts (2 entries)
- build-server: Four cores, 4 GB of RAM, runs the nightly build
also here: gateway

## infra (5 entries)
- ci-memory-limit: The CI image caps at 4 GB, so more than -j4 gets the runner killed
also here: backups-run-nightly, deploy-from-main, secrets-in-the-password-store, staging-mirrors-production

## persona (2 entries)
also here: reviews-need-a-diff

## project/atlas (4 entries)
- atlas: The example project this vault belongs to
- atlas-release-checklist: Migrations applied, changelog written, staging green for a full day
- atlas-tone: Product copy stays flat and factual, and never shouts
- atlas-uses-postgres: One Postgres database, no second store and no cache that outlives a request

## project/beacon (2 entries)

11 entries above are named only. Search the memory by name or topic to read them.
```

`## Always` holds the standing rules, and they are the reason the rest of this
page exists. `## Lately` holds the other half of what a session needs: the map
says what *exists*, and those lines say what was *decided*. An entry says
releases are cut from main; the journal says the release moved to Friday and
why. The first line says which project the payload was built for. Without it a line
about a release checklist looks the same whether it arrived because the session
is in that project or because somebody pinned it, and those are two different
reasons to trust it.

## Three things can happen to an entry

It gets a line, it gets its name under `also here`, or it gets counted in the
last line and nothing else.

The middle one is where most of a vault ends up, and it exists because **a
count is not a search key**. "Eleven entries not shown" cannot be acted on by a
reader who does not already know what is in there. `no-centred-layouts` can,
without that reader having to suspect it exists first. Measured against this
project's own entries, a full line costs about 39 tokens and a bare name about
8, so whatever the lines leave unspent buys roughly five times as many names.

The order is the same order as everything else here: what the rule chose and
the budget then cut is named first, because the rule had already said it was
worth showing. What no rule wanted is named with what is left.

`design` shows this at its clearest. Nothing in it was picked for this session,
and the session is still told that three entries live there and what they are
called. `persona` shows two entries and one bare name: the other is the rule
above, listed where it belongs rather than twice.

The last line accounts for all three, and it says which is which. "Every entry
is listed above" with eleven bare names under it would be the quiet stop this
tier exists against, in a politer wording.

## Why an entry has no line

```bash
mabolo context --why-not
```

```
  old-news  no rule chose it: not pinned, not in the active project, not touched this week, named
  atlas-tone  chosen, then cut: the budget ran out before this line, named
  beacon  no rule chose it: not pinned, not in the active project, not touched this week, and no room left for the name either
```

Two questions per entry, never folded into one: whether a rule wanted it, and
then whether there was room. They are answered by different levers. Raising the
budget brings back what it cut and will never bring back what no rule chose,
and a reader who cannot tell the two apart turns the wrong dial.

## Lately: a pointer, never a copy

The lines under `## Lately` come from `log.md`, the one file in a vault written
in the order things happened. A line belongs to a project when it links into
that project's area, which is the link form the rest of the vault already uses:

```markdown
## 2026-09-16

- The release moved to Friday, because the migration did not run through on
  staging twice in a row. See [atlas-release-checklist](project/atlas/atlas-release-checklist.md).
- A line naming no project belongs to no project, and appears in no payload.
```

The link is kept in the payload on purpose. It is the pointer: the line says
what was decided, and the link says which entry holds the long version, so the
payload never has to carry it. A line that wraps in an editor is folded back
into one sentence, because half a decision reads like a whole one.

Three rules hold this block:

* **Only the active project.** No project, no block. Handing a session the
  newest lines of somebody else's project would be worse than silence.
* **Newest first, and the budget takes the far end.** It has a budget of its
  own, 500 tokens, which is neither taken from the map nor added to it. When
  lines are cut the block says how many, because a list that quietly stops
  reads like a complete one.
* **Nothing dated later than the session counts.** One typo, or one machine
  with a wrong clock, would otherwise park a line at the top of every session
  from now on.

Journal lines are not entries. They carry no name, they are never searched, and
they are counted apart from the entries in every number this command prints.

A pin says "always", and the area says "here". A rule pinned inside
`project/beacon` is a standing rule for sessions about beacon, and nothing at
all for a session about atlas: where an entry lives is a statement about where
it applies. A pin outside any project area holds everywhere, which is what
pinning is for.

## Two kinds of entry, and only one of them is a rule

A fact can wait until somebody asks for it: you notice that you need the port
number, and you go and look. A rule cannot. Nobody looks up a rule they have
forgotten, they simply do not follow it, and nothing about that looks like an
error. So the payload keeps the two apart.

**A pinned entry is a standing rule.** It goes into the core, under `## Always`,
it is never cut, and it has a budget of its own. Everything else competes for
what is left.

## What gets in

An entry is offered when one of three things is true, and they are checked in
this order:

1. **It is pinned.** `mabolo.pin: true` in the entry. This one is the core.
2. **It belongs to the active project.**
3. **It was touched in the last seven days**, according to the entry itself: the
   later of `generated.at` and any `verified.at`. Both ends count: a timestamp
   in the future is not recent either, or one wrong clock would keep an entry at
   the front of every session for years.

That order is also the order entries survive in. When the map does not fit the
budget the least safe line goes first, and inside one class the oldest goes
before the newest. The core is not in that queue at all.

## Which project a session is about

Nobody types it. The project is the name of the repository you are in, and it
counts as a project when the vault already holds `project/<that name>`.

```
~/work/atlas/backend/src   ->  the nearest .git upwards is ~/work/atlas
                           ->  the vault has project/atlas
                           ->  Active project: atlas
```

The match is exact. Matching loosely would be worse than matching nothing: a
session in the wrong folder would be handed somebody else's decisions and never
say so. A folder that names no project is simply a session without one, which is
not an error, and the payload then holds what is pinned and what moved this
week. `--project` overrides the folder and `--no-project` switches it off.

Three properties fall out of taking the repository root rather than the working
directory: a subfolder of a project is still that project, a repository inside a
repository resolves to the inner one, where the work is happening, and a
worktree or a submodule counts, because there `.git` is a file rather than a
folder.

Reading the filesystem for this is the caller's job, the way reading the clock
is. The rule itself takes a name and a list of areas and returns a name.

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

## The budget, and the one it does not apply to

The target is about 800 tokens, estimated, and it is a target rather than a
limit: the hard ceiling belongs to whichever client receives the payload, and
clients disagree about it. `--budget` changes it for one run, which is the
quickest way to see what a smaller session would lose.

An entry the budget cut is counted separately from one the rule never chose.
They are different problems: the first is fixed by a larger budget, the second
by a pin.

**The core is never cut, so it has a target instead: about 300 tokens.** Written
as one sentence each, that is roughly fifteen standing rules. Pin the sixteenth
and nothing disappears; the payload says so at the end and `mabolo context`
exits 1:

```
The core is over its budget: 18 rules, about 380 tokens, target 300.
Nothing was dropped. Unpin what is no longer a rule.
```

That is deliberate. Trimming the core quietly would be the worst version of the
failure this whole design is against: a rule that vanishes is not missed, it is
simply not followed. So the tool refuses to make that decision and hands it
back, with the number that makes it decidable.

Pinning is therefore not free, and the price is visible: a rule takes its room
out of the same target, so the map shows one line fewer.

**Fifteen rules is the real ceiling on how many things can hold at all times.**
A vault with fifty of them has a different problem, and no budget solves it: see
[the limits](#the-limit-nobody-can-budget-their-way-out-of).

## The limit nobody can budget their way out of

Fifteen is not a technical number, it is the honest one. Measured against real
rules written as one sentence: eleven cost 227 tokens, twenty five cost 493, and
fifty cost 968, which is more than the whole payload is meant to be. Writing
each rule as a sentence instead of an essay buys a factor of thirty. It does not
buy an unlimited number of rules.

So a vault that keeps growing rules has to do one of three things, and only the
first exists today:

1. **Say the rule in one line**, and leave the reasoning in the body where it is
   read on demand. That is the format's job and it is built.
2. **Give a rule a trigger instead of a permanent seat.** A rule about how dates
   are written matters when a date is being written, not at every session start.
   `applies_to` does this for design rules against file patterns; the general
   case is not built.
3. **Let a rule die.** Facts have `stale_after` and a file to watch. A rule has
   neither, and a vault of fifty rules usually holds fifteen variations of the
   same one and ten that stopped being true.

## It is measured

An entry that stops being offered here is still findable by name, so every
search case keeps passing while the memory gets quieter. That is why the
question set has cases about this payload too, with `tier: hint`, and why they
record how close an entry is to being cut rather than only whether it is there.
See [measuring the memory](eval.md).

## The hook a client calls

`mabolo context` prints the payload for a person. `mabolo hook session-start`
hands the same payload, built by the same code and the same arguments, to a
client that supports a session start hook:

```bash
echo '{"cwd": "/path/to/a/project"}' | mabolo hook session-start
```

```json
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
```

Three promises hold here, and they are worth more than the payload itself.

**It never fails.** Whatever goes wrong, it exits 0 and says nothing. A session
that gets nothing starts the way it would have without Mabolo installed. A
memory that can stop a session from starting is worse than no memory: the
failure arrives before the person has typed anything and looks like the agent
is broken. What went wrong goes to stderr, where a person debugging the hook
looks and a session does not.

**It gives up on time.** Five seconds, and then nothing. The deadline is a
promise to the person waiting, so it is kept by giving up rather than by
finishing late.

**It checks the payload before sending it.** Every standing rule reached the
text, every chosen line reached it, no line was written twice, the map stayed
inside its budget and the journal block inside its own. Every one of those is
measured on the finished text: whole lines, counted where they were written.
An earlier version compared substrings and counted names in the selection
instead, which meant a line rendered twice went unnoticed and a busy journal
made a map that was well inside its budget look like it had broken it. The check reads the finished payload rather than the
selection that produced it, which is the only way it can catch a mistake the
selection did not know it made. It repairs nothing: a payload that fails is
replaced by the standing rules alone, plus a line saying the map could not be
built. Losing the map costs a session the knowledge that an entry exists, and
that can be recovered by asking. A rule that silently went missing cannot.
