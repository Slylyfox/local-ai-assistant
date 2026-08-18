"""General-purpose web search, since the recon tools only inspect a specific
URL and have no way to discover anything.

Primary backend: Brave Search API, if a key is configured in Settings (free
tier: 2,000 queries/month, no card required — https://api.search.brave.com/).
Real API contract, not scraping, so it doesn't degrade under repeated use.

Fallback backend (no key needed): DuckDuckGo's HTML results page, scraped.
Verified working during development, but DuckDuckGo's anti-bot defenses can
and did kick in after a burst of automated requests on the same network,
serving a decoy homepage instead of results — this is a real, observed
failure mode, not theoretical. It's kept as a zero-setup option and the
result clearly suggests adding a Brave key when it comes up empty, rather
than silently pretending to be reliable."""

import re
import urllib.parse

import requests

from config import load_config
from .common import truncate

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
DDG_SEARCH_URL = "https://html.duckduckgo.com/html/"
SEARCH_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

_RESULT_RE = re.compile(
    r'<a rel="nofollow" class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(html_fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", html_fragment)).strip()


def _resolve_url(raw_href: str) -> str:
    parsed = urllib.parse.urlparse(raw_href)
    qs = urllib.parse.parse_qs(parsed.query)
    if "uddg" in qs:
        return urllib.parse.unquote(qs["uddg"][0])
    return raw_href


def _brave_search(query: str, num_results: int, api_key: str) -> str:
    try:
        resp = requests.get(
            BRAVE_SEARCH_URL,
            params={"q": query, "count": min(max(num_results, 1), 20)},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return f"Brave Search request failed: {exc}"

    results = (data.get("web") or {}).get("results", [])
    if not results:
        return f"No results found for '{query}'."

    lines = []
    for i, r in enumerate(results[:num_results]):
        title = r.get("title", "")
        url = r.get("url", "")
        desc = _clean(r.get("description", ""))
        lines.append(f"{i + 1}. {title}\n   {url}\n   {desc}")
    return truncate("\n\n".join(lines))


def _duckduckgo_search(query: str, num_results: int) -> str:
    try:
        resp = requests.get(
            DDG_SEARCH_URL,
            params={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException as exc:
        return f"Web search failed: {exc}"

    matches = _RESULT_RE.findall(html)
    if not matches:
        return (
            f"No results found for '{query}' — DuckDuckGo may be rate-limiting automated "
            "requests right now. Add a free Brave Search API key in Settings for reliable "
            "results (https://api.search.brave.com/, 2,000 queries/month free)."
        )

    lines = []
    for i, (href, title_html, snippet_html) in enumerate(matches[: max(1, num_results)]):
        title = _clean(title_html)
        snippet = _clean(snippet_html)
        url = _resolve_url(href)
        lines.append(f"{i + 1}. {title}\n   {url}\n   {snippet}")
    return truncate("\n\n".join(lines))


def web_search(query: str, num_results: int = 5) -> str:
    """Search the web and return titles, URLs, and snippets."""
    api_key = load_config().brave_api_key.strip()
    if api_key:
        return _brave_search(query, num_results, api_key)
    return _duckduckgo_search(query, num_results)


def register(registry):
    registry.register(
        "web_search",
        "Search the web and return a list of titles, URLs, and snippets for a query. "
        "Uses a Brave Search API key if configured in Settings, otherwise falls back to "
        "a free but rate-limit-prone DuckDuckGo search.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "num_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Defaults to 5.",
                },
            },
            "required": ["query"],
        },
        web_search,
        category="research",
    )
