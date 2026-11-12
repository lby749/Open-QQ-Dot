"""Real-time web search and page reading for the group assistant's local model.

Providers (house.local.json group_bot.search):
- {"provider": "searxng", "base_url": "http://127.0.0.1:8888"}  a self-hosted SearXNG (fully local
  metasearch; enable the JSON format in its settings.yml).
- {"provider": "brave", "api_key": "..."}    Brave Search API.
- {"provider": "tavily", "api_key": "..."}   Tavily Search API.
- {"provider": "agent", "agent": "codex"}    ask an installed agent CLI that has its own sanctioned
  live search (Codex web_search="live") and return its results.
- {"provider": "off"}                        no search; the bot says it lacks current information.

Search engines' HTML pages are not scraped: they answer bots with human-verification challenges,
and those are not to be worked around.

fetch_url reads one public web page as plain text. It refuses non-HTTP(S) URLs, credentials in URLs,
and hosts that resolve to private, loopback or link-local addresses (also after every redirect), and
caps size and time. Everything returned is untrusted data for the model, never instructions.
"""
from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
from typing import Any, Callable

USER_AGENT = 'OpenQQDot-GroupBot/1.0 (+local assistant)'
MAX_RESULTS = 6
FETCH_MAX_BYTES = 1_500_000
FETCH_MAX_CHARS = 6000


class SearchError(RuntimeError):
    pass


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split('%')[0])
        if (address.is_private or address.is_loopback or address.is_link_local or address.is_multicast
                or address.is_reserved or address.is_unspecified):
            return False
    return bool(infos)


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname or ''
        if not _public_host(host):
            raise SearchError('redirect to a private or local address refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float, *, data: bytes | None = None, headers: dict | None = None,
          public_only: bool = False) -> tuple[bytes, str]:
    request = urllib.request.Request(url, data=data, headers={'User-Agent': USER_AGENT, **(headers or {})})
    opener = urllib.request.build_opener(_PublicRedirects) if public_only else urllib.request.build_opener()
    with opener.open(request, timeout=timeout) as response:
        return response.read(FETCH_MAX_BYTES + 1), response.headers.get_content_charset() or 'utf-8'


def _json(url: str, timeout: float, **kwargs) -> Any:
    body, charset = _open(url, timeout, **kwargs)
    return json.loads(body.decode(charset, errors='replace'))


def _strip_tags(value: str) -> str:
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', value))).strip()


def _result(title, url, snippet) -> dict[str, str]:
    return {'title': _strip_tags(str(title or ''))[:200], 'url': str(url or ''), 'snippet': _strip_tags(str(snippet or ''))[:400]}


def search_searxng(query: str, cfg: dict, timeout: float = 15) -> list[dict[str, str]]:
    base = (cfg.get('base_url') or 'http://127.0.0.1:8888').rstrip('/')
    data = _json(base + '/search?' + urllib.parse.urlencode({'q': query, 'format': 'json'}), timeout)
    return [_result(r.get('title'), r.get('url'), r.get('content')) for r in data.get('results', []) if r.get('url')]


def search_brave(query: str, cfg: dict, timeout: float = 15) -> list[dict[str, str]]:
    base = (cfg.get('base_url') or 'https://api.search.brave.com/res/v1/web/search').rstrip('/')
    data = _json(base + '?' + urllib.parse.urlencode({'q': query, 'count': MAX_RESULTS}), timeout,
                 headers={'Accept': 'application/json', 'X-Subscription-Token': cfg['api_key']})
    return [_result(r.get('title'), r.get('url'), r.get('description'))
            for r in (data.get('web') or {}).get('results', []) if r.get('url')]


def search_tavily(query: str, cfg: dict, timeout: float = 20) -> list[dict[str, str]]:
    base = (cfg.get('base_url') or 'https://api.tavily.com/search').rstrip('/')
    data = _json(base, timeout, data=json.dumps({'api_key': cfg['api_key'], 'query': query,
                                                 'max_results': MAX_RESULTS}).encode(),
                 headers={'Content-Type': 'application/json'})
    return [_result(r.get('title'), r.get('url'), r.get('content')) for r in data.get('results', []) if r.get('url')]


AGENT_SEARCH_PROMPT = (
    'Search the web now for: {query}\n'
    'Return ONLY a JSON array (no prose) of up to 5 results you actually found in this search, each '
    '{{"title": ..., "url": ..., "snippet": "the relevant fact, with its date if any"}}. '
    'If the search returns nothing, return [].')


def search_agent(query: str, cfg: dict, agent_fn: Callable[[str, str], str] | None) -> list[dict[str, str]]:
    if agent_fn is None:
        raise SearchError('agent search is not available here')
    text = agent_fn(cfg.get('agent') or 'codex', AGENT_SEARCH_PROMPT.format(query=query))
    match = re.search(r'\[[\s\S]*\]', text or '')
    try:
        items = json.loads(match.group(0)) if match else []
    except ValueError as exc:
        raise SearchError('agent search returned no result list') from exc
    return [_result(i.get('title'), i.get('url'), i.get('snippet')) for i in items
            if isinstance(i, dict) and str(i.get('url', '')).startswith(('http://', 'https://'))]


def search(query: str, cfg: dict[str, Any] | None = None,
           agent_fn: Callable[[str, str], str] | None = None) -> list[dict[str, str]]:
    cfg = cfg or {}
    provider = cfg.get('provider') or 'off'
    query = str(query or '').strip()[:300]
    if not query:
        raise SearchError('empty query')
    providers = {'searxng': search_searxng, 'brave': search_brave, 'tavily': search_tavily}
    try:
        if provider in providers:
            if provider in ('brave', 'tavily') and not cfg.get('api_key'):
                raise SearchError(f'{provider} search needs an api_key')
            return providers[provider](query, cfg)[:MAX_RESULTS]
        if provider == 'agent':
            return search_agent(query, cfg, agent_fn)[:MAX_RESULTS]
    except SearchError:
        raise
    except Exception as exc:
        raise SearchError(f'{provider} search failed: {str(exc)[:200]}') from exc
    raise SearchError('web search is off')


def fetch_url(url: str, timeout: float = 15) -> dict[str, str]:
    parts = urllib.parse.urlsplit(str(url or ''))
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise SearchError('only credential-free http(s) URLs can be read')
    if not _public_host(parts.hostname):
        raise SearchError('refusing to read a private or local address')
    try:
        body, charset = _open(url, timeout, public_only=True)
    except SearchError:
        raise
    except Exception as exc:
        raise SearchError(f'could not read {parts.hostname}: {str(exc)[:200]}') from exc
    page = body[:FETCH_MAX_BYTES].decode(charset, errors='replace')
    title = re.search(r'<title[^>]*>([\s\S]*?)</title>', page, re.I)
    page = re.sub(r'<(script|style|noscript|svg)[\s\S]*?</\1>', ' ', page, flags=re.I)
    return {'url': url, 'title': _strip_tags(title.group(1))[:200] if title else '',
            'text': _strip_tags(page)[:FETCH_MAX_CHARS]}
