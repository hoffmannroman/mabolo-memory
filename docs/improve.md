# When Mabolo itself goes wrong

A memory that fails quietly looks exactly like an empty one. So Mabolo writes
down its own faults, keeps them until somebody has dealt with them, and can
hand one at a time to an agent that tries to fix it.

## What is recorded

```bash
mabolo reports                 # what is open on this machine
mabolo reports --all           # including what was fixed or looked at
mabolo reports --close <id>    # settle one by hand, with --note for why
```

Three kinds of fault arrive, all in `~/.local/state/mabolo/reports.jsonl`:

* **A hook that gave up.** The next session start still says so once, as it
  always did. That notice is used up when it is shown; the record is not.
* **A tool that broke.** An `OSError`, a `ValueError` or anything unexpected
  under a tool call. A refusal is not recorded: a refusal is the gate working.
* **A session's report.** `mabolo_report` is a tool every session has, in read
  mode as well. An agent that sees Mabolo misbehave says what happened and
  what should have, and tells the person. It needs no quote, because it makes
  no claim about anybody, and it never touches the vault.

Repeats of one fault are one problem: the same place and the same kind of
exception share an id, so a hook that times out on every prompt is one problem
seen forty times. Nothing is ever deleted when it is dealt with. A status is
appended, and a problem that happens again afterwards is open again, which is
how a fix that did not work announces itself.

The record is per machine and stays out of the vault. The vault holds what a
person agreed to keep; this is not that.

## Fixing one

```bash
cd ~/path/to/mabolo            # the checkout the tool was installed from
mabolo improve --dry-run       # what the agent would be told
mabolo improve                 # take the most frequent open problem
mabolo improve --problem <id>  # take a particular one
```

It runs when you run it, never on a timer, and takes one problem per run.

1. It makes a Git worktree of the main branch under the state directory, so a
   session working in your checkout is not disturbed.
2. It runs the test suite, `eval` and `validate` there, as the line to hold. A
   main branch that fails its own tests stops the run: nothing can be measured
   against it.
3. It starts Claude Code headless in the worktree, without hooks and without MCP
   servers, so the agent cannot reach the memory, and with a fixed set of
   tools: read and edit files, run the tests, inspect Git, commit. The problem
   reaches it fenced and marked as data, because its text comes from places
   nobody vets. It is told to reproduce the fault with a failing test first.
4. Then this code, not the agent, decides:
   * the whole suite passes, and no fewer tests pass than before;
   * `eval` and `validate` do not end worse than before;
   * no test file was deleted, and the tests that hold the consent gate and the
     write tools to their promises only grew;
   * nothing protected changed: the consent gate, the write path, the proposal
     and decision code, `improve` itself, the test isolation, the dependencies
     and the client plugins. A fix there is a decision for a person;
   * the configured guard passes.
5. If everything holds, it rebases onto whatever reached the main branch
   meanwhile, runs the tests again, and pushes. Your checkout is fast-forwarded
   when it is clean and on the main branch, and the installed tool is
   reinstalled from it; `--no-install` skips that. Open sessions keep the old
   code until `/mcp reconnect mabolo`, and say so.
6. If anything fails, the fix stays on `improve/<id>` and nothing is pushed.
   The problem is marked as looked at, so the next run takes another one, and
   the branch has to be deleted by hand before the same problem is tried again.

An agent that finds no fault, or cannot reproduce one, changes nothing; what
it said is kept as the problem's note.

## The guard

```toml
[improve]
guard = ["python3", "tools/check.py", "--all"]
share = ["local-notes"]
```

`guard` runs in the worktree before a push and keeps the fix back when it
fails. It is the place for a check that only your machine can make, such as a
list of strings that must never reach a public repository. Without one, the
run says so at the start and relies on the checks above.

`share` names untracked paths of your checkout that the worktree gets a copy
of, for a guard or a Git hook that reads files nobody commits. A path Git does
not ignore is not copied, because the next commit would take it along.
