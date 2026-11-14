"""Thin client for the You.com Search and Contents APIs."""
from __future__ import annotations

import time
from typing import Any

import requests

from .config import you_api_key

BASE = "https://ydc-index.io/v1"


class YouComError(RuntimeError):
    pass


class YouClient:
    def __init__(self, api_key: str | None = None, timeout: int = 30):
        self._session = requests.Session()
        self._session.headers.update(
            {"X-API-Key": api_key or you_api_key(), "Content-Type": "application/json"}
        )
        self.timeout = timeout
        self.calls = {"search": 0, "contents_pages": 0}

    def _post(self, path: str, body: dict[str, Any], retries: int = 3) -> Any:
        last = ""
        for attempt in range(retries):
            if attempt:
                time.sleep(2 ** attempt)
            try:
                resp = self._session.post(f"{BASE}{path}", json=body, timeout=self.timeout)
            except requests.RequestException as e:
                # Timeouts / dropped connections are retried like a 5xx. Only the exception
                # type is kept: request details (headers) must never reach a log.
                last = type(e).__name__
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                continue
            if resp.status_code != 200:
                raise YouComError(f"{path} -> HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                return resp.json()
            except ValueError:
                last = "invalid JSON body"
        raise YouComError(f"{path} failed after {retries} attempts ({last})")

    def search(
        self,
        query: str,
        *,
        count: int = 20,
        freshness: str | None = None,
        exclude_domains: list[str] | None = None,
        include_domains: list[str] | None = None,
        highlights: bool = False,
    ) -> list[dict[str, Any]]:
        """Return results.web (news articles are included there).

        With highlights=True, query-relevant passages come back in
        r["contents"]["highlights"] instead of r["snippets"]; count must be <= 50.
        """
        body: dict[str, Any] = {"query": query, "count": count}
        if freshness:
            body["freshness"] = freshness
        if include_domains:
            body["include_domains"] = include_domains
        elif exclude_domains:
            body["exclude_domains"] = exclude_domains
        if highlights:
            if count > 50:
                raise ValueError("highlights extraction supports count <= 50")
            body["extraction"] = {"extraction_mode": "highlights"}
        data = self._post("/search", body)
        self.calls["search"] += 1
        results = data.get("results") if isinstance(data, dict) else None
        return (results or {}).get("web") or []

    def contents(self, urls: list[str], formats: tuple[str, ...] = ("markdown",)) -> list[dict]:
        if not urls:
            return []
        data = self._post("/contents", {"urls": urls, "formats": list(formats), "crawl_timeout": 20})
        self.calls["contents_pages"] += len(urls)
        return data if isinstance(data, list) else []
