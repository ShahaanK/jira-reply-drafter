# Jira reply drafter

A dry-run tool that triages tickets from a Jira Service Management help queue and drafts a reply for a staff member to review. I wrote it for a university IT help desk. This copy has the code only: no tickets, no staff names, no internal articles, and no site-specific URLs. The hostnames, product names and project key in it are placeholders.

The bot never writes to Jira. Everything it produces is a local HTML page.

## What it does

For each ticket it:

1. Triages it: picks the matching knowledge-base article (or "novel" if none fits), a confidence level, and whether a person needs to decide something.
2. Drafts a reply grounded in the KB articles and in similar, previously resolved tickets.
3. Writes two local pages: a staff view (the exact internal note it would post, plus checks and the past tickets it used) and a requester view (the email the requester would get if staff sent the draft unedited).

## How it works

```
ticket -> similar resolved tickets (local SQLite FTS5 search, recent ones preferred)
       -> matching KB articles + a product-name glossary
       -> model returns: category, confidence, needs-a-human, reply
       -> code adds the greeting and sign-off
       -> local checks: every link must exist in the KB, no outdated product names, no leftover placeholders
       -> review page (internal note) + requester page (email)
```

Design choices I made on purpose:

- Read-only Jira client. There is no code path that posts to Jira. The write payload is built only so it can be previewed.
- Project guard. `run_bot.py` refuses ticket keys from any project other than the configured one.
- Internal notes are never used as example answers, so staff-only information cannot leak into a requester-facing draft. "Staff" is inferred from who actually answers tickets in the project, not from an email domain.
- KB articles win over old ticket replies when they disagree, because policies change.
- Optional PII scrubber (`scrub.py`) replaces names, emails, phone numbers, IDs and secrets with placeholders before each model call, then restores them in the reply locally.
- The staff page has a scoring form (send as-is, minor edits, major edits, unusable) so a reviewer can rate drafts and download the scores as JSON.

## Stack

Python 3.11+, httpx and tenacity (Jira REST with retries and rate-limit handling), pydantic-settings, SQLite FTS5, scikit-learn (ticket clustering), the Anthropic API for triage and drafting, pytest. Packaged with uv.

## Layout

```
src/reply_drafter/
  config.py          settings from .env
  jira_client.py     read-only Jira REST client
  extractor.py       turns a ticket into (question, staff reply); skips internal notes
  corpus.py          SQLite full-text index of past tickets, recency-boosted search
  kb.py              loads KB articles, glossary, link list
  drafter.py         triage + draft for one ticket, greeting/sign-off, checks
  llm.py             model call
  scrub.py           optional name/email/ID pseudonymizer
  jira_note.py       builds the internal-note payload (preview only)
  review.py          staff review page
  requester_view.py  requester email page
scripts/
  phase1_ingest.py   pull tickets into the local corpus
  run_bot.py         eval / queue / keys dry runs
  rerender_review.py rebuild pages from saved results
  cluster_tickets.py topic clusters vs KB topics
kb/issues/           one synthetic sample article (the real ones are not included)
tests/
```

## Running it

You need a Jira Cloud site with a Service Management project, an Atlassian API token, an Anthropic API key, and a folder of KB articles in `kb/issues/` (Markdown with YAML front matter; see the sample).

```bash
uv sync
cp .env.example .env      # fill in the Jira and Anthropic values
uv run pytest
uv run scripts/phase1_ingest.py                 # build the local corpus (read-only)
uv run scripts/run_bot.py eval --n 10           # draft recent resolved tickets
uv run scripts/run_bot.py keys HELP-1 HELP-2    # specific tickets
```

Set `JIRA_CLOUD_ID` and `JIRA_PROJECT` in `.env` for your site. The review pages and the local corpus contain real ticket text, so `out/` and `.autodrafter/` are gitignored.

## Status

Dry run only. The tests cover the extractor, the scrubber, reply composition and the KB checks (22 passing on the synthetic data in this copy).

## License

MIT. See LICENSE.