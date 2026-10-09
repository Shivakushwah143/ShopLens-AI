from __future__ import annotations
import re
from typing import Any
from bs4 import BeautifulSoup
from .extractor import NormalizedItem, _domain_from_url
from .flight_utils import TIME_RANGES

class FlightExtractor:
    """Specialized extractor for Flights vertical."""

    def extract(
        self,
        *,
        html: str,
        base_url: str | None = None,
        time_pref: str | None = None,
        target_date: str | None = None,
    ) -> list[NormalizedItem]:
        soup = BeautifulSoup(html or "", "html.parser")
        domain = _domain_from_url(base_url)
        
        # Identify flight cards (common selectors across MMT, Cleartrip, Ixigo)
        # card-like elements often contain "flight", "itinerary", "result"
        selectors = [
            ".itinerary-card",
            ".fli-list",
            ".flight-card",
            "[class*='FlightCard']",
            "[class*='itinerary']",
            "article",
        ]
        
        candidates = []
        for selector in selectors:
            nodes = soup.select(selector)
            if nodes:
                candidates = nodes
                break
        
        if not candidates:
            # Fallback to looking for divs that contain price and time patterns
            candidates = soup.find_all("div", recursive=True)

        items: list[tuple[float, NormalizedItem]] = [] # (distance, item)
        for node in candidates:
            text = node.get_text(" ", strip=True)
            if not text or len(text) < 40:
                continue
            
            # Basic validation: must have a price and at least two times (dep/arr)
            price = self._parse_price(text)
            times = re.findall(r"\b([0-2]\d:[0-5]\d)\b", text)
            
            if not price or len(times) < 2:
                continue

            airline = self._guess_airline(node, text)
            dep_time = times[0]
            arr_time = times[1]
            duration = self._guess_duration(text)
            
            # Time proximity
            dist = self._time_distance(dep_time, time_pref) if time_pref else 0
            
            # booking link
            link = self._extract_link(node, base_url)
            
            items.append((
                dist,
                NormalizedItem(
                    name=f"{airline} ({dep_time} -> {arr_time})",
                    price=price,
                    rating=None,
                    link=link,
                    source_domain=domain,
                    raw={
                        "airline": airline,
                        "departure_time": dep_time,
                        "arrival_time": arr_time,
                        "duration": duration,
                        "time_distance": dist,
                        "snippet": text[:200]
                    }
                )
            ))

        # Filter: keep only those with dist == 0
        exact_matches = [it for d, it in items if d == 0]
        if exact_matches:
            return exact_matches
        
        # Fallback: if no exact matches, return top 10 nearest flights
        items.sort(key=lambda x: x[0])
        return [it for d, it in items[:10]]

    def _time_distance(self, time_str: str, pref: str) -> int:
        if pref not in TIME_RANGES:
            return 0
        try:
            hour = int(time_str.split(":")[0])
            start, end = TIME_RANGES[pref]
            
            if start < end:
                if start <= hour < end:
                    return 0
                return min(abs(hour - start), abs(hour - (end - 1)))
            else: # overnight (e.g. 21 to 6)
                if hour >= start or hour < end:
                    return 0
                # Distance to 21 or 6
                d_to_start = abs(hour - start)
                d_to_end = abs(hour - end)
                return min(d_to_start, d_to_end)
        except:
            return 99

    def _parse_price(self, text: str) -> float | None:
        m = re.search(r"(?:₹|rs\.?|inr)\s*([\d,]{3,})", text, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except:
                return None
        return None

    def _guess_airline(self, node, text: str) -> str:
        # Check for common airline names in text
        airlines = ["IndiGo", "Air India", "Vistara", "SpiceJet", "Akasa Air", "Air India Express", "Emirates", "Qatar Airways", "Lufthansa"]
        for a in airlines:
            if a.lower() in text.lower():
                return a
        
        # Airline names often in strong/b or specific classes
        for tag in ["strong", "b", "h3", "h4"]:
            el = node.find(tag)
            if el:
                t = el.get_text(strip=True)
                if 2 < len(t) < 30 and not re.search(r"\d", t): return t
        
        return "Airline"

    def _guess_duration(self, text: str) -> str:
        m = re.search(r"(\d+h\s*\d+m|\d+\s*h|\d+\s*m)", text, flags=re.IGNORECASE)
        return m.group(0) if m else "Unknown"

    def _extract_link(self, node, base_url: str | None) -> str | None:
        a = node.find("a", href=True)
        if not a: return base_url
        href = a["href"]
        if href.startswith("/"):
            from urllib.parse import urlparse
            parsed = urlparse(base_url)
            return f"{parsed.scheme}://{parsed.netloc}{href}"
        return href
