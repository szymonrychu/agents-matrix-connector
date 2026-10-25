import re

import pytest

from agents_matrix_connector.discovery import (
    Candidate,
    Skip,
    Unsupported,
    classify,
    effective_trigger,
    facebook_id,
    mentions_self,
    parse_message,
    truncate,
)

SELF = frozenset({"@me:hs.test"})
BOTS = ("@facebookbot:hs.test",)
GHOST = "@facebook_100000241356957:hs.test"


def ev(type_, content, state_key="", **extra):
    return {"type": type_, "state_key": state_key, "content": content, **extra}


def bridge(room_type=None, event_type="m.bridge", **channel):
    content = {
        "bridgebot": BOTS[0],
        "protocol": {"id": "facebook"},
        "channel": {"id": "1", **channel},
    }
    if room_type is not None:
        content["com.beeper.room_type.v2"] = room_type
    return ev(event_type, content, state_key="bridge")


def member(user_id, name=None, membership="join"):
    content = {"membership": membership}
    if name:
        content["displayname"] = name
    return ev("m.room.member", content, state_key=user_id)


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        ([bridge("dm")], "dm"),
        ([bridge("group_dm")], "group"),
        ([bridge("")], "group"),
        ([bridge()], "group"),
        ([bridge("space")], "infrastructure"),
        ([bridge("personal_filtering_space")], "infrastructure"),
        ([bridge("dm", event_type="uk.half-shot.bridge")], "dm"),
        ([], "matrix"),
        ([member("@me:hs.test")], "matrix"),
    ],
)
def test_room_kinds(state, kind):
    assert classify(state, SELF, BOTS).kind == kind


def test_encrypted_and_message_request():
    info = classify([bridge("dm"), ev("m.room.encryption", {"algorithm": "x"})], SELF, BOTS)
    assert info.encrypted is True
    assert info.message_request is False
    req = bridge("dm")
    req["content"]["channel"]["com.beeper.message_request"] = True
    info = classify([req], SELF, BOTS)
    assert info.message_request is True
    assert info.encrypted is False


def test_name_fallbacks():
    members = [member("@me:hs.test", "Me"), member(BOTS[0], "Bot"), member(GHOST, "Alice")]
    named = ev("m.room.name", {"name": "Room"})
    alias = ev("m.room.canonical_alias", {"alias": "#r:hs.test"})
    info = classify([bridge("dm", displayname="Chan"), named, alias, *members], SELF, BOTS)
    assert info.name == "Room"
    assert info.alias == "#r:hs.test"
    assert classify([bridge("dm", displayname="Chan"), *members], SELF, BOTS).name == "Chan"
    info = classify([bridge("dm"), *members], SELF, BOTS)
    assert info.name == "Alice"
    assert info.members == {"@me:hs.test": "Me", BOTS[0]: "Bot", GHOST: "Alice"}
    assert info.member_count == 3
    two = [*members, member("@bob:hs.test", "Bob")]
    assert classify(two, SELF, BOTS).name is None
    left = [*members[:2], member(GHOST, "Alice", membership="leave")]
    info = classify(left, SELF, BOTS)
    assert info.name is None
    assert GHOST not in info.members


def test_latest_bridge_event_wins():
    assert classify([bridge("dm"), bridge("space")], SELF, BOTS).kind == "infrastructure"


def test_facebook_id():
    pattern = re.compile("^@facebook_(?P<id>[0-9]+):")
    assert facebook_id(GHOST, pattern) == "100000241356957"
    assert facebook_id("@alice:hs.test", pattern) is None
    assert facebook_id(BOTS[0], pattern) is None


def msg(sender="@alice:hs.test", content=None, type_="m.room.message"):
    return {
        "type": type_,
        "event_id": "$e",
        "sender": sender,
        "origin_server_ts": 5,
        "content": {"msgtype": "m.text", "body": "hi"} if content is None else content,
    }


def test_parse_message():
    result = parse_message("!r:hs.test", msg(), SELF, BOTS)
    assert result == Candidate("$e", "!r:hs.test", "@alice:hs.test", "hi", None, (), 5)
    emote = msg(content={"msgtype": "m.emote", "body": "waves"})
    assert isinstance(parse_message("!r", emote, SELF, BOTS), Candidate)
    assert parse_message("!r", msg(sender="@me:hs.test"), SELF, BOTS) == Skip("$e", "@me:hs.test")
    assert isinstance(parse_message("!r", msg(sender=BOTS[0]), SELF, BOTS), Skip)
    edit = msg(
        content={
            "msgtype": "m.text",
            "body": "* x",
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$o"},
        }
    )
    assert isinstance(parse_message("!r", edit, SELF, BOTS), Skip)
    assert isinstance(parse_message("!r", msg(content={}), SELF, BOTS), Skip)
    assert isinstance(parse_message("!r", msg(type_="m.reaction"), SELF, BOTS), Skip)
    image = msg(content={"msgtype": "m.image", "body": "a.png"})
    assert parse_message("!r", image, SELF, BOTS) == Unsupported("$e", "@alice:hs.test")
    rich = msg(
        content={
            "msgtype": "m.text",
            "body": "b",
            "formatted_body": "<b>b</b>",
            "m.mentions": {"user_ids": ["@me:hs.test"]},
        }
    )
    got = parse_message("!r", rich, SELF, BOTS)
    assert isinstance(got, Candidate)
    assert got.formatted_body == "<b>b</b>"
    assert got.mention_ids == ("@me:hs.test",)


def cand(body="hi", formatted=None, mentions=()):
    return Candidate("$e", "!r", "@a:hs.test", body, formatted, tuple(mentions), 1)


def test_mentions_self():
    names = ["Claude"]
    assert mentions_self(cand(mentions=["@me:hs.test"]), SELF, names)
    assert mentions_self(
        cand(formatted='<a href="https://matrix.to/#/@me:hs.test">x</a>'), SELF, names
    )
    assert mentions_self(
        cand(formatted='<a href="https://matrix.to/#/%40me%3Ahs.test">x</a>'), SELF, names
    )
    assert mentions_self(cand(body="hey CLAUDE, hi"), SELF, names)
    assert not mentions_self(cand(body="hello"), SELF, names)
    assert not mentions_self(cand(body="hello"), SELF, [""])
    assert not mentions_self(cand(mentions=["@other:hs.test"]), SELF, names)
    assert not mentions_self(
        cand(formatted='<a href="https://matrix.to/#/@x:hs.test">x</a>'), SELF, names
    )


def test_effective_trigger():
    assert effective_trigger("dm", 2, None, "mention") == "all"
    assert effective_trigger("matrix", 2, None, "mention") == "all"
    assert effective_trigger("matrix", 3, None, "mention") == "mention"
    assert effective_trigger("group", 2, None, "mention") == "mention"
    assert effective_trigger("dm", 2, "mention", "all") == "mention"
    assert effective_trigger("group", 9, "all", "mention") == "all"


def test_truncate():
    assert truncate("abc", 3) == "abc"
    assert truncate("abcd", 3) == "abc [truncated]"
