import pytest

from mabolo import frontmatter
from mabolo.errors import MaboloError


def test_parses_frontmatter_and_body():
    meta, body = frontmatter.parse("---\ntype: reference\n---\n\nthe text\n")
    assert meta == {"type": "reference"}
    assert body == "the text\n"


def test_a_file_without_frontmatter_keeps_all_of_its_text():
    meta, body = frontmatter.parse("# just prose\n")
    assert meta is None
    assert body == "# just prose\n"


def test_three_dashes_in_the_body_do_not_end_the_block():
    meta, body = frontmatter.parse("---\ntype: reference\n---\n\nabove\n\n---\n\nbelow\n")
    assert meta == {"type": "reference"}
    assert "below" in body and "above" in body


def test_an_unterminated_block_is_an_error_not_a_file_without_frontmatter():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse("---\ntype: reference\n\nno end\n")


def test_a_frontmatter_that_is_not_a_mapping_is_refused():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse("---\n- a list\n---\n\ntext\n")


def test_an_invalid_date_is_reported_rather_than_crashing_the_reader():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse("---\nat: 2026-02-30\n---\n\ntext\n")


def test_writing_is_byte_stable():
    meta = {"mabolo": {"area": "infra"}, "type": "reference", "title": "T"}
    first = frontmatter.dump(meta, "body")
    assert first == frontmatter.dump(meta, "body\n\n")
    # Known keys come in the documented order, whatever order they were given in.
    assert first.index("type:") < first.index("title:") < first.index("mabolo:")


def test_a_round_trip_keeps_the_body_byte_for_byte():
    text = "---\ntype: reference\nmabolo:\n  area: infra\n---\n\nline one\n\n    indented\n"
    meta, body = frontmatter.parse(text)
    assert frontmatter.parse(frontmatter.dump(meta, body))[1] == body


def test_read_reports_a_revision_that_follows_the_content(tmp_path):
    path = tmp_path / "e.md"
    path.write_text("---\ntype: reference\n---\n\na\n", encoding="utf-8")
    first = frontmatter.read(path).revision
    assert frontmatter.read(path).revision == first
    path.write_text("---\ntype: reference\n---\n\nb\n", encoding="utf-8")
    assert frontmatter.read(path).revision != first


def test_write_normalises_the_file_name_to_nfc(tmp_path):
    decomposed = tmp_path / "grün.md"  # gru + combining diaeresis
    frontmatter.write(decomposed, {"type": "reference"}, "text")
    assert (tmp_path / "grün.md").exists()


def test_a_duplicated_frontmatter_key_is_an_error_not_a_silent_loss():
    with pytest.raises(MaboloError, match="twice"):
        frontmatter.parse("---\ntitle: first\ntitle: second\n---\n\ntext\n")


def test_a_file_far_too_large_is_refused_before_it_is_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(frontmatter, "MAX_FILE_BYTES", 100)
    path = tmp_path / "big.md"
    path.write_text("---\ntype: reference\n---\n\n" + "x" * 500, encoding="utf-8")
    with pytest.raises(MaboloError, match="byte limit"):
        frontmatter.read(path)


def test_a_yaml_error_does_not_quote_the_line_it_found(tmp_path):
    """Error text travels into reports, and the offending line is where a secret sits."""
    with pytest.raises(MaboloError) as caught:
        frontmatter.parse('---\nsecret: "swordfish\n---\n\ntext\n')
    assert "swordfish" not in str(caught.value)
