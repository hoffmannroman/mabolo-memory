<div align="center">

<img src="assets/logo.png" alt="Mabolo Memory" width="120">

# Mabolo Memory

**A memory for coding agents that you can read, edit and delete yourself.**<br>
Plain Markdown files in a Git repository. Nothing is written without your yes.

[![Licence: MIT](https://img.shields.io/badge/licence-MIT-8A2447)](LICENSE)
[![Status: early](https://img.shields.io/badge/status-early-A8660F)](#what-works-today)
[![Platforms: Linux and macOS](https://img.shields.io/badge/platforms-Linux%20%C2%B7%20macOS-3F6248)](#limits)
[![Format: OKF v0.2](https://img.shields.io/badge/format-OKF%20v0.2-2F58C9)](https://github.com/GoogleCloudPlatform/open-knowledge-format)

</div>

---

Your coding agent forgets what it learned when the session ends, and knows
nothing about it on your other machine. Mabolo keeps that knowledge as a folder
of Markdown files in a Git repository, one file per thing worth remembering.

The files are the memory. There is no database that owns them, no service that
has to be running, and no account. You can open them in any editor, search them
with `grep`, correct them by hand, and take them with you.

## What an entry looks like

```yaml
---
type: reference
title: Deploy from main only
description: Releases are cut from main; tags are labels, not sources
generated:
  by: claude-code/2.1.84
  at: 2026-08-14T10:22:00+03:00
verified:
  - by: human:alex
    at: 2026-08-14T10:25:00+03:00
sources:
  - id: s1
    resource: session://2026-08-14
status: stable
mabolo:
  area: infra
---

Releases are cut from `main`. Tags mark what shipped, they are never the source
of a deploy.[^s1]

[^s1]: "we deploy from main only, never from a tag"
```

A whole vault of these, filled in, is in [`examples/vault`](examples/vault).

## Why it is built this way

**You can check every claim.** An entry records who produced it, when, and the
sentence it came from. A claim in the body points at its source with a footnote.
When you ask why your agent believes something, the answer is a file, a quote
and a commit.

**Nothing is written without your yes.** An agent can propose. Approval is what
turns a proposal into a commit, and the `verified` field is only ever written
for a person. That rule is enforced in the validator, not just documented.

**It knows your taste, not just your facts.** Design decisions are their own kind
of entry. They carry the files they apply to and the default they override, so a
rule about how things should look arrives when a matching file is touched,
instead of sitting in a notebook nobody opens while the work happens.

```yaml
mabolo:
  area: design
  applies_to: ["*.css", "*.tsx", "*.astro"]
  instead_of: "the model's habit of centring everything on a landing page"
```

**Age is a weak signal, so an entry watches a file instead.** `mabolo.anchor`
names the thing an entry talks about. When that thing moves, the entry is
flagged. What makes a note wrong is rarely the calendar.

**It is organised the way your work is.** People, machines, infrastructure,
taste and one folder per project. The list is a line in the configuration:
delete what you do not need, add what you do.

**You can edit it by hand, and nothing is repaired quietly.** Correct an entry in
any editor, the way you would any other Markdown file. Whatever then cannot be
read or validated is reported by name and left alone. A tool that tidies your
notes behind your back is a tool you have to re-read, and then it saved you
nothing.

**The format is open.** Entries follow
[Open Knowledge Format v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format),
so the vault is readable by anything that speaks it, and everything specific to
Mabolo lives in one namespaced block you can ignore.

**One commit per change.** Git is the history, so `git revert` is the way back,
and `git log` tells you when a belief entered the memory.

## Several machines, no service of ours

A vault is a Git repository, so keeping two machines in step is the thing Git is
already good at. Point them at a bare repository on any host you can reach over
SSH, a small server of your own, a box at home, anything that runs `git`, and
each machine keeps its own clone.

```bash
mabolo init --remote git@your-host:mabolo-data.git
```

The far end runs `git` and nothing else. No process of ours lives there, so
there is nothing on that host to attack, to update or to pay for. Your vault can
equally stay on one machine and never leave it: a remote is one answer in
`init`, not the price of entry.

Today the syncing itself is plain `git pull` and `git push` in the vault, and
the tool only wires up the remote. Doing that safely while two sessions write at
the same time is being built.

## What works today

This is early, and the README says only what exists:

* `mabolo init` creates a vault, wires up a configuration, prints its plan
  before touching anything, and is safe to run twice.
* `mabolo validate` checks a vault against the format and says what is wrong in
  plain words, separating what breaks from what merely deserves a look.
* `mabolo eval` runs your own questions against your own vault and reports which
  entries came back, in which order, whether the memory stayed quiet when it
  should, and what a preview would cost. It compares every run against a saved
  baseline and fails when an answer slips down the list, before it disappears.
* The entry schema and its validator, with the example vault as the reference.
* A Git remote, if you want one: `init` sets it, and the vault is an ordinary
  repository you can pull and push yourself.

The MCP server and the context an agent actually receives are being built, the
latter against a budget the memory has to stay inside: a session should pay for
a short index, not for everything you ever wrote down. The measurement came
first on purpose, because it is the only thing that can show that a short index
replaced the long text instead of quietly losing half of it.

## Try it

```bash
git clone https://github.com/hoffmannroman/mabolo-memory.git
cd mabolo-memory
uv sync
uv run mabolo validate examples/vault
uv run mabolo eval examples/vault --explain

# A throwaway vault and a throwaway configuration to go with it. Without
# --config, `init` writes the real one in your config directory.
uv run mabolo --config /tmp/mabolo-demo.toml init --vault /tmp/my-vault --yes
```

## Limits

* **Linux and macOS.** Windows is not supported. File names are normalised to
  NFC on write, so an entry keeps one identity across sync tools.
* **A vault belongs to one person.** There is no team mode and no cloud.
* **The approval gate holds for Mabolo's own tools.** Any program on your
  machine can still edit a Markdown file.
* **The search index is SQLite**, which is a database. It is derived from the
  files, built in memory and never stored, so there is nothing to delete and
  nothing that can disagree with what is on disk.
* **The search is words, not meaning.** A question asked with synonyms, or in a
  different language from the entry, falls through. Measuring that honestly is
  what `mabolo eval` is for.

## It stays yours

Uninstalling leaves the vault where it is. It is a folder of Markdown in a Git
repository: it works without this tool, it is readable without this tool, and
nothing in it is locked to it.

MIT. Code and documentation in English; the content of a vault is whatever
language you think in.
