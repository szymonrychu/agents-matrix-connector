import pytest

from agents_matrix_connector.targets import InvalidTarget, Target, VanityLink, parse_target

PATTERN = "^@facebook_(?P<id>[0-9]+):"
SERVER = "szymonrichert.pl"
REFUSED = frozenset(
    {
        "@hermes:szymonrichert.pl",
        "@facebookbot:szymonrichert.pl",
        "@facebook_999:szymonrichert.pl",
    }
)


def parse(text):
    return parse_target(text, puppet_pattern=PATTERN, bridge_server=SERVER, refused=REFUSED)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@alice:example.org", Target("matrix", "@alice:example.org")),
        ("  @alice:example.org ", Target("matrix", "@alice:example.org")),
        ("@facebook_123:szymonrichert.pl", Target("facebook", "123")),
        ("@facebook_123:other.org", Target("matrix", "@facebook_123:other.org")),
        ("123456", Target("facebook", "123456")),
        (
            "https://www.facebook.com/profile.php?id=100012345&ref=x",
            Target("facebook", "100012345"),
        ),
        ("https://facebook.com/profile.php?id=7", Target("facebook", "7")),
        ("m.facebook.com/profile.php?id=42", Target("facebook", "42")),
        ("http://mobile.facebook.com/profile.php?id=42/", Target("facebook", "42")),
        ("https://web.facebook.com/profile.php/?id=42", Target("facebook", "42")),
        ("https://fb.com/profile.php?ref=y&id=42#top", Target("facebook", "42")),
        ("HTTPS://WWW.FACEBOOK.COM/profile.php?id=42", Target("facebook", "42")),
        ("John Smith", Target("name", "John Smith")),
        ("  Zoë  Smith ", Target("name", "Zoë  Smith")),
        ("j.smith", Target("name", "j.smith")),
    ],
)
def test_parse_target(text, expected):
    assert parse(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://facebook.com/john.smith",
        "facebook.com/john.smith/",
        "https://www.facebook.com/john.smith?ref=bookmarks",
        "m.facebook.com/someone",
    ],
)
def test_vanity_link_raises(text):
    with pytest.raises(VanityLink):
        parse(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "@hermes:szymonrichert.pl",
        "@facebookbot:szymonrichert.pl",
        "@facebook_999:szymonrichert.pl",
        "999",
        "https://facebook.com/profile.php?id=999",
        "https://example.com/x",
        "example.com/x",
        "https://facebook.com/profile.php?id=abc",
        "https://facebook.com/profile.php",
        "https://facebook.com/",
        "facebook.com",
        "@broken",
        "x" * 201,
    ],
)
def test_invalid_targets(text):
    with pytest.raises(InvalidTarget):
        parse(text)


def test_vanity_is_not_invalid():
    assert not issubclass(VanityLink, InvalidTarget)
