import re

import pytest

from agents_matrix_connector.discovery import (
    Candidate,
    Media,
    Skip,
    Unsupported,
    alias_mentioned,
    classify,
    effective_trigger,
    facebook_id,
    mentions_self,
    normalize_aliases,
    parse_message,
    truncate,
)
from agents_matrix_connector.policy import Rule, Subject, evaluate

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
    assert facebook_id(GHOST, pattern, "hs.test") == "100000241356957"
    assert facebook_id("@alice:hs.test", pattern, "hs.test") is None
    assert facebook_id(BOTS[0], pattern, "hs.test") is None
    assert facebook_id("@facebook_1", pattern, "hs.test") is None


def test_facebook_id_only_on_bridge_server():
    pattern = re.compile("^@facebook_(?P<id>[0-9]+):")
    foreign = "@facebook_100000241356957:evil.example"
    assert facebook_id(GHOST, pattern, "hs.test") == "100000241356957"
    assert facebook_id(foreign, pattern, "hs.test") is None
    assert facebook_id("@facebook_1:hs.test.evil.example", pattern, "hs.test") is None
    # The pattern must cover the whole `@localpart:`; a prefix match is not enough.
    loose = re.compile("@facebook_(?P<id>[0-9]+)")
    assert facebook_id("@facebook_1x:hs.test", loose, "hs.test") is None

    allow = [Rule(1, "allow", "facebook", "100000241356957")]

    def decide(sender: str) -> bool:
        subject = Subject(
            room_id="!r:hs.test",
            room_alias=None,
            room_kind="dm",
            encrypted=False,
            sender_id=sender,
            sender_facebook_id=facebook_id(sender, pattern, "hs.test"),
            sender_name="Alice",
        )
        return evaluate(subject, allow, "deny").allowed

    assert decide(GHOST) is True
    assert decide(foreign) is False


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


SELF2 = frozenset({"@me:hs.test", "@me-ghost:hs.test"})


def test_mentions_self_structured():
    assert mentions_self(cand(mentions=["@me:hs.test"]), SELF2, ())
    assert mentions_self(
        cand(formatted='<a href="https://matrix.to/#/@me:hs.test">x</a>'), SELF2, ()
    )
    assert mentions_self(
        cand(formatted='<a href="https://matrix.to/#/%40me%3Ahs.test">x</a>'), SELF2, ()
    )
    assert not mentions_self(cand(mentions=["@other:hs.test"]), SELF2, ())
    assert not mentions_self(
        cand(formatted='<a href="https://matrix.to/#/@x:hs.test">x</a>'), SELF2, ()
    )


def test_owner_tag_with_bot_display_name_not_mentioned():
    candidate = cand(
        body="@Szymon Richert",
        formatted=('<a href="https://matrix.to/#/@owner-puppet:hs.test">Szymon Richert</a>'),
    )
    assert not mentions_self(candidate, SELF2, ())


def test_reply_to_bot_is_mentioned():
    assert mentions_self(cand(body="Nie zesraj sie", mentions=["@me:hs.test"]), SELF2, ())


def test_self_name_in_body_not_mentioned():
    assert not mentions_self(cand(body="Hermes said hi"), SELF2, ())


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("bot ping", True),
        ("@bot ping", True),
        ("BOT, ping", True),
        ("hey @Bot!", True),
        ("(bot)", True),
        ("ping bot", True),
        ("bot\nhi", True),
        ("robot", False),
        ("bots", False),
        ("abot", False),
        ("bot_x", False),
        ("x_bot", False),
        ("b o t", False),
    ],
)
def test_alias_matches_whole_word(body, expected):
    assert alias_mentioned(body, ("bot",)) is expected


def test_alias_multi_word():
    assert alias_mentioned("hi agent   smith!", ("Agent Smith",))
    assert not alias_mentioned("agent smithers", ("Agent Smith",))


def test_alias_unicode_boundary():
    assert alias_mentioned("GOSIA?", ("Gosia",))
    assert not alias_mentioned("Gosiak", ("Gosia",))
    assert not alias_mentioned("zGosia", ("Gosia",))
    assert not alias_mentioned("Zo\u015bka", ("Zosia",))
    assert alias_mentioned("\u0142ukasz, hi", ("\u0141ukasz",))


def test_alias_metacharacters_literal():
    assert alias_mentioned("c++ help", ("c++",))
    assert not alias_mentioned("c help", ("c++",))
    assert not alias_mentioned("axb", ("a.b",))
    assert alias_mentioned("a.b", ("a.b",))


def test_empty_alias_list_never_matches():
    assert not alias_mentioned("bot", ())
    assert not mentions_self(cand(body="bot"), SELF2, ())


def test_alias_in_mentions_self():
    assert mentions_self(cand(body="@bot hi"), SELF2, ("bot",))


def test_normalize_aliases():
    assert normalize_aliases([" @Bot ", "bot", "Agent   Smith"]) == ("Bot", "Agent Smith")
    assert normalize_aliases([]) == ()


@pytest.mark.parametrize(
    "values",
    [
        [""],
        ["@"],
        ["   "],
        ["x" * 65],
        ["a\x00b"],
        ["a\u200bb"],
        ["!!!"],
        [f"alias{i}" for i in range(11)],
    ],
)
def test_normalize_aliases_rejects(values):
    with pytest.raises(ValueError):
        normalize_aliases(values)


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


MXC = "mxc://hs.test/abc"


@pytest.mark.parametrize(
    ("msgtype", "kind"),
    [("m.image", "image"), ("m.file", "file"), ("m.video", "video"), ("m.audio", "audio")],
)
def test_parse_media_msgtypes(msgtype, kind):
    content = {
        "msgtype": msgtype,
        "body": "IMG_1.jpg",
        "url": MXC,
        "info": {"mimetype": "image/jpeg", "size": 1234, "w": 30, "h": 20},
    }
    got = parse_message("!r", msg(content=content), SELF, BOTS)
    assert isinstance(got, Candidate)
    assert got.body == ""
    assert got.media == Media(kind, MXC, "image/jpeg", 1234, "IMG_1.jpg", 30, 20)


def test_parse_media_info_wrong_types_dropped():
    content = {
        "msgtype": "m.image",
        "body": "a.png",
        "url": MXC,
        "info": {"mimetype": 5, "size": "big", "w": True, "h": 2.5},
    }
    got = parse_message("!r", msg(content=content), SELF, BOTS)
    assert got.media == Media("image", MXC, None, None, "a.png", None, None)


def test_parse_sticker():
    content = {"body": "thumbs up", "url": MXC, "info": {"mimetype": "image/webp"}}
    got = parse_message("!r", msg(content=content, type_="m.sticker"), SELF, BOTS)
    assert isinstance(got, Candidate)
    assert got.media == Media("sticker", MXC, "image/webp", None, "thumbs up", None, None)
    assert got.body == ""


def test_parse_encrypted_file_unsupported():
    content = {"msgtype": "m.image", "body": "a.jpg", "file": {"url": MXC, "key": {}}}
    got = parse_message("!r", msg(content=content), SELF, BOTS)
    assert got == Unsupported("$e", "@alice:hs.test")
    sticker = msg(content={"body": "s", "file": {"url": MXC}}, type_="m.sticker")
    assert isinstance(parse_message("!r", sticker, SELF, BOTS), Unsupported)


def test_parse_location_unsupported():
    content = {"msgtype": "m.location", "body": "here", "geo_uri": "geo:1,2"}
    assert isinstance(parse_message("!r", msg(content=content), SELF, BOTS), Unsupported)
    other = {"msgtype": "m.notice", "body": "x", "url": MXC}
    assert isinstance(parse_message("!r", msg(content=other), SELF, BOTS), Unsupported)


def test_caption_rule():
    def parse(**content):
        got = parse_message(
            "!r", msg(content={"msgtype": "m.image", "url": MXC, **content}), SELF, BOTS
        )
        assert isinstance(got, Candidate)
        return got.body, got.media.name

    assert parse(body="look at this", filename="IMG_1.jpg") == ("look at this", "IMG_1.jpg")
    assert parse(body="IMG_1.jpg", filename="IMG_1.jpg") == ("", "IMG_1.jpg")
    assert parse(body="IMG_1.jpg") == ("", "IMG_1.jpg")
    assert parse(body="IMG_1.jpg", filename=7) == ("", "IMG_1.jpg")


def test_text_candidate_has_no_media():
    assert parse_message("!r", msg(), SELF, BOTS).media is None
