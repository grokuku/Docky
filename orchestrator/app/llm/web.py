"""WebClaw / Firecrawl integration (API /v1 compatible).

Extraite de ``app.llm.client``. Tous les symboles sont ré-exportés dans le
namespace ``app.llm.client`` (façade).
"""

import logging
from typing import Dict

import httpx

from app.config import load_settings
from app.llm.constants import _DEFAULT_WEB_ENDPOINT

logger = logging.getLogger(__name__)


def _get_web_endpoint():
    """Get the web extraction endpoint and API key from settings.

    Returns a tuple ``(endpoint, api_key)``.
    If ``endpoint`` is empty in settings, the default Firecrawl cloud URL
    is returned.  If ``api_key`` is empty, no ``Authorization`` header is
    needed (e.g. self-hosted WebClaw).
    """
    settings = load_settings()
    fc_settings = settings.get("firecrawl", {}) or {}
    api_key = fc_settings.get("api_key", "")
    endpoint = fc_settings.get("endpoint", "") or _DEFAULT_WEB_ENDPOINT
    return endpoint.rstrip("/"), api_key


def _firecrawl_headers(api_key: str) -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def firecrawl_search(query: str, limit: int = 5) -> str:
    """Search the web using Firecrawl/WebClaw API.

    Returns a text summary of the results.
    """
    endpoint, api_key = _get_web_endpoint()

    url = f"{endpoint}/search"
    body = {"query": query, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.post(url, json=body, headers=_firecrawl_headers(api_key))
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Firecrawl/WebClaw search HTTP %s: %s", exc.response.status_code, exc.response.text)
        return f"[error] Firecrawl/WebClaw search HTTP {exc.response.status_code}: {exc.response.text}"
    except httpx.RequestError as exc:
        logger.warning("Firecrawl/WebClaw search request error: %s", exc)
        return f"[error] Firecrawl/WebClaw search request error: {exc}"

    results = data.get("data") or data.get("results") or []
    if not results:
        return "Aucun résultat trouvé."

    lines = []
    for i, item in enumerate(results, 1):
        title = item.get("title") or item.get("metadata", {}).get("title", "")
        link = item.get("url") or item.get("link") or ""
        snippet = item.get("content") or item.get("snippet") or item.get("description", "")
        if snippet and len(snippet) > 500:
            snippet = snippet[:500] + "…"
        lines.append(f"{i}. {title}\n   URL: {link}\n   {snippet}")
    return "\n".join(lines)


async def firecrawl_scrape(url: str) -> str:
    """Scrape a URL using the Firecrawl/WebClaw API.

    Returns the page content as text.
    """
    endpoint, api_key = _get_web_endpoint()

    api_url = f"{endpoint}/scrape"
    body = {"url": url}
    try:
        async with httpx.AsyncClient(timeout=90.0) as http:
            resp = await http.post(api_url, json=body, headers=_firecrawl_headers(api_key))
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Firecrawl/WebClaw scrape HTTP %s: %s", exc.response.status_code, exc.response.text)
        return f"[error] Firecrawl/WebClaw scrape HTTP {exc.response.status_code}: {exc.response.text}"
    except httpx.RequestError as exc:
        logger.warning("Firecrawl/WebClaw scrape request error: %s", exc)
        return f"[error] Firecrawl/WebClaw scrape request error: {exc}"

    page_data = data.get("data") or data
    content = (
        page_data.get("markdown")
        or page_data.get("content")
        or page_data.get("html")
        or ""
    )
    if not content:
        return "Page scrapeée mais aucun contenu extrait."
    # Truncate very large pages
    if len(content) > 8000:
        content = content[:8000] + "\n\n… [contenu tronqué]"
    return content


async def firecrawl_map(url: str) -> str:
    """Map URLs on a site using the Firecrawl/WebClaw API.

    Returns a list of URLs as text.
    """
    endpoint, api_key = _get_web_endpoint()

    api_url = f"{endpoint}/map"
    body = {"url": url}
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.post(api_url, json=body, headers=_firecrawl_headers(api_key))
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Firecrawl/WebClaw map HTTP %s: %s", exc.response.status_code, exc.response.text)
        return f"[error] Firecrawl/WebClaw map HTTP {exc.response.status_code}: {exc.response.text}"
    except httpx.RequestError as exc:
        logger.warning("Firecrawl/WebClaw map request error: %s", exc)
        return f"[error] Firecrawl/WebClaw map request error: {exc}"

    links = data.get("data") or data.get("links") or []
    if not links:
        return "Aucune URL trouvée."

    # links may be list of strings or list of dicts with 'url'
    url_list = []
    for item in links:
        if isinstance(item, str):
            url_list.append(item)
        elif isinstance(item, dict):
            u = item.get("url") or item.get("link", "")
            if u:
                url_list.append(u)

    if not url_list:
        return "Aucune URL trouvée."

    # Limit output
    if len(url_list) > 100:
        url_list = url_list[:100]
        return "\n".join(url_list) + f"\n\n… ({len(links)} URLs au total, 100 affichées)"
    return "\n".join(url_list)
