"""The index. What is asserted here is what the eval measures against."""

import pytest

from conftest import entry_text
from mabolo import frontmatter, query
from mabolo.errors import MaboloError
from mabolo.index import Index, entry_line, estimate_tokens
from mabolo.schema import Entry, MaboloBlock


def build(vault, entries: dict[str, str], area: str = "infra") -> Index:
    """Write entries into a vault and index it, the way a command would.

    Each entry lands in the folder its own frontmatter names, so a test about a
    project area does not have to say where the file goes twice.
    """
    for name, text in entries.items():
        meta, _ = frontmatter.parse(text)
        declared = (meta or {}).get("mabolo", {}).get("area", area)
        folder = vault.area_dir(declared)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
    assert vault.validate().ok, vault.validate().render(vault.root)
    return Index.build(vault.entries())


def test_a_word_from_the_title_finds_the_entry(vault):
    index = build(vault, {
        "deploy-from-main": entry_text(title="Deploy from main only", body="Releases are cut from main."),
        "gateway": entry_text(title="Gateway", body="Terminates TLS and routes onwards."),
    })
    hits = index.search("where are releases cut from")
    assert [h.name for h in hits] == ["deploy-from-main"]


def test_singular_and_plural_find_the_same_entry(vault):
    index = build(vault, {
        "release-notes": entry_text(title="Release notes", body="Every release carries notes."),
    })
    assert index.search("release notes")[0].name == "release-notes"
    assert index.search("releases and their notes")[0].name == "release-notes"


def test_a_title_counts_for_more_than_a_body(vault):
    index = build(vault, {
        "in-the-title": entry_text(title="Nightly snapshot", body="Something else entirely here."),
        "in-the-body": entry_text(title="Something else", body="A nightly snapshot is taken here."),
    })
    assert [h.name for h in index.search("nightly snapshot")][0] == "in-the-title"


def test_one_matching_word_and_no_name_is_not_a_hit(vault):
    """Relevance is absolute. The best of a bad lot still reads like an answer."""
    index = build(vault, {
        "backups": entry_text(title="Backups", body="A snapshot every night."),
    })
    assert index.search("what does a snapshot cost in a database").hits == ()


def test_one_matching_word_plus_a_known_name_is_a_hit(vault):
    index = build(vault, {
        "backups": entry_text(title="Backups", body="A snapshot every night."),
    })
    assert [h.name for h in index.search("backups snapshot")] == ["backups"]


def test_a_question_of_only_stop_words_returns_nothing(vault):
    index = build(vault, {"backups": entry_text(title="Backups", body="A snapshot every night.")})
    assert index.search("what about that").hits == ()
    assert index.search("").hits == ()


def test_an_alias_resolves_to_its_entry(vault):
    index = build(vault, {
        "secrets-in-the-password-store": entry_text(
            title="Secrets live in the password store",
            body="Keys and tokens are kept there.",
            mabolo={"aliases": ["secrets", "password store"]},
        ),
    })
    assert index.resolve("secrets").name == "secrets-in-the-password-store"
    assert index.resolve("Password Store").name == "secrets-in-the-password-store"
    assert index.resolve("nothing-like-this") is None


def test_an_alias_is_searched_as_well(vault):
    index = build(vault, {
        "long-and-unmemorable-name": entry_text(
            title="Something", body="Nothing quotable in here.",
            mabolo={"aliases": ["kestrel"]},
        ),
    })
    assert [h.name for h in index.search("kestrel")] == ["long-and-unmemorable-name"]


def test_a_query_made_of_fts5_grammar_is_read_as_words(vault):
    index = build(vault, {"backups": entry_text(title="Backups", body="A snapshot every night.")})
    # Unquoted, each of these is grammar rather than words: the first would be a
    # syntax error and the second a search for something the person did not ask
    # for. A question is words.
    assert index.search('"(*)" AND -- OR ^').hits == ()
    assert index.search('backups NEAR "snapshot*" (x)')[0].name == "backups"
    # This one does reach SQLite: every word survives the pipeline, and the
    # grammar around them has to arrive as words or not at all.
    assert index.search('snapshot OR night NEAR backups')[0].name == "backups"


def test_two_entries_with_one_name_are_refused(tmp_path):
    entries = [
        Entry(type="reference", title="One", mabolo=MaboloBlock(area="infra"), path=tmp_path / "a" / "x.md"),
        Entry(type="reference", title="Two", mabolo=MaboloBlock(area="infra"), path=tmp_path / "b" / "x.md"),
    ]
    with pytest.raises(MaboloError, match="belong to more than one entry"):
        Index.build(entries)


def test_entries_that_score_the_same_are_ordered_by_name(vault):
    """The tie break, proved by handing the index its documents out of order.

    Building through `Index.build` cannot prove it: the rowids are handed out in
    name order, so SQLite returns the rows already sorted and the test stays
    green with the tie break removed. The documents go in backwards here.
    """
    entries = {
        name: entry_text(title="Nightly snapshot", body="Identical body text.")
        for name in ("one", "two", "three")
    }
    index = build(vault, entries)
    backwards = Index(list(reversed(index.documents)))
    assert [h.name for h in backwards.search("nightly snapshot")] == ["one", "three", "two"]
    assert [h.name for h in index.search("nightly snapshot")] == ["one", "three", "two"]


def test_the_limit_is_what_comes_back(vault):
    entries = {
        f"entry-{i}": entry_text(title="Nightly snapshot", body="A snapshot every night.")
        for i in range(8)
    }
    index = build(vault, entries)
    assert len(index.search("nightly snapshot")) == 5
    assert len(index.search("nightly snapshot", limit=2)) == 2


def test_a_hit_says_which_words_it_matched(vault):
    index = build(vault, {"backups": entry_text(title="Backups", body="A snapshot every night.")})
    hit = index.search("backups snapshot")[0]
    # Stems, not words: what is compared on both sides of the search.
    assert set(hit.matched) == {"backup", "snapshot"}
    assert hit.rank == 1


def test_a_preview_costs_something_and_says_so_as_an_estimate(vault):
    index = build(vault, {
        "backups": entry_text(
            title="Backups", description="A snapshot every night", body="Text."
        ),
    })
    hits = index.search("backups snapshot")
    assert index.preview(hits) == "- backups: A snapshot every night"
    assert index.preview_cost(hits) == estimate_tokens(index.preview(hits)) > 0
    assert estimate_tokens("") == 0


def test_an_alias_of_several_words_still_counts_as_naming_the_entry(vault):
    """A token can never equal `on call`, and an alias nobody can name is not one.

    The alias is two words of which only one is searchable, so the question
    matches a single word and gets in on the name alone.
    """
    index = build(vault, {
        "who-answers-out-of-hours": entry_text(
            title="Out of hours", description="Who answers at night",
            body="Nothing else quotable in here.",
            mabolo={"aliases": ["on call"]},
        ),
    })
    assert [h.name for h in index.search("who is on call tonight?")] == ["who-answers-out-of-hours"]


def test_a_named_entry_lowers_the_floor_for_itself_and_not_for_everything(vault):
    """The name is evidence about the entry it names, and about nothing else.

    A global "some name was mentioned" let a question about one project return
    an unrelated entry that shared one common word with it.
    """
    index = build(vault, {
        "atlas-uses-postgres": entry_text(
            area="project/atlas", title="Atlas stores everything in Postgres",
            body="One Postgres database, and no second store."),
        "atlas-tone": entry_text(
            area="project/atlas", title="Plain sentences", body="Copy says what the thing does."),
        "reviews-need-a-diff": entry_text(
            title="A review starts from a diff", body="A summary of a change does not."),
    })
    names = [h.name for h in index.search("which database does Atlas use?")]
    assert "atlas-uses-postgres" in names
    # `reviews-need-a-diff` contains `does` and nothing else of the question.
    assert "reviews-need-a-diff" not in names


def test_naming_a_project_is_a_statement_about_its_entries(vault):
    index = build(vault, {
        "atlas-uses-postgres": entry_text(
            area="project/atlas", title="Postgres", body="One database."),
    })
    assert [h.name for h in index.search("atlas database")] == ["atlas-uses-postgres"]


def test_an_entry_can_be_found_by_a_name_too_short_to_become_a_word(vault):
    """`ci` is a legal entry name and shorter than the shortest searchable word."""
    index = build(vault, {
        "ci": entry_text(title="CI", description="The continuous integration host",
                         body="Runs the pipeline."),
    })
    hit = index.search("ci")[0]
    assert hit.name == "ci" and hit.matched == () and hit.named == ("ci",)


def test_fts5_and_the_relevance_check_agree_on_what_a_word_is(vault):
    """The two sides of the search have to spell a word the same way.

    FTS5 strips diacritics unless told not to, and the relevance check never
    did. A question spelled without the accents matched inside SQLite and was
    then thrown away again by the floor, which is the one thing feeding FTS5 our
    own tokens was supposed to make impossible. From the outside the result is
    the same either way, because the floor is the stricter of the two; the
    disagreement shows in what BM25 scored on the way. So this asserts the
    invariant where it lives, on the candidates SQLite hands over.
    """
    index = build(vault, {
        "hiring-notes": entry_text(
            title="How we read a résumé", description="What matters in a résumé here",
            body="A résumé is read for what it says."),
    })
    parsed = query.build("resume")
    rows = index._db.execute(
        "SELECT rowid FROM entries WHERE entries MATCH ?", (query.to_match(parsed),)
    ).fetchall()
    assert rows == [], "SQLite matched a word the relevance check cannot see"
    assert [h.name for h in index.search("résumé matters")] == ["hiring-notes"]
    assert index.search("resume matters").hits == ()


def test_two_entries_that_answer_to_one_name_are_refused(vault):
    """Folded, because folded is the spelling a question is compared against."""
    for name, alias in (("one", "Kestrel"), ("two", "kestrel")):
        (vault.root / "infra" / f"{name}.md").write_text(
            entry_text(title="T", body="text", mabolo={"aliases": [alias]}), encoding="utf-8")
    with pytest.raises(MaboloError, match="kestrel"):
        Index.build(vault.entries())


def test_a_search_says_what_it_parsed_and_what_it_recognised(vault):
    """One object, so that an explanation cannot describe a different search."""
    index = build(vault, {
        "backups": entry_text(title="Backups", body="A snapshot every night."),
    })
    found = index.search("backups snapshot")
    assert found.query.stems == ("backup", "snapshot")
    assert found.named == ("backups",)
    assert found[0].named == ("backups",)


def test_an_index_can_be_closed(vault):
    index = build(vault, {"backups": entry_text(title="Backups", body="Text.")})
    with index:
        assert len(index) == 1
    with pytest.raises(Exception):
        index.search("backups")


# What the session index reads off a document


def test_a_document_carries_the_pin_and_the_moment_it_was_touched(vault):
    index = build(vault, {
        "pinned": entry_text(
            mabolo={"pin": True},
            generated={"by": "mabolo/0.1.0", "at": "2026-09-01T10:00:00+03:00"},
        ),
        "plain": entry_text(generated={"by": "mabolo/0.1.0", "at": "2026-09-01T10:00:00+03:00"}),
    })
    by_name = {d.name: d for d in index.documents}
    assert by_name["pinned"].pin is True
    assert by_name["plain"].pin is False
    # Normalised to UTC on the way in, because that is the value the session
    # index compares and sorts by.
    assert by_name["plain"].at.isoformat() == "2026-09-01T07:00:00+00:00"


def test_a_search_preview_line_cannot_forge_a_line_either(vault):
    """The session index learned this first, and the preview was the way back
    in: both build an entry's line through the same function now."""
    index = build(vault, {
        "sneaky": entry_text(
            title="t",
            description="harmless",
            body="text",
        ),
    })
    line = entry_line("bad\nname", "first\n\n## forged (9 entries)", "t")
    assert "\n" not in line
    assert line == "- bad name: first ## forged (9 entries)"
    assert entry_line("e", "", "A title") == "- e: A title"
    assert entry_line("e", "", "") == "- e: e"
