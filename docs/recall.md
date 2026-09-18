# Quiet recall

The session index answers "what is in this vault". This answers something
harder: the person wrote a sentence to their agent, and somewhere in the vault
is an entry that changes what the right answer is. Nobody asked for it.

```bash
echo '{"prompt": "I am about to raise the job count on the build server"}' \
  | mabolo hook prompt
```

```json
{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "## From memory, not from the prompt\n- build-server: Four cores, 4 GB of RAM, runs the nightly build"}}
```

For most prompts it prints nothing at all, and that is the design rather than a
shortfall.

## Silence is the feature

This runs ahead of every prompt, and the person did not ask for it. An offer
that is merely plausible is noise, and noise on every prompt teaches the reader
to stop looking at the block entirely, including on the one prompt where it
mattered. So:

* The relevance floor of the search decides, and it is the same floor the
  search itself uses. A question gets an entry back when it names that entry,
  or when two of the question's words are in it.
* Three lines is the ceiling. Not a budget decision: three is where a block
  stops reading as an aside and starts reading as a second opinion nobody asked
  for.
* Each line is capped at 200 characters, cut visibly with an ellipsis. Enough
  for a description to make its point, short enough that three of them do not
  push the person's own sentence off the screen.
* Nothing found prints nothing. No heading, no blank line, nothing for a client
  to strip. A block saying "nothing to add" would cost every prompt in this
  vault's life the tokens to say that.

The block names itself `## From memory, not from the prompt`, because an agent
that cannot tell the two apart may read a recalled entry as an instruction the
person just gave.

## What it costs, and what it cannot do

Measured against the example vault: about 25 tokens when it speaks. The budget
in the architecture is 150.

It is the tier with the sharpest known limit, and there is a case in the
example vault that exists to state it:

```yaml
id: quiet-recall-late-night
needs: meaning
query: I will start the big refactor now, it is half past eleven
expect:
  entries: [working-hours]
```

The entry says "deep work after 20:00". The prompt says "half past eleven".
They share no word, so keyword search cannot connect them, and this case is
red and stays red. It is kept rather than deleted or rewritten: deleting it
would delete the evidence that the gap exists, and rewriting it into something
the current search can answer would turn a known limit into a green tick.

`mabolo eval` prints it on a line of its own:

```
blocked  1 case waiting on meaning: this case needs meaning rather than words…
```

Embeddings are designed into the search interface and ship switched off. When
they are switched on, this is the case that says whether it worked.

## The promises it shares with the session start

Never fails, exits 0 whatever happens, says what went wrong on stderr. Gives up
after **two** seconds rather than five: a session start happens once and is
expected to take a moment, while a pause here is felt on every prompt and is
blamed on the agent.

It also stays quiet about a missing configuration, where the session start says
one sentence about it. Said once when a session begins that is help; repeated
on every prompt it is the tool nagging about its own setup.
