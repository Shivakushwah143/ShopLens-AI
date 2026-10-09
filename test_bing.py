import asyncio, httpx
from bs4 import BeautifulSoup

async def test():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
        resp = await client.get("https://www.bing.com/search", params={"q": "pre training post training machine learning", "cc": "us", "setlang": "en"})
        print("Bing status:", resp.status_code)
    soup = BeautifulSoup(resp.text, "html.parser")
    count = 0
    for node in soup.select("li.b_algo"):
        link = node.select_one("h2 a")
        if not link:
            continue
        href = link.get("href", "")
        title = link.get_text(" ", strip=True)
        print(f"  [{count+1}] {title[:70]}")
        print(f"       => {href[:90]}")
        count += 1
        if count >= 6:
            break
    print("Total Bing results:", count)

    # Test arXiv too
    print("\n--- arXiv ---")
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        r = await client.get("https://export.arxiv.org/api/query", params={"search_query": "all:pre-training post-training language model", "max_results": "5", "sortBy": "relevance"})
        print("arXiv status:", r.status_code, "length:", len(r.text))
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns)[:3]:
            t = entry.find("atom:title", ns)
            i = entry.find("atom:id", ns)
            print(f"  arXiv: {(t.text or '').strip()[:60]}")

asyncio.run(test())
