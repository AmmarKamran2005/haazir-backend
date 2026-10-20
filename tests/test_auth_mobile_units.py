"""Mobile sign-in pieces that need no database."""

from __future__ import annotations

import pytest

from haazir.auth import google
from haazir.auth.magic_link import code_hash
from haazir.services import mail

GOOD = {"aud": "web-client", "iss": "https://accounts.google.com", "sub": "123",
        "email": "A@Example.com", "email_verified": "true", "name": "A", "picture": "p"}


def test_a_google_token_for_our_client_is_accepted():
    who = google.check_claims(GOOD, ["web-client", "other"])
    assert who.email == "a@example.com" and who.sub == "123"


@pytest.mark.parametrize("bad", [
    {"aud": "someone-else"}, {"iss": "evil.com"}, {"email_verified": "false"}, {"email": ""},
])
def test_a_google_token_failing_our_checks_is_refused(bad):
    with pytest.raises(google.GoogleInvalid):
        google.check_claims({**GOOD, **bad}, ["web-client"])


def test_no_configured_client_refuses_everything():
    with pytest.raises(google.GoogleInvalid):
        google.check_claims(GOOD, [])


def test_the_code_hash_is_salted_by_address():
    assert code_hash("a@x.com", "123456") != code_hash("b@x.com", "123456")
    assert code_hash("A@x.com ", "123456") == code_hash("a@x.com", "123456")


def test_the_email_carries_the_code():
    subject, body, html = mail.magic_link_email("https://x/verify", 15, "042917")
    assert "042917" in subject and "042917" in body and "042917" in html
