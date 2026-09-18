from conftest import entry_text
from mabolo import context
from mabolo.validate import validate_meta, validate_vault
from mabolo.vault import Vault


def codes(problems):
    return {p.code for p in problems}


def good_meta(**overrides):
    meta = {
        "type": "reference",
        "title": "A title",
        "description": "One line that the index is built from",
        "mabolo": {"area": "infra", "pin": False},
    }
    meta.update(overrides)
    return meta


def test_a_conformant_entry_produces_nothing():
    assert validate_meta(good_meta(), body="Text.", expected_area="infra") == []


def test_type_is_the_one_field_okf_requires():
    meta = good_meta()
    del meta["type"]
    assert "okf.type.missing" in codes(validate_meta(meta, body="t"))


def test_an_unknown_type_is_only_a_warning_because_okf_allows_any_string():
    problems = validate_meta(good_meta(type="playbook"), body="t")
    assert [p.level for p in problems if p.code == "okf.type.unknown"] == ["warning"]


def test_the_description_is_an_error_because_the_index_is_built_from_it():
    meta = good_meta()
    del meta["description"]
    problem = [p for p in validate_meta(meta, body="t") if p.code == "mabolo.description.missing"]
    assert problem and problem[0].is_error


def test_a_description_over_several_lines_breaks_the_index_line():
    assert "mabolo.description.multiline" in codes(validate_meta(good_meta(description="a\nb"), body="t"))


def test_the_area_has_to_match_the_folder():
    problems = validate_meta(good_meta(), body="t", expected_area="persona")
    assert "mabolo.area.mismatch" in codes(problems)


def test_an_area_that_is_not_configured_is_refused():
    assert "mabolo.area.unknown" in codes(
        validate_meta(good_meta(mabolo={"area": "elsewhere"}), body="t")
    )


def test_a_project_area_needs_no_configuration():
    assert "mabolo.area.unknown" not in codes(
        validate_meta(good_meta(mabolo={"area": "project/atlas"}), body="t", expected_area="project/atlas")
    )


def test_status_outside_the_lifecycle_values_is_an_error():
    assert "okf.status.unknown" in codes(validate_meta(good_meta(status="aktiv"), body="t"))


def test_stale_after_without_an_offset_is_a_warning_because_machines_differ():
    assert "okf.stale_after.naive" in codes(
        validate_meta(good_meta(stale_after="2027-02-01T00:00:00"), body="t")
    )


def test_half_a_generated_block_is_an_error():
    assert "okf.generated.incomplete" in codes(
        validate_meta(good_meta(generated={"by": "mabolo/0.1.0"}), body="t")
    )


def test_only_a_person_may_appear_in_verified():
    """The gate rests on this: a tool writing `verified` proves nothing."""
    for actor in ("someone", "mabolo/0.1.0", "process:import"):
        problems = validate_meta(
            good_meta(verified=[{"by": actor, "at": "2026-01-01T00:00:00+03:00"}]), body="t"
        )
        assert "mabolo.verified.not_human" in codes(problems)
    assert "mabolo.verified.not_human" not in codes(
        validate_meta(good_meta(verified=[{"by": "human:someone", "at": "2026-01-01T00:00:00+03:00"}]), body="t")
    )


def test_an_alias_keeps_the_spelling_a_person_would_type_but_cannot_walk():
    ok = good_meta(mabolo={"area": "infra", "aliases": ["Old Name", "OldName"]})
    assert "mabolo.alias.invalid" not in codes(validate_meta(ok, body="t", expected_area="infra"))
    for bad in ("../elsewhere", "index", "a\nb"):
        problems = validate_meta(
            good_meta(mabolo={"area": "infra", "aliases": [bad]}), body="t", expected_area="infra"
        )
        assert "mabolo.alias.invalid" in codes(problems), bad


def test_a_source_without_a_resource_cannot_be_a_receipt():
    assert "okf.sources.resource.missing" in codes(
        validate_meta(good_meta(sources=[{"id": "s1"}]), body="t")
    )


def test_two_sources_with_the_same_id_make_a_footnote_ambiguous():
    meta = good_meta(sources=[{"id": "s1", "resource": "a"}, {"id": "s1", "resource": "b"}])
    assert "okf.sources.id.duplicate" in codes(validate_meta(meta, body="t"))


def test_a_footnote_without_a_source_is_named():
    meta = good_meta(sources=[{"id": "s1", "resource": "session://x"}])
    problems = validate_meta(meta, body='Claim.[^s1] Other.[^s9]\n\n[^s1]: "said"\n[^s9]: "said too"\n')
    assert "mabolo.footnote.unsourced" in codes(problems)


def test_a_design_entry_without_applies_to_would_never_be_loaded():
    meta = good_meta(mabolo={"area": "design"})
    problems = validate_meta(meta, body="t", expected_area="design")
    assert "mabolo.design.applies_to.missing" in codes(problems)
    assert "mabolo.design.instead_of.missing" in codes(problems)


def test_an_anchor_may_not_leave_the_project():
    assert "mabolo.anchor.absolute" in codes(
        validate_meta(good_meta(mabolo={"area": "infra", "anchor": "/etc/passwd"}), body="t")
    )
    assert "mabolo.anchor.traversal" in codes(
        validate_meta(good_meta(mabolo={"area": "infra", "anchor": "../../elsewhere"}), body="t")
    )


def test_a_wikilink_is_reported_because_it_breaks_outside_obsidian():
    assert "mabolo.link.wikilink" in codes(validate_meta(good_meta(), body="see [[other-entry]]"))


def test_an_empty_vault_is_valid(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    assert vault.validate().ok


def test_two_entries_with_the_same_name_are_an_error(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    for area in ("infra", "persona"):
        path = vault.path_for(area, "same-name")
        path.write_text(
            f"---\ntype: reference\ntitle: t\ndescription: d\nmabolo:\n  area: {area}\n---\n\ntext\n",
            encoding="utf-8",
        )
    assert "vault.name.duplicate" in codes(validate_vault(vault.root).problems)


def test_an_alias_may_not_be_the_name_of_another_entry(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.path_for("infra", "first").write_text(
        "---\ntype: reference\ntitle: t\ndescription: d\nmabolo:\n  area: infra\n---\n\ntext\n",
        encoding="utf-8",
    )
    vault.path_for("infra", "second").write_text(
        "---\ntype: reference\ntitle: t\ndescription: d\nmabolo:\n  area: infra\n  aliases: [first]\n---\n\ntext\n",
        encoding="utf-8",
    )
    assert "vault.alias.collision" in codes(validate_vault(vault.root).problems)


def test_a_concept_document_without_frontmatter_is_an_error(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.path_for("infra", "loose").write_text("just prose\n", encoding="utf-8")
    assert "okf.frontmatter.missing" in codes(validate_vault(vault.root).problems)


def test_the_reserved_files_keep_their_own_rules(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.log_file.write_text("---\ntype: reference\n---\n\nlog\n", encoding="utf-8")
    (vault.root / "infra" / "index.md").write_text(
        "---\ntype: reference\n---\n\nindex\n", encoding="utf-8"
    )
    found = codes(validate_vault(vault.root).problems)
    assert "okf.log.frontmatter" in found
    assert "okf.index.frontmatter" in found


def test_a_broken_link_is_a_warning_because_the_spec_says_tolerate(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.path_for("infra", "linker").write_text(
        "---\ntype: reference\ntitle: t\ndescription: d\nmabolo:\n  area: infra\n---\n\n[gone](../persona/gone.md)\n",
        encoding="utf-8",
    )
    problems = [p for p in validate_vault(vault.root).problems if p.code == "mabolo.link.broken"]
    assert problems and not problems[0].is_error


def test_a_symlinked_entry_is_reported_and_not_read(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntype: reference\n---\n\nnot vault knowledge\n", encoding="utf-8")
    vault = Vault(tmp_path / "v")
    vault.initialise()
    (vault.root / "infra" / "linked.md").symlink_to(outside)
    assert "vault.symlink" in codes(validate_vault(vault.root).problems)


def test_a_link_out_of_the_vault_is_named_as_such_not_as_a_healthy_link(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("anything\n", encoding="utf-8")
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.path_for("infra", "linker").write_text(
        "---\ntype: reference\ntitle: t\ndescription: d\nmabolo:\n  area: infra\n---\n\n"
        "[out](../../outside.md)\n",
        encoding="utf-8",
    )
    problems = validate_vault(vault.root).problems
    outside = [p for p in problems if p.code == "mabolo.link.outside"]
    # Pointing at a file in your own project is reasonable, so this is a warning
    # with a code of its own, not a broken link and not an error.
    assert outside and not outside[0].is_error


def test_an_index_below_the_root_may_not_carry_frontmatter(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    (vault.root / "infra" / "index.md").write_text(
        "---\nokf_version: '0.2'\n---\n\n# infra\n", encoding="utf-8"
    )
    assert "okf.index.frontmatter" in codes(validate_vault(vault.root).problems)


def test_the_log_has_to_be_dated_unique_and_newest_first(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.log_file.write_text("## 2026-01-01\n\n- a\n\n## 2026-09-01\n\n- b\n", encoding="utf-8")
    assert "okf.log.order" in codes(validate_vault(vault.root).problems)
    vault.log_file.write_text("## 2026-09-01\n\n- a\n\n## 2026-09-01\n\n- b\n", encoding="utf-8")
    assert "okf.log.duplicate" in codes(validate_vault(vault.root).problems)
    vault.log_file.write_text("## yesterday\n\n- a\n", encoding="utf-8")
    assert "okf.log.heading" in codes(validate_vault(vault.root).problems)


def test_an_entry_in_the_vault_root_is_not_in_any_area(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    (vault.root / "loose.md").write_text(
        "---\ntype: reference\ntitle: t\ndescription: d\nmabolo:\n  area: infra\n---\n\nText.\n",
        encoding="utf-8",
    )
    assert "mabolo.entry.at_root" in codes(validate_vault(vault.root).problems)


def test_a_source_needs_a_resource_that_is_actually_text():
    meta = {
        "type": "reference",
        "title": "t",
        "description": "d",
        "sources": [{"id": "s1", "resource": None}],
        "mabolo": {"area": "infra"},
    }
    assert "okf.sources.resource.missing" in codes(validate_meta(meta, body="t"))


def test_a_title_over_two_lines_would_write_two_lines_into_the_index():
    meta = {
        "type": "reference",
        "title": "first\nsecond",
        "description": "d",
        "mabolo": {"area": "infra"},
    }
    assert "mabolo.title.multiline" in codes(validate_meta(meta, body="t"))


def test_an_example_in_a_code_block_is_not_treated_as_a_link():
    meta = {"type": "reference", "title": "t", "description": "d", "mabolo": {"area": "infra"}}
    body = "Here is how it looks:\n\n```\nsee [[an-example]]\n```\n"
    assert "mabolo.link.wikilink" not in codes(validate_meta(meta, body=body))


def test_an_unreadable_folder_is_an_error_and_not_a_clean_run(tmp_path):
    """Skipping it quietly reported success over half a vault."""
    import os

    vault = Vault(tmp_path / "v")
    vault.initialise()
    locked = vault.root / "project" / "locked"
    locked.mkdir(parents=True)
    (locked / "x.md").write_text(
        "---\ntype: project\ndescription: A description long enough.\n"
        "mabolo:\n  area: project/locked\n---\n\nBody.\n",
        encoding="utf-8",
    )
    os.chmod(locked, 0)
    try:
        report = vault.validate()
    finally:
        os.chmod(locked, 0o755)
    assert not report.ok
    assert "vault.dir.unreadable" in codes(report.problems)


def test_one_broken_file_does_not_stop_the_others_from_being_checked(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    (vault.root / "infra" / "weird.md").write_text("---\n? [a, b]\n: x\n---\n\ntext\n", encoding="utf-8")
    (vault.root / "infra" / "fine.md").write_text(
        "---\ntype: reference\ndescription: A description long enough.\n"
        "mabolo:\n  area: infra\n---\n\nBody.\n",
        encoding="utf-8",
    )
    report = vault.validate()
    assert "okf.frontmatter.broken" in codes(report.problems)
    assert report.checked >= 2


def test_the_root_index_has_to_say_which_format_this_vault_is(tmp_path):
    """Without it, nothing says the fields mean what this validator assumes."""
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.index_file.write_text("# Vault\n\nNo frontmatter at all.\n", encoding="utf-8")
    assert "okf.version.missing" in codes(vault.validate().problems)

    vault.index_file.write_text("---\nokf_version: '0.1'\n---\n\n# Vault\n", encoding="utf-8")
    report = vault.validate()
    assert "okf.version.mismatch" in codes(report.problems)
    assert not report.ok


def test_a_value_of_the_wrong_type_is_an_error_not_a_foreign_format():
    """An unknown actor stays a warning. Something that is not text is not that."""
    problems = validate_meta(good_meta(generated={"by": 7, "at": "2026-01-01T00:00:00Z"}))
    assert "okf.generated.by.invalid" in codes(problems)
    assert any(p.is_error for p in problems if p.code == "okf.generated.by.invalid")

    problems = validate_meta(good_meta(generated={"by": "some-tool", "at": "2026-01-01T00:00:00Z"}))
    assert "okf.generated.actor" in codes(problems)
    assert not any(p.is_error for p in problems)


def test_a_tag_that_is_not_text_is_named(tmp_path):
    """The reader turned it into a Python repr, which nobody typed or searches for."""
    problems = validate_meta(good_meta(tags=[{"private": "value"}]))
    assert "okf.tags.item.invalid" in codes(problems)


def test_every_log_heading_is_checked_not_only_the_one_word_ones(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.log_file.write_text(
        "## 2026-09-01\n\nz\n\n## Two words\n\ny\n\n## 2026-01-01\n\nx\n", encoding="utf-8"
    )
    assert "okf.log.heading" in codes(vault.validate().problems)


def test_a_log_heading_in_a_code_block_is_not_a_heading(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.log_file.write_text(
        "## 2026-09-01\n\n```\n## not a heading\n```\n", encoding="utf-8"
    )
    assert "okf.log.heading" not in codes(vault.validate().problems)


def test_the_index_and_the_validator_agree_on_what_a_file_is(tmp_path):
    """One counted `.MD` and hidden folders, the other did not."""
    vault = Vault(tmp_path / "v")
    vault.initialise()
    hidden = vault.root / "infra" / ".drafts"
    hidden.mkdir()
    (hidden / "x.md").write_text(
        "---\ntype: reference\ndescription: A description long enough.\n"
        "mabolo:\n  area: infra\n---\n\nBody.\n",
        encoding="utf-8",
    )
    vault.rebuild_indexes()
    assert "infra/index.md) - 0 entries" in vault.index_file.read_text(encoding="utf-8")


def test_the_root_index_may_declare_the_vault_language(tmp_path):
    vault = Vault(tmp_path / "v", language="de")
    vault.initialise()
    assert vault.validate().ok, vault.validate().render(vault.root)
    assert vault.declared_language() == "de"


def test_the_root_index_carries_nothing_else_under_mabolo(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.index_file.write_text(
        "---\nokf_version: '0.2'\nmabolo:\n  language: en\n  area: infra\n---\n\n# Vault\n",
        encoding="utf-8",
    )
    codes = {p.code for p in vault.validate().errors}
    assert "mabolo.index.block" in codes


def test_a_language_that_is_not_a_code_is_an_error(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.index_file.write_text(
        "---\nokf_version: '0.2'\nmabolo:\n  language: Deutsch\n---\n\n# Vault\n",
        encoding="utf-8",
    )
    codes = {p.code for p in vault.validate().errors}
    assert "mabolo.index.language" in codes


def test_a_vault_that_declares_no_language_is_read_as_english(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.index_file.write_text("---\nokf_version: '0.2'\n---\n\n# Vault\n", encoding="utf-8")
    assert vault.validate().ok
    assert vault.declared_language() == "en"


def test_a_pinned_rule_without_a_seat_is_an_error_naming_the_rule(vault):
    """The payload's own line about this is read by the model. This is the one
    a person sees, and it says what to do about it."""
    for i in range(context.CORE_SEATS + 1):
        (vault.root / "persona" / f"rule-{i:02d}.md").write_text(
            entry_text(
                area="persona",
                title=f"Rule {i}",
                description="a standing rule",
                mabolo={"pin": f"2026-09-{i + 1:02d}"},
            ),
            encoding="utf-8",
        )
    report = validate_vault(vault.root)
    full = [p for p in report.problems if p.code == "mabolo.core.full"]
    assert len(full) == 1
    assert "rule-12" in str(full[0].path)
    assert "does not follow it" in full[0].message


def test_a_pin_that_names_no_day_is_a_warning_not_an_error(vault):
    """It still works, it just cannot be told apart from the oldest pin when
    the core fills up."""
    (vault.root / "persona" / "old-style.md").write_text(
        entry_text(area="persona", title="Old", description="a rule", mabolo={"pin": True}),
        encoding="utf-8",
    )
    report = validate_vault(vault.root)
    assert report.ok
    assert any(p.code == "mabolo.pin.undated" for p in report.problems)


def test_a_pin_that_is_neither_a_day_nor_a_boolean_is_an_error(vault):
    (vault.root / "persona" / "nonsense.md").write_text(
        entry_text(area="persona", title="N", description="a rule", mabolo={"pin": "someday"}),
        encoding="utf-8",
    )
    report = validate_vault(vault.root)
    assert any(p.code == "mabolo.pin.invalid" for p in report.problems)
