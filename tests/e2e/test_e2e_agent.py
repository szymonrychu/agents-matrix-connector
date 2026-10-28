"""End to end of the agent sends (design D16, G1-G3, G6): the agent is driven through the
agent listener's `/chat/v1` with a test bearer, Matrix through the FakeHomeserver helpers, the
chat-started sessions through the FakeOrchestrator helpers. A non-mx session is one the fake
reports running whose slug does not start with `mx-`."""

import re

import pytest

from .conftest import CHUNK_CHARS, Stack, puppet, text, wait_for

pytestmark = pytest.mark.e2e

ALICE = puppet("111")
BOB = puppet("222")


def reply(body: str) -> dict:
    return {"msgtype": "m.text", "body": body}


def flagged_room(stack: Stack, member: str = ALICE, name: str = "Alice") -> str:
    """An allowed DM the agents may start a chat in."""
    room_id = stack.portal("dm", {member: name})
    stack.allow_room(room_id)
    stack.flag_room(room_id)
    return room_id


def message_ref(prompt: str) -> str:
    """The first `m<n>` id in a prompt."""
    found = re.search(r'<message id="(m\d+)"', prompt)
    assert found, prompt
    return found.group(1)


def test_initiate_without_inbound(stack: Stack) -> None:
    room_id = flagged_room(stack)
    agent = stack.add_session("agent-a")

    accepted = stack.send(agent, "Hello from the agent", room=room_id)
    assert accepted.status_code == 202, accepted.text
    send_id = accepted.json()["id"]

    stack.wait_send(agent, send_id, "sent")
    assert stack.sent(room_id) == [reply("Hello from the agent")]
    assert stack.inbound(room_id) == []
    assert stack.sessions() == [agent]


def test_follow_ups_during_running_run(stack: Stack) -> None:
    room_id = stack.portal("dm", {ALICE: "Alice"})
    stack.allow_room(room_id)
    stack.post(room_id, ALICE, text("start something long"))
    [session] = stack.wait_session(1)
    prompt = stack.wait_prompt(session["id"], 1)

    first = stack.send(session, "Working on it")
    second = stack.send(session, "Halfway there")
    assert first.status_code == second.status_code == 202, (first.text, second.text)
    wait_for(lambda: len(stack.sent(room_id)) == 2, "both follow-ups in the room")

    # the chat-started run is still open: its answer has not been given
    assert stack.sent(room_id) == [reply("Working on it"), reply("Halfway there")]
    assert [r["status"] for r in stack.inbound(room_id)] == ["in-progress"]

    stack.complete_turn(prompt["id"], "All done")
    wait_for(lambda: len(stack.sent(room_id)) == 3, "the final answer in the room")
    assert stack.sent(room_id) == [
        reply("Working on it"),
        reply("Halfway there"),
        reply("All done"),
    ]


def test_reply_and_reaction_relations(stack: Stack) -> None:
    room_id = stack.portal("dm", {ALICE: "Alice"})
    stack.allow_room(room_id)
    event_id = stack.post(room_id, ALICE, text("what do you think?"))
    [session] = stack.wait_session(1)
    ref = message_ref(stack.wait_prompt(session["id"], 1)["text"])

    quoted = stack.send(session, "Quoted answer", reply_to=ref)
    assert quoted.status_code == 202, quoted.text
    stack.wait_send(session, quoted.json()["id"], "sent")
    reaction = stack.react(session, ref, "\N{THUMBS UP SIGN}")
    assert reaction.status_code == 202, reaction.text
    stack.wait_send(session, reaction.json()["id"], "sent")

    assert stack.sent(room_id) == [
        {
            "msgtype": "m.text",
            "body": "Quoted answer",
            "m.relates_to": {"m.in_reply_to": {"event_id": event_id}},
        },
        {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": "\N{THUMBS UP SIGN}",
            }
        },
    ]


def test_refusals(stack: Stack) -> None:
    denied = stack.portal("dm", {BOB: "Bob"})
    stack.allow_room(denied)
    stack.flag_room(denied)
    stack.add_rule("deny", "user", BOB)
    unflagged = stack.portal("dm", {ALICE: "Alice"})
    stack.allow_room(unflagged)
    open_room = flagged_room(stack, puppet("333"), "Carol")
    agent = stack.add_session("agent-a")

    refused_denied = stack.send(agent, "to the denied room", room=denied)
    assert refused_denied.status_code == 403, refused_denied.text
    assert refused_denied.json()["error"]["code"] == "room_not_allowed"
    refused_unflagged = stack.send(agent, "to the unflagged room", room=unflagged)
    assert refused_unflagged.status_code == 403, refused_unflagged.text
    assert refused_unflagged.json()["error"]["code"] == "room_not_authorised"
    bad_reply = stack.send(agent, "to a ghost", room=open_room, reply_to="m999999")
    assert bad_reply.status_code == 422, bad_reply.text
    assert bad_reply.json()["error"]["code"] == "bad_reference"
    bad_reaction = stack.react(agent, "s999999", "\N{THUMBS UP SIGN}")
    assert bad_reaction.status_code == 422, bad_reaction.text
    assert bad_reaction.json()["error"]["code"] == "bad_reference"

    # a send that is accepted afterwards proves the bot ticked past the refused ones
    control = stack.send(agent, "control", room=open_room)
    stack.wait_send(agent, control.json()["id"], "sent")
    assert stack.sent(denied) == []
    assert stack.sent(unflagged) == []
    assert stack.sent(open_room) == [reply("control")]
    assert stack.room(denied)["binding"] is None
    assert stack.room(unflagged)["binding"] is None


def test_initiation_binds_and_answer_reaches_session(stack: Stack) -> None:
    room_id = flagged_room(stack)
    agent = stack.add_session("agent-a")
    sent = stack.send(agent, "Question for you", room=room_id)
    stack.wait_send(agent, sent.json()["id"], "sent")

    binding = stack.room(room_id)["binding"]
    assert (binding["session_id"], binding["slug"], binding["origin"]) == (
        agent["id"],
        "agent-a",
        "agent",
    )

    stack.post(room_id, ALICE, text("my answer"))
    prompt = stack.wait_prompt(agent["id"], 1)
    assert "my answer" in prompt["text"]
    assert "untrusted" in prompt["text"]  # a session that never saw the chat gets the preamble
    [row] = stack.wait_inbound(room_id, 1, status="in-progress")
    assert row["run_id"] is not None
    assert stack.sessions() == [agent]  # no mx- session was started

    stack.complete_turn(prompt["id"], "Thanks")
    wait_for(lambda: len(stack.sent(room_id)) == 2, "the answer to the user's reply")
    assert stack.sent(room_id) == [reply("Question for you"), reply("Thanks")]
    stack.wait_inbound(room_id, 1, status="handled")
    assert stack.room(room_id)["binding"]["session_id"] == agent["id"]


def test_binding_refusals(stack: Stack) -> None:
    first = flagged_room(stack)
    second = flagged_room(stack, BOB, "Bob")
    third = flagged_room(stack, puppet("333"), "Carol")
    owner = stack.add_session("agent-a")
    other = stack.add_session("agent-b")
    bound = stack.send(owner, "I hold this room", room=first)
    stack.wait_send(owner, bound.json()["id"], "sent")

    taken = stack.send(other, "may I?", room=first)
    assert taken.status_code == 409, taken.text
    assert taken.json()["error"]["code"] == "room_bound"
    greedy = stack.send(owner, "and this one too", room=second)
    assert greedy.status_code == 409, greedy.text
    assert greedy.json()["error"]["code"] == "session_bound"

    # a send accepted afterwards proves the bot ticked past anything the refusals queued
    control = stack.send(other, "control", room=third)
    stack.wait_send(other, control.json()["id"], "sent")
    assert stack.sent(first) == [reply("I hold this room")]
    assert stack.sent(second) == []
    assert stack.sent(third) == [reply("control")]
    assert stack.room(first)["binding"]["session_id"] == owner["id"]
    assert stack.room(second)["binding"] is None


def test_release_on_session_delete(stack: Stack) -> None:
    room_id = flagged_room(stack)
    agent = stack.add_session("agent-a")
    sent = stack.send(agent, "Question for you", room=room_id)
    stack.wait_send(agent, sent.json()["id"], "sent")
    assert stack.room(room_id)["binding"]["session_id"] == agent["id"]

    stack.remove_session(agent["id"])
    stack.post(room_id, ALICE, text("is anyone there?"))
    [fresh] = [s for s in stack.wait_session(1) if s["id"] != agent["id"]]
    [row] = stack.wait_inbound(room_id, 1)
    assert fresh["slug"] == f"mx-{row['run_id']}"
    held = wait_for(
        lambda: (b := stack.room(room_id)["binding"]) and b["session_id"] == fresh["id"] and b,
        "the new mx- session to hold the room",
    )
    assert held["slug"] == fresh["slug"]
    assert held["origin"] == "inbound"

    answer = stack.wait_prompt(fresh["id"], 1)
    assert "is anyone there?" in answer["text"]
    stack.complete_turn(answer["id"], "Here now")
    wait_for(lambda: len(stack.sent(room_id)) == 2, "the new session's answer")
    assert stack.sent(room_id) == [reply("Question for you"), reply("Here now")]


def test_no_double_output(stack: Stack) -> None:
    room_id = stack.portal("dm", {ALICE: "Alice"})
    stack.allow_room(room_id)
    stack.post(room_id, ALICE, text("tell me the result"))
    [session] = stack.wait_session(1)
    prompt = stack.wait_prompt(session["id"], 1)

    said = stack.send(session, "The result is 42")
    stack.wait_send(session, said.json()["id"], "sent")
    stack.complete_turn(prompt["id"], "The result is 42")

    [row] = stack.wait_inbound(room_id, 1, status="handled")
    assert row["reason"] == "already_sent"
    assert stack.sent(room_id) == [reply("The result is 42")]


def test_max_text_delivered(stack: Stack) -> None:
    room_id = flagged_room(stack)
    agent = stack.add_session("agent-a")
    long_text = "\N{GRINNING FACE}" * 16000
    assert len(long_text.encode()) == 64000

    accepted = stack.send(agent, long_text, room=room_id)
    assert accepted.status_code == 202, accepted.text
    stack.wait_send(agent, accepted.json()["id"], "sent")

    bodies = [content["body"] for content in stack.sent(room_id)]
    assert len(bodies) == 16000 // CHUNK_CHARS
    assert all(len(body) == CHUNK_CHARS for body in bodies)
    assert "".join(bodies) == long_text
