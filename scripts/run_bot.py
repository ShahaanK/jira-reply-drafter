"""HELP bot dry run: triage + draft tickets and write a local review page. Never posts to Jira.

Usage:
    uv run scripts/run_bot.py eval [--n 10] [--days 60] [--seed 7] [--ab]
    uv run scripts/run_bot.py queue [--ab]
    uv run scripts/run_bot.py keys HELP-783 HELP-790 [--ab]

eval  : recent resolved tickets; drafts only see tickets answered before them,
        and the page shows what staff actually replied (policies may have changed since).
queue : open, unassigned HELP tickets (live preview).
--ab  : also draft with COMPARE_MODEL (Sonnet) for side-by-side scoring.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from reply_drafter.config import load_settings
from reply_drafter.corpus import CorpusStore
from reply_drafter.drafter import DraftResult, draft_ticket
from reply_drafter.jira_client import JiraClient
from reply_drafter.kb import load_articles
from reply_drafter.requester_view import write_requester_page
from reply_drafter.review import requester_page_name, write_review_page


def main() -> None:
    ap = argparse.ArgumentParser(description="HELP bot dry run (no Jira writes)")
    ap.add_argument("mode", choices=["eval", "queue", "keys"])
    ap.add_argument("keys", nargs="*", metavar="KEY")
    ap.add_argument("--n", type=int, default=10, help="eval: number of tickets")
    ap.add_argument("--days", type=int, default=60, help="eval: only tickets from the last N days")
    ap.add_argument("--seed", type=int, default=7, help="eval: sample seed (same seed = same tickets)")
    ap.add_argument("--ab", action="store_true", help="also draft with COMPARE_MODEL")
    args = ap.parse_args()

    settings = load_settings()
    project = settings.jira_project
    store = CorpusStore(settings.corpus_db_path)
    articles = load_articles(settings.kb_issues_dir)
    names = store.people_names()
    models = [settings.draft_model] + ([settings.compare_model] if args.ab else [])

    with JiraClient(settings) as jira:
        operator = jira.get_myself().get("displayName", "HELP assistant")
        keys = _select_keys(args, jira, store, project)
        bad = [k for k in keys if not k.startswith(f"{project}-")]
        if bad:
            sys.exit(f"Refusing tickets outside {project}: {', '.join(bad)}")
        print(f"{args.mode}: {len(keys)} tickets x {len(models)} model(s) | "
              f"scrub={settings.scrub_pii_before_llm}")

        tickets: list[tuple[DraftResult, ...]] = []
        requesters: dict[str, str] = {}
        for i, key in enumerate(keys, 1):
            issue = jira.get_issue(key)
            requesters[key] = ((issue.get("fields") or {}).get("reporter") or {}).get("displayName", "Requester")
            drafts = tuple(
                draft_ticket(issue, settings, store, articles, known_names=names, model=m,
                             as_of_ticket=(args.mode == "eval"))
                for m in models
            )
            status = "; ".join(
                f"{(d.llm.model.split('/')[-1] if d.llm else 'error')}: {d.category}"
                + (f" [{len(d.flags)} flags]" if d.flags else "") + (f" ERROR {d.error[:80]}" if d.error else "")
                for d in drafts
            )
            print(f"  [{i}/{len(keys)}] {key}: {status}")
            tickets.append(drafts)

    page = write_review_page(tickets, out_dir=settings.out_dir, mode=args.mode,
                             scrubbed=settings.scrub_pii_before_llm, operator=operator)
    write_requester_page(tickets, requesters, path=page.with_name(requester_page_name(page)),
                         operator=operator, review_page=page.name)
    dump = page.with_suffix(".json")
    dump.write_text(json.dumps([[_jsonable(d) for d in t] for t in tickets], indent=2, default=str),
                    encoding="utf-8")
    print(f"\nReview page: {page.resolve()}\nRaw results: {dump.resolve()}")


def _select_keys(args: argparse.Namespace, jira: JiraClient, store: CorpusStore, project: str) -> list[str]:
    if args.mode == "keys":
        if not args.keys:
            sys.exit("keys mode needs at least one ticket key")
        return [k.upper() for k in args.keys]
    if args.mode == "queue":
        jql = (f"project = {project} AND resolution = Unresolved AND assignee is EMPTY "
               "ORDER BY created ASC")
        return [i["key"] for i in jira.search_issues(jql, fields=["key"])]
    since = (date.today() - timedelta(days=args.days)).isoformat()
    pool = [r["key"] for r in store.pairs("answered")
            if (r["created"] or "") >= since and len(r["question"] or "") >= 40]
    rng = random.Random(args.seed)
    return sorted(rng.sample(pool, min(args.n, len(pool))), key=lambda k: int(k.split("-")[1]))


def _jsonable(d: DraftResult) -> dict:
    data = asdict(d)
    data["kb_candidates"] = [a.slug for a in d.kb_candidates]
    data["kb_article"] = d.kb_article.slug if d.kb_article else None
    return data


if __name__ == "__main__":
    main()
