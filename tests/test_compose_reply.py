"""Greeting and sign-off added in code (Jira adds neither)."""
from __future__ import annotations

from reply_drafter.drafter import SIGN_OFF, compose_reply


def test_adds_first_name_greeting_and_team_sign_off():
    out = compose_reply("Your access is ready.", "Jane Doe")
    assert out == f"Hi Jane,\n\nYour access is ready.\n\n{SIGN_OFF}"


def test_directory_style_name():
    assert compose_reply("Done.", "Doe, Jane").startswith("Hi Jane,")


def test_unknown_name_falls_back_to_hello():
    assert compose_reply("Done.", "").startswith("Hello,\n\n")


def test_does_not_double_up():
    body = f"Hi Jane,\n\nDone.\n\n{SIGN_OFF}"
    assert compose_reply(body, "Jane Doe") == body
