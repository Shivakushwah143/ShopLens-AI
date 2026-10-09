from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


async def fetch_with_httpx(url: str) -> str:
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TaskAutomationFetcher/1.0)"},
        ) as client:
            r = await client.get(url)
            if r.status_code == 200 and r.text and len(r.text) > 1500:
                return r.text
    except Exception as exc:
        logger.debug("httpx fetch failed: %s", exc)
    return ""


def fetch_with_drission(url: str) -> str:
    """Optional JS-rendered fetch via DrissionPage (only if installed).

    This keeps Task Automation modular: if DrissionPage isn't installed,
    this is a no-op.
    """
    try:
        # DrissionPage is optional; do not add hard dependency.
        from DrissionPage import ChromiumPage  # type: ignore
    except Exception:
        return ""

    page = None
    try:
        page = ChromiumPage()
        page.get(url)
        page.wait(2)
        html = page.html or ""
        return html
    except Exception as exc:
        logger.debug("drission fetch failed: %s", exc)
        return ""
    finally:
        try:
            if page:
                page.quit()
        except Exception:
            pass


async def fetch_html(url: str) -> str:
    html = await fetch_with_httpx(url)
    if len(html) < 1500:
        html = fetch_with_drission(url)
    return html or ""

