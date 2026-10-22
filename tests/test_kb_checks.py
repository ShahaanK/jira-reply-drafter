"""KB loading and the local checks run on every draft."""
from __future__ import annotations

from pathlib import Path

import pytest

from reply_drafter.drafter import DraftResult, _check
from reply_drafter.kb import find_urls, load_articles, normalize_url

_ROOT = Path(__file__).parent.parent
KB_DIR = next((d for d in (_ROOT / "kb-repo" / "content" / "issues", _ROOT / "kb" / "issues") if d.exists()),
              _ROOT / "kb" / "issues")

pytestmark = pytest.mark.skipif(
    not KB_DIR.exists(), reason="no KB articles found (kb/issues or kb-repo)"
)


def _result(reply: str) -> DraftResult:
    return DraftResult(target_key="HELP-1", summary="", question="", created="", reply=reply)


def test_finds_bare_and_schemed_links():
    urls = find_urls("See help.example.edu/ai-tool.html or https://assistant.example.edu/ and email help@example.edu.")
    assert {normalize_url(u) for u in urls} == {"help.example.edu/ai-tool.html", "assistant.example.edu"}


def test_kb_loads_all_published_articles():
    articles = load_articles(KB_DIR)
    assert len(articles) >= 1
    assert any("assistant.example.edu" in a.urls for a in articles)


def test_check_flags_invented_link_old_name_and_placeholders():
    articles = load_articles(KB_DIR)
    flags = _check(_result("Go to https://old-access.example.edu/form, OldAssistant, ask [PERSON_1]."), articles)
    assert any("old-access.example.edu" in f for f in flags)
    assert any("OldAssistant" in f for f in flags)
    assert any("[PERSON_1]" in f for f in flags)


def test_check_passes_kb_links():
    articles = load_articles(KB_DIR)
    assert _check(_result("Sign in at https://assistant.example.edu/ (NewAssistant)."), articles) == []
