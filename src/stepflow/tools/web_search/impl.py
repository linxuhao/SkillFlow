"""Web search via SearXNG JSON API."""

import os
from urllib.parse import urlencode

import httpx

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")
SEARCH_TIMEOUT = int(os.getenv("SEARXNG_TIMEOUT", "10"))


def web_search(query: str, max_results: int = 5, language: str = "auto",
               *, workspace_root: str = "") -> dict:
    max_results = max(1, min(10, max_results))
    params = {"q": query, "format": "json", "categories": "general"}
    if language != "auto":
        params["language"] = language

    url = f"{SEARXNG_URL}/search?{urlencode(params)}"
    try:
        resp = httpx.get(url, timeout=SEARCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return {"error": f"Search timed out after {SEARCH_TIMEOUT}s",
                "query": query, "results": []}
    except Exception as e:
        return {"error": f"Search failed: {e}", "query": query, "results": []}

    results = []
    for r in data.get("results", [])[:max_results]:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        })
    return {"query": query, "results": results,
            "total": data.get("number_of_results", len(results))}
