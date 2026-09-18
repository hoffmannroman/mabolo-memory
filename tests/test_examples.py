"""The example vault in `examples/` has to stay correct.

It is the thing somebody reads to understand what ends up on disk, and the only
realistic material the tests have. An example that drifts out of the format
teaches the wrong shape to every person who clones this.
"""

from pathlib import Path

from mabolo.validate import validate_vault
from mabolo.vault import Vault

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "vault"


def test_the_example_vault_is_valid():
    report = validate_vault(EXAMPLE)
    assert report.ok, report.render(EXAMPLE)
    assert not report.warnings, report.render(EXAMPLE)


def test_the_example_vault_shows_every_shape_that_matters():
    entries = {entry.name: entry for entry in Vault(EXAMPLE).entries()}
    assert {"user", "reference", "feedback", "project"} <= {e.type for e in entries.values()}
    design = entries["no-centred-layouts"]
    assert design.is_design and design.mabolo.applies_to and design.mabolo.instead_of
    quoted = entries["deploy-from-main"]
    assert quoted.verified and quoted.footnote_ids() == {"s1"}
    assert entries["ci-memory-limit"].mabolo.anchor
    assert any(e.area.startswith("project/") for e in entries.values())


def test_the_generated_indexes_match_the_entries():
    """The index is derived, so rebuilding it must change nothing."""
    before = {p: p.read_bytes() for p in EXAMPLE.rglob("index.md")}
    Vault(EXAMPLE).rebuild_indexes()
    assert {p: p.read_bytes() for p in EXAMPLE.rglob("index.md")} == before
