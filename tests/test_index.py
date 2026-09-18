"""The index. What is asserted here is what the eval measures against."""

import pytest

from conftest import entry_text
from mabolo.errors import MaboloError
from mabolo.index import Index, estimate_tokens
from mabolo.schema import Entry, MaboloBlock


def build(vault, entries: dict[str, str], area: str = "infra") -> Index:
    """Write entries into a vault and index it, the way a command would."""
    for name, text in entries.items():
        (vault.root / area / f"{name}.md").write_text(text, encoding="utf-8")
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
    assert index.search("what does a snapshot cost in a database") == []


def test_one_matching_word_plus_a_known_name_is_a_hit(vault):
    index = build(vault, {
        "backups": entry_text(title="Backups", body="A snapshot every night."),
    })
    assert [h.name for h in index.search("backups snapshot")] == ["backups"]


def test_a_question_of_only_stop_words_returns_nothing(vault):
    index = build(vault, {"backups": entry_text(title="Backups", body="A snapshot every night.")})
    assert index.search("what about that") == []
    assert index.search("") == []


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
    assert index.search('"(*)" AND -- OR ^') == []
    assert index.search('backups NEAR "snapshot*" (x)')[0].name == "backups"


def test_two_entries_with_one_name_are_refused(tmp_path):
    entries = [
        Entry(type="reference", title="One", mabolo=MaboloBlock(area="infra"), path=tmp_path / "a" / "x.md"),
        Entry(type="reference", title="Two", mabolo=MaboloBlock(area="infra"), path=tmp_path / "b" / "x.md"),
    ]
    with pytest.raises(MaboloError, match="more than one entry called x"):
        Index.build(entries)


def test_the_same_files_always_produce_the_same_order(vault):
    entries = {
        "one": entry_text(title="Nightly snapshot", body="Identical body text."),
        "two": entry_text(title="Nightly snapshot", body="Identical body text."),
        "three": entry_text(title="Nightly snapshot", body="Identical body text."),
    }
    first = [h.name for h in build(vault, entries).search("nightly snapshot")]
    second = [h.name for h in Index.build(vault.entries()).search("nightly snapshot")]
    # Three entries that cannot be told apart by score are still ordered, and
    # ordered by name, or the eval would report a different rank every run.
    assert first == second == ["one", "three", "two"]


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
