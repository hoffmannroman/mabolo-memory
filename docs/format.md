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
| `generated` | optional | `by` and `at`: who produced the entry and when. `by` is `producer/version`, `human:<id>` or `process:<id>` |
| `verified` | optional | A list of `by` and `at`. Mabolo only ever writes `human:<id>` here: a person is what verification means |
| `sources` | optional | Where a claim came from. `resource` is required, `id` is what a footnote in the body cites |
| `status` | optional | `draft`, `stable` or `deprecated`. Defaults to `stable` |
| `stale_after` | optional | An ISO-8601 timestamp with an offset, after which the entry wants a look |
| `mabolo.area` | yes | Which folder the entry belongs to, and it has to match the folder it is in |
| `mabolo.anchor` | optional | A path inside a project. When that path moves, the entry is worth checking |
| `mabolo.pin` | optional | `true` keeps an entry in the short index a session starts with |
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
| `index.md` | One per folder, derived from the entries and rewritten by Mabolo. Only the one in the vault root carries frontmatter, and only `okf_version` |
| `log.md` | The journal. No frontmatter, one ISO date heading per day, newest first |

Both are generated, so an entry may not be called `index` or `log`.

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

Nothing is ever repaired. Findings are named with the file, the field and a code
you can grep for.
