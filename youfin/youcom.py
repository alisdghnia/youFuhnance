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
        for attempt in range(retries):
            resp = self._session.post(f"{BASE}{path}", json=body, timeout=self.timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                raise YouComError(f"{path} -> HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise YouComError(f"{path} failed after {retries} retries")

    def search(
        self,
        query: str,
        *,
        count: int = 20,
        freshness: str | None = None,
        exclude_domains: list[str] | None = None,
        include_domains: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return results.web (news articles are included there)."""
        body: dict[str, Any] = {"query": query, "count": count}
        if freshness:
            body["freshness"] = freshness
        if include_domains:
            body["include_domains"] = include_domains
        elif exclude_domains:
            body["exclude_domains"] = exclude_domains
        data = self._post("/search", body)
        self.calls["search"] += 1
        return data.get("results", {}).get("web", []) or []

    def contents(self, urls: list[str], formats: tuple[str, ...] = ("markdown",)) -> list[dict]:
        if not urls:
            return []
        data = self._post("/contents", {"urls": urls, "formats": list(formats), "crawl_timeout": 20})
        self.calls["contents_pages"] += len(urls)
        return data if isinstance(data, list) else []
