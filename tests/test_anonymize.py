import hashlib
import re

import pytest

from agents_matrix_connector.anonymize import (
    Labeler,
    disclosure_message,
    is_admin,
    pseudonym,
    pseudonyms_in,
    replace,
)
from agents_matrix_connector.db.directory import MemberRow

ANN = "@facebook_1:hs"
BOB = "@facebook_2:hs"
BOT = "@hermes:hs"


def _digest(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()[:12]


def _member(user_id, name, facebook_id=None, membership="join"):
    return MemberRow(user_id, name, facebook_id, membership)


def test_pseudonym_is_plain_sha256_of_the_mxid():
    assert pseudonym(ANN) == f"user-{_digest(ANN)}"
    assert pseudonym(ANN, admin=True) == f"admin-{_digest(ANN)}"
    assert pseudonym(ANN) != pseudonym(BOB)
    assert re.fullmatch(r"user-[0-9a-f]{12}", pseudonym(ANN))


def test_is_admin_by_mxid_or_facebook_id():
    assert is_admin(ANN, "1", ["@facebook_1:hs"])
    assert is_admin(ANN, "1", ["1"])
    assert not is_admin(BOB, "2", ["1", ANN])
    assert not is_admin(BOB, None, [])


@pytest.mark.parametrize(
    "text",
    [
        "user-0123456789ab",
        "hi ADMIN-0123456789AB.",
        "(user-abcdefabcdef)",
        "see user-0123456789ab\n",
        "User-0123456789Ab, ok",
        "my_user-0123456789ab",
        "user-0123456789abc",
        "user-0123456789ab_x",
    ],
)
def test_pseudonym_tokens_found(text):
    assert pseudonyms_in(text)


@pytest.mark.parametrize(
    "text",
    [
        "xuser-0123456789ab",
        "user-0123456789",
        "user-0123456789ag",
        "user 0123456789ab",
        "superuser-0123456789ab",
        "the user said hi",
    ],
)
def test_pseudonym_tokens_not_found(text):
    assert pseudonyms_in(text) == []


def test_pseudonyms_in_reports_tokens_as_written_once_in_order():
    text = "ask User-0123456789Ab, then admin-abcdefabcdef and user-0123456789ab again"
    assert pseudonyms_in(text) == ["User-0123456789Ab", "admin-abcdefabcdef"]
    assert pseudonyms_in("plain text, Anna Smith") == []


def test_pseudonyms_in_sees_through_width_and_invisible_characters():
    assert pseudonyms_in("ｕｓｅｒ-０１２３４５６７８９ａｂ") == [
        "ｕｓｅｒ-０１２３４５６７８９ａｂ"
    ]
    assert pseudonyms_in("us\u200ber-0123456789ab!") == ["us\u200ber-0123456789ab"]


def test_disclosure_message_names_tokens_and_asks_to_rephrase():
    message = disclosure_message(["user-0123456789ab", "admin-abcdefabcdef"])
    assert "user-0123456789ab, admin-abcdefabcdef" in message
    assert "must not be sent to the chat" in message
    assert "refer to the person differently" in message


def test_replace_keeps_the_rest_and_prefers_longest():
    text = "Hi ANNA SMITH and Anna, from Anna Smith-Jones."
    out = replace(text, {"Anna Smith": "<A>", "Anna Smith-Jones": "<AJ>"})
    assert out == "Hi <A> and Anna, from <AJ>."
    assert replace("Ａｎｎａ Ｓｍｉｔｈ!", {"Anna Smith": "x"}) == "x!"
    assert replace("Józef?", {"Józef": "y"}) == "y?"
    assert replace("Jozef?", {"Józef": "y"}) == "Jozef?"
    assert replace("nothing", {"Anna Smith": "x"}) == "nothing"
    assert replace("An\u200dna Smith", {"Anna Smith": "x"}) == "x"
    assert replace("Annabel", {"Anna": "x", "": "y"}) == "Annabel"


def test_labeler_user_and_admin():
    labeler = Labeler(["2"], lambda user_id: user_id.split("_")[1].split(":")[0])
    assert labeler.user(ANN) == pseudonym(ANN)
    assert labeler.user(BOB) == pseudonym(BOB, admin=True)


def test_labeler_room_names():
    labeler = Labeler([], lambda _u: None)
    members = [
        _member(ANN, "Anna Smith"),
        _member(BOB, "Bob Lee", membership="leave"),
        _member(BOT, "Hermes"),
    ]
    excluded = frozenset({BOT})
    dm = labeler.room("Anna Smith", "dm", members, excluded)
    assert dm == f"{pseudonym(ANN)}, {pseudonym(BOB)}"
    group = labeler.room("Anna Smith, Bob Lee and Hermes", "group", members, excluded)
    assert group == f"{pseudonym(ANN)}, {pseudonym(BOB)} and Hermes"
    assert labeler.room("Family", "group", members, excluded) == "Family"
    assert labeler.room(None, "group", members, excluded) is None
    assert labeler.room(None, "dm", [_member(BOT, "Hermes")], excluded) is None
