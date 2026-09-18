"""The eval harness: what it scores, what it refuses to score, and what it guards."""

import json
import shutil

import yaml

import pytest

from conftest import entry_text
import datetime as dt

from mabolo import evaluate, journal
from mabolo.errors import MaboloError
from mabolo.index import Index


def case_file(vault, name: str, text: str):
    vault.eval_dir.mkdir(parents=True, exist_ok=True)
    path = vault.eval_dir / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def store(vault) -> evaluate.EvalStore:
    return evaluate.EvalStore(vault)


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


def test_a_case_without_a_tier_is_a_search_case(tmp_path):
    case = evaluate.parse_case(
        {"id": "x", "query": "q", "expect": {"entries": ["a"]}}, tmp_path / "c.yaml"
    )
    assert case.tier == "search" and case.rank_within == evaluate.DEFAULT_RANK_WITHIN


def test_a_key_nobody_knows_is_refused_rather_than_ignored(tmp_path):
    """An instrument that ignores a setting reports a number about something else."""
    with pytest.raises(MaboloError, match="does not belong"):
        evaluate.parse_case(
            {"id": "x", "query": "q", "expect": {"entries": ["a"]}, "rank": 2}, tmp_path / "c.yaml"
        )
    with pytest.raises(MaboloError, match="does not belong"):
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
        store(vault).cases()


def test_a_case_file_that_cannot_be_read_stops_the_run(vault):
    case_file(vault, "broken", "id: x\nquery: [unclosed\n")
    with pytest.raises(MaboloError, match="cannot be read as a case"):
        store(vault).cases()


def test_a_case_that_sets_a_key_twice_is_refused(vault):
    """Plain `yaml.safe_load` keeps the last one without a word.

    A case file could then show one query and measure another, which is the
    exact failure the entry reader already has a strict loader for.
    """
    case_file(
        vault, "dup",
        "id: dup\nquery: where are releases cut from\n"
        "expect:\n  entries: [deploy-from-main]\nquery: carry on\n",
    )
    with pytest.raises(MaboloError, match="twice"):
        store(vault).cases()


def test_the_baseline_is_not_mistaken_for_a_case(vault):
    case_file(vault, "one", "id: one\nquery: q\nexpect:\n  entries: [a]\n")
    (vault.eval_dir / evaluate.BASELINE_FILE).write_text("{}", encoding="utf-8")
    assert [c.id for c in store(vault).cases()] == ["one"]


def test_a_file_that_is_not_a_case_is_named_rather_than_skipped(vault):
    """A case nobody notices is a question nobody asked."""
    case_file(vault, "one", "id: one\nquery: q\nexpect:\n  entries: [a]\n")
    (vault.eval_dir / "notes.txt").write_text("just a note", encoding="utf-8")
    with pytest.raises(MaboloError, match="notes.txt is not a case file"):
        store(vault).cases()


def test_a_case_file_spelled_in_capitals_is_still_a_case(vault):
    (vault.eval_dir / "LOUD.YAML").write_text(
        "id: loud\nquery: q\nexpect:\n  silence: true\n", encoding="utf-8")
    assert [c.id for c in store(vault).cases()] == ["loud"]


def test_the_eval_folder_is_not_read_through_a_symlink(vault, tmp_path):
    """The vault refuses to read through a link everywhere else, and now here."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.yaml").write_text("id: x\nquery: q\nexpect:\n  silence: true\n", encoding="utf-8")
    shutil.rmtree(vault.eval_dir)
    vault.eval_dir.symlink_to(elsewhere)
    # Either complaint is the refusal: a link out of the vault is caught as
    # leaving it, a link inside it as a link.
    with pytest.raises(MaboloError, match="outside the vault|symlink"):
        store(vault).cases()


def test_a_case_that_is_a_symlink_is_refused(vault, tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text("id: x\nquery: q\nexpect:\n  silence: true\n", encoding="utf-8")
    (vault.eval_dir / "x.yaml").symlink_to(outside)
    with pytest.raises(MaboloError, match="symlink"):
        store(vault).cases()


def test_the_baseline_is_not_written_through_a_symlink(vault, tmp_path):
    """`--save-baseline` used to write wherever the link pointed."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(vault.eval_dir)
    vault.eval_dir.symlink_to(elsewhere)
    result = evaluate.Run(results=[], entries=0)
    with pytest.raises(MaboloError, match="outside the vault|symlink"):
        store(vault).write_baseline(result, "en")
    assert not (elsewhere / evaluate.BASELINE_FILE).exists()


def test_only_the_named_cases_are_selected(vault):
    case_file(vault, "one", "id: one\nquery: q\nexpect:\n  silence: true\n")
    case_file(vault, "two", "id: two\nquery: q\nexpect:\n  silence: true\n")
    cases = store(vault).cases()
    assert [c.id for c in evaluate.select(cases, ["two"])] == ["two"]
    with pytest.raises(MaboloError, match="no case called nothing"):
        evaluate.select(cases, ["nothing"])


# Running a case


def test_a_case_passes_when_the_entry_comes_back_high_enough(vault):
    index = small_vault(vault)
    case = evaluate.parse_case(
        {"id": "c", "query": "where are releases cut from", "expect": {"entries": ["deploy-from-main"]}},
        vault.eval_dir / "c.yaml",
    )
    result = evaluate.run_case(index, case)
    assert result.passed and result.rank == 1 and result.cost > 0
    assert result.query.stems  # the parsed question travels with the result


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
    # No rank either: a rank that is stored for a failing case makes the gate
    # report that a still failing case improved.
    assert result.rank is None


def test_a_case_naming_an_entry_that_is_gone_says_so(vault):
    """The signal a rename produces, and the reason it must not look like a miss."""
    index = small_vault(vault)
    case = evaluate.parse_case(
        {"id": "c", "query": "releases cut from main", "expect": {"entries": ["renamed-away"]}},
        vault.eval_dir / "c.yaml",
    )
    result = evaluate.run_case(index, case)
    assert not result.passed and result.rank is None
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


def test_a_deep_case_is_searched_deep_and_costed_shallow(vault):
    """The cost is what a preview would send, not how far the case looked.

    Costing the deeper list made a case with `rank_within: 20` report a preview
    nobody would ever be given.
    """
    for i in range(8):
        (vault.root / "infra" / f"note-{i}.md").write_text(
            entry_text(title="Nightly snapshot", description="A snapshot every night",
                       body="Snapshots run at night."),
            encoding="utf-8")
    index = Index.build(vault.entries())
    shallow = evaluate.parse_case(
        {"id": "a", "query": "nightly snapshot", "expect": {"entries": ["note-0"]}},
        vault.eval_dir / "a.yaml")
    deep = evaluate.parse_case(
        {"id": "b", "query": "nightly snapshot", "expect": {"entries": ["note-0"], "rank_within": 8}},
        vault.eval_dir / "b.yaml")
    a, b = evaluate.run_case(index, shallow), evaluate.run_case(index, deep)
    assert len(b.hits) > len(a.hits)
    assert a.cost == b.cost


def test_a_case_that_needs_a_model_is_named_and_never_passed(vault):
    index = small_vault(vault)
    for tier in evaluate.DEFERRED:
        case = evaluate.parse_case(
            {"id": tier, "query": "q", "expect": {"entries": ["deploy-from-main"]}, "tier": tier},
            vault.eval_dir / "c.yaml",
        )
        result = evaluate.run_case(index, case)
        assert not result.measured and not result.passed
        assert result.reasons == (evaluate.DEFERRED[tier],)


def test_every_tier_is_either_measured_or_says_why_it_is_not(vault):
    """A tier that can be written into a case and has no reason is a KeyError,
    and one that is both measured and deferred would be scored and excused."""
    assert set(evaluate.TIERS) == set(evaluate.MEASURED_TIERS) | set(evaluate.DEFERRED)
    assert not set(evaluate.MEASURED_TIERS) & set(evaluate.DEFERRED)


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
            {"id": "b", "query": "q", "expect": {"entries": ["deploy-from-main"]}, "tier": "design"},
            vault.eval_dir / "b.yaml",
        ),
    ]
    result = evaluate.run(index, cases)
    assert len(result.results) == 2 and len(result.measured) == 1 and len(result.deferred) == 1
    assert "not measured yet" in evaluate.render(result)


def test_a_case_waiting_on_something_is_not_counted_as_a_pass_either(vault):
    """A whole tier can be unbuilt, and a single case can be waiting inside a
    tier that works. Both are unmeasured, and both have to be visible: a case
    kept as evidence of a gap that nothing prints has become invisible instead,
    which is the opposite of why it is kept."""
    index = small_vault(vault)
    case = evaluate.parse_case(
        {
            "id": "b",
            "query": "it is half past eleven",
            "needs": "meaning",
            "expect": {"entries": ["deploy-from-main"]},
            "tier": "recall",
        },
        vault.eval_dir / "b.yaml",
    )
    result = evaluate.run(index, [case])
    assert not result.measured and len(result.deferred) == 1
    assert "waiting on meaning" in evaluate.render(result)


def test_a_case_cannot_wait_on_something_nobody_defined(vault):
    with pytest.raises(MaboloError, match="not something a case can wait for"):
        evaluate.parse_case(
            {"id": "b", "query": "q", "needs": "telepathy", "expect": {"entries": ["x"]}},
            vault.eval_dir / "b.yaml",
        )


def test_a_recall_case_cannot_ask_for_a_rank_no_prompt_is_shown(vault):
    """The block holds three lines. A case allowed to ask for rank five would
    pass on an entry nobody is ever shown."""
    with pytest.raises(MaboloError, match="the block a prompt is shown holds"):
        evaluate.parse_case(
            {"id": "b", "query": "q", "tier": "recall", "expect": {"entries": ["x"], "rank_within": 5}},
            vault.eval_dir / "b.yaml",
        )


def test_a_recall_case_never_looks_past_the_lines_a_prompt_is_shown(vault):
    """The one difference between this tier and a search: an entry below the
    third line was never shown, so it cannot count as recalled. The rank rule
    above is half of that, and this is the other half: the search itself is
    asked for no more than the block holds."""
    index = small_vault(vault)
    case = evaluate.parse_case(
        {"id": "b", "query": "releases", "tier": "recall", "expect": {"entries": ["deploy-from-main"]}},
        vault.eval_dir / "b.yaml",
    )
    seen: list[int] = []
    original = Index.search
    try:
        Index.search = lambda self, text, limit=5: seen.append(limit) or original(self, text, limit)
        evaluate.run_recall_case(index, case)
    finally:
        Index.search = original
    assert seen == [evaluate.recall.LIMIT]


# The baseline


def run_one(vault, query_text, expect, case_id="c"):
    index = small_vault(vault)
    case = evaluate.parse_case(
        {"id": case_id, "query": query_text, "expect": expect}, vault.eval_dir / f"{case_id}.yaml"
    )
    return evaluate.run(index, [case])


def passing(vault):
    return run_one(vault, "where are releases cut from", {"entries": ["deploy-from-main"]})


def test_the_baseline_round_trips(vault):
    result = passing(vault)
    saved = store(vault).write_baseline(result, "en")
    assert saved == vault.eval_dir / evaluate.BASELINE_FILE
    read = store(vault).read_baseline()
    assert read == evaluate.Baseline.from_run(result, "en")
    assert evaluate.compare(read, result) == []


def test_the_baseline_stores_ranks_and_not_scores(vault):
    """A score is a float that moves with the corpus. A gate on it fires on noise."""
    stored = evaluate.Baseline.from_run(passing(vault), "en").to_json()
    assert stored["cases"]["c"] == {"tier": "search", "passed": True, "rank": 1}
    assert "score" not in json.dumps(stored)


def test_the_baseline_says_which_language_it_was_measured_in(vault):
    """Stop words and suffixes differ, so two languages are two measurements."""
    stored = evaluate.Baseline.from_run(passing(vault), "de")
    assert stored.to_json()["language"] == "de"
    stored.check_language("de")
    with pytest.raises(MaboloError, match="cannot be compared"):
        stored.check_language("en")


def test_no_baseline_yet_means_nothing_to_compare(vault):
    result = passing(vault)
    assert store(vault).read_baseline() is None
    assert evaluate.compare(None, result) == []


def test_a_baseline_from_another_version_is_refused(vault):
    path = vault.eval_dir / evaluate.BASELINE_FILE
    path.write_text(json.dumps({"version": 99, "cases": {}}), encoding="utf-8")
    with pytest.raises(MaboloError, match="baseline version 99"):
        store(vault).read_baseline()


def test_a_baseline_that_is_not_one_is_refused(vault):
    path = vault.eval_dir / evaluate.BASELINE_FILE
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(MaboloError, match="not a baseline"):
        store(vault).read_baseline()


def test_a_record_that_is_not_a_record_is_refused(vault):
    """It used to end as a traceback out of `compare`, several steps later."""
    path = vault.eval_dir / evaluate.BASELINE_FILE
    base = {"version": evaluate.BASELINE_VERSION, "language": "en"}
    for broken, complaint in (
        ({"x": True}, "not a mapping"),
        ({"x": {"passed": True, "rank": 1}}, "does not say which tier"),
        ({"x": {"tier": "search", "passed": "false"}}, "not yes or no"),
        ({"x": {"tier": "search", "passed": True, "rank": True}}, "not a position"),
        ({"x": {"tier": "search", "passed": True, "rank": 0}}, "not a position"),
    ):
        path.write_text(json.dumps({**base, "cases": broken}), encoding="utf-8")
        with pytest.raises(MaboloError, match=complaint):
            store(vault).read_baseline()


def test_a_case_that_stops_passing_is_a_regression(vault):
    baseline = evaluate.Baseline.from_run(passing(vault), "en")
    worse = run_one(vault, "where are releases cut from", {"entries": ["gone-away"]})
    changes = evaluate.compare(baseline, worse)
    assert [c.kind for c in changes] == ["regressed"] and changes[0].is_worse


def test_a_rank_that_slips_without_failing_is_still_a_regression(vault):
    """The early warning. By the time it fails, the cause is several commits back."""
    baseline = evaluate.Baseline(language="en", cases={"c": evaluate.BaselineCase(tier="search", passed=True, rank=1)})
    result = run_one(
        vault,
        "snapshot every night and releases cut from main",
        {"entries": ["deploy-from-main"], "rank_within": 5},
    )
    changes = evaluate.compare(baseline, result)
    assert [c.kind for c in changes] == ["slipped"] and changes[0].is_worse
    assert "rank 1 to rank 2" in changes[0].message


def test_two_failing_runs_are_never_called_an_improvement(vault):
    """A failing case has no rank, so there is nothing to call better."""
    baseline = evaluate.Baseline(language="en", cases={"c": evaluate.BaselineCase(tier="search", passed=False, rank=None)})
    result = run_one(
        vault, "snapshot every night and releases cut from main",
        {"entries": ["deploy-from-main"], "rank_within": 1},
    )
    assert not result.ok
    assert evaluate.compare(baseline, result) == []


def test_getting_better_is_news_and_not_a_failure(vault):
    baseline = evaluate.Baseline(language="en", cases={"c": evaluate.BaselineCase(tier="search", passed=True, rank=4)})
    changes = evaluate.compare(baseline, passing(vault))
    assert [c.kind for c in changes] == ["improved"] and not changes[0].is_worse


def test_a_case_the_baseline_never_saw_is_reported_as_new(vault):
    changes = evaluate.compare(evaluate.Baseline(language="en"), passing(vault))
    assert [c.kind for c in changes] == ["new"] and not changes[0].is_worse


def test_a_case_the_baseline_knows_and_the_run_left_out_is_reported(vault):
    baseline = evaluate.Baseline(language="en", cases={
        "c": evaluate.BaselineCase(tier="search", passed=True, rank=1), "missing": evaluate.BaselineCase(tier="search", passed=True, rank=1)})
    changes = evaluate.compare(baseline, passing(vault))
    assert [(c.kind, c.case_id) for c in changes] == [("gone", "missing")]


def test_a_partial_run_does_not_report_the_cases_it_left_out(vault):
    """`--case` means leaving them out, so reporting them is true and useless."""
    baseline = evaluate.Baseline(language="en", cases={
        "c": evaluate.BaselineCase(tier="search", passed=True, rank=1), "missing": evaluate.BaselineCase(tier="search", passed=True, rank=1)})
    assert evaluate.compare(baseline, passing(vault), subset=True) == []


def test_the_baseline_is_written_with_a_stable_byte_order(vault):
    result = passing(vault)
    first = store(vault).write_baseline(result, "en").read_bytes()
    second = store(vault).write_baseline(result, "en").read_bytes()
    assert first == second and first.endswith(b"\n")


# The hint tier: is the entry in what a session starts with


def hint_case(tmp_path, expect: dict, **state) -> evaluate.Case:
    """One hint case, with `as_of` filled in unless the test is about it."""
    state.setdefault("as_of", "2026-09-18")
    return evaluate.parse_case(
        {"id": "c", "tier": "hint", "state": state, "expect": expect}, tmp_path / "c.yaml"
    )


HINT = """
id: session-index
tier: hint
state:
  project: atlas
  as_of: 2026-09-18
expect:
  in_payload: [deploy-from-main]
  not_in_payload: [backups-run-nightly]
  budget_tokens: 800
"""


def hint_vault(vault):
    """Two entries, one pinned and one not, at a known moment."""
    (vault.root / "infra" / "deploy-from-main.md").write_text(
        entry_text(
            title="Deploy from main only",
            description="Releases are cut from main, tags are labels",
            mabolo={"pin": True},
            generated={"by": "mabolo/0.1.0", "at": "2026-09-17T10:00:00+03:00"},
        ),
        encoding="utf-8",
    )
    (vault.root / "infra" / "backups-run-nightly.md").write_text(
        entry_text(
            title="Backups",
            description="A snapshot every night",
            generated={"by": "mabolo/0.1.0", "at": "2026-01-01T10:00:00+03:00"},
        ),
        encoding="utf-8",
    )
    folder = vault.area_dir("project/atlas")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "atlas-tone.md").write_text(
        entry_text(area="project/atlas", title="Tone", description="Flat and factual"),
        encoding="utf-8",
    )
    return Index.build(vault.entries())


def test_a_hint_case_is_read_with_its_state(tmp_path):
    case = evaluate.parse_case(yaml.safe_load(HINT), tmp_path / "c.yaml")
    assert case.tier == "hint"
    assert case.project == "atlas"
    assert case.as_of.date().isoformat() == "2026-09-18"
    assert case.in_payload == ("deploy-from-main",)
    assert case.not_in_payload == ("backups-run-nightly",)
    assert case.budget_tokens == 800
    assert case.measurable


def test_a_hint_case_refuses_a_query(tmp_path):
    """The session start is the trigger. A query here would look like a search
    and measure something else."""
    with pytest.raises(MaboloError, match="does not belong on a hint case"):
        evaluate.parse_case(
            {"id": "c", "tier": "hint", "query": "anything", "state": {"as_of": "2026-09-18"},
             "expect": {"in_payload": ["a"]}},
            tmp_path / "c.yaml",
        )


def test_a_search_case_refuses_a_state(tmp_path):
    with pytest.raises(MaboloError, match="does not belong on a search case"):
        evaluate.parse_case(
            {"id": "c", "query": "anything", "state": {"as_of": "2026-09-18"},
             "expect": {"entries": ["a"]}},
            tmp_path / "c.yaml",
        )


def test_a_hint_case_without_a_moment_is_refused(tmp_path):
    """Without a fixed moment the case would measure a different vault every week."""
    with pytest.raises(MaboloError, match="state.as_of"):
        evaluate.parse_case(
            {"id": "c", "tier": "hint", "state": {"project": "atlas"},
             "expect": {"in_payload": ["a"]}},
            tmp_path / "c.yaml",
        )


def test_a_hint_case_without_a_state_is_refused(tmp_path):
    with pytest.raises(MaboloError, match="needs state"):
        evaluate.parse_case(
            {"id": "c", "tier": "hint", "expect": {"in_payload": ["a"]}}, tmp_path / "c.yaml"
        )


def test_a_hint_case_refuses_the_keys_of_a_search_case(tmp_path):
    with pytest.raises(MaboloError, match="does not belong"):
        evaluate.parse_case(
            {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18"},
             "expect": {"entries": ["a"], "rank_within": 1}},
            tmp_path / "c.yaml",
        )


def test_an_unknown_key_under_state_is_refused(tmp_path):
    with pytest.raises(MaboloError, match="unknown key"):
        evaluate.parse_case(
            {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18", "user": "alex"},
             "expect": {"in_payload": ["a"]}},
            tmp_path / "c.yaml",
        )


def test_a_hint_case_that_expects_nothing_is_refused(tmp_path):
    with pytest.raises(MaboloError, match="expects nothing"):
        evaluate.parse_case(
            {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18"}, "expect": {}},
            tmp_path / "c.yaml",
        )


def test_a_name_cannot_be_expected_in_and_out_at_once(tmp_path):
    with pytest.raises(MaboloError, match="both in the session index and out of it"):
        evaluate.parse_case(
            {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18"},
             "expect": {"in_payload": ["a"], "not_in_payload": ["a"]}},
            tmp_path / "c.yaml",
        )


@pytest.mark.parametrize("budget", [0, -1, True, "800"])
def test_a_budget_that_is_not_a_count_is_refused(tmp_path, budget):
    with pytest.raises(MaboloError, match="budget_tokens"):
        evaluate.parse_case(
            {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18"},
             "expect": {"in_payload": ["a"], "budget_tokens": budget}},
            tmp_path / "c.yaml",
        )


def test_a_hint_case_passes_when_the_entry_is_in_the_session_index(vault, tmp_path):
    index = hint_vault(vault)
    result = evaluate.run_case(index, evaluate.parse_case(yaml.safe_load(HINT), tmp_path / "c.yaml"))
    assert result.passed, result.reasons
    assert result.rank == 1, "the pinned entry is the safest line in the index"
    assert result.cost > 0
    assert result.payload is not None


def test_a_hint_case_fails_and_says_how_full_the_index_was(vault, tmp_path):
    index = hint_vault(vault)
    case = evaluate.parse_case(
        {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18"},
         "expect": {"in_payload": ["backups-run-nightly"]}},
        tmp_path / "c.yaml",
    )
    result = evaluate.run_case(index, case)
    assert not result.passed
    assert result.rank is None
    assert "is not in the session index, which holds 1 of 3 entries" in result.reasons[0]


def test_a_hint_case_fails_when_something_that_should_be_gone_is_still_there(vault, tmp_path):
    index = hint_vault(vault)
    case = evaluate.parse_case(
        {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18"},
         "expect": {"not_in_payload": ["deploy-from-main"]}},
        tmp_path / "c.yaml",
    )
    result = evaluate.run_case(index, case)
    assert not result.passed
    assert "at position 1" in result.reasons[0]


def test_a_hint_case_names_an_entry_the_vault_does_not_have(vault, tmp_path):
    index = hint_vault(vault)
    case = evaluate.parse_case(
        {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18"},
         "expect": {"in_payload": ["renamed-away"]}},
        tmp_path / "c.yaml",
    )
    result = evaluate.run_case(index, case)
    assert not result.passed
    assert "is not an entry in this vault" in result.reasons[0]


def test_a_hint_case_resolves_an_alias(vault, tmp_path):
    (vault.root / "infra" / "deploy-from-main.md").write_text(
        entry_text(
            title="Deploy",
            description="Releases are cut from main",
            mabolo={"pin": True, "aliases": ["release source"]},
        ),
        encoding="utf-8",
    )
    index = Index.build(vault.entries())
    case = evaluate.parse_case(
        {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18"},
         "expect": {"in_payload": ["release source"]}},
        tmp_path / "c.yaml",
    )
    assert evaluate.run_case(index, case).passed


def test_the_asserted_budget_does_not_change_the_index_it_measures(vault, tmp_path):
    """Building the payload to the asserted budget would make the case confirm
    itself: whatever number it names, the payload would fit inside it."""
    index = hint_vault(vault)
    case = evaluate.parse_case(
        {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18"},
         "expect": {"in_payload": ["deploy-from-main"], "budget_tokens": 1}},
        tmp_path / "c.yaml",
    )
    result = evaluate.run_case(index, case)
    assert not result.passed
    assert "wanted 1 or fewer" in result.reasons[-1]
    assert result.payload.names == ("deploy-from-main",), "built with the shipped budget"


def test_the_two_measured_tiers_are_counted_apart(vault, tmp_path):
    index = hint_vault(vault)
    cases = [
        evaluate.parse_case(yaml.safe_load(HINT), tmp_path / "hint.yaml"),
        evaluate.parse_case(
            {"id": "search", "query": "where are releases cut from",
             "expect": {"entries": ["deploy-from-main"]}},
            tmp_path / "search.yaml",
        ),
    ]
    result = evaluate.run(index, cases)
    assert len(result.in_tier("hint")) == 1
    assert len(result.in_tier("search")) == 1
    report = evaluate.render(result)
    assert "search   1 case, 1 pass, 0 fail" in report
    assert "hint     1 case, 1 pass, 0 fail" in report
    assert "the session index held what was asked in 1 of 1 cases" in report


def test_a_hint_case_that_slips_down_the_index_is_reported(vault, tmp_path):
    """The early warning: still in the index, and closer to the line where the
    budget cuts."""
    baseline = evaluate.Baseline(language="en", cases={"c": evaluate.BaselineCase(tier="hint", passed=True, rank=1)})
    case = evaluate.parse_case(
        {"id": "c", "tier": "hint", "state": {"as_of": "2026-09-18"},
         "expect": {"in_payload": ["a"]}},
        tmp_path / "c.yaml",
    )
    run = evaluate.Run(results=[evaluate.Result(case=case, passed=True, rank=3)])
    changes = evaluate.compare(baseline, run)
    assert [c.kind for c in changes] == ["slipped"]


# What the audits found: a case that cannot fail, and a run that cannot finish


def test_a_hint_case_that_only_asserts_absences_passes_and_has_no_rank(vault, tmp_path):
    """The parser allows it, so the runner has to survive it. `max` of an empty
    list used to end the whole run in a traceback, with no report and no
    baseline, and a case saying only "these must be gone" is a real case."""
    index = hint_vault(vault)
    result = evaluate.run_case(index, hint_case(tmp_path, {"not_in_payload": ["backups-run-nightly"]}))
    assert result.passed, result.reasons
    assert result.rank is None


def test_a_budget_only_hint_case_passes_and_has_no_rank(vault, tmp_path):
    index = hint_vault(vault)
    result = evaluate.run_case(index, hint_case(tmp_path, {"budget_tokens": 800}))
    assert result.passed and result.rank is None


def test_a_hint_case_naming_a_project_the_vault_does_not_have_fails(vault, tmp_path):
    """Otherwise a typo is the quietest pass there is: the project rule never
    fires, the pins still show up, and the case goes green having measured half
    of what it says."""
    index = hint_vault(vault)
    case = hint_case(tmp_path, {"in_payload": ["deploy-from-main"]}, project="atlsa")
    result = evaluate.run_case(index, case)
    assert not result.passed
    assert "is not a project area in this vault" in result.reasons[0]


def test_an_expectation_never_resolves_to_a_project_name(vault, tmp_path):
    """`resolve` lets a question name a project and mean all of its entries. An
    expectation names one entry, so a project holding exactly one entry used to
    make a case pass against something its file never mentioned."""
    folder = vault.area_dir("project/gamma")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "only-one.md").write_text(
        entry_text(area="project/gamma", title="Only", description="The only one", mabolo={"pin": True}),
        encoding="utf-8",
    )
    index = Index.build(vault.entries())
    assert index.resolve("gamma") is not None, "a question may still name the project"
    assert index.resolve_entry("gamma") is None
    result = evaluate.run_case(index, hint_case(tmp_path, {"in_payload": ["gamma"]}))
    assert not result.passed
    assert "is not an entry in this vault" in result.reasons[0]


def test_a_search_case_names_an_entry_and_not_a_project_either(vault, tmp_path):
    folder = vault.area_dir("project/gamma")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "only-one.md").write_text(
        entry_text(area="project/gamma", title="Only", description="The only one in gamma"),
        encoding="utf-8",
    )
    index = Index.build(vault.entries())
    case = evaluate.parse_case(
        {"id": "c", "query": "the only one in gamma", "expect": {"entries": ["gamma"]}},
        tmp_path / "c.yaml",
    )
    result = evaluate.run_case(index, case)
    assert not result.passed
    assert "is not an entry in this vault" in result.reasons[0]


def test_a_tier_that_is_not_text_is_refused_rather_than_defaulted(tmp_path):
    """`tier: false` used to become a search case through a truthiness test."""
    for broken in (False, 0, ""):
        with pytest.raises(MaboloError, match="tier is"):
            evaluate.parse_case(
                {"id": "c", "tier": broken, "query": "q", "expect": {"entries": ["a"]}},
                tmp_path / "c.yaml",
            )


def test_an_id_that_is_not_text_is_refused(tmp_path):
    """The baseline is keyed by it, so 123 silently becoming "123" is a record
    about a case nobody can find again."""
    with pytest.raises(MaboloError, match="needs an id"):
        evaluate.parse_case({"id": 123, "query": "q", "expect": {"entries": ["a"]}}, tmp_path / "c.yaml")


def test_an_empty_name_in_a_list_is_refused_rather_than_dropped(tmp_path):
    with pytest.raises(MaboloError, match="holds an empty name"):
        hint_case(tmp_path, {"in_payload": ["deploy-from-main", "  "]})
    with pytest.raises(MaboloError, match="holds an empty name"):
        evaluate.parse_case(
            {"id": "c", "query": "q", "expect": {"entries": ["a", ""]}}, tmp_path / "c.yaml"
        )


def test_a_missing_moment_and_an_unreadable_one_are_different_complaints(tmp_path):
    """Telling somebody who wrote `as_of: yesterday` that the field is required
    is a true statement about the wrong problem."""
    with pytest.raises(MaboloError, match="It is required"):
        evaluate.parse_case(
            {"id": "c", "tier": "hint", "state": {}, "expect": {"in_payload": ["a"]}},
            tmp_path / "c.yaml",
        )
    with pytest.raises(MaboloError, match="is not a date"):
        hint_case(tmp_path, {"in_payload": ["a"]}, as_of="yesterday")


def test_the_old_name_of_the_search_tier_still_reads(tmp_path):
    """A vault written before the rename keeps working, and nothing rewrites it."""
    case = evaluate.parse_case(
        {"id": "c", "tier": "index", "query": "q", "expect": {"entries": ["a"]}},
        tmp_path / "c.yaml",
    )
    assert case.tier == "search"


def test_a_case_that_changed_tier_is_new_and_not_a_slip(vault, tmp_path):
    """A rank means where an entry came back in a search, or how far it sits
    from the line the budget cuts at. Comparing one against the other is a red
    run about nothing."""
    baseline = evaluate.Baseline(
        language="en", cases={"c": evaluate.BaselineCase(tier="search", passed=True, rank=1)}
    )
    case = hint_case(tmp_path, {"in_payload": ["a"]})
    run = evaluate.Run(results=[evaluate.Result(case=case, passed=True, rank=6)])
    changes = evaluate.compare(baseline, run)
    assert [c.kind for c in changes] == ["new"]
    assert not changes[0].is_worse
    assert "was measured as a search case" in changes[0].message


def test_a_hint_case_is_measured_with_the_journal_block_in_the_payload(tmp_path, vault):
    """What is measured has to be the payload a session actually receives.

    Built without the block, the harness reports a cost nobody is charged, and
    the gap grows quietly as the journal does. The case also has to see the
    lines: a decision from last week is exactly what a hint case asks about.
    """
    index = hint_vault(vault)
    case = hint_case(tmp_path, {"in_payload": ["deploy-from-main"]}, project="atlas")
    notes = [
        journal.Note(
            at=dt.date(2026, 9, 16),
            text="the release moved to Friday, see [atlas-tone](project/atlas/atlas-tone.md)",
            project="atlas",
        )
    ]
    bare = evaluate.run_hint_case(index, case)
    with_journal = evaluate.run_hint_case(index, case, notes)
    assert bare.passed and with_journal.passed
    assert with_journal.cost > bare.cost


def test_a_journal_line_of_another_project_costs_the_payload_nothing(tmp_path, vault):
    index = hint_vault(vault)
    case = hint_case(tmp_path, {"in_payload": ["deploy-from-main"]}, project="atlas")
    elsewhere = [journal.Note(at=dt.date(2026, 9, 16), text="not here", project="beacon")]
    assert evaluate.run_hint_case(index, case, elsewhere).cost == evaluate.run_hint_case(index, case).cost


def test_a_baseline_remembers_which_selection_rule_measured_it(vault):
    index = small_vault(vault)
    case = evaluate.parse_case(
        {"id": "a", "query": "releases", "expect": {"entries": ["deploy-from-main"]}},
        vault.eval_dir / "a.yaml",
    )
    baseline = evaluate.Baseline.from_run(evaluate.run(index, [case]), "en")
    assert baseline.to_json()["policy"] == evaluate.context.POLICY
    assert baseline.policy_note() is None


def test_a_baseline_from_another_policy_is_compared_and_says_so(vault):
    """A note, not a refusal. The numbers stay comparable; what changed is what
    they mean, and that is something to be told rather than protected from."""
    baseline = evaluate.Baseline(language="en", policy=evaluate.context.POLICY - 1)
    note = baseline.policy_note()
    assert note and "rather than the memory getting worse" in note


def test_a_baseline_written_before_policies_existed_still_reads(vault):
    """It has no policy field at all, and refusing to read it would throw away
    a gate for a field that was added after it was written."""
    data = {"version": evaluate.BASELINE_VERSION, "language": "en", "cases": {}}
    assert evaluate.Baseline.from_json(data, vault.eval_dir / "baseline.json").policy == 1


def test_a_change_under_a_new_policy_is_named_for_what_it_is(vault):
    """The note alone was decoration: the comparison still called it a
    regression, which is a claim about quality. The only honest claim is that
    the question changed. It still fails the run, because a rule change can be
    a rule change and a regression on the same day."""
    baseline = evaluate.Baseline(
        language="en",
        policy=evaluate.context.POLICY - 1,
        cases={"c": evaluate.BaselineCase(tier="hint", passed=True, rank=1)},
    )
    result = evaluate.Result(
        case=evaluate.Case(id="c", query="", path=None, tier="hint"), passed=False, rank=None
    )
    changes = evaluate.compare(baseline, evaluate.Run(results=[result], entries=1))
    assert [c.kind for c in changes] == [evaluate.POLICY_CHANGED]
    assert all(c.is_worse for c in changes), "the gate stays shut until a person looks"


def test_the_same_change_under_the_same_policy_is_still_a_regression(vault):
    baseline = evaluate.Baseline(
        language="en",
        policy=evaluate.context.POLICY,
        cases={"c": evaluate.BaselineCase(tier="hint", passed=True, rank=1)},
    )
    result = evaluate.Result(
        case=evaluate.Case(id="c", query="", path=None, tier="hint"), passed=False, rank=None
    )
    assert [c.kind for c in evaluate.compare(baseline, evaluate.Run(results=[result], entries=1))] == [
        "regressed"
    ]
