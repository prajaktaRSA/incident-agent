import pytest

from incident_agent.redact import redact


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("Authorization: Basic aW5jaWRlbnQ6c2VjcmV0", "aW5jaWRlbnQ6c2VjcmV0"),
        ("upstream said: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi"),
        ("bad request api_key=abc123def", "abc123def"),
        ("signature=sk_live_a1b2c3d4 mismatch", "sk_live_a1b2c3d4"),
        ("POST https://hooks.acme.com/in?token=xyz&x=1 failed", "token=xyz"),
    ],
)
def test_removes_secrets(text, leaked):
    assert leaked not in redact(text)


def test_keeps_ordinary_text():
    text = "503 Service Unavailable from https://hooks.acme.com/in"
    assert redact(text) == text
