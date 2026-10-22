"""Ticket text extraction and bucketing.

Phase 0 findings drive the design:
- Replies live primarily in COMMENTS (most answered tickets in a sample),
  authored by staff with @example.edu email addresses.
- A minority of tickets are email-forwards where the staff reply is embedded
  in the description above quote delimiters (From: / Sent: / ---Original---).
- Junk: newsletters, service-renewal notices, CLONE tickets, form-dumps
  with no useful question text.

Extraction contract:
- question: the clean inbound request text (no quoted history, no staff reply)
- reply:    the clean staff response (no quoted history, no greeting boilerplate)
- bucket:   "answered" | "unanswered" | "junk"
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

# ── Quote / signature delimiters ──────────────────────────────────────────────

_QUOTE_RE = re.compile(
    r"(?:"
    r"^-{3,}\s*Original Message\s*-{3,}"  # ---Original Message---
    r"|^From:\s+\S"                         # From: email@...
    r"|^Sent:\s+\w"                         # Sent: Monday ...
    r"|^On\s+.{5,80}\s+wrote:\s*$"         # On <date> X wrote:
    r"|^>{1,}"                              # > quoted line
    r")",
    re.IGNORECASE | re.MULTILINE,
)

_SIG_RE = re.compile(
    r"^(?:Thanks?|Thank\s+you|Best(?:\s+regards?)?|Regards?|Sincerely|Cheers|Warmly)[,!.]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Website-form submissions via the IT help portal
_WEBSITE_FORM_RE = re.compile(
    r"A user has submitted a help request through the help website\.",
    re.IGNORECASE,
)
_REQUEST_DETAILS_RE = re.compile(
    r"Request Details:\s*\n?(.*)",
    re.IGNORECASE | re.DOTALL,
)

# Contact-block fallback for signatures without a sign-off word.
# "Example University" must be on its own line to avoid matching it inside
# body sentences like "access to the assistant at Example University!".
_CONTACT_BLOCK_RE = re.compile(
    r"\b[a-z0-9._%+-]+@(?:[a-z]+\.)?example\.edu\b"  # university email address
    r"|\b\d{3}\.\d{3}\.\d{4}\b"                      # dotted phone number
    r"|^Example University\s*$",                   # university name alone on a line
    re.IGNORECASE | re.MULTILINE,
)

# Staff forwarding/routing preambles that precede a quoted client message.
# Detected before the first quote delimiter; if matched, we extract the
# quoted content instead of discarding it.
_ROUTING_PREAMBLE_RE = re.compile(
    r"forwarding\s+(?:this|on|over|along)"
    r"|forwarded\s+(?:this|on|along)"
    r"|per\s+\w.{0,30}(?:below|message)"
    r"|please\s+see\s+(?:below|attached|the\s+forwarded)"
    r"|how\s+(?:would\s+you\s+like|should\s+(?:i|we))\s+(?:me\s+to\s+)?respond",
    re.IGNORECASE,
)

# Legal confidentiality footers appended by some email clients
_CONF_FOOTER_RE = re.compile(
    r"\n(?:CONFIDENTIALITY\s+(?:NOTICE|STATEMENT|DISCLAIMER)"
    r"|This\s+e[- ]?mail\s+(?:message|and|is)"
    r"|DISCLAIMER:).{0,2000}$",
    re.IGNORECASE | re.DOTALL,
)

# Junk heuristics
_JUNK_SUBJECT_RE = re.compile(
    r"\b(renewal|invoice|reminder|advertisement|unsubscribe|mailing list)\b",
    re.IGNORECASE,
)

_ORG_EMAIL_RE = re.compile(r"@example\.edu", re.IGNORECASE)

# ── ADF -> plain text ──────────────────────────────────────────────────────────

_INLINE_NODES = {"text", "hardBreak", "mention", "emoji", "inlineCard"}
_BLOCK_NODES = {
    "paragraph", "heading", "bulletList", "orderedList",
    "listItem", "blockquote", "codeBlock", "panel",
    "table", "tableRow", "tableCell", "tableHeader",
}


def adf_to_text(node: Any) -> str:
    """Flatten Atlassian Document Format to plain text (best-effort)."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    parts: list[str] = []
    for child in node.get("content") or []:
        t = child.get("type", "")
        if t == "text":
            parts.append(child.get("text", ""))
        elif t == "hardBreak":
            parts.append("\n")
        elif t in _BLOCK_NODES:
            inner = adf_to_text(child)
            if inner.strip():
                parts.append(inner.rstrip())
                parts.append("\n")
        else:
            parts.append(adf_to_text(child))
    return "".join(parts)


# ── Text cleaning ──────────────────────────────────────────────────────────────


def strip_quoted_thread(text: str) -> str:
    """Return only the portion of text before the first quote delimiter."""
    m = _QUOTE_RE.search(text)
    if m:
        return text[: m.start()].strip()
    return text.strip()


def strip_signature(text: str) -> str:
    """Remove trailing signature lines.

    Two-pass: (1) sign-off keyword on its own line, (2) contact-block fallback
    for signatures that start directly with a name/title (no sign-off word).
    Only scans the last 8 lines for the contact-block to avoid false positives
    when an email address appears naturally in the question body.
    """
    m = _SIG_RE.search(text)
    if m:
        return text[: m.start()].strip()
    # Fallback: find a university contact marker in the trailing lines
    lines = text.splitlines()
    search_from = max(0, len(lines) - 8)
    for i in range(len(lines) - 1, search_from - 1, -1):
        if _CONTACT_BLOCK_RE.search(lines[i]):
            # Walk back to the blank line that separates the signature from the body
            for k in range(i - 1, max(i - 6, -1), -1):
                if not lines[k].strip():
                    return "\n".join(lines[:k]).strip()
            # No blank line found; signatures are typically 4-6 lines deep,
            # so back up 5 lines from the contact marker to clear name/title/dept
            return "\n".join(lines[: max(0, i - 4)]).strip()
    return text.strip()


def _strip_website_form(text: str) -> str:
    """If text is an website-form submission, return only the Request Details body.

    Website-form tickets arrive as:
        Hello <staff>, how would you like me to respond...
        A user has submitted a help request through the help website.
        Client Name: ...  Client Email: ...  Subject: ...
        Request Details:
        <actual question here>

    Everything before 'Request Details:' is routing boilerplate.
    """
    if not _WEBSITE_FORM_RE.search(text):
        return text
    m = _REQUEST_DETAILS_RE.search(text)
    if m:
        return m.group(1).strip()
    return text


def _is_routing_preamble(text: str) -> bool:
    """Return True if text is a short staff forwarding message, not a real question."""
    if not text or len(text) > 400:
        return False
    return bool(_ROUTING_PREAMBLE_RE.search(text))


def _strip_confidentiality_footer(text: str) -> str:
    """Remove legal confidentiality disclaimers appended by some email clients."""
    m = _CONF_FOOTER_RE.search(text)
    if m:
        return text[: m.start()].strip()
    return text


def normalize(text: str) -> str:
    """Collapse whitespace; used for dedup hashing."""
    return re.sub(r"\s+", " ", text).strip().lower()


def sha256(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


# ── Extraction ────────────────────────────────────────────────────────────────


@dataclass
class ExtractedPair:
    key: str
    summary: str
    question: str
    reply: str
    reply_author: str
    reply_author_email: str
    created: str
    status: str
    bucket: str          # answered | unanswered | junk
    reply_hash: str      # SHA-256 of normalized reply (for dedup)
    source: str          # "comments" | "description" | "none"
    cluster_id: str = ""  # set by ingest after near-dupe clustering


def extract(issue: dict[str, Any], staff_ids: set[str] | None = None) -> ExtractedPair:
    """Extract a (question, reply) pair from a Jira issue dict.

    staff_ids: accountIds of HELP responders (see infer_staff_ids). When None,
    falls back to the v1 heuristic (any @example.edu author who is not the reporter).
    Internal comments (jsdPublic == False) are never used as the reply: they are
    staff-only notes and must not leak into requester-facing drafts.
    """
    key = issue["key"]
    fields = issue.get("fields") or {}

    summary = fields.get("summary") or ""
    status = (fields.get("status") or {}).get("name", "")
    created = fields.get("created", "")[:10]

    desc_text = adf_to_text(fields.get("description"))
    comments_raw = (fields.get("comment") or {}).get("comments") or []

    # ── Junk detection ────────────────────────────────────────────────────────
    if _is_junk(summary, desc_text, comments_raw):
        return _empty_pair(key, summary, status, created, bucket="junk")

    # ── Primary path: staff comments ─────────────────────────────────────────
    # Exclude the reporter: a self-resolution note ("working for me now")
    # written by the reporter themselves teaches the wrong voice.
    reporter_account_id = (fields.get("reporter") or {}).get("accountId", "")
    staff_comments = [
        c for c in comments_raw
        if _is_public(c) and _is_staff(c.get("author") or {}, reporter_account_id, staff_ids)
    ]

    if staff_comments:
        question = _extract_question_from_description(desc_text, summary)
        best_comment = staff_comments[0]
        reply_text = strip_signature(
            strip_quoted_thread(adf_to_text(best_comment.get("body")))
        ).strip()
        reply_author = (best_comment.get("author") or {}).get("displayName", "")
        reply_email = (best_comment.get("author") or {}).get("emailAddress", "")

        if question and reply_text:
            return ExtractedPair(
                key=key, summary=summary,
                question=question, reply=reply_text,
                reply_author=reply_author, reply_author_email=reply_email,
                created=created, status=status,
                bucket="answered", reply_hash=sha256(reply_text),
                source="comments",
            )

    # ── Secondary path: email-forward in description ──────────────────────────
    if desc_text and _QUOTE_RE.search(desc_text):
        m = _QUOTE_RE.search(desc_text)
        assert m is not None
        staff_reply = desc_text[: m.start()].strip()
        quoted_question = desc_text[m.start():].strip()
        staff_reply = strip_signature(staff_reply).strip()

        if staff_reply and quoted_question:
            return ExtractedPair(
                key=key, summary=summary,
                question=_clean_quoted_text(quoted_question),
                reply=staff_reply,
                reply_author="", reply_author_email="",
                created=created, status=status,
                bucket="answered", reply_hash=sha256(staff_reply),
                source="description",
            )

    # ── No usable reply ───────────────────────────────────────────────────────
    question = _extract_question_from_description(desc_text, summary)
    bucket = "unanswered" if question else "junk"
    return _empty_pair(key, summary, status, created, bucket=bucket)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_staff_email(email: str) -> bool:
    return bool(email) and _ORG_EMAIL_RE.search(email) is not None


def _is_public(comment: dict[str, Any]) -> bool:
    """Internal JSM notes carry jsdPublic == False; anything else is requester-visible."""
    return comment.get("jsdPublic", True) is not False


def _is_staff(author: dict[str, Any], reporter_account_id: str, staff_ids: set[str] | None) -> bool:
    account_id = author.get("accountId", "")
    if not account_id or account_id == reporter_account_id:
        return False
    if staff_ids is not None:
        return account_id in staff_ids
    return _is_staff_email(author.get("emailAddress", ""))


def infer_staff_ids(issues: list[dict[str, Any]], *, min_tickets: int = 3) -> set[str]:
    """Infer HELP responders from the project's own history.

    The agent-permission listing is not readable with this account, and the
    JSM agent role may be granted broadly, so "staff" means people who
    actually answer HELP tickets: anyone ever assigned a ticket, anyone who
    wrote an internal note, or anyone who commented on >= min_tickets tickets
    they did not report. A CC'd student or faculty member who chimes in once
    does not qualify.
    """
    staff: set[str] = set()
    tickets_commented: dict[str, set[str]] = {}
    for issue in issues:
        fields = issue.get("fields") or {}
        assignee = (fields.get("assignee") or {}).get("accountId")
        if assignee:
            staff.add(assignee)
        reporter = (fields.get("reporter") or {}).get("accountId", "")
        for c in (fields.get("comment") or {}).get("comments") or []:
            account_id = (c.get("author") or {}).get("accountId", "")
            if not account_id or account_id == reporter:
                continue
            if not _is_public(c):
                staff.add(account_id)
            tickets_commented.setdefault(account_id, set()).add(issue["key"])
    staff.update(a for a, keys in tickets_commented.items() if len(keys) >= min_tickets)
    return staff


def collect_people(issues: list[dict[str, Any]]) -> dict[str, str]:
    """accountId -> displayName for everyone seen on the tickets (feeds the PII scrubber)."""
    people: dict[str, str] = {}
    for issue in issues:
        fields = issue.get("fields") or {}
        users = [fields.get("reporter"), fields.get("assignee")]
        users += [c.get("author") for c in (fields.get("comment") or {}).get("comments") or []]
        for u in users:
            if u and u.get("accountId") and u.get("displayName"):
                people[u["accountId"]] = u["displayName"]
    return people


def _is_junk(summary: str, desc: str, comments: list[dict]) -> bool:
    if _JUNK_SUBJECT_RE.search(summary):
        return True
    if summary.upper().startswith("CLONE - "):
        return True
    # Form-dump: summary is the form name, no description, no comments
    form_patterns = [
        "Example Access Request Form",
        "Website Request:",
    ]
    for pat in form_patterns:
        if pat.lower() in summary.lower() and not desc.strip() and not comments:
            return True
    return False


def _extract_question_from_description(desc: str, summary: str) -> str:
    """Get the inbound question text from the description."""
    if not desc.strip():
        return summary.strip()
    # Website-form submission: extract only the Request Details body, discarding
    # any staff routing preamble that precedes the form header.
    text = _strip_website_form(desc)
    # Forwarded email: if the content before the first quote delimiter is purely
    # staff routing boilerplate, extract the client message from the quoted thread
    # instead of discarding it along with the quote.
    m = _QUOTE_RE.search(text)
    if m:
        preamble = text[: m.start()].strip()
        if _is_routing_preamble(preamble):
            text = _clean_quoted_text(text[m.start():])
        else:
            text = preamble
    text = _strip_confidentiality_footer(text)
    text = strip_signature(text)
    # If what remains is just routing boilerplate (no actual question), use summary
    if _is_routing_preamble(text):
        text = ""
    return text.strip() or summary.strip()


def _clean_quoted_text(text: str) -> str:
    """Strip email-header lines from quoted text to get the question body."""
    lines = text.splitlines()
    cleaned: list[str] = []
    skip_patterns = re.compile(
        r"^(From:|Sent:|To:|Cc:|Subject:|Date:|On\s+.+wrote:|-{3,})",
        re.IGNORECASE,
    )
    for line in lines:
        if skip_patterns.match(line.strip()):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _empty_pair(
    key: str, summary: str, status: str, created: str, bucket: str
) -> ExtractedPair:
    return ExtractedPair(
        key=key, summary=summary,
        question="", reply="",
        reply_author="", reply_author_email="",
        created=created, status=status,
        bucket=bucket, reply_hash="",
        source="none",
    )
