"""Synchronous Jira Cloud REST client.

Endpoints used:
- POST /rest/api/3/search/jql  - paginate all issues in a project
- GET  /rest/api/3/issue/{key} - fetch one issue (description + comments)
- POST /rest/api/3/issue/{key}/comment - add a comment (Phase 3 only)

Design: sync httpx with tenacity retries. Respects 429 Retry-After.
Never logs the token.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from reply_drafter.config import Settings

log = logging.getLogger(__name__)

USER_AGENT = "jira-reply-drafter/0.1 (IT help desk)"


class JiraAuthError(Exception):
    pass


class JiraNotFoundError(Exception):
    pass


class JiraRateLimitError(Exception):
    pass


class JiraClient:
    """Thin sync wrapper around the Jira Cloud v3 REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.Client(
            auth=settings.basic_auth(),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_myself(self) -> dict[str, Any]:
        return self._get("/myself")

    def get_issue(self, key: str) -> dict[str, Any]:
        return self._get(f"/issue/{key}", params={
            "fields": "summary,description,comment,status,created,reporter,assignee",
        })

    def search_issues(
        self,
        jql: str,
        *,
        fields: list[str] | None = None,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Yield all issues matching the JQL query, handling cursor pagination."""
        body: dict[str, Any] = {
            "jql": jql,
            "maxResults": page_size,
            "fields": fields or ["summary", "key", "status", "created"],
        }
        while True:
            data = self._post("/search/jql", body)
            for issue in data.get("issues", []):
                yield issue
            next_token = data.get("nextPageToken")
            if not next_token:
                break
            body["nextPageToken"] = next_token

    def add_comment(
        self,
        key: str,
        body_adf: dict[str, Any],
        *,
        visibility: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"body": body_adf}
        if visibility:
            payload["visibility"] = visibility
        return self._post(f"/issue/{key}/comment", payload)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json=body)

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type(JiraRateLimitError),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._settings.jira_base_url}{path}"
        headers: dict[str, str] = {}
        if json is not None:
            headers["Content-Type"] = "application/json"

        log.debug("%s %s", method, path)
        resp = self._client.request(method, url, params=params, json=json,
                                    headers=headers)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            log.warning("429 from Jira; sleeping %.1fs", retry_after)
            time.sleep(retry_after)
            raise JiraRateLimitError(f"429 on {path}")

        if resp.status_code in (401, 403):
            raise JiraAuthError(f"{resp.status_code} on {path}")

        if resp.status_code == 404:
            raise JiraNotFoundError(f"404 on {path}")

        resp.raise_for_status()
        return resp.json()
