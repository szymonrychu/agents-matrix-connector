"""Approvals foundation: verdict parsers, texts and the taskboard delegate client."""

import httpx
import pytest

from agents_matrix_connector.approvals import (
    GO,
    NO_GO,
    TaskboardDelegate,
    TaskboardError,
    approval_text,
    digest_text,
    digest_verdict,
    reaction_verdict,
    text_verdict,
)
from agents_matrix_connector.fake_taskboard import FakeTaskboard


def test_reaction_verdicts():
    assert reaction_verdict("\N{THUMBS UP SIGN}") == GO
    assert reaction_verdict("\N{THUMBS UP SIGN}\N{EMOJI MODIFIER FITZPATRICK TYPE-4}") == GO
    assert reaction_verdict("\N{THUMBS UP SIGN}\N{VARIATION SELECTOR-16}") == GO
    assert reaction_verdict("\N{WHITE HEAVY CHECK MARK}") == GO
    assert reaction_verdict("\N{THUMBS DOWN SIGN}") == NO_GO
    assert reaction_verdict("\N{CROSS MARK}") == NO_GO
    assert reaction_verdict("\N{HEAVY BLACK HEART}") is None


def test_text_verdicts():
    texts = ("Go", "go!", "no-go", "No go.", "go but wait", "> quoted\n\ngo")
    assert [text_verdict(t) for t in texts] == [GO, GO, NO_GO, NO_GO, None, GO]


def test_trailing_punctuation_after_space_is_ignored():
    assert [text_verdict(t) for t in ("go .", "no-go !", "go . !")] == [GO, NO_GO, GO]
    assert digest_verdict("all go .") == GO


def test_digest_verdict():
    assert [digest_verdict(t) for t in ("all go", "All go!", "go")] == [GO, GO, None]


def test_approval_and_digest_texts():
    item = {
        "id": 412,
        "asking_slug": "mx-88",
        "task_key": "T-88",
        "question": "merge PR 12?",
        "subject": None,
    }
    tail = (
        " React with \N{THUMBS UP SIGN} for Go or \N{THUMBS DOWN SIGN} for No-go,"
        " or reply to this message with go or no-go."
    )
    assert approval_text(item) == "Approval D-412 from mx-88 (T-88): merge PR 12?" + tail
    with_subject = approval_text({**item, "subject": "pr-12", "asking_slug": None})
    assert with_subject == (
        "Approval D-412 from a session (T-88): merge PR 12? Subject: pr-12." + tail
    )
    assert digest_text([412, 413]) == (
        "Open approvals D-412, D-413."
        " Reply to this message with all go to answer Go to exactly these."
    )


async def test_delegate_client_paths_and_source():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"items": [{"id": 1}]})
        return httpx.Response(200, json={"items": [{"id": 1, "result": "answered"}]})

    client = TaskboardDelegate("http://tb:8003/", "tok", transport=httpx.MockTransport(handler))
    assert await client.open_approvals() == [{"id": 1}]
    items = [{"id": 1, "verdict": GO}]
    assert await client.answer(items) == [{"id": 1, "result": "answered"}]
    await client.aclose()
    get, post = seen
    assert get.url.path == "/taskboard/api/v1/approvals"
    assert get.url.query == b"status=open&limit=200"
    assert post.url.path == "/taskboard/api/v1/approvals/answer"
    assert post.read() == b'{"items":[{"id":1,"verdict":"go"}],"source":"matrix"}'
    assert get.headers["Authorization"] == "Bearer tok"
    assert post.headers["Authorization"] == "Bearer tok"


async def test_delegate_client_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/answer"):
            return httpx.Response(422, json={})
        return httpx.Response(500)

    client = TaskboardDelegate("http://tb", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(TaskboardError) as server:
        await client.open_approvals()
    assert server.value.status == 500 and server.value.retryable
    with pytest.raises(TaskboardError) as invalid:
        await client.answer([{"id": 1, "verdict": GO}])
    assert invalid.value.status == 422 and not invalid.value.retryable

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    down = TaskboardDelegate("http://tb", "t", transport=httpx.MockTransport(refuse))
    with pytest.raises(TaskboardError) as unreachable:
        await down.open_approvals()
    assert unreachable.value.status is None and unreachable.value.retryable


async def test_delegate_client_refuses_items_that_are_not_a_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": {"id": 1, "result": "answered"}})

    client = TaskboardDelegate("http://tb", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(TaskboardError) as listed:
        await client.open_approvals()
    assert listed.value.status == 200 and not listed.value.retryable
    with pytest.raises(TaskboardError) as answered:
        await client.answer([{"id": 1, "verdict": GO}])
    assert answered.value.status == 200 and not answered.value.retryable
    await client.aclose()


async def test_fake_taskboard_answers():
    fake = FakeTaskboard("tok")
    fake.approvals = {
        1: {"id": 1, "status": "open", "task_key": "T-1"},
        2: {"id": 2, "status": "answered", "task_key": "T-2"},
        3: {"id": 3, "status": "expired", "task_key": "T-3"},
    }
    client = TaskboardDelegate("http://tb", "tok", transport=httpx.ASGITransport(app=fake.app))
    assert [a["id"] for a in await client.open_approvals()] == [1]
    items = [{"id": i, "verdict": GO} for i in (1, 2, 3, 9)]
    results = await client.answer(items)
    assert [r["result"] for r in results] == [
        "answered",
        "already_answered",
        "expired",
        "not_found",
    ]
    assert fake.answers == [{"items": items, "source": "matrix"}]
    bad = TaskboardDelegate("http://tb", "wrong", transport=httpx.ASGITransport(app=fake.app))
    with pytest.raises(TaskboardError) as err:
        await bad.open_approvals()
    assert err.value.status == 401
