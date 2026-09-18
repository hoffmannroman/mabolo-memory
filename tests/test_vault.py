import pytest

from mabolo.errors import MaboloError
from mabolo.schema import Entry, MaboloBlock
from mabolo.vault import Vault


def test_init_creates_a_skeleton_that_validates(tmp_path):
    vault = Vault(tmp_path / "v")
    assert not vault.is_initialised()
    vault.initialise()
    assert vault.is_initialised()
    assert vault.log_file.exists()
    assert (vault.root / ".gitignore").exists()
    assert vault.eval_dir.is_dir()
    assert vault.validate().ok


def test_the_plan_touches_nothing(tmp_path):
    vault = Vault(tmp_path / "v")
    plan = vault.plan()
    assert not vault.root.exists()
    assert all(action.kind == "create" for action in plan)


def test_running_init_twice_changes_nothing(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    before = {p: p.read_bytes() for p in vault.root.rglob("*") if p.is_file()}
    vault.initialise()
    after = {p: p.read_bytes() for p in vault.root.rglob("*") if p.is_file()}
    assert before == after


def test_the_root_index_declares_the_format_version(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    text = vault.index_file.read_text(encoding="utf-8")
    assert "okf_version: '0.2'" in text
    assert "* [persona](persona/index.md)" in text


def test_an_index_lists_entries_with_their_description(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    entry = Entry(
        type="reference",
        title="Deploy from main only",
        description="Releases are cut from main",
        mabolo=MaboloBlock(area="infra"),
        body="Text.",
        path=vault.path_for("infra", "deploy-from-main"),
    )
    vault.write_entry(entry)
    vault.rebuild_indexes()
    index = (vault.root / "infra" / "index.md").read_text(encoding="utf-8")
    assert "* [Deploy from main only](deploy-from-main.md) - Releases are cut from main" in index
    assert "1 entries" in vault.index_file.read_text(encoding="utf-8")


def test_an_entry_cannot_be_written_outside_the_vault(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    entry = Entry(type="reference", description="d", mabolo=MaboloBlock(area="infra"))
    with pytest.raises(MaboloError, match="outside the vault"):
        vault.write_entry(entry, path=tmp_path / "elsewhere.md")


def test_entries_are_read_back_with_their_revision(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    path = vault.write_entry(
        Entry(
            type="reference",
            title="T",
            description="d",
            mabolo=MaboloBlock(area="persona"),
            body="Text.",
            path=vault.path_for("persona", "someone"),
        )
    )
    read = vault.read_entry(path)
    assert read.name == "someone" and read.area == "persona" and read.revision


def test_git_makes_one_commit_of_the_skeleton(tmp_path, git_identity):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    message = vault.git_initialise()
    assert "committed" in message
    assert vault.is_git_repository()
    assert vault.git_initialise().startswith("nothing to commit")


def test_without_a_git_identity_nothing_is_committed_and_it_says_so(tmp_path, monkeypatch):
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    vault = Vault(tmp_path / "v")
    vault.initialise()
    assert "user.email" in vault.git_initialise()


def test_a_remote_can_be_set_and_moved(tmp_path, git_identity):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.git_initialise()
    assert "set to" in vault.git_set_remote("git@example.invalid:me/a.git")
    assert "already points" in vault.git_set_remote("git@example.invalid:me/a.git")
    assert "moved from" in vault.git_set_remote("git@example.invalid:me/b.git")


def test_an_area_may_not_be_a_path_out_of_the_vault(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.md").write_text("not yours\n", encoding="utf-8")
    with pytest.raises(MaboloError, match="plain folder name"):
        Vault(tmp_path / "v", areas=("../outside",)).initialise()
    assert (outside / "index.md").read_text(encoding="utf-8") == "not yours\n"


def test_an_area_folder_that_is_a_symlink_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.md").write_text("not yours\n", encoding="utf-8")
    vault = Vault(tmp_path / "v")
    vault.root.mkdir(parents=True)
    (vault.root / "infra").symlink_to(outside)
    with pytest.raises(MaboloError):
        vault.initialise()
    assert (outside / "index.md").read_text(encoding="utf-8") == "not yours\n"


def test_an_entry_may_not_be_written_over_a_generated_index(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    entry = Entry(type="reference", title="t", description="d", mabolo=MaboloBlock(area="infra"))
    with pytest.raises(MaboloError, match="reserved"):
        vault.write_entry(entry, path=vault.root / "infra" / "index.md")


def test_the_plan_says_an_index_is_rewritten_rather_than_left_alone(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    assert any(a.kind == "rewrite" and a.target.endswith("index.md") for a in vault.plan())


def test_a_title_cannot_add_a_line_to_the_index(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.path_for("infra", "tricky").write_text(
        "---\ntype: reference\ntitle: 'Config [prod] (v2)'\ndescription: d\nmabolo:\n  area: infra\n---\n\nText.\n",
        encoding="utf-8",
    )
    vault.rebuild_indexes()
    line = [
        line
        for line in (vault.root / "infra" / "index.md").read_text(encoding="utf-8").splitlines()
        if "tricky.md" in line
    ][0]
    assert line.endswith("(tricky.md) - d")
    assert "\\[prod\\]" in line


def test_git_failure_reads_as_a_sentence_not_a_traceback(tmp_path, git_identity, monkeypatch):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    with pytest.raises(MaboloError, match="git"):
        vault.git("rev-parse", "--verify", "definitely-not-a-ref")


def test_init_commits_only_the_files_it_made(tmp_path, git_identity):
    vault = Vault(tmp_path / "v")
    vault.initialise()
    stray = vault.root / "infra" / "not-mine.md"
    stray.write_text("someone else's file\n", encoding="utf-8")
    vault.git_initialise()
    tracked = vault.git("ls-files").stdout.split()
    assert "infra/not-mine.md" not in tracked
    assert "infra/index.md" in tracked


def test_an_entry_with_errors_is_not_written_by_accident(tmp_path):
    """A read, a small change and a write must not repair the rest of a file."""
    vault = Vault(tmp_path / "v")
    vault.initialise()
    broken = Entry(type="reference", title="t", description="", mabolo=MaboloBlock(area="infra"))
    broken.path = vault.path_for("infra", "broken")
    with pytest.raises(MaboloError, match="was not written"):
        vault.write_entry(broken)
    assert not broken.path.exists()
    vault.write_entry(broken, allow_findings=True)
    assert broken.path.exists()


def test_an_entry_keeps_the_mapping_it_was_read_from(tmp_path):
    """A mutation patches this field by field instead of writing a tidied copy back."""
    vault = Vault(tmp_path / "v")
    vault.initialise()
    path = vault.path_for("infra", "handwritten")
    path.write_text(
        "---\ntype: reference\ntitle: t\ndescription: d\n"
        "verified:\n  - by: human:x\n"  # no `at`, so the tidied view drops it
        "mabolo:\n  area: infra\n---\n\nText.\n",
        encoding="utf-8",
    )
    entry = vault.read_entry(path)
    assert entry.verified == []  # the tidied view
    assert entry.raw["verified"] == [{"by": "human:x"}]  # what the file actually says
