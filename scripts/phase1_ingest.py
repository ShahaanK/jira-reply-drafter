"""Phase 1 ingest - pull all HELP tickets and build the FTS5 corpus.

Paginates through the full HELP project, extracts
(question, reply) pairs, buckets them, deduplicates near-identical replies,
and writes the result to the SQLite FTS5 corpus.

Staff are inferred from HELP history (extractor.infer_staff_ids) and
internal comments are never used as replies. Every display name seen is
stored in the people table so the PII scrubber can redact it.

Ends with a STOP report: counts by bucket, dedup stats, 3 scrubbed sample pairs.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the scripts/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from reply_drafter.config import load_settings
from reply_drafter.corpus import CorpusStore
from reply_drafter.extractor import ExtractedPair, collect_people, extract, infer_staff_ids
from reply_drafter.jira_client import JiraClient
from reply_drafter.scrub import Scrubber
TOKEN_OVERLAP_THRESHOLD = 0.90  # near-dupe threshold for cluster assignment


def token_overlap(a: str, b: str) -> float:
    """Simple token-level Jaccard similarity."""
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def main() -> None:
    settings = load_settings()
    db_path = settings.corpus_db_path
    store = CorpusStore(db_path)
    project = settings.jira_project

    print("=== Phase 1: Full HELP corpus ingest ===\n")

    with JiraClient(settings) as client:
        me = client.get_myself()
        print(f"Auth OK - {me.get('displayName')}\n")

        print(f"Paginating project {project}...")
        raw_issues: list[dict] = []
        for issue in client.search_issues(
            f"project = {project} ORDER BY key ASC",
            fields=["summary", "key", "status", "created", "description",
                    "comment", "reporter", "assignee"],
            page_size=50,
        ):
            raw_issues.append(issue)
            if len(raw_issues) % 50 == 0:
                print(f"  ...fetched {len(raw_issues)} tickets so far")

    print(f"\nFetched {len(raw_issues)} total tickets.\n")

    staff_ids = infer_staff_ids(raw_issues)
    people = collect_people(raw_issues)
    internal = sum(
        1 for i in raw_issues
        for c in ((i.get("fields") or {}).get("comment") or {}).get("comments") or []
        if c.get("jsdPublic") is False
    )
    print(f"Inferred HELP staff: {len(staff_ids)} | people seen: {len(people)} | "
          f"internal comments skipped: {internal}\n")

    # ── Extract pairs ─────────────────────────────────────────────────────────
    pairs: list[ExtractedPair] = []
    for issue in raw_issues:
        pair = extract(issue, staff_ids)
        pairs.append(pair)

    answered = [p for p in pairs if p.bucket == "answered"]
    unanswered = [p for p in pairs if p.bucket == "unanswered"]
    junk = [p for p in pairs if p.bucket == "junk"]

    # ── Dedup: exact (SHA-256) ─────────────────────────────────────────────────
    seen_hashes: set[str] = set()
    exact_dupes: list[str] = []
    deduped_answered: list[ExtractedPair] = []
    for p in answered:
        if p.reply_hash and p.reply_hash in seen_hashes:
            exact_dupes.append(p.key)
        else:
            if p.reply_hash:
                seen_hashes.add(p.reply_hash)
            deduped_answered.append(p)

    # ── Cluster assignment: union-find over near-dupe reply pairs ────────────
    # Each answered pair starts in its own cluster (its key).
    # Pairs with Jaccard(reply, reply) >= threshold are merged.
    # The cluster_id is the lexicographically smallest key in the cluster,
    # giving retrieval a stable representative to collapse on.
    parent: dict[str, str] = {p.key: p.key for p in deduped_answered}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # Smaller key becomes canonical representative
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    near_dupes: list[tuple[str, str, float]] = []
    replies = [(p.key, p.reply) for p in deduped_answered if p.reply]
    for i, (ki, ri) in enumerate(replies):
        for j, (kj, rj) in enumerate(replies):
            if j <= i:
                continue
            score = token_overlap(ri, rj)
            if score >= TOKEN_OVERLAP_THRESHOLD:
                near_dupes.append((ki, kj, score))
                union(ki, kj)

    # Assign cluster_id as the root of each node's union-find tree
    cluster_counts: dict[str, int] = {}
    for p in deduped_answered:
        cid = find(p.key)
        p.cluster_id = cid
        cluster_counts[cid] = cluster_counts.get(cid, 0) + 1

    multi_member_clusters = {cid: n for cid, n in cluster_counts.items() if n > 1}

    # ── Write to corpus ───────────────────────────────────────────────────────
    all_pairs = deduped_answered + unanswered + junk
    store.upsert_many(all_pairs)
    store.upsert_people(people)
    stats = store.stats()

    # ── STOP Report ───────────────────────────────────────────────────────────
    print("=== CHECKPOINT 1 REPORT ===\n")
    print(f"Total tickets pulled:        {len(raw_issues)}")
    print(f"Answered (usable pairs):     {len(answered)}")
    print(f"  Exact dupes removed:       {len(exact_dupes)}")
    print(f"  After dedup (indexed):     {len(deduped_answered)}")
    print(f"  Near-dupe pairs flagged:   {len(near_dupes)}")
    print(f"  Multi-member clusters:     {len(multi_member_clusters)}")
    print(f"  Distinct clusters (index): {len(cluster_counts)}")
    print(f"Unanswered:                  {len(unanswered)}")
    print(f"Junk:                        {len(junk)}")
    print(f"\nCorpus DB: {db_path}")
    print(f"DB stats: {stats}")

    if exact_dupes:
        print(f"\nExact-dupe keys removed: {exact_dupes}")

    if near_dupes:
        print("\nNear-dupe pairs (Jaccard >= 0.90):")
        for ka, kb, sc in near_dupes[:10]:
            print(f"  {ka} <-> {kb}  ({sc:.2f})")

    print("\n=== 3 SAMPLE EXTRACTED PAIRS (scrubbed) ===\n")
    scrubber = Scrubber(people.values())
    samples = [p for p in deduped_answered if len(p.question) > 50 and len(p.reply) > 50][:3]
    for i, p in enumerate(samples, 1):
        print(f"--- Sample {i}: {p.key} (source={p.source}) ---")
        print(f"QUESTION ({len(p.question)} chars):")
        print(scrubber.scrub(p.question[:400]))
        print(f"\nREPLY ({len(p.reply)} chars):")
        print(scrubber.scrub(p.reply[:400]))
        print()

    print("=== STOP - review before Phase 2 ===")


if __name__ == "__main__":
    main()
