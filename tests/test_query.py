"""The query pipeline. Every assertion here is a rule the ranking depends on."""

import pytest

from mabolo import query


def test_a_name_survives_tokenising_in_one_piece():
    assert query.tokenise("deploy-from-main and example.dev") == [
        "deploy-from-main",
        "and",
        "example.dev",
    ]


def test_folding_makes_one_spelling_out_of_several():
    assert query.tokenise("Straße STRASSE") == ["strasse", "strasse"]


def test_punctuation_around_a_word_is_not_part_of_it():
    assert query.tokenise("(centred), -- yes.") == ["centred", "yes"]


def test_stop_words_are_dropped_in_english():
    assert "the" not in query.stems_of("the release from the tag")


def test_stop_words_are_dropped_in_the_vault_language_and_in_english():
    stems = query.stems_of("das ist the build", language="de")
    assert "das" not in stems and "ist" not in stems and "the" not in stems
    assert stems == ["build"]


def test_an_unknown_language_still_drops_the_english_stop_words():
    assert query.stems_of("the build", language="xx") == ["build"]


def test_a_word_shorter_than_three_characters_is_not_searched():
    assert query.stems_of("a j4 build") == ["build"]


def test_a_suffix_is_only_cut_while_four_characters_remain():
    assert query.stem("keys") == "keys"  # cutting the s would leave three
    assert query.stem("working") == "work"


def test_a_stem_may_be_shorter_than_the_word_the_entry_uses():
    # `releases` loses `es` and not just `s`, so the stem is not a word. That is
    # fine and deliberate: the index matches a stem as a prefix, so a stem that
    # is too short still finds both spellings, while one that is too long finds
    # neither. `test_singular_and_plural_find_the_same_entry` is the other half.
    assert query.stem("releases") == "releas"


def test_a_suffix_is_cut_once_and_not_repeatedly():
    # `addressed` -> `address`, not `addr`, which would match half a vault.
    assert query.stem("addressed") == "address"


def test_a_hyphenated_name_is_also_searched_as_a_phrase():
    parsed = query.build("what about deploy-from-main?")
    assert "deploy from main" in parsed.phrases
    assert {"deploy", "main"} <= set(parsed.stems)


def test_a_single_word_produces_no_phrase():
    assert query.build("release").phrases == ()


def test_a_question_made_only_of_stop_words_searches_for_nothing():
    assert query.build("what is that").is_empty
    assert query.build("").is_empty


def test_the_match_expression_joins_the_terms_with_or():
    match = query.to_match(query.build("release tag"))
    assert match == '"release"* OR "tag"*'


def test_grammar_characters_never_reach_the_match_expression():
    # `*`, `"`, `(` and `:` are FTS5 grammar, and a person's question is words.
    match = query.to_match(query.build('release* OR NOT "tag" (x) col:y'))
    assert match == '"release"* OR "tag"* OR "col"*'


def test_the_pipeline_is_a_pure_function_of_its_arguments():
    first = query.build("nightly snapshot", language="de")
    second = query.build("nightly snapshot", language="de")
    assert first == second


def test_a_german_question_drops_the_pronoun_the_person_starts_it_with():
    """A memory about one person is asked questions that begin with "ich".

    The German list held `wir uns ihr euch sie man` and none of the singular
    pronouns, so the word every such question starts with counted as evidence
    about whatever entry happened to contain it. A pronoun paradigm is filled
    in whole or not at all.
    """
    stems = query.stems_of("nehme ich abends ein Buch", language="de")
    assert "ich" not in stems
    assert stems == ["nehm", "abends", "buch"]


@pytest.mark.parametrize("word", ["welchen", "welchem", "dieses", "diesem", "diesen",
                                  "seiner", "seines", "ihrem", "ihren", "aller", "allen"])
def test_a_determiner_is_a_stop_word_in_every_form_or_in_none(word):
    """The quieter half of the same bug.

    The list held `welche welcher welches` and not `welchen`, so the form the
    person happened to type was evidence about whatever entry contained it and
    the other two were not. Which form a question uses is grammar, not meaning.
    """
    assert query.is_stopword(word, "de"), word


def test_a_stop_word_with_an_umlaut_is_one_in_both_of_its_spellings():
    """`fold` normalises and case folds; it does not transliterate.

    So `fuer` and `für` are two different words to everything downstream, and
    only this list can say they are the same one. A list that held the umlaut
    spelling alone stopped whichever half of them the person did not type.
    """
    for pair in (("für", "fuer"), ("über", "ueber")):
        for spelling in pair:
            assert query.is_stopword(spelling, "de"), spelling
    assert query.stems_of("fuer den build ueber nacht", language="de") == ["build", "nacht"]
