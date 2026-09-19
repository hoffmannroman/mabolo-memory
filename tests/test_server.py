"""The tools a client sees, and the gate in front of the four that write."""

import subprocess

import anyio
import pytest
from mcp import Client

from conftest import entry_text
from mabolo import consent, frontmatter, inbox, validate
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
    assert tool_names(reading) == ["mabolo_search", "mabolo_read", "mabolo_propose"]


def test_the_full_server_offers_the_four_that_write(server):
    assert tool_names(server) == [
        "mabolo_search",
        "mabolo_read",
        "mabolo_propose",
        "mabolo_write",
        "mabolo_edit",
        "mabolo_forget",
        "mabolo_decide",
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


# Proposing and deciding: what an agent may do alone, and what it may only
# relay.


ENTRY_TEXT = """---
type: reference
title: Deploy from main only
description: Releases are cut from main, never from a tag
status: stable
mabolo:
  area: infra
---

Releases are cut from `main`.
"""


def propose(server, **over) -> str:
    arguments = {
        "action": "write",
        "target": "infra/deploy-from-main",
        "quote": QUOTE,
        "entry": ENTRY_TEXT,
    }
    arguments.update(over)
    return call(server, "mabolo_propose", arguments)


def test_an_agent_alone_may_propose_and_the_vault_does_not_move(git_vault, prompts, tmp_path):
    reading = build(
        git_vault,
        Settings(actor="human:alex", cwd=tmp_path, session="s", prompts=prompts, read_only=True),
    )
    before = git_vault.git("rev-parse", "HEAD").stdout
    answer = propose(reading)
    assert "waiting for an answer" in answer
    assert git_vault.git("rev-parse", "HEAD").stdout == before
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()
    assert len(inbox.pending(git_vault.root)) == 1


def test_the_same_suggestion_twice_is_one_thing_to_read(server, git_vault):
    propose(server)
    assert "already waiting" in propose(server)
    assert len(inbox.pending(git_vault.root)) == 1


def test_a_verdict_without_the_id_in_it_decides_nothing(server, git_vault, prompts, tmp_path):
    """A bare yes is a signature of nothing. The id is the evidence that the
    person looked at the inbox."""
    propose(server)
    waiting = inbox.pending(git_vault.root)[0]
    consent.record("yes go ahead with that", cwd=tmp_path, session="s", directory=prompts)
    answer = call(server, "mabolo_decide", {
        "id": waiting.id, "verdict": "yes", "quote": "yes go ahead with that",
    })
    assert "no prompt on this machine answers" in answer
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()


def test_the_word_has_to_stand_next_to_the_id(server, git_vault, prompts, tmp_path):
    """"a3f2 yesterday we reviewed it" holds the id and holds "yes", and means
    neither. The neighbours are what a person means by an answer."""
    propose(server)
    waiting = inbox.pending(git_vault.root)[0]
    said = f"{waiting.id} yesterday we reviewed it"
    consent.record(said, cwd=tmp_path, session="s", directory=prompts)
    answer = call(server, "mabolo_decide", {"id": waiting.id, "verdict": "yes", "quote": said})
    assert "no prompt on this machine answers" in answer
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()


def test_one_sentence_answering_two_proposals_answers_each_as_it_was_written(
    server, git_vault, prompts, tmp_path
):
    """The documented shape is "a3f2 yes, 7c01 no". Reading the id and the word
    as two independent facts let a call approve the one the person refused."""
    propose(server)
    propose(
        server,
        target="infra/other-entry",
        entry=ENTRY_TEXT.replace("Deploy from main only", "Other").replace(
            "Releases are cut from main, never from a tag", "Something else entirely"
        ),
    )
    waiting = inbox.pending(git_vault.root)
    yes, no = waiting[0], waiting[1]
    said = f"{yes.id} yes, {no.id} no"
    consent.record(said, cwd=tmp_path, session="s", directory=prompts)

    wrong = call(server, "mabolo_decide", {"id": no.id, "verdict": "yes", "quote": said})
    assert "the person said 'no'" in wrong
    right = call(server, "mabolo_decide", {"id": yes.id, "verdict": "yes", "quote": said})
    assert "approved" in right


def test_an_id_answered_both_ways_is_refused_rather_than_resolved(
    server, git_vault, prompts, tmp_path
):
    propose(server)
    waiting = inbox.pending(git_vault.root)[0]
    said = f"{waiting.id} yes, on second thought {waiting.id} no"
    consent.record(said, cwd=tmp_path, session="s", directory=prompts)
    answer = call(server, "mabolo_decide", {"id": waiting.id, "verdict": "yes", "quote": said})
    assert "answered both ways" in answer
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()


def test_a_verdict_nobody_typed_decides_nothing(server, git_vault, prompts, tmp_path):
    propose(server)
    waiting = inbox.pending(git_vault.root)[0]
    answer = call(server, "mabolo_decide", {
        "id": waiting.id, "verdict": "yes", "quote": f"{waiting.id} yes",
    })
    assert "nothing was written" in answer
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()


def test_a_verdict_the_person_typed_writes_the_entry(server, git_vault, prompts, tmp_path):
    propose(server)
    waiting = inbox.pending(git_vault.root)[0]
    said = f"{waiting.id[:4]}: yes"
    consent.record(said, cwd=tmp_path, session="s", directory=prompts)
    answer = call(server, "mabolo_decide", {
        "id": waiting.id[:4], "verdict": "yes", "quote": said,
    })
    assert "approved" in answer
    assert (git_vault.root / "infra" / "deploy-from-main.md").exists()
    assert inbox.pending(git_vault.root) == []


def test_a_verdict_that_is_neither_yes_nor_no_is_refused(server, git_vault):
    propose(server)
    waiting = inbox.pending(git_vault.root)[0]
    assert "yes or no" in call(server, "mabolo_decide", {
        "id": waiting.id, "verdict": "maybe", "quote": f"{waiting.id} maybe",
    })


def test_an_id_that_names_nothing_is_a_sentence(server):
    assert "no open proposal" in call(server, "mabolo_decide", {
        "id": "beef", "verdict": "yes", "quote": "beef yes",
    })


def test_a_read_says_when_the_file_an_entry_watches_has_moved(git_vault, prompts, tmp_path):
    """Here and not in the session index. At read time the reader is about to
    rely on the text, which is the moment the question matters."""
    repository = tmp_path / "work"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    watched = repository / "build.yml"
    watched.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "later than the entry"],
                   check=True)

    area = git_vault.root / "project" / "work"
    area.mkdir(parents=True)
    (area / "watched.md").write_text(
        entry_text(
            area="project/work",
            description="d",
            generated={"by": "mabolo/0.1.0", "at": "2020-01-01T00:00:00+00:00"},
            mabolo={"anchor": "build.yml"},
        ),
        encoding="utf-8",
    )
    server = build(
        git_vault,
        Settings(actor="human:alex", cwd=repository, session="s", prompts=prompts),
    )
    answer = call(server, "mabolo_read", {"names": ["watched"]})
    assert "the file it watches moved" in answer
    assert "build.yml" in answer


def test_a_read_of_an_entry_that_watches_nothing_says_nothing_extra(server, git_vault):
    written(server)
    answer = call(server, "mabolo_read", {"names": ["deploy-from-main"]})
    assert ">" not in answer.split("---")[-1]


def test_a_verdict_somewhere_else_in_the_sentence_is_not_an_answer(
    server, git_vault, prompts, tmp_path
):
    """"yes" has to stand next to the id. A sentence that says yes about
    something else entirely, and mentions a proposal in passing, is not an
    answer to that proposal."""
    propose(server)
    waiting = inbox.pending(git_vault.root)[0]
    said = f"yes I saw {waiting.id} and I will look at it tomorrow"
    consent.record(said, cwd=tmp_path, session="s", directory=prompts)
    answer = call(server, "mabolo_decide", {"id": waiting.id, "verdict": "yes", "quote": said})
    assert "no prompt on this machine answers" in answer
    assert not (git_vault.root / "infra" / "deploy-from-main.md").exists()
