"""Build the exact internal-note payload the bot would post, and render it for preview.

Dry-run only: nothing in this module talks to Jira. The
review page renders the payload itself, so what you approve is byte-for-byte
what would be sent once the write path is approved.

Payload shape: POST /rest/api/3/issue/{key}/comment with an ADF body and the
JSM property sd.public.comment = {"internal": true}, which makes it an
internal note (agents only, never visible to the requester).
"""
from __future__ import annotations

import html
from typing import Any

from reply_drafter.drafter import DraftResult
from reply_drafter.kb import NOVEL

INTERNAL_PROPERTY = {"key": "sd.public.comment", "value": {"internal": True}}


def build_payload(result: DraftResult) -> dict[str, Any]:
    return {"body": build_note_adf(result), "properties": [INTERNAL_PROPERTY]}


def build_note_adf(result: DraftResult) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        _para(_text("Suggested reply - review before sending", strong=True)),
    ]
    for chunk in [p.strip() for p in result.reply.split("\n\n") if p.strip()]:
        content.append(_para(*_lines(chunk)))

    content.append({"type": "rule"})
    category = result.kb_article.title if result.kb_article else "Novel - not covered by the KB"
    triage = [_text("Triage: ", strong=True), _text(f"{category} ({result.confidence} confidence)")]
    content.append(_para(*triage))
    if result.needs_human:
        reason = result.needs_human_reason or "needs a staff decision"
        content.append(_para(_text("Needs a human: ", strong=True), _text(reason)))
    if result.staff_summary:
        content.append(_para(_text("Request: ", strong=True), _text(result.staff_summary)))
    if result.kb_article and result.category != NOVEL:
        kb = result.kb_article
        parts = [_text("KB article: ", strong=True)]
        parts.append(_text(kb.title, link=kb.public_url) if kb.public_url else _text(kb.title))
        content.append(_para(*parts))
    if result.exemplars:
        parts = [_text("Similar past tickets: ", strong=True)]
        for i, h in enumerate(result.exemplars):
            if i:
                parts.append(_text(", "))
            parts.append(_text(h.key))
        content.append(_para(*parts))
    model = result.llm.model if result.llm else "unknown model"
    content.append(_para(_text(f"Drafted by the HELP assistant ({model}). Edit before sending.", em=True)))
    return {"type": "doc", "version": 1, "content": content}


def adf_to_html(doc: dict[str, Any]) -> str:
    """Render the subset of ADF this module produces."""
    out: list[str] = []
    for node in doc.get("content", []):
        if node["type"] == "rule":
            out.append("<hr>")
        elif node["type"] == "paragraph":
            out.append("<p>" + "".join(_inline_html(n) for n in node.get("content", [])) + "</p>")
    return "\n".join(out)


def _inline_html(node: dict[str, Any]) -> str:
    if node["type"] == "hardBreak":
        return "<br>"
    text = html.escape(node.get("text", ""))
    for mark in node.get("marks", []):
        if mark["type"] == "strong":
            text = f"<strong>{text}</strong>"
        elif mark["type"] == "em":
            text = f"<em>{text}</em>"
        elif mark["type"] == "link":
            text = f'<a href="{html.escape(mark["attrs"]["href"])}">{text}</a>'
    return text


def _para(*inline: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(inline)}


def _lines(chunk: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for i, line in enumerate(chunk.split("\n")):
        if i:
            nodes.append({"type": "hardBreak"})
        if line:
            nodes.append(_text(line))
    return nodes


def _text(text: str, *, strong: bool = False, em: bool = False, link: str = "") -> dict[str, Any]:
    marks: list[dict[str, Any]] = []
    if strong:
        marks.append({"type": "strong"})
    if em:
        marks.append({"type": "em"})
    if link:
        marks.append({"type": "link", "attrs": {"href": link}})
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node
