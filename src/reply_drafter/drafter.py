"""Triage + reply drafter for one HELP ticket. Output only; never writes to Jira.

Grounding: similar resolved tickets from the corpus
(recency-boosted), the KB articles, and the terminology glossary. KB
articles win over old ticket replies when they disagree, because policies change.

The model returns JSON with a triage category (a KB article slug or "novel")
and a requester-facing reply. Checks run locally afterwards: links outside the
KB, outdated product names, and any leftover placeholders.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from reply_drafter.config import Settings
from reply_drafter.corpus import CorpusStore, RetrievalHit
from reply_drafter.extractor import extract
from reply_drafter.kb import (
    NOVEL, OUTDATED_TERMS, Article, find_urls, glossary_text, normalize_url, rank_articles,
)
from reply_drafter.llm import LLMResult, generate
from reply_drafter.scrub import Scrubber

log = logging.getLogger(__name__)

_SYSTEM = """You help university IT staff answer tickets in the help (HELP) queue.
HELP covers the university's AI services: the university's AI assistant (formerly OldAssistant) and the AI tools the university licenses.

For the ticket you are given:
1. Triage it: pick the ONE KB article slug that best matches, or "novel" if no article covers it.
2. Draft a reply a staff member could send to the requester after a quick review.

Rules for the reply:
- Direct, warm, and specific. Plain text, no markdown. No greeting line and no sign-off.
- Ground every fact in the KB articles first, then the similar resolved tickets. When an older
  ticket reply disagrees with a KB article, follow the KB article: policies change.
- Only use links that appear in the KB articles' official links. Never invent links, steps,
  services, timelines, or promises.
- If the request needs a human decision (granting access, exceptions, billing, anything not
  covered), say what IT will do next instead of deciding it, and set needs_human.
- Use current product names:
{glossary}

Respond with JSON only, no prose around it:
{{"category": "<kb slug or novel>", "confidence": "high|medium|low",
  "needs_human": true|false, "needs_human_reason": "<short, empty if false>",
  "staff_summary": "<one line for the agent: what the requester wants>",
  "reply": "<the drafted reply>"}}"""

_PLACEHOLDER_RE = re.compile(r"\[(?:PERSON|EMAIL|PHONE|ID|NETID|SECRET)_\d+\]")


@dataclass
class DraftResult:
    target_key: str
    summary: str
    question: str
    created: str
    category: str = NOVEL
    confidence: str = "low"
    needs_human: bool = True
    needs_human_reason: str = ""
    staff_summary: str = ""
    reply: str = ""
    exemplars: list[RetrievalHit] = field(default_factory=list)
    kb_candidates: list[Article] = field(default_factory=list)
    kb_article: Article | None = None
    flags: list[str] = field(default_factory=list)
    llm: LLMResult | None = None
    historical_reply: str = ""
    error: str = ""


def draft_ticket(
    issue: dict[str, Any],
    settings: Settings,
    store: CorpusStore,
    articles: list[Article],
    *,
    known_names: list[str],
    model: str | None = None,
    exemplars_n: int = 5,
    as_of_ticket: bool = False,
) -> DraftResult:
    """Draft for a raw Jira issue dict.

    as_of_ticket: for evaluation on resolved tickets, only use exemplars created
    before the ticket, so the bot cannot copy its own (or a later) answer.
    """
    pair = extract(issue)
    question = pair.question or pair.summary
    result = DraftResult(
        target_key=pair.key, summary=pair.summary, question=question,
        created=pair.created, historical_reply=pair.reply,
    )

    query = f"{pair.summary}\n{question}"
    result.exemplars = store.search(
        query, limit=exemplars_n, exclude_key=pair.key,
        created_before=pair.created if as_of_ticket else None,
    )
    result.kb_candidates = rank_articles(query, articles, limit=3)

    scrubber = (
        Scrubber(known_names, keep_emails=_public_emails(articles))
        if settings.scrub_pii_before_llm else None
    )
    prompt = _build_prompt(pair.key, pair.summary, question, result.exemplars,
                           result.kb_candidates, articles)
    system = _SYSTEM.format(glossary=glossary_text())
    try:
        llm = generate(settings, system=system, prompt=prompt, model=model,
                       max_tokens=4000, scrubber=scrubber)
    except Exception as exc:  # surfaced on the review page, never swallowed silently
        result.error = str(exc)
        return result
    result.llm = llm
    _apply_model_output(result, llm.text, articles)
    if result.reply:
        reporter = ((issue.get("fields") or {}).get("reporter") or {}).get("displayName", "")
        result.reply = compose_reply(result.reply, reporter)
    result.flags = _check(result, articles)
    return result


SIGN_OFF = "Thanks,\nIT Help Desk"
_GREETINGS = ("hi ", "hi,", "hello", "hey", "dear", "good morning", "good afternoon", "good evening")


def compose_reply(body: str, requester_display_name: str) -> str:
    """Add the greeting and team sign-off in code.

    Jira does not add either, and the requester's name never needs to go to the model.
    """
    body = body.strip()
    first = _first_name(requester_display_name)
    if not body.lower().startswith(_GREETINGS):
        body = f"{'Hi ' + first + ',' if first else 'Hello,'}\n\n{body}"
    if not body.rstrip().endswith(SIGN_OFF):
        body = f"{body}\n\n{SIGN_OFF}"
    return body


def _first_name(display_name: str) -> str:
    name = (display_name or "").strip()
    if "," in name:                      # "Last, First" directory style
        name = name.split(",", 1)[1].strip()
    first = name.split(" ")[0] if name else ""
    return first if first.isalpha() else ""


def _build_prompt(
    key: str,
    summary: str,
    question: str,
    hits: list[RetrievalHit],
    candidates: list[Article],
    articles: list[Article],
) -> str:
    parts = [f"TICKET: {key}", f"SUMMARY: {summary}", "", "REQUEST:", question.strip()]

    parts.append("\n--- ALL KB ARTICLES (slug: title - summary) ---")
    parts += [f"{a.slug}: {a.title} - {a.summary}" for a in articles]

    if candidates:
        parts.append("\n--- MOST RELEVANT KB ARTICLES (full) ---")
        parts += [f"\n{a.for_prompt()}" for a in candidates]

    if hits:
        parts.append("\n--- SIMILAR RESOLVED TICKETS (older replies may be out of date) ---")
        for h in hits:
            parts.append(f"\n[{h.key}, {h.created or 'date unknown'}] {h.summary}")
            parts.append(f"Q: {(h.question or '')[:500]}")
            parts.append(f"A: {(h.reply or '')[:800]}")

    parts.append("\nReturn the JSON now.")
    return "\n".join(parts)


def _apply_model_output(result: DraftResult, text: str, articles: list[Article]) -> None:
    data = _parse_json(text)
    if data is None:
        result.error = "Model did not return valid JSON"
        result.reply = text
        return
    slugs = {a.slug: a for a in articles}
    category = str(data.get("category", NOVEL)).strip()
    result.category = category if category in slugs else NOVEL
    result.kb_article = slugs.get(result.category)
    result.confidence = str(data.get("confidence", "low")).lower()
    result.needs_human = bool(data.get("needs_human", True))
    result.needs_human_reason = str(data.get("needs_human_reason", "")).strip()
    result.staff_summary = str(data.get("staff_summary", "")).strip()
    result.reply = str(data.get("reply", "")).strip()


def _public_emails(articles: list[Article]) -> set[str]:
    return {u.removeprefix("mailto:") for a in articles for _, u in a.links if u.startswith("mailto:")}


def _parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _check(result: DraftResult, articles: list[Article]) -> list[str]:
    flags: list[str] = []
    allowed = set().union(*(a.urls for a in articles)) if articles else set()
    for url in find_urls(result.reply):
        if normalize_url(url) not in allowed:
            flags.append(f"Link not found in the KB: {url}")
    if OUTDATED_TERMS.search(result.reply):
        flags.append("Uses the old name OldAssistant (should be NewAssistant)")
    leftover = _PLACEHOLDER_RE.findall(result.reply)
    if leftover:
        flags.append(f"Unresolved placeholders in reply: {', '.join(sorted(set(leftover)))}")
    if not result.reply:
        flags.append("Empty reply")
    return flags
