"""The content pass: what it reports, and the much longer list of what it does not.

Every test here runs against a real vault on disk, because half of these
findings are about what is at the other end of a path, and a fixture that
answered for the filesystem would only ever confirm this file's own idea of
what a vault looks like.

The moment every run is measured against sits far in the future on purpose. A
check that read the clock instead of its parameter would put every entry here
hundreds of days past every line, and the boundary tests would go red, which is
the only way to prove a replay is a replay.
"""

import datetime as dt
from pathlib import Path

import pytest

from conftest import entry_text
from mabolo import schema, drift, lint

#: One fixed moment, far enough ahead that the clock cannot be mistaken for it.
MOMENT = dt.datetime(2199, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def entry(
    vault,
    name: str,
    *,
    area: str = "infra",
    body: str = "nothing worth saying here",
    aliases=(),
    title: str = "t",
    description: str = "d",
    written: dt.datetime | None = None,
    verified: dt.datetime | None = None,
    expires: dt.datetime | None = None,
    anchor: str | None = None,
) -> str:
    """One entry on disk, in the area it belongs to."""
    head = {"title": title, "description": description}
    if written is not None:
        head["generated"] = {"by": "claude-code/2.1.84", "at": written.isoformat()}
    if verified is not None:
        head["verified"] = [{"by": "human:alex", "at": verified.isoformat()}]
    if expires is not None:
        head["stale_after"] = expires.isoformat()
    block = {}
    if aliases:
        block["aliases"] = list(aliases)
    if anchor:
        block["anchor"] = anchor
    folder = vault.root / Path(area)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(
        entry_text(area=area, body=body, mabolo=block, **head), encoding="utf-8"
    )
    return name


def read(vault):
    return vault.entries()


# Dead links


def test_a_link_to_a_file_that_exists_is_not_a_dead_link(vault):
    entry(vault, "beacon-notes")
    entry(vault, "atlas-deploy", body="see [the other one](beacon-notes.md)")
    assert lint.dead_links(read(vault), vault.root) == []


def test_a_link_to_a_file_that_does_not_exist_is_a_dead_link(vault):
    entry(vault, "atlas-deploy", body="see [the other one](beacon-notes.md)")
    found = lint.dead_links(read(vault), vault.root)
    assert [(f.name, f.target) for f in found] == [("atlas-deploy", "beacon-notes.md")]
    assert "points at nothing" in found[0].message


def test_a_link_that_leaves_the_vault_is_not_a_dead_link(vault):
    """The validator already says this once, and it is not a tangle in the content."""
    entry(vault, "atlas-deploy", body="see [the plan](../../elsewhere/plan.md)")
    assert lint.dead_links(read(vault), vault.root) == []


def test_a_broken_link_inside_a_code_fence_is_not_a_dead_link(vault):
    entry(
        vault,
        "atlas-deploy",
        body="an example:\n\n```markdown\n[the other one](beacon-notes.md)\n```\n",
    )
    assert lint.dead_links(read(vault), vault.root) == []


# Missing links


def test_a_name_in_prose_without_a_link_is_a_missing_link(vault):
    entry(vault, "beacon-notes")
    entry(vault, "atlas-deploy", body="this follows beacon-notes to the letter")
    found = lint.missing_links(read(vault), vault.root)
    assert [(f.name, f.other) for f in found] == [("atlas-deploy", "beacon-notes")]


def test_a_name_inside_a_code_fence_is_not_a_missing_link(vault):
    entry(vault, "beacon-notes")
    entry(
        vault,
        "atlas-deploy",
        body="an example:\n\n```text\nbeacon-notes\n```\n\nand nothing else",
    )
    assert lint.missing_links(read(vault), vault.root) == []


def test_a_name_inside_an_existing_link_is_not_a_missing_link(vault):
    """The link goes somewhere else, so the name is words a person chose on purpose."""
    entry(vault, "beacon-notes")
    entry(vault, "harbour-rota")
    entry(vault, "atlas-deploy", body="read [the beacon-notes rule](harbour-rota.md)")
    assert lint.missing_links(read(vault), vault.root) == []


def test_an_entry_that_already_links_to_the_other_one_is_not_a_missing_link(vault):
    entry(vault, "beacon-notes")
    entry(
        vault,
        "atlas-deploy",
        body="read [the rule](beacon-notes.md), and beacon-notes again later",
    )
    assert lint.missing_links(read(vault), vault.root) == []


def test_a_name_inside_a_longer_word_is_not_a_missing_link(vault):
    entry(vault, "beacon-notes")
    entry(vault, "atlas-deploy", body="the beacon-notes-archive is somewhere else")
    assert lint.missing_links(read(vault), vault.root) == []


def test_an_alias_names_the_entry_as_much_as_its_name_does(vault):
    entry(vault, "beacon-notes", aliases=["harbour-rule"])
    entry(vault, "atlas-deploy", body="the harbour-rule says otherwise")
    found = lint.missing_links(read(vault), vault.root)
    assert [(f.name, f.other) for f in found] == [("atlas-deploy", "beacon-notes")]
    assert "harbour-rule" in found[0].message


def test_an_alias_shorter_than_three_characters_is_never_hunted(vault):
    entry(vault, "beacon-notes", aliases=["cd"])
    entry(vault, "atlas-deploy", body="we cd into the folder and stop")
    assert lint.missing_links(read(vault), vault.root) == []


def test_a_name_in_a_footnote_definition_is_not_a_missing_link(vault):
    """A footnote holds a quotation, and nobody edits a link into what was said."""
    entry(vault, "beacon-notes")
    entry(
        vault,
        "atlas-deploy",
        body="the rule holds.[^s1]\n\n[^s1]: \"beacon-notes is the one we follow\"",
    )
    assert lint.missing_links(read(vault), vault.root) == []


# Duplicate candidates


def test_an_overlap_exactly_at_the_threshold_is_a_finding(vault):
    entry(vault, "atlas-deploy", body="harbour lantern compass")
    entry(vault, "beacon-notes", body="harbour beacon marina")
    found = lint.duplicates(read(vault))
    assert [(f.name, f.other) for f in found] == [("atlas-deploy", "beacon-notes")]
    assert found[0].score == pytest.approx(0.2)


def test_an_overlap_just_under_the_threshold_is_not_a_finding(vault):
    entry(vault, "atlas-deploy", body="harbour lantern compass")
    entry(vault, "beacon-notes", body="harbour beacon marina quarry")
    assert lint.duplicates(read(vault)) == []


def test_two_entries_in_different_areas_are_never_a_pair(vault):
    entry(vault, "atlas-deploy", area="infra", body="harbour lantern compass")
    entry(vault, "beacon-notes", area="design", body="harbour lantern compass")
    assert lint.duplicates(read(vault)) == []


def test_a_pair_is_reported_once_and_not_twice(vault):
    entry(vault, "atlas-deploy", body="harbour lantern compass")
    entry(vault, "beacon-notes", body="harbour lantern compass")
    found = lint.duplicates(read(vault))
    assert len(found) == 1
    assert (found[0].name, found[0].other) == ("atlas-deploy", "beacon-notes")


# Expired


def test_an_entry_past_its_own_expiry_date_is_expired(vault):
    entry(vault, "atlas-deploy", expires=MOMENT - dt.timedelta(seconds=1))
    found = lint.expired(read(vault), MOMENT)
    assert [f.name for f in found] == ["atlas-deploy"]
    assert "stale_after" in found[0].message


def test_an_entry_whose_expiry_date_is_still_ahead_is_not_expired(vault):
    entry(vault, "atlas-deploy", expires=MOMENT + dt.timedelta(seconds=1))
    assert lint.expired(read(vault), MOMENT) == []


def test_an_expiry_is_measured_against_the_given_moment_and_never_the_clock(vault):
    """The moment is a parameter so a run can be replayed. The clock is today,
    which is nowhere near either of these two dates."""
    entry(vault, "atlas-deploy", expires=MOMENT + dt.timedelta(days=1))
    assert lint.expired(read(vault), MOMENT) == []
    later = lint.expired(read(vault), MOMENT + dt.timedelta(days=2))
    assert [f.name for f in later] == ["atlas-deploy"]


def test_an_anchor_that_could_not_be_checked_is_not_reported_by_lint(vault):
    """Anchors are doctor's half of the pair, so only the expiry reason gets through."""
    entry(vault, "atlas-deploy", anchor="src/build.py", area="infra")
    assert lint.expired(read(vault), MOMENT) == []
    assert drift.judge(read(vault)[0], moment=MOMENT).state == drift.UNCHECKED


# Untouched


def test_exactly_one_hundred_and_twenty_days_is_not_untouched(vault):
    entry(vault, "atlas-deploy", written=MOMENT - dt.timedelta(days=120))
    assert lint.untouched(read(vault), MOMENT) == []


def test_one_day_past_one_hundred_and_twenty_is_untouched(vault):
    entry(vault, "atlas-deploy", written=MOMENT - dt.timedelta(days=121))
    found = lint.untouched(read(vault), MOMENT)
    assert [f.name for f in found] == ["atlas-deploy"]
    assert "121 days" in found[0].message


def test_one_day_short_of_one_hundred_and_twenty_is_not_untouched(vault):
    entry(vault, "atlas-deploy", written=MOMENT - dt.timedelta(days=119))
    assert lint.untouched(read(vault), MOMENT) == []


def test_a_verification_is_a_touch_and_the_writing_date_is_not_the_last_word(vault):
    entry(
        vault,
        "atlas-deploy",
        written=MOMENT - dt.timedelta(days=900),
        verified=MOMENT - dt.timedelta(days=119),
    )
    assert lint.untouched(read(vault), MOMENT) == []


def test_an_entry_with_no_date_and_no_history_says_exactly_that(vault):
    """Its own category, not a long silence and not an age. "Untouched for 120
    days" is a claim about time, and an entry with no date supports no such
    claim."""
    entry(vault, "atlas-deploy")
    found = lint.untouched(read(vault), MOMENT)
    assert [f.name for f in found] == ["atlas-deploy"]
    assert found[0].kind == lint.UNDATED
    assert "no date and has no commit" in found[0].message


def test_an_undated_entry_in_a_repository_is_dated_by_its_last_commit(git_vault):
    """A hand written entry with no `generated` block is the normal way a vault
    grows, and the repository already records when it was last touched."""
    entry(git_vault, "atlas-deploy")
    git_vault.git("add", "infra/atlas-deploy.md")
    git_vault.git("commit", "-qm", "write it by hand")
    # Against the real clock, because the commit carries the real clock's date
    # and no argument can move it.
    today = schema.now()
    assert lint.untouched(read(git_vault), today) == [], "a commit from today is not untouched"
    later = lint.untouched(read(git_vault), today + dt.timedelta(days=200))
    assert [f.kind for f in later] == [lint.UNTOUCHED]
    assert "dated by its last commit" in later[0].message


# The whole pass


def test_inspect_says_nothing_about_a_tidy_vault(vault):
    entry(vault, "atlas-deploy", body="harbour lantern compass", written=MOMENT)
    entry(vault, "beacon-notes", body="quarry marina sextant", written=MOMENT)
    assert lint.inspect(read(vault), root=vault.root, moment=MOMENT) == []


def test_render_groups_by_kind_and_ends_with_a_count(vault):
    entry(vault, "beacon-notes", written=MOMENT)
    entry(
        vault,
        "atlas-deploy",
        body="follows beacon-notes, see [the plan](harbour-rota.md)",
        written=MOMENT - dt.timedelta(days=200),
    )
    found = lint.inspect(read(vault), root=vault.root, moment=MOMENT)
    text = lint.render(found)
    assert [f.kind for f in found] == [lint.DEAD_LINK, lint.MISSING_LINK, lint.UNTOUCHED]
    assert lint.HEADINGS[lint.DEAD_LINK] in text
    assert lint.HEADINGS[lint.MISSING_LINK] in text
    assert text.splitlines()[-1] == "3 findings: 1 dead link, 1 missing link, 1 untouched"


def test_render_of_nothing_still_says_how_many_that_was():
    assert lint.render([]) == "0 findings"


def test_the_pass_writes_nothing_at_all(vault):
    """Lint prints and never writes. Filing these would fill the inbox with
    questions a person then has to answer one by one."""
    entry(vault, "beacon-notes")
    entry(vault, "atlas-deploy", body="follows beacon-notes, see [x](gone.md)")
    before = {
        p: p.read_bytes() for p in sorted(vault.root.rglob("*")) if p.is_file()
    }
    assert lint.inspect(read(vault), root=vault.root, moment=MOMENT)
    after = {p: p.read_bytes() for p in sorted(vault.root.rglob("*")) if p.is_file()}
    assert after == before
