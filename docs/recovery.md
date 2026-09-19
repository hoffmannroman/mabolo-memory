# When something has gone wrong

Four commands, and four procedures that are printed rather than performed.
The split is deliberate: a tool that repairs things on your behalf is a tool
whose repairs you have to audit, and the whole point of this one is that you do
not have to.

## The commands

```bash
mabolo revert <commit>            # undo one commit, as a commit of its own
mabolo recover files <path...>    # put named files back to what the last commit says
mabolo recover push               # send what this clone has, or show both sides
mabolo reindex                    # rebuild an index that no longer matches its entries
```

**`revert`** is `git revert` with three things added: the undoing carries the
same trailer as everything else Mabolo commits, so `doctor` does not report the
repair as a commit from nowhere; it goes out with the same lease; and it prints
the plan first, so `--dry-run` shows you exactly which files would come back or
go away. It is built from the trees rather than from a patch, so it is either
the whole commit or nothing. A commit with two parents is refused: Mabolo never
merges, so that is not one of its own, and which side of a merge to keep is
your decision.

**`recover files`** exists for the one case the transaction leaves open. A
change is committed first and written into your folder afterwards, so a failure
in between leaves a repository that is right and a file that is stale. This
rewrites the named files from the last commit.

It refuses a file whose current bytes were never committed under that name.
Those bytes are somebody's hand edit, and a repair that threw one away would be
the damage rather than the cure. The refusal says so and names the file.

**`recover push`** sends commits the remote does not have. If the two sides have
diverged it stops and prints both lists, because merging or rebasing on your
behalf is a decision. It never merges, never rebases and never forces: a lease
alone would not save you here, since a lease permits overwriting a remote that
sits exactly where you last looked.

**`reindex`** repairs the one file that is derived rather than written, in the
one case nothing else is watching. Every write works out which indexes it made
wrong and commits them in the same commit as the entry, so the tools keep
themselves straight. What they cannot see is a change that did not come through
them: an entry created in Obsidian, a merge, a commit made by hand. That lands
next to the entries without deriving anything, and the folder's index goes on
describing a vault that no longer exists.

`mabolo init` would also rebuild it, and that is the trap this replaces: init
is for a vault that does not exist yet, so reaching for it to repair a live one
is the wrong command on the right problem. `reindex` prints what is stale, and
`--dry-run` stops there. Otherwise it goes out as a commit of its own kind,
with the trailer and the lease, so the history says plainly which commits
changed what the vault knows and which only made an index agree with it again.

`doctor` finds them without being asked, by rebuilding every index and seeing
whether it comes out the same. It used to ask only whether each entry was
*named* by its index, which an index can do while being wrong about every one
of them.

## The procedures

**A diverged branch.** `mabolo recover push` printed both sides. Fetch, look at
them, then `git merge` or `git rebase` in the vault by hand. Mabolo refuses to
write while a merge or rebase is in progress, so finish it before the next
session starts.

**A secret that was committed.** Git keeps it, so removing the line is not
enough. Rewrite the history with `git filter-repo`, force push every branch to
every remote, and prove it is gone with `git log -S'<the secret>' --all`. Then
rotate the secret anyway: it existed in a repository, and you cannot know who
cloned it.

**A lost machine.** The clone is disposable. Rotate the key that machine used on
the remote, and clone again somewhere else. Nothing in a vault is encrypted and
nothing in it is unique to one machine except what was never pushed, which is
what `mabolo doctor` counts for you under "commits not on the remote".

**A broken clone.** Delete it and clone again. The remote is the truth; the only
thing that lives in a clone alone is a hand edit you have not committed, and
`git status` is what tells you whether there is one.

## What needs no command

A leftover lock file needs no cleaning: the lock is held by a process, not by
the file, so it is released when that process ends however it ends. `doctor`
names the file only when the lock is actually held and old.

The search index has nothing to repair. It is built in memory from the files
every time and never stored, so there is nothing that can disagree with what is
on disk.
