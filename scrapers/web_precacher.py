"""Pre-cache public study material without sending private context to the cloud."""

import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _normalize_result_url(url: str) -> str:
    """Turn DuckDuckGo relative redirect URLs into absolute URLs."""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://duckduckgo.com" + url
    return url


async def pre_cache_web() -> None:
    """Find a study topic locally, then cache web research for that topic.

    The curated brain and the derived topic are private, so both local-model
    calls explicitly fail closed.  Public web text is only stored after it has
    been summarized locally.
    """
    base_dir = Path(__file__).resolve().parents[1]
    brain_file = base_dir / "curated_brain.md"
    if not brain_file.exists():
        logger.info("No curated brain found. Skipping web pre-caching.")
        return

    try:
        brain = await asyncio.to_thread(brain_file.read_text, encoding="utf-8")
        logger.info("Asking local model to identify research topics...")
        prompt = (
            "Based on the following curated brain of a student, identify ONE specific "
            "academic topic they are currently learning that would benefit from extra "
            "web research (e.g., 'Quadratic Formula', 'Cellular Respiration').\n"
            "Reply with ONLY the topic name. If there are no academic topics, reply "
            "with 'NONE'.\n\n"
            f"BRAIN:\n{brain}"
        )

        from llm_router import call_local_rpc

        topic = await asyncio.to_thread(
            call_local_rpc,
            prompt=prompt,
            max_tokens=50,
            timeout=120,
            classification="PRIVATE",
        )
        if not topic or any(
            phrase in topic.lower()[:50]
            for phrase in ("i cannot", "i'm sorry", "i don't know", "as an ai", "none", "⚠️")
        ):
            logger.info("No valid topics found or local inference failed.")
            return

        topic = re.sub(r"[^a-zA-Z0-9\s]", "", topic).strip()
        if not topic or len(topic) > 50:
            logger.info("No valid topics found to research.")
            return

        logger.info("Identified topic for pre-caching: %s", topic)
        search_url = (
            "https://html.duckduckgo.com/html/?q="
            f"{quote_plus(topic + ' explanation examples')}"
        )
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(search_url, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        urls_to_scrape = [
            _normalize_result_url(result["href"])
            for result in soup.find_all("a", class_="result__snippet", limit=3)
            if result.get("href")
        ]
        if not urls_to_scrape:
            logger.warning("No search results found.")
            return

        research_sections: list[str] = []
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            for url in urls_to_scrape:
                try:
                    page_response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    page_response.raise_for_status()
                    page_soup = BeautifulSoup(page_response.text, "html.parser")
                    page_text = " ".join(p.get_text(" ", strip=True) for p in page_soup.find_all("p"))
                    if not page_text:
                        continue

                    summary_prompt = (
                        "Summarize the following educational text. Extract key formulas, "
                        f"facts, and examples.\n\nTEXT:\n{page_text[:10000]}"
                    )
                    summary = await asyncio.to_thread(
                        call_local_rpc,
                        prompt=summary_prompt,
                        max_tokens=500,
                        timeout=120,
                        classification="PRIVATE",
                    )
                    if not summary or any(
                        phrase in summary.lower()[:50]
                        for phrase in ("i cannot", "i'm sorry", "i don't know", "as an ai", "⚠️")
                    ):
                        summary = "Summary unavailable."
                    research_sections.append(f"\n### Source: {url}\n{summary}\n")
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("Failed to scrape %s: %s", url, exc)

        if not research_sections:
            logger.warning("No web research could be cached for %s", topic)
            return

        db_dir = base_dir / "study_database"
        filename = f"{topic.replace(' ', '_').lower()}.md"
        output_file = db_dir / filename
        contents = f"# Pre-Cached Research: {topic}\n{''.join(research_sections)}"
        await asyncio.to_thread(db_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(output_file.write_text, contents, encoding="utf-8")
        logger.info("Successfully cached research for %s", topic)
    except (OSError, httpx.HTTPError, ValueError) as exc:
        logger.error("Web pre-cacher failed: %s", exc)


if __name__ == "__main__":
    asyncio.run(pre_cache_web())
