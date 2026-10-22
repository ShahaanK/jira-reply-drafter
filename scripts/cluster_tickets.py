"""Cluster HELP tickets locally and compare the clusters with the KB topics.

Usage:
    uv run scripts/cluster_tickets.py [--k 0] [--no-names]

TF-IDF + k-means run entirely on this machine. Only short,
scrubbed cluster digests (top terms + a few ticket summaries) go to Haiku to
get a readable name. Each cluster is matched to its nearest KB article;
clusters with no close article are KB gaps, i.e. candidate new triage
categories or new articles.

Output: out/clusters-<stamp>.html and .json (gitignored).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score
from sklearn.metrics.pairwise import cosine_similarity

from reply_drafter.config import load_settings
from reply_drafter.corpus import CorpusStore
from reply_drafter.kb import load_articles
from reply_drafter.llm import generate
from reply_drafter.scrub import Scrubber

GAP_THRESHOLD = 0.12   # cosine similarity below this = no KB article covers the cluster
_EXTRA_STOP = [
    "hi", "hello", "thanks", "thank", "please", "regards", "best", "dear", "help", "know", "like",
    "just", "need", "want", "able", "would", "could", "also", "get", "use", "using", "im", "ive",
    "exampleu", "university", "edu", "email", "sent", "subject", "wrote", "com",
    "https", "http", "www", "let", "question", "request", "ticket", "help",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=0, help="number of clusters (0 = pick by silhouette)")
    ap.add_argument("--no-names", action="store_true", help="skip the Haiku naming step")
    args = ap.parse_args()

    settings = load_settings()
    store = CorpusStore(settings.corpus_db_path)
    articles = load_articles(settings.kb_issues_dir)
    rows = store.pairs("answered") + store.pairs("unanswered")
    docs = [_clean(f"{r['summary']} {r['summary']} {r['question']}") for r in rows]
    print(f"Tickets: {len(rows)} (answered + unanswered), KB articles: {len(articles)}")

    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS
    vec = TfidfVectorizer(stop_words=list(ENGLISH_STOP_WORDS.union(_EXTRA_STOP)),
                          ngram_range=(1, 2), min_df=3, max_df=0.5, sublinear_tf=True)
    X = vec.fit_transform(docs)
    terms = np.array(vec.get_feature_names_out())

    k = args.k or _pick_k(X)
    km = KMeans(n_clusters=k, n_init=10, random_state=7).fit(X)
    print(f"k = {k}")

    kb_docs = [_clean(f"{a.title} {a.title} {a.summary} {a.short_answer} {' '.join(a.steps)}") for a in articles]
    kb_X = vec.transform(kb_docs)
    sims = cosine_similarity(km.cluster_centers_, kb_X.toarray())

    names = store.people_names()
    recent = (date.today() - timedelta(days=90)).isoformat()
    clusters = []
    for c in range(k):
        idx = np.where(km.labels_ == c)[0]
        center = km.cluster_centers_[c]
        top = terms[np.argsort(center)[::-1][:10]].tolist()
        closeness = np.asarray(X[idx] @ center).ravel()
        members = [int(i) for i in idx[np.argsort(-closeness)]]
        best = int(np.argmax(sims[c]))
        created = sorted((rows[i]["created"] or "") for i in idx)
        clusters.append({
            "id": c,
            "size": int(len(idx)),
            "last_90_days": int(sum(1 for i in idx if (rows[i]["created"] or "") >= recent)),
            "first": created[0] if created else "",
            "last": created[-1] if created else "",
            "top_terms": top,
            "examples": [{"key": rows[i]["key"], "summary": rows[i]["summary"]} for i in members[:6]],
            "kb_slug": articles[best].slug,
            "kb_title": articles[best].title,
            "kb_similarity": round(float(sims[c][best]), 3),
            "gap": float(sims[c][best]) < GAP_THRESHOLD,
            "name": "", "description": "",
        })

    if not args.no_names:
        for cl in clusters:
            cl["name"], cl["description"] = _name_cluster(settings, cl, names)
            print(f"  cluster {cl['id']:>2} ({cl['size']:>3}): {cl['name']}")

    clusters.sort(key=lambda c: -c["size"])
    kb_hits = Counter(c["kb_slug"] for c in clusters if not c["gap"])
    uncovered = [a.slug for a in articles if a.slug not in kb_hits]

    settings.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out = settings.out_dir / f"clusters-{stamp}"
    out.with_suffix(".json").write_text(
        json.dumps({"k": k, "tickets": len(rows), "gap_threshold": GAP_THRESHOLD,
                    "clusters": clusters, "kb_articles_without_cluster": uncovered}, indent=2),
        encoding="utf-8")
    out.with_suffix(".html").write_text(_render(clusters, uncovered, len(rows), k), encoding="utf-8")
    gaps = [c for c in clusters if c["gap"]]
    print(f"\n{len(gaps)} of {k} clusters have no close KB article "
          f"({sum(c['size'] for c in gaps)} tickets).")
    print(f"KB articles no cluster maps to: {', '.join(uncovered) or 'none'}")
    print(f"Report: {out.with_suffix('.html').resolve()}")


def _clean(text: str) -> str:
    text = re.sub(r"\S+@\S+", " ", text or "")
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"\d+", " ", text).lower()


def _pick_k(X, lo: int = 12, hi: int = 30) -> int:
    best_k, best_s = lo, -1.0
    for k in range(lo, hi + 1, 2):
        labels = KMeans(n_clusters=k, n_init=5, random_state=7).fit_predict(X)
        s = silhouette_score(X, labels, metric="cosine", sample_size=min(600, X.shape[0]), random_state=7)
        if s > best_s:
            best_k, best_s = k, s
    return best_k


def _name_cluster(settings, cl: dict, names: list[str]) -> tuple[str, str]:
    digest = "Top terms: " + ", ".join(cl["top_terms"]) + "\nTicket subjects:\n" + "\n".join(
        f"- {e['summary']}" for e in cl["examples"])
    try:
        r = generate(
            settings,
            system=("You label clusters of IT help-desk tickets for Example University's help queue. "
                    "Reply with JSON only: {\"name\": \"<3-6 word category name>\", "
                    "\"description\": \"<one sentence: what these requesters want>\"}"),
            prompt=digest, max_tokens=300, scrubber=Scrubber(names),
        )
        m = re.search(r"\{.*\}", r.text, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        return str(data.get("name", "")).strip(), str(data.get("description", "")).strip()
    except Exception as exc:
        return "(naming failed)", str(exc)[:120]


def _render(clusters: list[dict], uncovered: list[str], n: int, k: int) -> str:
    rows = []
    for c in clusters:
        ex = "".join(f"<li>{html.escape(e['key'])}: {html.escape(e['summary'][:90])}</li>" for e in c["examples"][:4])
        status = ('<span class="gap">KB gap</span>' if c["gap"]
                  else f'{html.escape(c["kb_title"])} <span class="muted">({c["kb_similarity"]})</span>')
        rows.append(
            f"<tr><td><strong>{html.escape(c['name'] or 'Cluster ' + str(c['id']))}</strong>"
            f"<div class='muted'>{html.escape(c['description'])}</div></td>"
            f"<td class='num'>{c['size']}</td><td class='num'>{c['last_90_days']}</td>"
            f"<td>{status}</td><td class='muted'>{html.escape(', '.join(c['top_terms'][:6]))}</td>"
            f"<td><ul>{ex}</ul></td></tr>")
    unc = ", ".join(html.escape(s) for s in uncovered) or "none"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>HELP ticket clusters</title>
<style>
body {{ margin: 0; padding: 16px; font: 14px/20px -apple-system, "Segoe UI", Roboto, sans-serif; color: #172B4D; background: #F7F8F9; }}
h1 {{ font-size: 18px; }} .muted {{ color: #44546F; font-size: 12px; }}
.wrap {{ overflow-x: auto; background: #fff; border: 1px solid #DCDFE4; border-radius: 6px; }}
table {{ border-collapse: collapse; width: 100%; min-width: 900px; }}
th, td {{ text-align: left; vertical-align: top; padding: 8px 10px; border-bottom: 1px solid #DCDFE4; }}
th {{ font-size: 12px; text-transform: uppercase; color: #44546F; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.gap {{ background: #FFECEB; color: #AE2E24; font-weight: 700; padding: 0 6px; border-radius: 3px; }}
ul {{ margin: 0; padding-left: 16px; }}
</style></head><body>
<h1>HELP ticket clusters vs KB topics</h1>
<p class="muted">{n} tickets, {k} clusters (TF-IDF + k-means, local). Names from Haiku on scrubbed digests.
KB articles no cluster maps to: {unc}</p>
<div class="wrap"><table><thead><tr><th>Cluster</th><th>Tickets</th><th>Last 90d</th><th>Nearest KB article</th>
<th>Top terms</th><th>Examples</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</body></html>"""


if __name__ == "__main__":
    main()
