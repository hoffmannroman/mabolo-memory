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
    result = vault.git_initialise()
    assert result.completed and result.revision
    assert "committed" in result.message
    assert vault.is_git_repository()
    assert vault.git_initialise().message.startswith("nothing to commit")


def test_without_a_git_identity_nothing_is_committed_and_it_says_so(tmp_path, monkeypatch):
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    vault = Vault(tmp_path / "v")
    vault.initialise()
    result = vault.git_initialise()
    assert not result.completed
    assert "user.email" in result.message


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
    assert vault.git_initialise().completed
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


# What an audit found, and what now holds instead. Each of these failed before
# the fix above it, which is the only reason to keep them.


def test_reading_an_entry_and_writing_it_back_does_not_repair_it(vault):
    """The one that matters most: a rewrite must not drop what it cannot parse.

    The tolerant read turns an incomplete `generated` block into None and a
    `tags: 7` into an empty list. Writing the tidied view back deleted both and
    left a file the validator then called clean. Provenance was the first thing
    to go.
    """
    path = vault.root / "infra" / "half.md"
    path.write_text(
        "---\n"
        "type: reference\n"
        "title: Half\n"
        "description: A description long enough to pass.\n"
        "generated:\n"
        "  by: some-tool/1.0\n"
        "verified:\n"
        "  - by: human:someone\n"
        "tags: 7\n"
        "mabolo:\n"
        "  area: infra\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    entry = vault.read_entry(path)
    with pytest.raises(MaboloError, match="error"):
        vault.write_entry(entry, path)
    assert path.read_bytes() == before


def test_what_the_tolerant_read_dropped_survives_a_deliberate_rewrite(vault):
    """With `allow_findings` the caller reports the findings, and still loses nothing."""
    path = vault.root / "infra" / "half.md"
    path.write_text(
        "---\ntype: reference\ndescription: A description long enough to pass.\n"
        "generated:\n  by: some-tool/1.0\n"
        "mabolo:\n  area: infra\n---\n\nBody.\n",
        encoding="utf-8",
    )
    vault.write_entry(vault.read_entry(path), path, allow_findings=True)
    assert "some-tool/1.0" in path.read_text(encoding="utf-8")


def test_an_entry_name_may_not_be_a_path(vault):
    """`../persona/victim` stays inside the vault, so containment alone allows it."""
    victim = vault.path_for("persona", "victim")
    victim.write_text(
        "---\ntype: user\ndescription: Precious.\nmabolo:\n  area: persona\n---\n\nPrecious.\n",
        encoding="utf-8",
    )
    before = victim.read_bytes()
    with pytest.raises(MaboloError, match="not a usable entry name"):
        vault.path_for("infra", "../persona/victim")
    assert victim.read_bytes() == before


def test_an_entry_is_never_written_outside_its_own_area(vault):
    entry = Entry(
        type="reference",
        title="T",
        description="A description long enough to pass.",
        mabolo=MaboloBlock(area="infra"),
        body="Body.",
    )
    for target, expected in [
        (vault.root / "persona" / "wrong.md", "path says"),
        (vault.root / "loose.md", "area folder"),
        (vault.root / "elsewhere" / "x.md", "not an area"),
        (vault.root / ".git" / "config", "not a Markdown file"),
        (vault.root / "infra" / "Upper Name.md", "not a usable entry name"),
    ]:
        with pytest.raises(MaboloError, match=expected):
            vault.write_entry(entry, target)


def test_an_entry_without_a_path_is_refused_rather_than_hidden(vault):
    """It used to land in `<area>/.md`, which validate never looks at."""
    entry = Entry(
        type="reference",
        title="T",
        description="A description long enough to pass.",
        mabolo=MaboloBlock(area="infra"),
        body="Body.",
    )
    with pytest.raises(MaboloError, match="needs a name"):
        vault.write_entry(entry)
    assert not (vault.root / "infra" / ".md").exists()


def test_a_hand_written_index_is_not_replaced_by_a_generated_one(vault):
    """Both of these were overwritten, and their contents gone."""
    hand_written = vault.root / "project" / "notes" / "index.md"
    hand_written.parent.mkdir(parents=True)
    hand_written.write_text("---\ntitle: my own index\n---\n\nHand written.\n", encoding="utf-8")
    (hand_written.parent / "a-note.md").write_text(
        "---\ntype: project\ndescription: A description long enough.\n"
        "mabolo:\n  area: project/notes\n---\n\nBody.\n",
        encoding="utf-8",
    )
    with pytest.raises(MaboloError, match="does not write"):
        vault.rebuild_indexes()
    assert "Hand written." in hand_written.read_text(encoding="utf-8")

    hand_written.write_text("---\ntitle: a\ntitle: b\n---\n\nUnreadable.\n", encoding="utf-8")
    with pytest.raises(MaboloError, match="cannot be read"):
        vault.rebuild_indexes()
    assert "Unreadable." in hand_written.read_text(encoding="utf-8")


def test_init_refuses_a_folder_that_belongs_to_something_else(tmp_path):
    """One mistyped path, and every index.md below it would be rewritten."""
    alien = tmp_path / "website"
    (alien / "docs").mkdir(parents=True)
    landing = alien / "docs" / "index.md"
    landing.write_text("# Welcome\n\nHand written landing page.\n", encoding="utf-8")
    with pytest.raises(MaboloError, match="not a vault"):
        Vault(alien).initialise()
    assert "Hand written landing page." in landing.read_text(encoding="utf-8")


def test_the_plan_names_every_index_that_init_rewrites(vault):
    """The plan is the consent, so it may not leave out what the run will touch."""
    extra = vault.root / "project" / "atlas"
    extra.mkdir(parents=True)
    (extra / "atlas.md").write_text(
        "---\ntype: project\ndescription: A description long enough.\n"
        "mabolo:\n  area: project/atlas\n---\n\nBody.\n",
        encoding="utf-8",
    )
    planned = {action.target for action in vault.plan()}
    written = {str(p) for p in vault.rebuild_indexes()}
    assert written <= planned


def test_entries_refuses_to_look_complete_when_it_is_not(vault):
    path = vault.root / "infra" / "broken.md"
    path.write_text("---\ntitle: a\ntitle: b\n---\n\nBroken.\n", encoding="utf-8")
    with pytest.raises(MaboloError, match="not every entry"):
        vault.entries()
    readable, unreadable = vault.readable_entries()
    assert readable == [] and unreadable == [path]


def test_init_does_not_commit_what_somebody_else_had_staged(tmp_path, git_identity):
    """`git commit` without paths picked up the person's own staged work."""
    root = tmp_path / "v"
    root.mkdir()
    vault = Vault(root)
    vault.git("init", "-b", "main")
    # Not Markdown: a folder that already holds Markdown is refused outright,
    # which is a different rule, tested next to this one.
    private = root / "private.txt"
    private.write_text("not for this commit\n", encoding="utf-8")
    vault.git("add", "--", "private.txt")
    vault.initialise()
    assert vault.git_initialise().completed
    committed = vault.git("show", "--name-only", "--format=", "HEAD").stdout.split()
    assert "private.txt" not in committed
    assert "index.md" in committed
    # And it is still staged, because nothing here is allowed to unstage it.
    assert "private.txt" in vault.git("diff", "--cached", "--name-only").stdout.split()


def test_a_repository_that_lives_outside_the_vault_is_refused(tmp_path, git_identity):
    """`.git` can be a file pointing anywhere, and a commit then runs its hooks."""
    root = tmp_path / "v"
    root.mkdir()
    elsewhere = tmp_path / "external.git"
    Vault(root).git("init", f"--separate-git-dir={elsewhere}", ".")
    vault = Vault(root)
    assert not vault.is_git_repository()
    vault.initialise()
    result = vault.git_initialise()
    assert not result.completed and "outside the vault" in result.message
    assert not (elsewhere / "refs" / "heads" / "main").exists()


def test_a_vault_without_a_commit_is_not_reported_as_a_success(tmp_path, monkeypatch, capsys):
    """Git was asked for, did not happen, and the run exited 0 anyway."""
    from mabolo.cli import main

    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    code = main(["--config", str(tmp_path / "c.toml"), "init", "--vault", str(tmp_path / "v"),
                 "--actor", "human:someone", "--yes"])
    assert code != 0
    assert "no commit" in capsys.readouterr().out


def test_rebuilding_an_index_keeps_what_the_vault_says_about_itself(tmp_path):
    """Every command but `init` leaves the declaration alone."""
    vault = Vault(tmp_path / "v", language="de")
    vault.initialise()
    Vault(tmp_path / "v").rebuild_indexes()
    assert Vault(tmp_path / "v").declared_language() == "de"


def test_a_root_index_with_a_foreign_frontmatter_is_still_refused(tmp_path):
    """The block that was allowed in is `mabolo`, not anything a person wrote."""
    vault = Vault(tmp_path / "v")
    vault.initialise()
    vault.index_file.write_text("---\ntitle: my own landing page\n---\n\nmine\n", encoding="utf-8")
    with pytest.raises(MaboloError, match="does not write"):
        vault.rebuild_indexes()
