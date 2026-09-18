"""The eval harness: what it scores, what it refuses to score, and what it guards."""

import json

import pytest

from conftest import entry_text
from mabolo import evaluate
from mabolo.errors import MaboloError
from mabolo.index import Index


def case_file(vault, name: str, text: str):
    vault.eval_dir.mkdir(parents=True, exist_ok=True)
    path = vault.eval_dir / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def small_vault(vault):
    (vault.root / "infra" / "deploy-from-main.md").write_text(
        entry_text(
            title="Deploy from main only",
            description="Releases are cut from main, tags are labels",
            body="Releases are cut from main. A tag marks what shipped.",
        ),
        encoding="utf-8",
    )
    (vault.root / "infra" / "backups-run-nightly.md").write_text(
        entry_text(title="Backups", description="A snapshot every night", body="Restored monthly."),
        encoding="utf-8",
    )
    return Index.build(vault.entries())


# Reading a case


def test_a_case_is_read_with_everything_it_says(tmp_path):
    case = evaluate.parse_case(
        {
            "id": "deploy-source",
            "query": "can we release from a tag?",
            "expect": {"entries": ["deploy-from-main"], "rank_within": 3, "must_cite": True},
            "tier": "index",
        },
        tmp_path / "c.yaml",
    )
    assert case.entries == ("deploy-from-main",) and case.rank_within == 3
    assert case.must_cite and case.measurable and not case.silence


def test_a_case_without_a_tier_is_an_index_case(tmp_path):
    case = evaluate.parse_case(
        {"id": "x", "query": "q", "expect": {"entries": ["a"]}}, tmp_path / "c.yaml"
    )
    assert case.tier == "index" and case.rank_within == evaluate.DEFAULT_RANK_WITHIN


def test_a_key_nobody_knows_is_refused_rather_than_ignored(tmp_path):
    """An instrument that ignores a setting reports a number about something else."""
    with pytest.raises(MaboloError, match="unknown key"):
        evaluate.parse_case(
            {"id": "x", "query": "q", "expect": {"entries": ["a"]}, "rank": 2}, tmp_path / "c.yaml"
        )
    with pytest.raises(MaboloError, match="unknown key"):
        evaluate.parse_case(
            {"id": "x", "query": "q", "expect": {"entries": ["a"], "top": 2}}, tmp_path / "c.yaml"
        )


def test_a_case_that_expects_nothing_at_all_is_refused(tmp_path):
    with pytest.raises(MaboloError, match="expects nothing"):
        evaluate.parse_case({"id": "x", "query": "q", "expect": {}}, tmp_path / "c.yaml")


def test_a_case_either_names_entries_or_asserts_silence(tmp_path):
    with pytest.raises(MaboloError, match="not both"):
        evaluate.parse_case(
            {"id": "x", "query": "q", "expect": {"entries": ["a"], "silence": True}},
            tmp_path / "c.yaml",
        )


def test_an_unknown_tier_is_refused(tmp_path):
    with pytest.raises(MaboloError, match="tier is one of"):
        evaluate.parse_case(
            {"id": "x", "query": "q", "expect": {"entries": ["a"]}, "tier": "vibes"},
            tmp_path / "c.yaml",
        )


def test_two_cases_with_one_id_are_refused(vault):
    case_file(vault, "one", "id: same\nquery: q\nexpect:\n  entries: [a]\n")
    case_file(vault, "two", "id: same\nquery: q\nexpect:\n  entries: [b]\n")
    with pytest.raises(MaboloError, match="both call themselves"):
        evaluate.load_cases(vault.eval_dir)


def test_a_case_file_that_cannot_be_read_stops_the_run(vault):
    case_file(vault, "broken", "id: x\nquery: [unclosed\n")
    with pytest.raises(MaboloError, match="cannot be read as a case"):
        evaluate.load_cases(vault.eval_dir)


def test_the_baseline_is_not_mistaken_for_a_case(vault):
    case_file(vault, "one", "id: one\nquery: q\nexpect:\n  entries: [a]\n")
    (vault.eval_dir / evaluate.BASELINE_FILE).write_text("{}", encoding="utf-8")
    assert [c.id for c in evaluate.load_cases(vault.eval_dir)] == ["one"]


# Running a case


def test_a_case_passes_when_the_entry_comes_back_high_enough(vault):
    index = small_vault(vault)
    case = evaluate.parse_case(
        {"id": "c", "query": "where are releases cut from", "expect": {"entries": ["deploy-from-main"]}},
        vault.eval_dir / "c.yaml",
    )
    result = evaluate.run_case(index, case)
    assert result.passed and result.rank == 1 and result.cost > 0


def test_a_case_fails_when_the_entry_comes_back_too_low(vault):
    index = small_vault(vault)
    case = evaluate.parse_case(
        {
            "id": "c",
            "query": "snapshot every night and releases cut from main",
            "expect": {"entries": ["deploy-from-main"], "rank_within": 1},
        },
        vault.eval_dir / "c.yaml",
    )
    result = evaluate.run_case(index, case)
    assert not result.passed
    assert "rank 2" in result.reasons[0]


def test_a_case_naming_an_entry_that_is_gone_says_so(vault):
    """The signal a rename produces, and the reason it must not look like a miss."""
    index = small_vault(vault)
    case = evaluate.parse_case(
        {"id": "c", "query": "releases cut from main", "expect": {"entries": ["renamed-away"]}},
        vault.eval_dir / "c.yaml",
    )
    result = evaluate.run_case(index, case)
    assert not result.passed
    assert "not an entry in this vault" in result.reasons[0]


def test_a_negative_case_passes_on_silence_and_fails_on_a_hit(vault):
    index = small_vault(vault)
    quiet = evaluate.parse_case(
        {"id": "q", "query": "carry on", "expect": {"silence": True}}, vault.eval_dir / "q.yaml"
    )
    loud = evaluate.parse_case(
        {"id": "l", "query": "releases cut from main", "expect": {"silence": True}},
        vault.eval_dir / "l.yaml",
    )
    assert evaluate.run_case(index, quiet).passed
    result = evaluate.run_case(index, loud)
    assert not result.passed and "expected silence" in result.reasons[0]


def test_a_case_that_needs_a_model_is_named_and_never_passed(vault):
    index = small_vault(vault)
    for tier in ("recall", "design"):
        case = evaluate.parse_case(
            {"id": tier, "query": "q", "expect": {"entries": ["deploy-from-main"]}, "tier": tier},
            vault.eval_dir / "c.yaml",
        )
        result = evaluate.run_case(index, case)
        assert not result.measured and not result.passed
        assert result.reasons == (evaluate.DEFERRED[tier],)


def test_must_cite_is_reported_and_not_scored(vault):
    index = small_vault(vault)
    case = evaluate.parse_case(
        {
            "id": "c",
            "query": "where are releases cut from",
            "expect": {"entries": ["deploy-from-main"], "must_cite": True},
        },
        vault.eval_dir / "c.yaml",
    )
    result = evaluate.run(index, [case])
    assert result.ok and len(result.uncited) == 1
    assert evaluate.DEFERRED_CITE in evaluate.render(result)


def test_an_unmeasured_case_is_not_counted_as_a_pass(vault):
    index = small_vault(vault)
    cases = [
        evaluate.parse_case(
            {"id": "a", "query": "where are releases cut from", "expect": {"entries": ["deploy-from-main"]}},
            vault.eval_dir / "a.yaml",
        ),
        evaluate.parse_case(
            {"id": "b", "query": "q", "expect": {"entries": ["deploy-from-main"]}, "tier": "recall"},
            vault.eval_dir / "b.yaml",
        ),
    ]
    result = evaluate.run(index, cases)
    assert len(result.results) == 2 and len(result.measured) == 1 and len(result.deferred) == 1
    assert "not measured yet" in evaluate.render(result)


# The baseline


def run_one(vault, query_text, expect, case_id="c"):
    index = small_vault(vault)
    case = evaluate.parse_case(
        {"id": case_id, "query": query_text, "expect": expect}, vault.eval_dir / f"{case_id}.yaml"
    )
    return evaluate.run(index, [case])


def test_the_baseline_round_trips(vault):
    result = run_one(vault, "where are releases cut from", {"entries": ["deploy-from-main"]})
    path = vault.eval_dir / evaluate.BASELINE_FILE
    evaluate.write_baseline(path, result)
    assert evaluate.read_baseline(path) == evaluate.snapshot(result)
    assert evaluate.compare(evaluate.read_baseline(path), result) == []


def test_the_baseline_stores_ranks_and_not_scores(vault):
    """A score is a float that moves with the corpus. A gate on it fires on noise."""
    result = run_one(vault, "where are releases cut from", {"entries": ["deploy-from-main"]})
    stored = evaluate.snapshot(result)
    assert stored["cases"]["c"] == {"passed": True, "rank": 1}
    assert "score" not in json.dumps(stored)


def test_no_baseline_yet_means_nothing_to_compare(vault):
    result = run_one(vault, "where are releases cut from", {"entries": ["deploy-from-main"]})
    assert evaluate.read_baseline(vault.eval_dir / evaluate.BASELINE_FILE) is None
    assert evaluate.compare(None, result) == []


def test_a_baseline_from_another_version_is_refused(vault):
    path = vault.eval_dir / evaluate.BASELINE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99, "cases": {}}), encoding="utf-8")
    with pytest.raises(MaboloError, match="baseline version 99"):
        evaluate.read_baseline(path)


def test_a_baseline_that_is_not_one_is_refused(vault):
    path = vault.eval_dir / evaluate.BASELINE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(MaboloError, match="not a baseline"):
        evaluate.read_baseline(path)


def test_a_case_that_stops_passing_is_a_regression(vault):
    result = run_one(vault, "where are releases cut from", {"entries": ["deploy-from-main"]})
    baseline = evaluate.snapshot(result)
    worse = run_one(vault, "where are releases cut from", {"entries": ["gone-away"]})
    changes = evaluate.compare(baseline, worse)
    assert [c.kind for c in changes] == ["regressed"] and changes[0].is_worse


def test_a_rank_that_slips_without_failing_is_still_a_regression(vault):
    """The early warning. By the time it fails, the cause is several commits back."""
    baseline = {"version": 1, "cases": {"c": {"passed": True, "rank": 1}}}
    result = run_one(
        vault,
        "snapshot every night and releases cut from main",
        {"entries": ["deploy-from-main"], "rank_within": 5},
    )
    changes = evaluate.compare(baseline, result)
    assert [c.kind for c in changes] == ["slipped"] and changes[0].is_worse
    assert "rank 1 to rank 2" in changes[0].message


def test_getting_better_is_news_and_not_a_failure(vault):
    baseline = {"version": 1, "cases": {"c": {"passed": True, "rank": 4}}}
    result = run_one(vault, "where are releases cut from", {"entries": ["deploy-from-main"]})
    changes = evaluate.compare(baseline, result)
    assert [c.kind for c in changes] == ["improved"] and not changes[0].is_worse


def test_a_case_the_baseline_never_saw_is_reported_as_new(vault):
    baseline = {"version": 1, "cases": {}}
    result = run_one(vault, "where are releases cut from", {"entries": ["deploy-from-main"]})
    changes = evaluate.compare(baseline, result)
    assert [c.kind for c in changes] == ["new"] and not changes[0].is_worse


def test_a_case_the_baseline_knows_and_the_run_left_out_is_reported(vault):
    baseline = {"version": 1, "cases": {"c": {"passed": True, "rank": 1}, "missing": {"passed": True, "rank": 1}}}
    result = run_one(vault, "where are releases cut from", {"entries": ["deploy-from-main"]})
    changes = evaluate.compare(baseline, result)
    assert [(c.kind, c.case_id) for c in changes] == [("gone", "missing")]


def test_the_baseline_is_written_with_a_stable_byte_order(vault):
    result = run_one(vault, "where are releases cut from", {"entries": ["deploy-from-main"]})
    path = vault.eval_dir / evaluate.BASELINE_FILE
    first = evaluate.write_baseline(path, result).read_bytes()
    second = evaluate.write_baseline(path, result).read_bytes()
    assert first == second and first.endswith(b"\n")
