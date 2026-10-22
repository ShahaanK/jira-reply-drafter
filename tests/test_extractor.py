"""Extractor rules that protect drafts: no internal notes, no non-staff replies."""
from __future__ import annotations

from reply_drafter.extractor import collect_people, extract, infer_staff_ids


def _adf(text: str) -> dict:
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _user(account_id: str, name: str, email: str = "") -> dict:
    return {"accountId": account_id, "displayName": name, "emailAddress": email}


def _comment(author: dict, text: str, public: bool = True) -> dict:
    return {"author": author, "body": _adf(text), "jsdPublic": public}


REPORTER = _user("r1", "Riley Requester", "rreq@example.edu")
STAFF = _user("s1", "Sam Staff", "sstaff@example.edu")
CC_FACULTY = _user("f1", "Frankie Faculty", "ffac@example.edu")


def _issue(key: str, comments: list[dict], assignee: dict | None = None) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": "NewAssistant access",
            "description": _adf("I cannot log in to assistant.example.edu, it says access denied."),
            "status": {"name": "Closed"},
            "created": "2026-09-01T10:00:00.000-0400",
            "reporter": REPORTER,
            "assignee": assignee,
            "comment": {"comments": comments},
        },
    }


def test_internal_comment_is_never_the_reply():
    issue = _issue("HELP-1", [
        _comment(STAFF, "Internal: this user was flagged, do not grant yet.", public=False),
        _comment(STAFF, "You now have access. Please sign out and back in."),
    ])
    pair = extract(issue, staff_ids={"s1"})
    assert pair.bucket == "answered"
    assert "Internal" not in pair.reply
    assert pair.reply.startswith("You now have access")


def test_only_internal_comments_means_unanswered():
    issue = _issue("HELP-2", [_comment(STAFF, "Internal note only.", public=False)])
    assert extract(issue, staff_ids={"s1"}).bucket == "unanswered"


def test_cc_faculty_is_not_staff_when_staff_ids_known():
    issue = _issue("HELP-3", [_comment(CC_FACULTY, "Same problem here, any update?")])
    assert extract(issue, staff_ids={"s1"}).bucket == "unanswered"


def test_reporter_is_never_staff():
    issue = _issue("HELP-4", [_comment(REPORTER, "Never mind, it works now.")])
    assert extract(issue, staff_ids={"r1"}).bucket == "unanswered"


def test_v1_email_fallback_still_works_without_staff_ids():
    issue = _issue("HELP-5", [_comment(STAFF, "You now have access.")])
    assert extract(issue).bucket == "answered"


def test_infer_staff_ids():
    issues = [
        _issue("HELP-10", [_comment(STAFF, "Done.")], assignee=None),
        _issue("HELP-11", [_comment(STAFF, "Done.")]),
        _issue("HELP-12", [_comment(STAFF, "Done."), _comment(CC_FACULTY, "+1")]),
        _issue("HELP-13", [_comment(_user("n1", "Nora Note"), "internal", public=False)]),
        _issue("HELP-14", [], assignee=_user("a1", "Ari Assignee")),
    ]
    staff = infer_staff_ids(issues, min_tickets=3)
    assert "s1" in staff        # commented on 3 tickets they did not report
    assert "n1" in staff        # wrote an internal note
    assert "a1" in staff        # was assigned a ticket
    assert "f1" not in staff    # one-off CC'd commenter
    assert "r1" not in staff    # reporter


def test_collect_people():
    people = collect_people([_issue("HELP-20", [_comment(STAFF, "hi")])])
    assert people == {"r1": "Riley Requester", "s1": "Sam Staff"}
