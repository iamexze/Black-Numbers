"""Web and research skills — search, fetch, summarise.

Uses only the standard library so it works on a bare clone. Search goes through
DuckDuckGo's HTML endpoint (no key); fetch pulls a page and strips it to
readable text. When the LLM brain is active it summarises the fetched text; the
offline brain just returns the top results, which is still useful.

Everything here is read-only from the user's side — it reads the web, it never
posts — so nothing needs confirmation. But it does reach the network, which the
transcript records on every call.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

from .base import Context, Result, Risk, Skill

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Black Number/0.1"


def _get(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    enc = "utf-8"
    ctype = r.headers.get("Content-Type", "")
    if "charset=" in ctype:
        enc = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    return raw.decode(enc, errors="replace")


def _strip(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _post(url: str, fields: dict, timeout: int = 12) -> str:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _unwrap_ddg(href: str) -> str:
    # DDG wraps results as /l/?uddg=<encoded real url>. Unwrap to the target.
    if "uddg=" in href:
        q = urllib.parse.urlparse(href).query
        for k, v in urllib.parse.parse_qsl(q):
            if k == "uddg":
                return v
    return href if href.startswith("http") else "https:" + href


def _web_search(a: dict, ctx: Context) -> Result:
    query = a.get("query", "").strip()
    if not query:
        return Result.fail("Search for what?")
    # DuckDuckGo's HTML endpoint answers to POST, not GET.
    try:
        page = _post("https://html.duckduckgo.com/html/", {"q": query})
    except Exception as e:
        return Result.fail(f"Search failed: {e}")
    results = []
    for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', page):
        href, title = _unwrap_ddg(m.group(1)), _strip(m.group(2))
        if title:
            results.append((title, href))
        if len(results) >= 6:
            break
    if not results:
        return Result.say(f"No clear results for {query}.")
    detail = "\n".join(f"{i+1}. {t}\n   {u}" for i, (t, u) in enumerate(results))
    return Result.say(
        f"Top result: {results[0][0]}.",
        detail=detail,
        data=[{"title": t, "url": u} for t, u in results],
    )


def _web_fetch(a: dict, ctx: Context) -> Result:
    url = a.get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return Result.fail("Give me a full http(s) URL.")
    try:
        text = _strip(_get(url))
    except Exception as e:
        return Result.fail(f"Couldn't fetch that: {e}")
    snippet = text[:6000]
    return Result.say(
        f"Fetched {len(text)} characters.",
        detail=snippet,
        data=snippet,
    )


def skills() -> list[Skill]:
    return [
        Skill(
            "web_search", "Search the web and return the top results.", _web_search,
            parameters={"query": {"type": "string", "required": True}}, risk=Risk.READ_ONLY,
        ),
        Skill(
            "web_fetch", "Fetch a web page and return its readable text.", _web_fetch,
            parameters={"url": {"type": "string", "required": True}}, risk=Risk.READ_ONLY,
        ),
    ]
