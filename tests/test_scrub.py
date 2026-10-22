"""PII scrubber: what must never leave the machine, and what must survive."""
from __future__ import annotations

from reply_drafter.scrub import Scrubber

TEXT = (
    "Hi Jordan, this is Alex Approver. Reach me at aapprover@example.edu or 555.010.1234. "
    "My student ID is 123456789 and NetID: jdoe01. Key sk-ant-abcdefghijklmnop1234. "
    "See https://help.example.edu/kb/assistant-access.html for steps on HELP-123."
)


def test_pii_is_removed():
    out = Scrubber(["Jordan Doe", "Alex Approver"]).scrub(TEXT)
    for leaked in ("Jordan", "Approver", "Alex", "aapprover@example.edu", "010.1234", "123456789", "jdoe01", "sk-ant"):
        assert leaked not in out, leaked


def test_useful_context_survives():
    out = Scrubber(["Jordan Doe", "Alex Approver"]).scrub(TEXT)
    assert "https://help.example.edu/kb/assistant-access.html" in out
    assert "HELP-123" in out


def test_restore_round_trip_except_secrets():
    s = Scrubber(["Jordan Doe", "Alex Approver"])
    restored = s.restore(s.scrub(TEXT))
    assert "Jordan" in restored and "aapprover@example.edu" in restored
    assert "sk-ant" not in restored
    assert "[SECRET_1]" in restored


def test_same_value_gets_same_placeholder():
    s = Scrubber(["Jordan Doe"])
    out = s.scrub("Jordan asked. Later JORDAN DOE asked again. jordan@example.edu, Jordan@example.edu")
    assert out.count("[EMAIL_1]") == 2
    assert "[EMAIL_2]" not in out


def test_ambiguous_tokens_need_full_name():
    out = Scrubber(["Will Grant"]).scrub("Will this grant access? Will Grant asked.")
    assert out.startswith("Will this grant access?")
    assert "Will Grant" not in out


def test_public_role_address_is_kept():
    out = Scrubber(keep_emails=["help@example.edu"]).scrub("Email help@example.edu or me at pat@example.edu")
    assert "help@example.edu" in out and "pat@example.edu" not in out


def test_greeting_name_without_directory_entry():
    out = Scrubber().scrub("Hello Priya,\nThanks for reaching out. Hi there, Hello Team.")
    assert "Priya" not in out
    assert "Hi there" in out and "Hello Team" in out
