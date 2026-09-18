import datetime as dt

from mabolo.schema import Entry, Generated, MaboloBlock, Source, area_is_known, is_actor, iso, parse_time


def test_an_entry_round_trips_through_the_frontmatter_mapping():
    meta = {
        "type": "reference",
        "title": "Deploy from main only",
        "description": "Releases are cut from main",
        "sources": [{"id": "s1", "resource": "session://2026-08-14"}],
        "generated": {"by": "claude-code/2.1.84", "at": "2026-08-14T10:22:00+03:00"},
        "verified": [{"by": "human:alex", "at": "2026-08-14T10:25:00+03:00"}],
        "status": "stable",
        "stale_after": "2027-02-01T00:00:00+03:00",
        "mabolo": {"area": "infra", "anchor": ".github/workflows/ci.yml", "pin": True},
    }
    assert Entry.from_meta(meta).to_meta() == meta


def test_unknown_fields_survive_a_round_trip_instead_of_being_dropped():
    meta = {"type": "reference", "mabolo": {"area": "infra", "legacy": {"host": "somewhere"}},
            "house_rule": "keep me"}
    out = Entry.from_meta(meta).to_meta()
    assert out["house_rule"] == "keep me"
    assert out["mabolo"]["legacy"] == {"host": "somewhere"}


def test_a_single_verification_mapping_is_read_as_a_list():
    entry = Entry.from_meta({"type": "reference", "verified": {"by": "human:x", "at": "2026-01-01T00:00:00+00:00"}})
    assert [v.by for v in entry.verified] == ["human:x"]


def test_malformed_values_become_nothing_rather_than_an_exception():
    entry = Entry.from_meta({"type": "reference", "sources": "not a list", "verified": 7,
                             "mabolo": "not a mapping"})
    assert entry.sources == [] and entry.verified == [] and entry.mabolo.area == ""


def test_the_actor_convention_from_the_spec():
    assert is_actor("claude-code/2.1.84")
    assert is_actor("human:someone")
    assert is_actor("process:mabolo-import")
    assert not is_actor("someone")
    assert not is_actor("Human:Someone")


def test_project_areas_are_recognised_without_being_configured():
    assert area_is_known("persona")
    assert area_is_known("project/atlas")
    assert area_is_known("project/example.dev")
    assert not area_is_known("project/")
    assert not area_is_known("elsewhere")


def test_times_are_normalised_to_iso_strings():
    assert iso(dt.date(2026, 9, 18)) == "2026-09-18"
    assert iso(dt.datetime(2026, 9, 18, 4, 30, 0)) == "2026-09-18T04:30:00"
    assert parse_time("2026-09-18T04:30:00Z").tzinfo is not None
    assert parse_time("no") is None


def test_footnotes_are_read_out_of_the_body_because_that_is_where_quotes_live():
    entry = Entry.from_meta(
        {"type": "reference"},
        body='A claim.[^s1] Another.[^s2]\n\n[^s1]: "he said this"\n',
    )
    assert entry.footnote_ids() == {"s1"}
    assert entry.footnote_refs() == {"s2"}


def test_a_design_block_keeps_its_own_fields():
    block = MaboloBlock.from_meta(
        {"area": "design", "scope": "global", "applies_to": ["*.css"], "instead_of": "the default"}
    )
    assert block.to_meta()["applies_to"] == ["*.css"]
    assert block.to_meta()["instead_of"] == "the default"


def test_sources_and_generated_refuse_to_be_built_from_half_a_mapping():
    assert Source.from_meta({"id": "s1"}) is None
    assert Generated.from_meta({"by": "mabolo/0.1.0"}) is None
    assert Generated.from_meta({"by": "mabolo/0.1.0", "at": "2026-01-01T00:00:00+00:00"}) is not None


def test_a_source_keeps_fields_this_version_does_not_know():
    meta = {
        "type": "reference",
        "title": "t",
        "description": "d",
        "sources": [{"id": "s1", "resource": "session://x", "quote": "what was said"}],
        "mabolo": {"area": "infra"},
    }
    assert Entry.from_meta(meta).to_meta()["sources"][0]["quote"] == "what was said"


def test_a_type_that_is_not_text_does_not_become_the_word_none():
    assert Entry.from_meta({"type": None}).type == ""
