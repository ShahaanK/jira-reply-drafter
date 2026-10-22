"""Re-render a review page from its saved JSON (no model calls, same drafts, scores kept).

Usage:
    uv run scripts/rerender_review.py out/review-eval-20261019-1327.json [more.json ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from reply_drafter.config import load_settings
from reply_drafter.corpus import RetrievalHit
from reply_drafter.drafter import DraftResult
from reply_drafter.kb import load_articles
from reply_drafter.llm import LLMResult
from reply_drafter.requester_view import write_requester_page
from reply_drafter.review import requester_page_name, write_review_page


def main() -> None:
    settings = load_settings()
    articles = {a.slug: a for a in load_articles(settings.kb_issues_dir)}
    from reply_drafter.jira_client import JiraClient

    with JiraClient(settings) as jira:
        operator = jira.get_myself().get("displayName", "HELP assistant")
        for arg in sys.argv[1:]:
            src = Path(arg)
            tickets = [tuple(_draft(d, articles) for d in t) for t in json.loads(src.read_text(encoding="utf-8"))]
            requesters = {
                t[0].target_key: (jira.get_issue(t[0].target_key)["fields"].get("reporter") or {}).get("displayName", "Requester")
                for t in tickets
            }
            mode = src.stem.split("-")[1]
            page = write_review_page(tickets, out_dir=src.parent, mode=mode,
                                     scrubbed=settings.scrub_pii_before_llm,
                                     operator=operator, path=src.with_suffix(".html"))
            req = write_requester_page(tickets, requesters, path=page.with_name(requester_page_name(page)),
                                       operator=operator, review_page=page.name)
            print(f"Re-rendered {page.resolve()}\n            {req.resolve()}")


def _draft(d: dict, articles: dict) -> DraftResult:
    d = dict(d)
    d["exemplars"] = [RetrievalHit(**h) for h in d.get("exemplars", [])]
    d["kb_candidates"] = [articles[s] for s in d.get("kb_candidates", []) if s in articles]
    d["kb_article"] = articles.get(d.get("kb_article") or "")
    d["llm"] = LLMResult(**d["llm"]) if d.get("llm") else None
    return DraftResult(**d)


if __name__ == "__main__":
    main()
