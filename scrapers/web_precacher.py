"""Opt-in, bounded public-web enrichment.

This module never sends the private curated brain, digests, chat history, or
scraped source text to cloud inference.  It derives a conservative topic
locally, fetches a small number of public HTTPS documents, and writes the
result into the private runtime study database.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from pathlib import Path
import re
import socket
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

import config


logger = logging.getLogger(__name__)
_USER_AGENT = "PersonalAssistantBot/1.0 (+local educational cache)"
_TOPIC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &'\-]{1,72}$")
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}


def _safe_topic(candidate: str) -> str | None:
    text = " ".join(str(candidate).strip().split())
    if not _TOPIC.fullmatch(text):
        return None
    return text


def _host_is_public(host: str) -> bool:
    host = host.rstrip(".").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return False
    return bool(addresses)


def _safe_https_url(value: str, *, allow_duckduckgo_redirect: bool = False) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    if not _host_is_public(parsed.hostname or ""):
        return None
    if allow_duckduckgo_redirect and parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return _safe_https_url(unquote(target)) if target else None
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def fetch_public_page(url: str, *, max_chars: int = 12_000, max_links: int = 16) -> tuple[str, str, list[str]]:
    """Fetch one public HTTPS page with SSRF-safe redirects and extract its text.

    This is intentionally synchronous for callers that already run in a worker
    thread (such as the browser-backed Canvas scraper).  It sends no cookies,
    credentials, Canvas text, or other private data to the destination.
    """
    current = url
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
    headers = {"User-Agent": _USER_AGENT}
    try:
        with httpx.Client(timeout=timeout, headers=headers, follow_redirects=False) as client:
            for _ in range(3):
                safe_url = _safe_https_url(current)
                if not safe_url:
                    return "", "", []
                response = client.get(safe_url)
                if response.is_redirect:
                    current = urljoin(safe_url, response.headers.get("location", ""))
                    continue
                if response.status_code != 200:
                    return "", "", []
                content_type = response.headers.get("content-type", "").lower()
                if "html" not in content_type or len(response.content) > config.MAX_WEB_FETCH_BYTES:
                    return "", "", []
                soup = BeautifulSoup(response.text, "html.parser")
                for node in soup(["script", "style", "nav", "footer", "noscript"]):
                    node.decompose()
                links: list[str] = []
                for anchor in soup.find_all("a", href=True):
                    candidate = _safe_https_url(urljoin(safe_url, str(anchor["href"])))
                    if candidate and candidate not in links:
                        links.append(candidate)
                    if len(links) >= max_links:
                        break
                text = " ".join(part.strip() for part in soup.stripped_strings)
                return safe_url, text[:max(0, max_chars)], links
    except (httpx.HTTPError, OSError, ValueError):
        return "", "", []
    return "", "", []


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    """Fetch one prevalidated public HTTPS URL with redirect revalidation."""
    current = url
    for _ in range(3):
        validated = _safe_https_url(current, allow_duckduckgo_redirect=True)
        if not validated:
            return ""
        response = await client.get(validated, follow_redirects=False)
        if response.is_redirect:
            location = response.headers.get("location", "")
            current = urljoin(validated, location)
            continue
        if response.status_code != 200:
            return ""
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type or len(response.content) > config.MAX_WEB_FETCH_BYTES:
            return ""
        soup = BeautifulSoup(response.text, "html.parser")
        for node in soup(["script", "style", "nav", "footer", "noscript"]):
            node.decompose()
        return " ".join(part.strip() for part in soup.stripped_strings)[:40_000]
    return ""


async def _public_search(client: httpx.AsyncClient, topic: str) -> list[str]:
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(f"{topic} explanation examples")
    response = await client.get(url, follow_redirects=False)
    if response.status_code != 200 or len(response.content) > config.MAX_WEB_FETCH_BYTES:
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[str] = []
    for anchor in soup.select("a.result__a, a.result__url"):
        href = anchor.get("href", "")
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin("https://duckduckgo.com", href)
        safe = _safe_https_url(href, allow_duckduckgo_redirect=True)
        if safe and safe not in results:
            results.append(safe)
        if len(results) >= config.MAX_WEB_SOURCES:
            break
    return results


async def pre_cache_web() -> bool:
    if not config.ENABLE_WEB_RESEARCH:
        logger.info("Web enrichment is disabled")
        return False
    try:
        brain = config.CURATED_BRAIN_FILE.read_text(encoding="utf-8", errors="replace")[-12_000:]
    except OSError:
        logger.info("No curated brain available for local topic extraction")
        return False
    if not brain.strip():
        return False

    from llm_router import call_local_rpc_result

    topic_result = await asyncio.to_thread(
        call_local_rpc_result,
        prompt=(
            "Extract one general academic topic from these private notes. "
            "Reply with the topic only; do not repeat names, dates, assignments, or private details.\n\n"
            + brain
        ),
        max_tokens=30,
        timeout=60,
        allow_cloud=False,
    )
    topic = _safe_topic(topic_result.text if topic_result.ok else "")
    if not topic:
        logger.info("Local topic extraction produced no safe public query")
        return False

    timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    async with httpx.AsyncClient(timeout=timeout, limits=limits, headers={"User-Agent": _USER_AGENT}) as client:
        try:
            urls = await _public_search(client, topic)
            texts = await asyncio.gather(*(_fetch_text(client, url) for url in urls))
        except (httpx.HTTPError, OSError) as exc:
            logger.info("Public web enrichment unavailable: %s", type(exc).__name__)
            return False
    entries = [(url, text) for url, text in zip(urls, texts) if text]
    if not entries:
        return False

    filename = re.sub(r"[^a-z0-9]+", "_", topic.lower()).strip("_")[:60] or "topic"
    output = config.STUDY_DATABASE_DIR / f"{filename}.md"
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = "# Public web reference: " + topic + "\n\n" + "\n\n".join(
        f"## Source\n{url}\n\n{text}" for url, text in entries
    )
    temporary = output.with_suffix(".tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(output)
    output.chmod(0o600)
    logger.info("Cached %d public reference(s) for %s", len(entries), topic)
    return True


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(pre_cache_web()) else 1)
