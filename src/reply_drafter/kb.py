"""KB articles as grounding + the triage taxonomy, plus the terminology glossary.

Source of truth: kb-repo/content/issues/*.md (YAML front matter). These
are scrubbed, human-reviewed articles, so they replace the old llms.txt
grounding.

Triage taxonomy v0: one category per KB article plus
"novel" for anything the KB does not cover. Clustering results may refine it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

NOVEL = "novel"

# Public copies on a static site (only some articles are published there).
_PUBLIC_BASE = "https://kb.example.edu/"

# Terminology the drafts must follow (set by the help-desk owner). Applied as a prompt
# instruction; the review page also flags drafts that still use old names.
GLOSSARY: list[tuple[str, str]] = [
    ("OldAssistant / oldAssistant / Old Assistant (the previous assistant platform)",
     "NewAssistant. The URL is still https://assistant.example.edu/ ."),
    ("CampusSearch (class search tool)",
     "CampusSearch (unchanged)."),
]
OUTDATED_TERMS = re.compile(r"\boldassistant\b", re.IGNORECASE)


@dataclass
class Article:
    slug: str
    title: str
    category: str
    summary: str
    short_answer: str
    steps: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    last_reviewed: str = ""
    public_url: str = ""
    urls: set[str] = field(default_factory=set)   # every link anywhere in the article, normalized

    def for_prompt(self) -> str:
        lines = [f"[{self.slug}] {self.title} (last reviewed {self.last_reviewed or 'unknown'})",
                 self.short_answer.strip()]
        if self.steps:
            lines.append("Steps:")
            lines += [f"  {i}. {s}" for i, s in enumerate(self.steps, 1)]
        if self.links:
            lines.append("Official links:")
            lines += [f"  - {t}: {u}" for t, u in self.links]
        return "\n".join(lines)


def load_articles(issues_dir: Path) -> list[Article]:
    docs_dir = issues_dir.parent.parent / "docs"
    articles: list[Article] = []
    for path in sorted(issues_dir.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta = _front_matter(raw)
        if not meta or meta.get("status", "published") != "published":
            continue
        slug = path.stem
        articles.append(Article(
            slug=slug,
            title=str(meta.get("title", slug)),
            category=str(meta.get("category", "")),
            summary=str(meta.get("summary", "")),
            short_answer=str(meta.get("short_answer", "")),
            steps=[str(s) for s in meta.get("steps") or []],
            links=[(str(l.get("text", "")), str(l.get("url", ""))) for l in meta.get("official_links") or []],
            last_reviewed=str(meta.get("last_reviewed", "")),
            public_url=_PUBLIC_BASE + f"{slug}.html" if (docs_dir / f"{slug}.html").exists() else "",
            urls={normalize_url(u) for u in find_urls(raw)},
        ))
    return articles


# Links with or without a scheme ("help.example.edu/ai-tool.html" counts too).
_LINK_RE = re.compile(
    r"(?:https?://|mailto:)[^\s)>\]\"'<]+"
    r"|(?<![@\w.\-])(?:[a-z0-9-]+\.)+(?:edu|com|org|net|ai|io|gov)\b(?:/[^\s)>\]\"'<]*)?",
    re.IGNORECASE,
)


def find_urls(text: str) -> list[str]:
    return [m for m in _LINK_RE.findall(text) if "@" not in m]


def normalize_url(url: str) -> str:
    url = re.sub(r"^(?:https?://|mailto:)", "", url.strip(), flags=re.IGNORECASE)
    return url.rstrip("/.,;:").lower()


def rank_articles(question: str, articles: list[Article], *, limit: int = 3) -> list[Article]:
    """Token-overlap ranking over title + summary + short answer (a few dozen articles; no index needed)."""
    q = _tokens(question)
    if not q:
        return []
    scored = []
    for a in articles:
        title = _tokens(a.title)
        body = _tokens(f"{a.summary} {a.short_answer} {' '.join(a.steps)}")
        score = 2 * len(q & title) + len(q & body)
        if score:
            scored.append((score, a))
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored[:limit]]


def glossary_text() -> str:
    return "\n".join(f"- {old} -> {new}" for old, new in GLOSSARY)


def _front_matter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    data = yaml.safe_load(m.group(1))
    return data if isinstance(data, dict) else {}


_STOP = frozenset({
    "the", "and", "for", "you", "your", "with", "this", "that", "how", "can", "what", "are",
    "have", "has", "not", "but", "from", "was", "get", "use", "using", "into", "about", "would",
    "could", "please", "thanks", "thank", "hello", "there", "just", "need", "want", "able",
})


def _tokens(text: str) -> set[str]:
    words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", text)}
    return {w.rstrip("s") for w in words if w not in _STOP}
