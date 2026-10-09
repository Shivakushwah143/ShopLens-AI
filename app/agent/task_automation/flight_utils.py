from __future__ import annotations
import re
from datetime import datetime, timedelta
from typing import Any

# Expanded airport codes
AIRPORT_CODES = {
    "delhi": "DEL",
    "mumbai": "BOM",
    "bangalore": "BLR",
    "bengaluru": "BLR",
    "chennai": "MAA",
    "kolkata": "CCU",
    "hyderabad": "HYD",
    "pune": "PNQ",
    "ahmedabad": "AMD",
    "goa": "GOI",
    "kochi": "COK",
    "jaipur": "JAI",
    "lucknow": "LKO",
    "dubai": "DXB",
    "london": "LHR",
    "new york": "JFK",
    "singapore": "SIN",
    "bangkok": "BKK",
}

TIME_RANGES = {
    "morning": (6, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
    "night": (21, 6),
}

def resolve_airport(city: str) -> str:
    city = city.lower().strip()
    return AIRPORT_CODES.get(city, city.upper()[:3])

def resolve_date(date_str: str) -> datetime:
    now = datetime.now()
    ds = date_str.lower().strip()
    
    if "tomorrow" in ds:
        return now + timedelta(days=1)
    if "today" in ds:
        return now
    if "this weekend" in ds:
        # Friday (4)
        days_ahead = (4 - now.weekday()) % 7
        if days_ahead <= 0: days_ahead += 7
        return now + timedelta(days=days_ahead)
    
    # Simple regex for DD/MM or DD Month
    m = re.search(r"(\d{1,2})[/\-\s](jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2})", ds)
    if m:
        day = int(m.group(1))
        # month = m.group(2) # complex to parse all months here, fallback to next week
        return now + timedelta(days=7)

    return now

def get_flight_search_urls(origin: str, destination: str, date: datetime) -> list[str]:
    from_code = resolve_airport(origin)
    to_code = resolve_airport(destination)
    
    # Formats: 
    # Cleartrip: DD/MM/YYYY
    # MMT: DD/MM/YYYY
    d_str = date.strftime("%d/%m/%Y")
    d_ixigo = date.strftime("%d%m%Y")
    
    urls = [
        f"https://www.cleartrip.com/flights/results?from={from_code}&to={to_code}&depart_date={d_str}&adults=1&childs=0&infants=0&class=Economy",
        f"https://www.makemytrip.com/flight/search?itinerary={from_code}-{to_code}-{d_str}&tripType=O&paxType=A-1_C-0_I-0&intl=false&cabinClass=E",
        f"https://www.ixigo.com/search/result/flight/{from_code}/{to_code}/{d_ixigo}/1/0/0/Economy",
    ]
    return urls

def extract_flight_params(query: str) -> dict[str, Any]:
    low = query.lower()
    params = {
        "origin": "Delhi",
        "destination": "Mumbai",
        "date_str": "today",
        "time_pref": None,
    }
    
    # Origin/Destination
    m = re.search(r"from\s+([a-z\s]+?)\s+to\s+([a-z\s]+)", low)
    if m:
        src = m.group(1).strip()
        dst = m.group(2).strip()
        # Clean destination from common date words
        for kw in ["today", "tomorrow", "this weekend", "morning", "afternoon", "evening", "night"]:
            dst = dst.replace(kw, "").strip()
        params["origin"] = src.title()
        params["destination"] = dst.title()
    
    # Date
    if "tomorrow" in low:
        params["date_str"] = "tomorrow"
    elif "this weekend" in low:
        params["date_str"] = "this weekend"
    elif "today" in low:
        params["date_str"] = "today"
        
    # Time pref
    for pref in TIME_RANGES:
        if pref in low:
            params["time_pref"] = pref
            break
            
    return params
