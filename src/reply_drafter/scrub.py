"""Optional PII scrubber applied before model calls.

Pseudonymizes personal data in text before it leaves this machine, then
restores the placeholders in the model's reply so the local preview reads
naturally. The real -> placeholder mapping never leaves the process.

Switch: SCRUB_PII_BEFORE_LLM (see config.py), off by default. The clustering
script always uses it when asking for cluster names.

Coverage (regex + known names, no model calls):
- secrets: API keys / tokens (never restored)
- emails, phone numbers, 9-digit student IDs, explicit "NetID: x" mentions
- person names: every display name seen in HELP (reporters, assignees,
  commenters) plus names that follow a greeting ("Hi Jordan,")
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

_SECRET_RE = re.compile(
    r"\b(?:sk-(?:ant|or|proj)?-?[A-Za-z0-9_\-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[abpr]-[A-Za-z0-9\-]{10,}"
    r"|[A-Fa-f0-9]{32,}"
    r"|[A-Za-z0-9_\-]{40,})\b"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\w)")
_STUDENT_ID_RE = re.compile(r"(?<!\d)\d{9}(?!\d)")
_NETID_RE = re.compile(r"(?i)\b(net\s?id\s*(?:is|:|-)?\s*)([a-z][a-z0-9]{1,15})\b")
_GREETING_RE = re.compile(
    r"\b(?:Hi|Hello|Hey|Dear|Good\s+(?:morning|afternoon|evening))[,\s]+([A-Z][a-z]{2,})\b"
)

# Name tokens that are also everyday words; only matched as part of a full name.
_AMBIGUOUS = frozenset({
    "will", "may", "mark", "grant", "bill", "rose", "june", "april", "august",
    "art", "joy", "hope", "faith", "summer", "dawn", "chase", "page", "lane",
    "young", "king", "long", "white", "brown", "green", "black", "gray", "grey",
    "wood", "hall", "hill", "ford", "price", "rich", "sterling", "star", "doe",
    "assistant", "help", "team", "desk", "support", "service", "admin", "its",
    "university", "exampleu", "office", "staff", "user", "student",
})

_NO_RESTORE = frozenset({"SECRET"})


class Scrubber:
    """Reversible pseudonymizer. One instance per ticket so numbering stays stable."""

    def __init__(self, known_names: Iterable[str] = (), keep_emails: Iterable[str] = ()) -> None:
        self._forward: dict[str, str] = {}
        self._reverse: dict[str, str] = {}
        self._counts: Counter[str] = Counter()
        self._name_re = _compile_names(known_names)
        # Public role addresses (e.g. help@example.edu from the KB) are not personal data.
        self._keep_emails = {e.lower() for e in keep_emails}

    def scrub(self, text: str) -> str:
        if not text:
            return text
        text = _SECRET_RE.sub(lambda m: self._token("SECRET", m.group(0)), text)
        text = _EMAIL_RE.sub(
            lambda m: m.group(0) if m.group(0).lower() in self._keep_emails else self._token("EMAIL", m.group(0)),
            text,
        )
        text = _PHONE_RE.sub(lambda m: self._token("PHONE", m.group(0)), text)
        text = _STUDENT_ID_RE.sub(lambda m: self._token("ID", m.group(0)), text)
        text = _NETID_RE.sub(lambda m: m.group(1) + self._token("NETID", m.group(2)), text)
        if self._name_re is not None:
            text = self._name_re.sub(lambda m: self._token("PERSON", m.group(0)), text)
        text = _GREETING_RE.sub(self._greeting, text)
        return text

    def restore(self, text: str) -> str:
        if not text or not self._reverse:
            return text
        for placeholder, original in self._reverse.items():
            if placeholder.strip("[]").rsplit("_", 1)[0] in _NO_RESTORE:
                continue
            text = text.replace(placeholder, original)
        return text

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def _greeting(self, m: re.Match[str]) -> str:
        name = m.group(1)
        if name.lower() in _AMBIGUOUS or name.lower() in {"there", "all", "everyone", "folks"}:
            return m.group(0)
        return m.group(0)[: m.start(1) - m.start(0)] + self._token("PERSON", name)

    def _token(self, kind: str, original: str) -> str:
        key = original.lower() if kind in {"EMAIL", "PERSON", "NETID"} else original
        if key in self._forward:
            return self._forward[key]
        self._counts[kind] += 1
        placeholder = f"[{kind}_{self._counts[kind]}]"
        self._forward[key] = placeholder
        self._reverse[placeholder] = original
        return placeholder


def _compile_names(known_names: Iterable[str]) -> re.Pattern[str] | None:
    """Full names match case-insensitively; single tokens only when Capitalized."""
    full: set[str] = set()
    tokens: set[str] = set()
    for name in known_names:
        name = re.sub(r"\s+", " ", (name or "").strip())
        if not name or "@" in name:
            continue
        parts = [p for p in re.split(r"[\s,]+", name) if p]
        if len(parts) >= 2:
            full.add(name)
        for p in parts:
            p = p.strip(".()")
            if len(p) >= 3 and p.isalpha() and p.lower() not in _AMBIGUOUS:
                tokens.add(p[0].upper() + p[1:].lower())
    if not full and not tokens:
        return None
    alts = [f"(?i:{re.escape(n)})" for n in sorted(full, key=len, reverse=True)]
    alts += [re.escape(t) for t in sorted(tokens, key=len, reverse=True)]
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b")
