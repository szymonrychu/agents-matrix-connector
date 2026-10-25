import dataclasses

import pytest

from agents_matrix_connector.policy import (
    KINDS,
    NOT_ALLOWED,
    ROOM_ENCRYPTED,
    ROOM_INFRASTRUCTURE,
    Decision,
    Rule,
    Subject,
    denied,
    evaluate,
    validate_rule,
)

ROOM = "!a:hs"
ALIAS = "#chat:hs"
SENDER = "@facebook_1:hs"
FACEBOOK = "1"
NAME = "Alice Smith"


def subject(**overrides) -> Subject:
    values = {
        "room_id": ROOM,
        "room_alias": ALIAS,
        "room_kind": "dm",
        "encrypted": False,
        "sender_id": SENDER,
        "sender_facebook_id": FACEBOOK,
        "sender_name": NAME,
    }
    values.update(overrides)
    return Subject(**values)


def test_constants():
    assert KINDS == {
        "allow": ("user", "facebook", "room", "alias"),
        "deny": ("user", "facebook", "room", "alias", "display_name"),
    }
    assert ROOM_INFRASTRUCTURE == "room_infrastructure"
    assert ROOM_ENCRYPTED == "room_encrypted"
    assert NOT_ALLOWED == "not_allowed"
    assert denied("facebook") == "denied: facebook"
    for cls in (Rule, Subject, Decision):
        assert dataclasses.is_dataclass(cls)
    with pytest.raises(dataclasses.FrozenInstanceError):
        Rule(1, "allow", "room", ROOM).value = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        subject().room_id = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        Decision(True, None, None, "default").allowed = False


def test_expected_outcome():
    rules = []
    assert evaluate(subject(), rules, "deny") == Decision(False, "not_allowed", None, "default")
    rules.append(Rule(1, "allow", "room", ROOM))
    assert evaluate(subject(), rules, "deny") == Decision(True, None, 1, "rule")
    rules.append(Rule(2, "deny", "facebook", FACEBOOK))
    assert evaluate(subject(), rules, "deny") == Decision(False, "denied: facebook", 2, "rule")


def test_default_deny_with_no_rules():
    subjects = [
        subject(),
        subject(room_alias=None, room_kind=None, sender_facebook_id=None, sender_name=None),
        subject(room_kind="group"),
        subject(room_kind="matrix", sender_id="@someone:other", sender_facebook_id=None),
    ]
    for s in subjects:
        assert evaluate(s, [], "deny") == Decision(False, NOT_ALLOWED, None, "default")


def test_default_allow():
    assert evaluate(subject(), [], "allow") == Decision(True, None, None, "default")
    assert evaluate(subject(sender_id=None), [], "allow") == Decision(True, None, None, "default")


def test_unknown_default_policy_is_refused():
    with pytest.raises(ValueError, match="^default_policy:"):
        evaluate(subject(), [], "maybe")


def test_deny_wins_on_every_key():
    allows = [
        Rule(1, "allow", "room", ROOM),
        Rule(2, "allow", "alias", ALIAS),
        Rule(3, "allow", "user", SENDER),
        Rule(4, "allow", "facebook", FACEBOOK),
    ]
    denies = {
        "room": Rule(10, "deny", "room", ROOM),
        "alias": Rule(11, "deny", "alias", ALIAS),
        "user": Rule(12, "deny", "user", SENDER),
        "facebook": Rule(13, "deny", "facebook", FACEBOOK),
        "display_name": Rule(14, "deny", "display_name", NAME),
    }
    for kind, rule in denies.items():
        for policy in ("deny", "allow"):
            assert evaluate(subject(), [*allows, rule], policy) == Decision(
                False, denied(kind), rule.id, "rule"
            ), kind

    # Every deny matches: the lowest id is reported, whatever the input order.
    every = [denies["display_name"], denies["alias"], *allows, denies["user"], denies["room"]]
    every.append(Rule(9, "deny", "facebook", FACEBOOK))
    assert evaluate(subject(), every, "allow") == Decision(False, "denied: facebook", 9, "rule")

    # A deny on an unrelated value does not refuse.
    others = [
        Rule(20, "deny", "room", "!b:hs"),
        Rule(21, "deny", "alias", "#other:hs"),
        Rule(22, "deny", "user", "@facebook_2:hs"),
        Rule(23, "deny", "facebook", "2"),
        Rule(24, "deny", "display_name", "Bob"),
    ]
    assert evaluate(subject(), [*others, allows[0]], "deny") == Decision(True, None, 1, "rule")


def test_display_name_deny_only():
    rule = Rule(5, "deny", "display_name", "alice SMITH")
    for name in ("Alice Smith", "ALICE SMITH", "alice smith"):
        decision = evaluate(
            subject(sender_name=name), [Rule(1, "allow", "room", ROOM), rule], "deny"
        )
        assert decision == Decision(False, "denied: display_name", 5, "rule")
    # Casefolding, not just lowercasing.
    assert evaluate(
        subject(sender_name="STRASSE"), [Rule(6, "deny", "display_name", "straße")], "allow"
    ) == Decision(False, "denied: display_name", 6, "rule")
    # A different name, or no name, is not matched.
    assert evaluate(subject(sender_name="Alice"), [rule], "allow") == Decision(
        True, None, None, "default"
    )
    assert evaluate(subject(sender_name=None), [rule], "allow") == Decision(
        True, None, None, "default"
    )

    with pytest.raises(ValueError, match="^kind:") as exc:
        validate_rule("allow", "display_name", NAME)
    assert "chosen by the sender" in str(exc.value)
    assert "cannot grant access" in str(exc.value)

    # An allow display_name rule that bypassed validation never grants access.
    smuggled = Rule(1, "allow", "display_name", NAME)
    assert evaluate(subject(), [smuggled], "deny") == Decision(False, NOT_ALLOWED, None, "default")


def test_allow_room_and_allow_user():
    room_rule = Rule(3, "allow", "room", ROOM)
    for sender, fb, name in [
        (SENDER, FACEBOOK, NAME),
        ("@facebook_77:hs", "77", "Bob"),
        ("@matrixuser:other", None, None),
    ]:
        s = subject(sender_id=sender, sender_facebook_id=fb, sender_name=name)
        assert evaluate(s, [room_rule], "deny") == Decision(True, None, 3, "rule")

    alias_rule = Rule(4, "allow", "alias", ALIAS)
    assert evaluate(subject(), [alias_rule], "deny") == Decision(True, None, 4, "rule")
    # Lowest id among matching room-side allows.
    assert evaluate(subject(), [alias_rule, room_rule], "deny") == Decision(True, None, 3, "rule")
    # Room-side allow is checked before sender-side allow.
    user_rule = Rule(1, "allow", "user", SENDER)
    assert evaluate(subject(), [user_rule, alias_rule], "deny") == Decision(True, None, 4, "rule")
    # An allow on another room does not admit.
    assert evaluate(subject(), [Rule(5, "allow", "room", "!b:hs")], "deny") == Decision(
        False, NOT_ALLOWED, None, "default"
    )
    assert evaluate(subject(room_alias=None), [alias_rule], "deny") == Decision(
        False, NOT_ALLOWED, None, "default"
    )

    for room in (ROOM, "!b:hs", "!c:other"):
        s = subject(room_id=room, room_alias=None)
        assert evaluate(s, [user_rule], "deny") == Decision(True, None, 1, "rule")
    fb_rule = Rule(2, "allow", "facebook", FACEBOOK)
    assert evaluate(subject(room_id="!b:hs"), [fb_rule], "deny") == Decision(True, None, 2, "rule")
    assert evaluate(subject(), [fb_rule, user_rule], "deny") == Decision(True, None, 1, "rule")
    assert evaluate(subject(sender_facebook_id=None), [fb_rule], "deny") == Decision(
        False, NOT_ALLOWED, None, "default"
    )
    # Another sender is not admitted by the user rule.
    other = subject(sender_id="@facebook_2:hs", sender_facebook_id="2")
    assert evaluate(other, [user_rule, fb_rule], "deny") == Decision(
        False, NOT_ALLOWED, None, "default"
    )
    # An allowed user is refused in a denied room.
    deny_room = Rule(9, "deny", "room", "!b:hs")
    s = subject(room_id="!b:hs")
    assert evaluate(s, [user_rule, deny_room], "deny") == Decision(False, "denied: room", 9, "rule")


def test_infrastructure_and_encrypted_refused():
    rules = [
        Rule(1, "allow", "room", ROOM),
        Rule(2, "allow", "alias", ALIAS),
        Rule(3, "allow", "user", SENDER),
        Rule(4, "allow", "facebook", FACEBOOK),
    ]
    for policy in ("deny", "allow"):
        assert evaluate(subject(room_kind="infrastructure"), rules, policy) == Decision(
            False, ROOM_INFRASTRUCTURE, None, "room"
        )
        assert evaluate(subject(encrypted=True), rules, policy) == Decision(
            False, ROOM_ENCRYPTED, None, "room"
        )
        # Infrastructure is checked before encryption and before deny rules.
        s = subject(room_kind="infrastructure", encrypted=True)
        assert evaluate(s, [Rule(0, "deny", "room", ROOM)], policy) == Decision(
            False, ROOM_INFRASTRUCTURE, None, "room"
        )
        assert evaluate(
            subject(encrypted=True), [Rule(0, "deny", "room", ROOM)], policy
        ) == Decision(False, ROOM_ENCRYPTED, None, "room")
        assert evaluate(
            subject(room_kind="infrastructure", sender_id=None), rules, policy
        ) == Decision(False, ROOM_INFRASTRUCTURE, None, "room")


def test_validate_rule_formats():
    accepted = [
        (("allow", "user", "  @alice:hs.example "), ("allow", "user", "@alice:hs.example")),
        (("deny", "user", "@facebook_1:hs:8448"), ("deny", "user", "@facebook_1:hs:8448")),
        (("allow", "facebook", "\t1234567890\n"), ("allow", "facebook", "1234567890")),
        (("deny", "facebook", "9" * 32), ("deny", "facebook", "9" * 32)),
        (("allow", "room", " !abc:hs "), ("allow", "room", "!abc:hs")),
        (("deny", "room", "!abc:hs"), ("deny", "room", "!abc:hs")),
        (("allow", "alias", "#chat:hs"), ("allow", "alias", "#chat:hs")),
        (("deny", "alias", " #chat:hs"), ("deny", "alias", "#chat:hs")),
        (("deny", "display_name", "  Alice Smith  "), ("deny", "display_name", "Alice Smith")),
        (("deny", "display_name", "x" * 128), ("deny", "display_name", "x" * 128)),
        (("deny", "user", "@" + "a" * 250 + ":hs"), ("deny", "user", "@" + "a" * 250 + ":hs")),
    ]
    for args, expected in accepted:
        assert validate_rule(*args) == expected, args

    rejected = [
        (("block", "user", "@a:hs"), "list:"),
        (("", "user", "@a:hs"), "list:"),
        (("Allow", "user", "@a:hs"), "list:"),
        (("allow", "email", "a@b"), "kind:"),
        (("deny", "", "@a:hs"), "kind:"),
        (("allow", "display_name", "Alice"), "kind:"),
        (("allow", "user", ""), "value:"),
        (("allow", "user", "   "), "value:"),
        (("allow", "user", "@" + "a" * 252 + ":hs"), "value:"),
        (("allow", "user", "alice:hs"), "value:"),
        (("allow", "user", "@alice"), "value:"),
        (("allow", "user", "@:hs"), "value:"),
        (("allow", "user", "@al ice:hs"), "value:"),
        (("allow", "user", "@alice:h s"), "value:"),
        (("allow", "user", "@alice:"), "value:"),
        (("allow", "user", "@alice:hs\nx"), "value:"),
        (("allow", "facebook", "12a"), "value:"),
        (("allow", "facebook", "1" * 33), "value:"),
        (("allow", "facebook", "-1"), "value:"),
        (("allow", "facebook", "١٢"), "value:"),
        (("allow", "room", "#abc:hs"), "value:"),
        (("allow", "room", "!abc"), "value:"),
        (("allow", "room", "!:hs"), "value:"),
        (("allow", "alias", "!abc:hs"), "value:"),
        (("allow", "alias", "#abc"), "value:"),
        (("allow", "alias", "#a b:hs"), "value:"),
        (("deny", "display_name", ""), "value:"),
        (("deny", "display_name", "x" * 129), "value:"),
    ]
    for args, prefix in rejected:
        with pytest.raises(ValueError) as exc:
            validate_rule(*args)
        assert str(exc.value).startswith(prefix), (args, str(exc.value))


def test_room_level_decision():
    room_level = subject(sender_id=None, sender_facebook_id=None, sender_name=None)
    assert evaluate(room_level, [], "deny") == Decision(False, NOT_ALLOWED, None, "default")
    assert evaluate(room_level, [Rule(1, "allow", "room", ROOM)], "deny") == Decision(
        True, None, 1, "rule"
    )
    assert evaluate(room_level, [Rule(2, "deny", "alias", ALIAS)], "allow") == Decision(
        False, "denied: alias", 2, "rule"
    )

    # Sender rules play no part, even if stray sender fields are set.
    stray = subject(sender_id=None)
    sender_rules = [
        Rule(3, "allow", "user", SENDER),
        Rule(4, "allow", "facebook", FACEBOOK),
    ]
    assert evaluate(stray, sender_rules, "deny") == Decision(False, NOT_ALLOWED, None, "default")
    sender_denies = [
        Rule(5, "deny", "user", SENDER),
        Rule(6, "deny", "facebook", FACEBOOK),
        Rule(7, "deny", "display_name", NAME),
    ]
    assert evaluate(stray, sender_denies, "allow") == Decision(True, None, None, "default")
    assert evaluate(stray, [*sender_denies, Rule(8, "allow", "room", ROOM)], "deny") == Decision(
        True, None, 8, "rule"
    )
