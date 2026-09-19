"""The tools a client sees, and the gate in front of the four that write."""

import anyio
import pytest
from mcp import Client

from mabolo import consent, frontmatter, validate
from mabolo.server import MAX_READ, Settings, build

QUOTE = "releases are cut from main only"
PROMPT = f"please remember that {QUOTE}, and never from a tag"


@pytest.fixture
def prompts(tmp_path):
    where = tmp_path / "prompts"
    consent.record(PROMPT, cwd=tmp_path, session="s", directory=where)
    return where


@pytest.fixture
def server(git_vault, prompts, tmp_path):
    return build(
        git_vault,
        Settings(actor="human:alex", cwd=tmp_path, session="s", prompts=prompts),
    )


def call(server, name: str, arguments: dict) -> str:
    """One tool call, through a real client, and the text it answered with."""

    async def go() -> str:
        async with Client(server) as client:
            result = await client.call_tool(name, arguments)
            return "\n".join(part.text for part in result.content if hasattr(part, "text"))

    return anyio.run(go)


def tool_names(server) -> list[str]:
    async def go() -> list[str]:
        async with Client(server) as client:
            return [tool.name for tool in (await client.list_tools()).tools]

    return anyio.run(go)


def written(server, **over) -> str:
    arguments = {
        "area": "infra",
        "name": "deploy-from-main",
        "description": "Releases are cut from main, never from a tag",
        "body": "Releases are cut from `main`.",
        "quote": QUOTE,
    }
    arguments.update(over)
    return call(server, "mabolo_write", arguments)


def test_read_mode_does_not_offer_a_way_to_write(git_vault, prompts, tmp_path):
    reading = build(
        git_vault,
        Settings(actor="human:alex", cwd=tmp_path, session="s", prompts=prompts, read_only=True),
    )
    assert tool_names(reading) == ["mabolo_search", "mabolo_read"]


def test_the_full_server_offers_the_four_that_write(server):
    assert tool_names(server) == [
        "mabolo_search",
        "mabolo_read",
        "mabolo_write",
        "mabolo_edit",
        "mabolo_forget",
        "journal_add",
    ]


def test_a_quoted_sentence_is_written_and_carries_its_evidence(server, git_vault):
    answer = written(server)
    assert "infra/deploy-from-main.md" in answer
    document = frontmatter.read(git_vault.root / "infra" / "deploy-from-main.md")
    assert document.meta["verified"] == [
        {"by": "human:alex", "at": document.meta["verified"][0]["at"]}
    ]
    assert document.meta["sources"][0]["id"] == "q1"
    assert f'[^q1]: "{QUOTE}"' in document.body
    assert "[^q1]" in document.body.split("\n")[0]
    assert git_vault.validate().ok


def test_a_sentence_nobody_typed_writes_nothing_at_all(server, git_vault):
    before = git_vault.git("rev-parse", "HEAD").stdout
    answer = written(server, quote="the model made this sentence up entirely")
    assert "nothing was written" in answer
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()
    assert git_vault.git("rev-parse", "HEAD").stdout == before


def test_a_paraphrase_is_not_a_quote(server, git_vault):
    answer = written(server, quote="releases come from the main branch only")
    assert "nothing was written" in answer
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()


def test_what_the_validator_rejects_never_reaches_the_vault(server, git_vault):
    answer = written(server, area="not-an-area")
    assert "refused" in answer
    assert not list((git_vault.root).glob("not-an-area/*"))


def test_an_entry_the_validator_calls_an_error_is_refused(server, git_vault):
    """The area is checked by the path rules before the validator ever runs.
    This one is only an error to the validator, so it is what says the tools
    are behind the same gate as every other way into the vault."""
    answer = written(server, body="x" * (validate.MAX_BODY + 1))
    assert "refused" in answer
    assert "error" in answer
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()


def test_a_read_hands_back_the_revision_an_edit_needs(server, git_vault):
    written(server)
    answer = call(server, "mabolo_read", {"names": ["deploy-from-main"]})
    document = frontmatter.read(git_vault.root / "infra" / "deploy-from-main.md")
    assert f"revision {document.revision}" in answer


def test_an_edit_against_an_old_revision_is_refused(server, git_vault):
    written(server)
    answer = call(server, "mabolo_edit", {
        "name": "deploy-from-main", "old": "Releases are cut", "new": "Nothing is cut",
        "quote": QUOTE, "revision": "0" * 12,
    })
    assert "not 000000000000" in answer
    assert "Nothing is cut" not in (git_vault.root / "infra" / "deploy-from-main.md").read_text()


def test_an_edit_replaces_one_passage_and_leaves_the_prose_alone(server, git_vault):
    written(server, body="One sentence. Another sentence. A third one.")
    path = git_vault.root / "infra" / "deploy-from-main.md"
    revision = frontmatter.read(path).revision
    answer = call(server, "mabolo_edit", {
        "name": "deploy-from-main", "old": "Another sentence.", "new": "A replaced sentence.",
        "quote": QUOTE, "revision": revision,
    })
    assert "refused" not in answer
    document = frontmatter.read(path)
    assert "One sentence. A replaced sentence. A third one." in document.body
    assert f'[^q1]: "{QUOTE}"' in document.body
    # The same sentence approving two changes is one approval, not two.
    assert len(document.meta["verified"]) == 1


def test_an_ambiguous_passage_is_refused_rather_than_guessed(server, git_vault):
    written(server, body="The same words. The same words.")
    path = git_vault.root / "infra" / "deploy-from-main.md"
    revision = frontmatter.read(path).revision
    answer = call(server, "mabolo_edit", {
        "name": "deploy-from-main", "old": "The same words.", "new": "Different words.",
        "quote": QUOTE, "revision": revision,
    })
    assert "2 times" in answer
    assert "Different words." not in path.read_text(encoding="utf-8")


def test_a_passage_that_is_not_there_is_refused(server, git_vault):
    written(server)
    path = git_vault.root / "infra" / "deploy-from-main.md"
    answer = call(server, "mabolo_edit", {
        "name": "deploy-from-main", "old": "nothing like this", "new": "x",
        "quote": QUOTE, "revision": frontmatter.read(path).revision,
    })
    assert "not in" in answer


def test_an_edit_needs_a_quote_of_its_own(server, git_vault):
    written(server)
    path = git_vault.root / "infra" / "deploy-from-main.md"
    answer = call(server, "mabolo_edit", {
        "name": "deploy-from-main", "old": "Releases are cut", "new": "Nothing is cut",
        "quote": "a sentence that was never typed", "revision": frontmatter.read(path).revision,
    })
    assert "nothing was written" in answer
    assert "Nothing is cut" not in path.read_text(encoding="utf-8")


def test_forgetting_removes_the_file_and_names_what_pointed_at_it(server, git_vault):
    written(server)
    written(server, name="other-entry", body="See [deploy-from-main](deploy-from-main.md).")
    path = git_vault.root / "infra" / "deploy-from-main.md"
    answer = call(server, "mabolo_forget", {
        "name": "deploy-from-main", "quote": QUOTE, "revision": frontmatter.read(path).revision,
        "reason": "moved to the handbook",
    })
    assert "is gone" in answer
    assert "other-entry" in answer
    assert not path.exists()
    assert "moved to the handbook" in git_vault.git("log", "--format=%s", "-1").stdout


def test_a_journal_line_needs_no_quote(server, git_vault):
    answer = call(server, "journal_add", {"line": "wrote down where releases come from",
                                          "project": "atlas"})
    assert "noted under" in answer
    text = git_vault.log_file.read_text(encoding="utf-8")
    assert "[atlas](project/atlas/index.md): wrote down where releases come from" in text
    assert git_vault.validate().ok


def test_a_search_that_finds_nothing_says_nothing(server):
    assert "nothing" in call(server, "mabolo_search", {"query": "quantum tunnelling"})


def test_a_search_offers_lines_and_the_price_of_reading_them(server, git_vault):
    written(server)
    answer = call(server, "mabolo_search", {"query": "releases cut from main"})
    assert "deploy-from-main" in answer
    assert "tokens" in answer


def test_a_read_of_everything_is_refused(server):
    answer = call(server, "mabolo_read", {"names": ["a"] * (MAX_READ + 1)})
    assert "at most" in answer
    assert call(server, "mabolo_read", {"names": []}) == "name at least one entry."


def test_an_unknown_name_is_a_sentence_not_a_traceback(server):
    assert "no entry called" in call(server, "mabolo_read", {"names": ["nothing-like-this"]})
