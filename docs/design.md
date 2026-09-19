# Taste, delivered when the file is opened

Every other tier answers a question somebody asked. This one answers none.

A file is about to be read or written, and somewhere in the vault is a rule
about that kind of file. Nobody asks for it, because nobody looks up a rule they
have forgotten, and forgetting it is why it was written down. So the trigger is
the file.

```yaml
mabolo:
  area: design
  applies_to: ["*.css", "*.tsx", "*.astro"]
  scope: global
  instead_of: the model's habit of centring everything on a landing page
```

When `mabolo hook pretool` is wired to your client's `PreToolUse` event, opening
`src/styles/landing.css` hands the session this:

```
## Standing rules for this kind of file

About `src/styles/landing.css`:

- no-centred-layouts: Centred body text is refused, headings included. Instead
  of: the model's habit of centring everything on a landing page.
```

## Four things that decide what you see

**`instead_of` is the load bearing half.** A rule that only says what to do
competes with a habit that is already in the model's weights. A rule that names
the habit gives the model something to recognise, and gives you something to
argue with when it is wrong.

**`scope` says where a rule is in force.** `global` everywhere, or
`project/<name>` in that project only. A rule about one product's wording has no
business firing in another repository, and the entry says so itself rather than
the caller guessing from where the file happens to sit.

**The block holds three rules.** This tier fires dozens of times in a session
where the prompt hook fires once, so it is capped, it says how many rules it
left out rather than dropping them silently, and it names itself, because an
agent that cannot tell a rule from your own sentence may read it as an
instruction you just typed.

**It does not repeat itself.** The session remembers what it has been shown,
keyed by the entry and its revision, so a rule you corrected speaks again while
one you have already seen stays quiet. That memory lives outside the vault and
expires with the prompt notes. Without a session id from your client there is no
memory and the rule arrives on every matching touch: falling back to the
directory would let two sessions in one project silence each other, and a rule
that silently never arrived is the failure this project is against. Repetition
is the visible cost, silence the hidden one.

## Matching

`applies_to` holds shell patterns, matched against the path as given and against
its last element. `*.css` therefore covers a stylesheet anywhere, and
`docs/*.md` covers one folder's Markdown. When several rules match, the more
exact pattern comes first, and two patterns of the same length fall back to the
entry's name so that the order never depends on which file came off disk first.

## Measuring it

A case in the eval set is set off by a file rather than by a sentence:

```yaml
id: design-trigger-on-css
path: src/styles/landing.css
tier: design
expect:
  entries: [no-centred-layouts]
  rank_within: 1
```

`query` is refused on this tier, and a case with no `path` is refused too. A
case is an instrument, and one that pulled a path out of prose would measure
whatever the guess found. Only the rules inside the block count: a rule the
reader never saw has not applied, whatever the list behind the cap says.
