from __future__ import annotations
import asyncio
import logging

from dataclasses import dataclass
from pathlib import Path
import base64
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from bs4 import BeautifulSoup
import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.core.config import get_settings
from app.core.stream import screencast_stream
from app.models.schemas import BrowserAction, BrowserActionType, EvidenceItem, EvidenceType, ImageItem
from app.safety.guardrails import safety_guard
from app.utils.text import keyword_overlap_score

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass
class PageCapture:
    url: str
    title: str
    html: str
    text: str
    screenshot_path: str | None
    metadata: dict


class BrowserWorker:
    def __init__(self, task_id: str = "") -> None:
        self.settings = get_settings()
        self.task_id = task_id
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._cdp_sessions = []

    async def _attach_screencast(self, page: Page):
        if not self.task_id:
            return
        try:
            page_id = str(id(page))
            cdp = await page.context.new_cdp_session(page)
            self._cdp_sessions.append(cdp)
            await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60, "everyNthFrame": 1})
            
            def handle_screencast(event):
                session_id = event.get("sessionId")
                data = event.get("data")
                asyncio.create_task(cdp.send("Page.screencastFrameAck", {"sessionId": session_id}))
                asyncio.create_task(screencast_stream.broadcast(self.task_id, page_id, f"data:image/jpeg;base64,{data}"))
                
            cdp.on("Page.screencastFrame", handle_screencast)
        except Exception as e:
            print(f"Failed to attach screencast: {e}")

    async def __aenter__(self) -> "BrowserWorker":
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.settings.browser_headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                ],
            )
            self.context = await self.browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                },
            )
            self.page = await self.context.new_page()
            await self._attach_screencast(self.page)
            # Mask Playwright fingerprint
            await self.page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            self.page.set_default_timeout(self.settings.browser_timeout_ms)
            return self
        except Exception as exc:
            await self._safe_shutdown()
            message = str(exc)
            if "NotImplementedError: Browser initialization failed" in message:
                raise RuntimeError(
                    "Browser worker startup failed: Playwright could not initialize the browser in this "
                    "runtime. On Windows, start the API without hot reload using "
                    "`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir .`."
                ) from exc
            if "Executable doesn't exist" in message or "browserType.launch" in message:
                raise RuntimeError(
                    "Chromium could not be launched by Playwright. Run "
                    "`python -m playwright install chromium` inside the active virtual environment "
                    "and restart the server."
                ) from exc
            raise RuntimeError(f"Browser worker startup failed: {message}") from exc

    async def __aexit__(self, *_: object) -> None:
        await self._safe_shutdown()

    async def web_search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """Multi-engine search: official API first, httpx scraping second, Playwright last."""
        results: list[SearchResult] = []
        seen: set[str] = set()

        def _add(items: list[SearchResult]) -> None:
            for r in items:
                if r.url not in seen:
                    seen.add(r.url)
                    results.append(r)

        # --- Layer 0: Bing Web Search API (best quality, needs API key) ---
        try:
            _add(await self._bing_api_search(query, limit * 2))
        except Exception:
            pass

        # --- Layer 1: Startpage (privacy proxy to Google, no CAPTCHA) ---
        if len(results) < limit:
            try:
                _add(await self._startpage_search(query, limit * 2))
            except Exception:
                pass

        # --- Layer 2: Mojeek (independent search engine, no CAPTCHA) ---
        if len(results) < limit:
            try:
                _add(await self._mojeek_search(query, limit * 2))
            except Exception:
                pass

        # --- Layer 3: Research APIs (Wikipedia, arXiv, Semantic Scholar) ---
        if len(results) < limit:
            try:
                _add(await self._research_source_search(query, limit))
            except Exception:
                pass

        # --- Layer 4: Playwright DuckDuckGo (only if API methods failed) ---
        if len(results) < limit:
            try:
                _add(await self._playwright_ddg_search(query, limit * 2))
            except Exception:
                pass

        # --- Layer 5: Playwright Bing (last resort) ---
        if len(results) < limit:
            try:
                _add(await self._playwright_bing_search(query, limit * 2))
            except Exception:
                pass

        # --- Layer 6: DuckDuckGo Instant Answers API ---
        if len(results) < 3:
            try:
                _add(await self._api_search_fallback(query, limit * 2))
            except Exception:
                pass

        relevant = _filter_results_by_query(query, results)
        return relevant[:limit]

    async def _bing_api_search(self, query: str, limit: int) -> list[SearchResult]:
        """Bing Web Search API v7 — official, structured JSON, never blocked.
        
        Requires BING_API_KEY in .env. If not configured, returns empty (skipped).
        Free tier: 1,000 calls/month.
        Get a key at: https://www.microsoft.com/en-us/bing/apis/bing-web-search-api
        """
        from app.core.config import get_settings
        settings = get_settings()
        if not settings.bing_api_key:
            return []  # No API key configured — silently skip to next layer

        results: list[SearchResult] = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.bing.microsoft.com/v7.0/search",
                params={
                    "q": query,
                    "count": str(min(limit, 50)),
                    "mkt": "en-US",
                    "responseFilter": "Webpages",
                    "textFormat": "Raw",
                },
                headers={
                    "Ocp-Apim-Subscription-Key": settings.bing_api_key,
                },
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            for item in data.get("webPages", {}).get("value", []):
                title = item.get("name", "")
                url = item.get("url", "")
                snippet = item.get("snippet", "")
                if title and url:
                    results.append(SearchResult(title=title, url=url, snippet=snippet))
                if len(results) >= limit:
                    break
        return results

    async def image_search(self, query: str, limit: int = 10) -> list[ImageItem]:
        """Performs a dedicated image search."""
        from app.core.config import get_settings
        settings = get_settings()
        
        if not self.context:
            return []
            
        if settings.bing_api_key:
            return await self._bing_api_image_search(query, limit)
        else:
            return await self._duckduckgo_image_search(query, limit)

    async def _bing_api_image_search(self, query: str, limit: int) -> list[ImageItem]:
        """Bing Image Search API v7."""
        from app.core.config import get_settings
        settings = get_settings()
        results: list[ImageItem] = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.bing.microsoft.com/v7.0/images/search",
                params={"q": query, "count": str(min(limit, 50)), "mkt": "en-US"},
                headers={"Ocp-Apim-Subscription-Key": settings.bing_api_key},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            for item in data.get("value", []):
                results.append(ImageItem(
                    task_id=self.task_id,
                    src=item.get("contentUrl", ""),
                    alt=item.get("name", ""),
                    source_url=item.get("hostPageUrl", ""),
                    source_title=item.get("hostPageDisplayUrl", ""),
                    relevance_score=1.0 # API results are generally relevant
                ))
                if len(results) >= limit:
                    break
        return results

    async def _duckduckgo_image_search(self, query: str, limit: int) -> list[ImageItem]:
        """Scrapes DuckDuckGo Images using Playwright."""
        results: list[ImageItem] = []
        page = None
        try:
            page = await self.context.new_page()
            url = f"https://duckduckgo.com/?q={query.replace(' ', '+')}&iax=images&ia=images"
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_selector(".tile--img", timeout=10000)
            
            tiles = await page.query_selector_all(".tile--img")
            for tile in tiles[:limit]:
                img = await tile.query_selector("img.tile--img__img")
                if img:
                    src = await img.get_attribute("src")
                    alt = await img.get_attribute("alt") or ""
                    # DDG uses proxy URLs for images often, but we can try to get the original if needed.
                    # For now, let's take what we have.
                    if src:
                        if src.startswith("//"): src = "https:" + src
                        results.append(ImageItem(
                            task_id=self.task_id,
                            src=src,
                            alt=alt,
                            source_url=url,
                            source_title="DuckDuckGo Images",
                            relevance_score=0.9
                        ))
        except Exception as e:
            logger.error(f"DuckDuckGo image search failed: {e}")
        finally:
            if page:
                await page.close()
        return results

    async def _startpage_search(self, query: str, limit: int) -> list[SearchResult]:
        """Startpage search via httpx POST — privacy proxy to Google, no CAPTCHA."""
        results: list[SearchResult] = []
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            resp = await client.post(
                "https://www.startpage.com/sp/search",
                data={"query": query, "cat": "web", "language": "english"},
            )
            if resp.status_code != 200:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.select(".w-gl__result"):
                link = item.select_one("a.w-gl__result-title")
                snippet_el = item.select_one(".w-gl__description")
                if not link:
                    continue
                href = str(link.get("href") or "")
                if not href.startswith("http"):
                    continue
                title = link.get_text(" ", strip=True)
                snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
                results.append(SearchResult(title=title, url=href, snippet=snippet))
                if len(results) >= limit:
                    break
        return results

    async def _mojeek_search(self, query: str, limit: int) -> list[SearchResult]:
        """Mojeek independent search engine via httpx — no CAPTCHA."""
        results: list[SearchResult] = []
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(
                "https://www.mojeek.com/search",
                params={"q": query},
            )
            if resp.status_code != 200:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.select("ul.results-standard li"):
                link = item.select_one("a.ob")
                snippet_el = item.select_one("p.s")
                if not link:
                    continue
                href = str(link.get("href") or "")
                if not href.startswith("http"):
                    continue
                title = link.get_text(" ", strip=True)
                snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
                results.append(SearchResult(title=title, url=href, snippet=snippet))
                if len(results) >= limit:
                    break
        return results




    async def _playwright_ddg_search(self, query: str, limit: int) -> list[SearchResult]:
        """DuckDuckGo search via Playwright stealth browser (fallback)."""
        search_page = await self.context.new_page()
        await self._attach_screencast(search_page)
        await search_page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            await search_page.goto(
                f"https://html.duckduckgo.com/html/?q={quote_plus(query)}&kl=us-en",
                wait_until="domcontentloaded",
            )
            await search_page.wait_for_timeout(1500)
            body_text = (await search_page.locator("body").inner_text()).lower()
            if "bots use duckduckgo" in body_text or "captcha" in body_text or "verify" in body_text:
                return []
            soup = BeautifulSoup(await search_page.content(), "html.parser")
        finally:
            await search_page.close()
        results: list[SearchResult] = []
        for node in soup.select(".result"):
            link_node = node.select_one(".result__title a")
            if not link_node:
                continue
            title = link_node.get_text(" ", strip=True)
            href = str(link_node.get("href") or "")
            snippet_node = node.select_one(".result__snippet")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            resolved = _resolve_result_url(href)
            if resolved and title:
                results.append(SearchResult(title=title, url=resolved, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    async def _playwright_bing_search(self, query: str, limit: int) -> list[SearchResult]:
        """Bing search via Playwright stealth browser."""
        search_page = await self.context.new_page()
        await self._attach_screencast(search_page)
        await search_page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            await search_page.goto(
                f"https://www.bing.com/search?q={quote_plus(query)}&cc=us&setlang=en",
                wait_until="domcontentloaded",
            )
            await search_page.wait_for_timeout(1500)
            body_text = (await search_page.locator("body").inner_text()).lower()
            if "captcha" in body_text or "verify" in body_text or "solve the challenge" in body_text:
                return []
            soup = BeautifulSoup(await search_page.content(), "html.parser")
        finally:
            await search_page.close()
        results: list[SearchResult] = []
        for node in soup.select("li.b_algo"):
            link_node = node.select_one("h2 a")
            if not link_node:
                continue
            title = link_node.get_text(" ", strip=True)
            href = str(link_node.get("href") or "")
            snippet_node = node.select_one(".b_caption p")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            resolved = _resolve_result_url(href)
            if resolved and title:
                results.append(SearchResult(title=title, url=resolved, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    async def _research_source_search(self, query: str, limit: int) -> list[SearchResult]:
        """Search research-grade sources: arXiv, Semantic Scholar, and Wikipedia."""
        results: list[SearchResult] = []
        headers = {
            "User-Agent": "ResearchAgent/1.0 (bot@example.com)",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            # Wikipedia API
            try:
                wiki_resp = await client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": query,
                        "utf8": "",
                        "format": "json",
                        "srlimit": limit
                    }
                )
                if wiki_resp.status_code == 200:
                    for item in wiki_resp.json().get("query", {}).get("search", []):
                        title = item.get("title", "")
                        snippet = BeautifulSoup(item.get("snippet", ""), "html.parser").get_text()
                        if title:
                            url = f"https://en.wikipedia.org/wiki/{quote_plus(title.replace(' ', '_'))}"
                            results.append(SearchResult(title=f"[Wikipedia] {title}", url=url, snippet=snippet))
            except Exception:
                pass

            # arXiv API
            try:
                arxiv_resp = await client.get(
                    "https://export.arxiv.org/api/query",
                    params={"search_query": f"all:{query}", "max_results": str(limit), "sortBy": "relevance"},
                )
                if arxiv_resp.status_code == 200:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(arxiv_resp.text)
                    ns = {"atom": "http://www.w3.org/2005/Atom"}
                    for entry in root.findall("atom:entry", ns)[:limit]:
                        title_el = entry.find("atom:title", ns)
                        summary_el = entry.find("atom:summary", ns)
                        id_el = entry.find("atom:id", ns)
                        title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
                        summary = (summary_el.text or "").strip()[:300] if summary_el is not None else ""
                        url = (id_el.text or "").strip() if id_el is not None else ""
                        if title and url:
                            results.append(SearchResult(title=f"[arXiv] {title}", url=url, snippet=summary))
            except Exception:
                pass

            # Semantic Scholar API
            try:
                ss_resp = await client.get(
                    "https://api.semanticscholar.org/graph/v1/paper/search",
                    params={"query": query, "limit": str(limit), "fields": "title,url,abstract"},
                    headers={"User-Agent": "ResearchAgent/1.0"},
                )
                if ss_resp.status_code == 200:
                    for paper in ss_resp.json().get("data", [])[:limit]:
                        title = str(paper.get("title") or "")
                        url = str(paper.get("url") or "")
                        abstract = str(paper.get("abstract") or "")[:300]
                        if title and url:
                            results.append(SearchResult(title=f"[Paper] {title}", url=url, snippet=abstract))
            except Exception:
                pass

        return results[:limit]

    async def youtube_search(self, query: str, limit: int = 5) -> list[SearchResult]:
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2500)
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.35)")
        await self.page.wait_for_timeout(1000)
        anchors = await self.page.query_selector_all("a#video-title")
        results = []
        for anchor in anchors[: limit * 2]:
            href = await anchor.get_attribute("href")
            title = ((await anchor.get_attribute("title")) or (await anchor.inner_text()) or "").strip()
            if href and "/watch" in href and title:
                results.append(
                    SearchResult(
                        title=title,
                        url=f"https://www.youtube.com{href}",
                        snippet="YouTube search result",
                    )
                )
            if len(results) >= limit:
                break
        return results

    # ── Page Usability Detection ────────────────────────────────────────

    _BLOCK_MARKERS = [
        "access denied", "403 forbidden", "401 unauthorized",
        "captcha", "verify you are human", "verify you are not a robot",
        "please complete the security check", "robot or human",
        "enable javascript", "enable cookies", "js required",
        "sign in to continue", "log in to continue", "login required",
        "create an account", "subscribe to read", "subscribe to continue",
        "this page isn't available", "page not found", "404 not found",
        "sorry, you have been blocked", "unusual traffic", "bot detection",
        "your connection is not private", "ssl_error", "protocol error",
        "the page you requested cannot be displayed",
        "we can't connect to the server", "redirecting...", "just a moment",
        "cloudflare", "ddos protection", "checking your browser",
    ]

    def _detect_page_issues(self, text: str, html: str, title: str) -> str | None:
        """Return a short reason if the page is unusable, else None."""
        lowered = text.lower()
        title_low = title.lower()

        # Check for block markers in text or title
        for marker in self._BLOCK_MARKERS:
            if marker in lowered or marker in title_low:
                return f"blocked:{marker[:40]}"

        # Very short / empty pages (often means JS failed or access denied)
        stripped = text.strip()
        words = stripped.split()
        if len(words) < 50:
            return "too_short_minimal_content"

        # Login / paywall gate (dominant content is auth UI)
        auth_signals = sum(1 for kw in ["password", "username", "sign in", "log in", "sign up", "forgot password", "email"]
                          if kw in lowered)
        if auth_signals >= 4:
            return "login_wall_detected"

        return None

    async def capture_page(self, url: str, screenshot_file: Path | None = None) -> PageCapture:
        if not safety_guard.is_safe_url(url):
            raise ValueError(safety_guard.reason_for_block(url))
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(1500)
        try:
            await self.page.evaluate("window.scrollTo(0, 600)")
        except Exception:
            pass
        await self.page.wait_for_timeout(500)
        html = await self.page.content()
        title = await self.page.title()
        text = await self.page.locator("body").inner_text()
        screenshot_path = None
        if screenshot_file:
            screenshot_file.parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(screenshot_file), full_page=False)
            screenshot_path = str(screenshot_file)
        soup = BeautifulSoup(html, "html.parser")
        metadata = {
            "description": _get_meta_content(soup, "description"),
            "author": _get_meta_content(soup, "author"),
            "published_time": _get_meta_content(soup, "article:published_time"),
        }
        issue = self._detect_page_issues(text, html, title)
        metadata["_page_issue"] = issue
        return PageCapture(
            url=url, title=title, html=html, text=text,
            screenshot_path=screenshot_path, metadata=metadata,
        )

    async def capture_page_safe(
        self,
        url: str,
        screenshot_file: Path | None = None,
        *,
        blocked_domains: set[str] | None = None,
    ) -> PageCapture | None:
        """Attempt to capture a page with access recovery and UA rotation."""
        domain = urlparse(url).netloc.replace("www.", "")
        if blocked_domains and domain in blocked_domains:
            return None

        # --- Attempt 1: Default Context ---
        try:
            page = await self.capture_page(url, screenshot_file)
            if not page.metadata.get("_page_issue"):
                return page
        except Exception:
            pass

        # --- Attempt 2: Fresh Page + Mobile User-Agent ---
        retry_page_obj = None
        try:
            retry_page_obj = await self.context.new_page()
            await retry_page_obj.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            # Use a realistic mobile UA
            mobile_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1"
            await retry_page_obj.set_extra_http_headers({"User-Agent": mobile_ua})
            
            await retry_page_obj.goto(url, wait_until="networkidle", timeout=25000)
            await retry_page_obj.wait_for_timeout(3000)
            
            html2 = await retry_page_obj.content()
            title2 = await retry_page_obj.title()
            text2 = await retry_page_obj.locator("body").inner_text()
            
            issue2 = self._detect_page_issues(text2, html2, title2)
            if not issue2:
                soup2 = BeautifulSoup(html2, "html.parser")
                metadata2 = {
                    "description": _get_meta_content(soup2, "description"),
                    "author": _get_meta_content(soup2, "author"),
                    "published_time": _get_meta_content(soup2, "article:published_time"),
                    "_page_issue": None,
                }
                ss_path = None
                if screenshot_file:
                    screenshot_file.parent.mkdir(parents=True, exist_ok=True)
                    await retry_page_obj.screenshot(path=str(screenshot_file), full_page=False)
                    ss_path = str(screenshot_file)
                return PageCapture(
                    url=url, title=title2, html=html2, text=text2,
                    screenshot_path=ss_path, metadata=metadata2,
                )
        except Exception:
            pass
        finally:
            if retry_page_obj:
                try:
                    await retry_page_obj.close()
                except Exception:
                    pass

        if blocked_domains is not None:
            blocked_domains.add(domain)
        return None

    async def capture_page_on_new_tab(
        self,
        url: str,
        screenshot_file: Path | None = None,
        *,
        blocked_domains: set[str] | None = None,
    ) -> PageCapture | None:
        """Capture a page on a dedicated new browser tab — safe for parallel execution.

        Opens a fresh tab, loads the URL, extracts content, and closes the tab when done.
        Returns None if the page is blocked, too short, or errored. Does NOT fall back to a
        mobile UA (keeps things fast); the orchestrator can retry via the serial fallback path
        if needed.
        """
        domain = urlparse(url).netloc.replace("www.", "")
        if blocked_domains and domain in blocked_domains:
            return None
        if not safety_guard.is_safe_url(url):
            return None

        new_page = None
        try:
            new_page = await self.context.new_page()
            await self._attach_screencast(new_page)
            await new_page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            new_page.set_default_timeout(self.settings.browser_timeout_ms)

            await new_page.goto(url, wait_until="domcontentloaded")
            await new_page.wait_for_timeout(1500)
            try:
                await new_page.evaluate("window.scrollTo(0, 600)")
            except Exception:
                pass
            await new_page.wait_for_timeout(500)

            html = await new_page.content()
            title = await new_page.title()
            text = await new_page.locator("body").inner_text()

            # Reject blocked / empty pages immediately (no retry in parallel path)
            issue = self._detect_page_issues(text, html, title)
            if issue or len(text.strip()) < 200:
                return None

            screenshot_path = None
            if screenshot_file:
                screenshot_file.parent.mkdir(parents=True, exist_ok=True)
                await new_page.screenshot(path=str(screenshot_file), full_page=False)
                screenshot_path = str(screenshot_file)

            soup = BeautifulSoup(html, "html.parser")
            metadata = {
                "description": _get_meta_content(soup, "description"),
                "author": _get_meta_content(soup, "author"),
                "published_time": _get_meta_content(soup, "article:published_time"),
                "_page_issue": None,
            }
            return PageCapture(
                url=url, title=title, html=html, text=text,
                screenshot_path=screenshot_path, metadata=metadata,
            )
        except Exception:
            return None
        finally:
            if new_page:
                try:
                    await new_page.close()
                except Exception:
                    pass

    async def _safe_shutdown(self) -> None:
        for cdp in self._cdp_sessions:
            try:
                await cdp.detach()
            except Exception:
                pass
        self._cdp_sessions = []
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None

    async def _parse_duckduckgo_results(self, query: str, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        soup = BeautifulSoup(await self.page.content(), "html.parser")
        for node in soup.select(".result"):
            link_node = node.select_one(".result__title a")
            if not link_node:
                continue
            title = link_node.get_text(" ", strip=True)
            href = link_node.get("href")
            snippet_node = node.select_one(".result__snippet")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            resolved_url = _resolve_result_url(href)
            if resolved_url and title:
                results.append(SearchResult(title=title, url=resolved_url, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    async def _parse_bing_results(self, query: str, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        soup = BeautifulSoup(await self.page.content(), "html.parser")
        for node in soup.select("li.b_algo"):
            link_node = node.select_one("h2 a")
            if not link_node:
                continue
            title = link_node.get_text(" ", strip=True)
            href = link_node.get("href")
            snippet_node = node.select_one(".b_caption p")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            resolved_url = _resolve_result_url(href)
            if resolved_url and title:
                results.append(SearchResult(title=title, url=resolved_url, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    async def _is_bot_block_page(self) -> bool:
        body_text = (await self.page.locator("body").inner_text()).lower()
        blocked_markers = [
            "unfortunately, bots use duckduckgo too",
            "please complete the following challenge",
            "verify you are human",
            "captcha",
        ]
        return any(marker in body_text for marker in blocked_markers)

    async def _api_search_fallback(self, query: str, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        seen_urls: set[str] = set()
        timeout = httpx.Timeout(12.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            try:
                ddg = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_redirect": "1", "no_html": "1"},
                )
                data = ddg.json()
                if data.get("AbstractURL"):
                    url = str(data["AbstractURL"])
                    seen_urls.add(url)
                    results.append(
                        SearchResult(
                            title=data.get("Heading") or query,
                            url=url,
                            snippet=data.get("AbstractText") or "DuckDuckGo instant answer",
                        )
                    )
                for topic in data.get("RelatedTopics", []):
                    if "Topics" in topic:
                        items = topic.get("Topics", [])
                    else:
                        items = [topic]
                    for item in items:
                        url = str(item.get("FirstURL") or "")
                        text = str(item.get("Text") or "")
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        results.append(SearchResult(title=text[:120] or query, url=url, snippet=text))
                        if len(results) >= limit:
                            return results[:limit]
            except Exception:
                pass
        return results[:limit]



def _get_meta_content(soup: BeautifulSoup, name: str) -> str | None:
    node = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    return node.attrs.get("content") if node else None


def _resolve_result_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    query = parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return unquote(query["uddg"][0])
    if parsed.netloc.endswith("bing.com") and "u" in query and query["u"]:
        decoded = _decode_bing_redirect(query["u"][0])
        if decoded:
            return decoded
    return url


def _decode_bing_redirect(value: str) -> str | None:
    token = value
    if token.startswith("a1"):
        token = token[2:]
    padding = "=" * ((4 - len(token) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(token + padding).decode("utf-8", errors="ignore")
    except Exception:
        return None
    parsed = urlparse(decoded)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return decoded
    return None


def _filter_results_by_query(query: str, results: list[SearchResult]) -> list[SearchResult]:
    filtered: list[SearchResult] = []
    from app.utils.text import STOP_WORDS
    query_terms = {term for term in re.findall(r"\w+", query.lower()) if len(term) > 2 and term not in STOP_WORDS}
    blocked_domain_hints = {
        "dictionary", "wiktionary.org", "thesaurus.com", "vocabulary.com",
        "pinterest.com", "instagram.com", "tiktok.com",
    }
    for result in results:
        domain = urlparse(result.url).netloc.lower()
        if any(hint in domain for hint in blocked_domain_hints):
            continue
        combined = f"{result.title} {result.snippet or ''}"
        text_terms = set(re.findall(r"\w+", combined.lower()))
        overlap_count = len(query_terms & text_terms)
        overlap_score = keyword_overlap_score(query, combined)
        # Need at least 2 keyword matches OR a good overlap score
        if overlap_count >= 2 or overlap_score >= 0.25:
            filtered.append(result)
    # If filtering was too aggressive, return all non-blocked results
    if len(filtered) < 3 and len(results) > len(filtered):
        for result in results:
            domain = urlparse(result.url).netloc.lower()
            if any(hint in domain for hint in blocked_domain_hints):
                continue
            if result not in filtered:
                filtered.append(result)
            if len(filtered) >= 5:
                break
    return filtered


