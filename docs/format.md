# The entry format

A vault is a folder of Markdown files in a Git repository. Every file that is
not a reserved name is one entry: a YAML frontmatter block, then prose.

Entries follow [Open Knowledge Format v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format).
Everything Mabolo adds lives under a namespaced `mabolo` block, so a reader that
only knows the format can ignore it.

A filled in vault to read alongside this page is in [`examples/vault`](../examples/vault).

## Fields

| Field | Required | What it holds |
|---|---|---|
| `type` | yes | The only field the format requires. Mabolo uses `user`, `feedback`, `project`, `reference`; another value is allowed and only warned about |
| `title` | recommended | One line, what the entry is called in a list |
| `description` | yes, here | One line, and the line an index and a search are built from. Mabolo treats a missing one as an error even though the format allows it |
| `generated` | optional | `by` and `at`: who produced the entry and when. `by` is `producer/version`, `human:<id>` or `process:<id>`. The later of this `at` and any `verified.at` is what counts as when the entry was last touched |
| `verified` | optional | A list of `by` and `at`. A single mapping is read as a list of one and written back as a list. Mabolo only ever writes `human:<id>` here: a person is what verification means |
| `sources` | optional | Where a claim came from. `resource` is required, `id` is what a footnote in the body cites |
| `resource` | optional | What this entry is about, when it is about one thing: a path, a URL, an identifier |
| `tags` | optional | A list of words that group entries. Every one of them is text. The search does not read them today: it looks at the name, the title, the aliases, the description and the body |
| `usage_window` | optional | When the knowledge in the entry applies, as the format defines it |
| `status` | optional | `draft`, `stable` or `deprecated`. Defaults to `stable` |
| `stale_after` | optional | An ISO-8601 timestamp with an offset, after which the entry wants a look |
| `mabolo.area` | yes | Which folder the entry belongs to, and it has to match the folder it is in |
| `mabolo.anchor` | optional | A path inside a project. When that path moves, the entry is worth checking |
| `mabolo.pin` | optional | `true` keeps an entry in the short index a session starts with, ahead of the active project and of whatever changed this week, and it is the last thing a tight budget cuts |
| `mabolo.aliases` | optional | Names an entry also answers to, for instance after a rename |

## Areas

An area is a folder. The ones a new vault starts with:

| Area | What goes in |
|---|---|
| `persona` | Who the person is and how they want to be worked with |
| `hosts` | One profile per machine |
| `infra` | What holds across machines: network, services, accounts |
| `design` | Taste: rules that override a default when a file is touched |
| `project/<name>` | One folder per project, created when it is first used |

The list is a line in the configuration. Delete what you do not need, add what
you do. Project areas are created on demand and are not listed there.

## Design entries

Taste is a kind of knowledge, and it is only useful at the moment it applies. A
design entry therefore carries two fields the other areas do not need:

```yaml
mabolo:
  area: design
  scope: global                     # global or project/<name>
  applies_to: ["*.css", "*.tsx", "*.astro", "*.md"]
  instead_of: "the model's habit of centring everything on a landing page"
```

`applies_to` decides when the entry is loaded: when a file it matches is touched,
rather than at the start of every session. `instead_of` names the default the
rule overrides, which is what makes it checkable later.

## Quotes are footnotes

An entry that makes three claims cannot carry one quote field. Each claim points
at an id in `sources` with an ordinary Markdown footnote:

```markdown
Releases are cut from `main`.[^s1]

[^s1]: "we deploy from main only, never from a tag"
```

## Reserved files

| File | Rules |
|---|---|
| `index.md` | One per folder, derived from the entries and rewritten by Mabolo. Only the one in the vault root carries frontmatter: `okf_version`, which it has to carry because it says which version of the format the rest of the vault is read as, and a `mabolo` block whose one key is `language` |
| `log.md` | The journal. No frontmatter, one ISO date heading per day, newest first |

Both are generated, so an entry may not be called `index` or `log`.

Mabolo only ever replaces an `index.md` it could have written itself: one with
no frontmatter, or the root one with nothing but `okf_version`. Anything else in
a file of that name is reported and left alone, because a file Mabolo did not
write is a file somebody else did.

```yaml
---
okf_version: '0.2'
mabolo:
  language: de
---
```

`language` is the language the entries are written in, as a short code, and it
belongs to the vault rather than to a machine: the search drops stop words in it
and cuts suffixes by its rules, so a vault that carried its language in a local
configuration file would answer differently on two clones of itself. A vault
that says nothing is read as English.

One folder in a vault is not made of entries. `.mabolo/` holds what is derived
and disposable and is ignored by Git, with one exception: `.mabolo/eval/` holds
the questions the memory is measured with, and those are versioned alongside the
entries they query. See [measuring the memory](eval.md).

## What makes an entry recent

The session index offers what changed in the last seven days, and it reads that
off the entry itself: the later of `generated.at` and any `verified.at`. Not the
file's modification time, which is not part of a commit and would differ in a
fresh clone. A timestamp without an offset is read as UTC, so the same commit
sorts the same way in every time zone. An entry that gives no time at all is
never recent, though a pin or an active project still brings it in.

## Links

Links between entries are relative Markdown links, so they work in an editor, in
a Git host's file view and in anything that renders Markdown. `[[wikilinks]]`
are reported rather than rewritten: they break everywhere outside the editor
that invented them.

## What the validator checks

`mabolo validate <vault>` separates two levels, and the difference is the point:

* an **error** means the entry cannot be trusted to behave, for instance a
  missing `type`, an area that does not match its folder, or a `verified` field
  written by something that is not a person,
* a **warning** means a person should look, for instance a description over the
  budget, a footnote without a source, or a link that points at nothing.

Nothing is ever repaired, and nothing is skipped in silence: a folder that
cannot be read is an error of its own rather than a gap in the count, because a
run over half a vault must not look like a clean one.

Findings are named with the file, the field and a code you can grep for.

An entry read from disk and written back keeps what the reader could not
interpret. A malformed `generated` block is reported, not dropped, and writing
an entry that has errors is refused: a read, a small change and a write must
never turn into a silent repair of the rest of the file.
