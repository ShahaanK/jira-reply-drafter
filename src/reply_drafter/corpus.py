"""SQLite FTS5 store for ticket (question, reply) pairs.

Schema: ticket_pairs holds all extracted pairs (answered/unanswered/junk).
ticket_pairs_fts indexes the question text for BM25 retrieval.

Adapted from an earlier internal tool IndexStore - different schema, same WAL + FTS5 pattern.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from reply_drafter.extractor import ExtractedPair

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_pairs (
    key              TEXT PRIMARY KEY,
    summary          TEXT,
    question         TEXT,
    reply            TEXT,
    reply_author     TEXT,
    reply_author_email TEXT,
    created          TEXT,
    status           TEXT,
    bucket           TEXT,
    reply_hash       TEXT,
    source           TEXT,
    cluster_id       TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS ticket_pairs_fts USING fts5(
    key       UNINDEXED,
    question,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS people (
    account_id   TEXT PRIMARY KEY,
    display_name TEXT
);
"""

# Recency boost: a ticket from today scores up to (1 + RECENCY_BOOST)x better
# than an old one, halving every RECENCY_HALF_LIFE_DAYS. Policies change, so
# recent answers should win ties.
RECENCY_BOOST = 0.5
RECENCY_HALF_LIFE_DAYS = 120


@dataclass
class RetrievalHit:
    key: str
    summary: str
    question: str
    reply: str
    reply_author: str
    score: float
    created: str = ""


class CorpusStore:
    """FTS5-backed store for ticket pairs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def upsert_many(self, pairs: list[ExtractedPair]) -> None:
        with self.connect() as conn:
            for pair in pairs:
                conn.execute(
                    """
                    INSERT INTO ticket_pairs
                        (key, summary, question, reply, reply_author, reply_author_email,
                         created, status, bucket, reply_hash, source, cluster_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        summary=excluded.summary, question=excluded.question,
                        reply=excluded.reply, reply_author=excluded.reply_author,
                        reply_author_email=excluded.reply_author_email,
                        created=excluded.created, status=excluded.status,
                        bucket=excluded.bucket, reply_hash=excluded.reply_hash,
                        source=excluded.source, cluster_id=excluded.cluster_id
                    """,
                    (pair.key, pair.summary, pair.question, pair.reply,
                     pair.reply_author, pair.reply_author_email,
                     pair.created, pair.status, pair.bucket,
                     pair.reply_hash, pair.source, pair.cluster_id),
                )
                conn.execute(
                    "DELETE FROM ticket_pairs_fts WHERE key = ?", (pair.key,)
                )
                if pair.bucket == "answered" and pair.question:
                    conn.execute(
                        "INSERT INTO ticket_pairs_fts (key, question) VALUES (?, ?)",
                        (pair.key, pair.question),
                    )

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        exclude_key: str | None = None,
        created_before: str | None = None,
    ) -> list[RetrievalHit]:
        """Cluster-aware BM25 search over question text.

        Fetches a wider candidate set (limit * 6), then collapses near-dupe
        clusters to one representative (the highest-scoring member), then
        returns the top `limit` across distinct clusters. This prevents a
        boilerplate family (e.g. 10 identical 'oldAssistant access granted' replies)
        from consuming most of the exemplar slots.
        """
        if not query.strip():
            return []
        fts_query = _escape_fts(query)
        params: list[object] = [fts_query]
        exclude_sql = ""
        if exclude_key:
            exclude_sql = "AND t.key != ?"
            params.append(exclude_key)
        if created_before:
            # Evaluation: only learn from tickets answered before the target ticket.
            exclude_sql += " AND t.created < ?"
            params.append(created_before)
        # Fetch a wide candidate set so cluster collapsing can pick the best
        # representative from each cluster before we hit the final limit.
        candidate_limit = limit * 6
        params.append(candidate_limit)

        sql = f"""
            SELECT t.key, t.summary, t.question, t.reply, t.reply_author,
                   t.cluster_id, t.created,
                   bm25(ticket_pairs_fts) AS score
            FROM ticket_pairs_fts
            JOIN ticket_pairs t ON t.key = ticket_pairs_fts.key
            WHERE ticket_pairs_fts MATCH ?
            {exclude_sql}
            ORDER BY score
            LIMIT ?
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        # bm25() is negative (lower is better), so a weight > 1 improves the score.
        today = date.today()
        rows = sorted(rows, key=lambda r: float(r["score"] or 0.0) * _recency_weight(r["created"], today))

        # Collapse clusters: keep only the best-scoring hit per cluster_id.
        # Singletons each have their own cluster_id (their key), so they pass through.
        seen_clusters: set[str] = set()
        deduped: list[RetrievalHit] = []
        for r in rows:
            cluster = r["cluster_id"] or r["key"]
            if cluster in seen_clusters:
                continue
            seen_clusters.add(cluster)
            deduped.append(RetrievalHit(
                key=r["key"], summary=r["summary"],
                question=r["question"], reply=r["reply"],
                reply_author=r["reply_author"],
                score=float(r["score"] or 0.0),
                created=r["created"] or "",
            ))
            if len(deduped) >= limit:
                break
        return deduped

    def upsert_people(self, people: dict[str, str]) -> None:
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO people (account_id, display_name) VALUES (?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET display_name=excluded.display_name",
                list(people.items()),
            )

    def people_names(self) -> list[str]:
        with self.connect() as conn:
            return [r[0] for r in conn.execute("SELECT display_name FROM people")]

    def pairs(self, bucket: str = "answered") -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM ticket_pairs WHERE bucket = ? ORDER BY created", (bucket,)
            ).fetchall()

    def stats(self) -> dict[str, object]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM ticket_pairs").fetchone()[0]
            by_bucket = conn.execute(
                "SELECT bucket, COUNT(*) as n FROM ticket_pairs GROUP BY bucket"
            ).fetchall()
            dedup_query = """
                SELECT COUNT(DISTINCT reply_hash) as unique_replies
                FROM ticket_pairs WHERE bucket='answered' AND reply_hash != ''
            """
            unique_replies = conn.execute(dedup_query).fetchone()[0]
        return {
            "total": total,
            "by_bucket": {r["bucket"]: r["n"] for r in by_bucket},
            "unique_replied": unique_replies,
            "db_path": str(self.db_path),
            "db_size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }


def _recency_weight(created: str | None, today: date) -> float:
    try:
        age_days = (today - date.fromisoformat((created or "")[:10])).days
    except ValueError:
        return 1.0
    return 1.0 + RECENCY_BOOST * 0.5 ** (max(age_days, 0) / RECENCY_HALF_LIFE_DAYS)


_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "i", "we", "you",
    "he", "she", "it", "they", "this", "that", "these", "those", "my",
    "your", "his", "her", "our", "their", "what", "which", "who", "when",
    "where", "how", "not", "no", "if", "so", "up", "out", "about", "into",
    "than", "then", "just", "also", "get", "any", "all", "am", "im",
    "hi", "hello", "dear", "thanks", "thank", "please", "me", "us",
    "its", "per", "via", "let", "see", "use", "now", "new", "well",
    "very", "too", "set", "one", "two", "way", "want", "need", "able",
    "like", "some", "more", "other", "good", "know", "think", "make",
    "here", "there", "having", "going", "being", "doing",
})

_MAX_QUERY_TERMS = 20


def _escape_fts(query: str) -> str:
    """Build an OR-joined FTS5 MATCH query from free text.

    Tokenizes to alphabetic words (3+ chars), drops stopwords, deduplicates,
    caps at _MAX_QUERY_TERMS, and OR-joins so BM25 ranks on partial overlap.
    The previous AND-join caused zero hits whenever any one token was absent
    from a candidate (routinely triggered by signature and boilerplate text).
    """
    tokens = re.findall(r"[a-zA-Z]{3,}", query)
    terms = list(dict.fromkeys(
        t.lower() for t in tokens if t.lower() not in _STOPWORDS
    ))[:_MAX_QUERY_TERMS]
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms)
