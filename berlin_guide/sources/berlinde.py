"""berlin.de/events: Highlights (Festivals, Märkte, Konzertreihen).

Der RSS-Feed listet die Events, die Termine stehen als schema.org-JSON-LD
auf der jeweiligen Detailseite. robots.txt erlaubt das; wir identifizieren uns
per User-Agent und rufen gedrosselt ab.
"""
from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ..models import Event

log = logging.getLogger(__name__)

FEEDS = ["https://www.berlin.de/events/index.rss"]
DELAY_S = 1.0
TZ = ZoneInfo("Europe/Berlin")
LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def fetch(days: int = 30, session: Optional[requests.Session] = None) -> List[Event]:
    s = session or requests.Session()
    today = datetime.now(TZ).date()
    horizon = today + timedelta(days=days)
    out: List[Event] = []
    seen = set()
    for feed in FEEDS:
        r = s.get(feed, timeout=20)
        r.raise_for_status()
        for item in ET.fromstring(r.content).iter("item"):
            link = (item.findtext("link") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            time.sleep(DELAY_S)
            try:
                ev = _detail(s, link, item)
            except Exception as exc:  # noqa: BLE001
                log.warning("berlin.de: %s übersprungen (%s)", link, exc)
                continue
            # nur was im Zeitfenster liegt oder gerade läuft
            if ev and ev.start.date() <= horizon and (ev.end or ev.start).date() >= today:
                out.append(ev)
    log.info("berlin.de: %d Events", len(out))
    return out


def _detail(s: requests.Session, url: str, item) -> Optional[Event]:
    r = s.get(url, timeout=20)
    r.raise_for_status()
    data = None
    for block in LD_RE.findall(r.text):
        try:
            obj = json.loads(block)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("startDate"):
            data = obj
            break
    if not data:
        return None
    start_s, end_s = data["startDate"], data.get("endDate") or data["startDate"]
    all_day = "T" not in start_s
    if all_day:
        sd, ed = date.fromisoformat(start_s[:10]), date.fromisoformat(end_s[:10])
        start = datetime.combine(sd, dtime.min, TZ)
        end = datetime.combine(ed, dtime.min, TZ)  # inklusiv; ICS-Export addiert 1 Tag
    else:
        start = datetime.fromisoformat(start_s).astimezone(TZ)
        end = datetime.fromisoformat(end_s).astimezone(TZ) if "T" in end_s else None
    loc = data.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address") if isinstance(loc, dict) else None
    if isinstance(addr, dict):
        addr = ", ".join(x for x in (addr.get("streetAddress"), addr.get("postalCode"), addr.get("addressLocality")) if x)
    desc = BeautifulSoup(item.findtext("description") or "", "html.parser").get_text(" ", strip=True)
    return Event(
        uid=f"berlinde:{item.findtext('guid') or url}",
        source="berlin.de",
        title=(item.findtext("title") or data.get("name") or "").strip(),
        start=start,
        end=end,
        url=url,
        venue=(loc.get("name") or "") if isinstance(loc, dict) else "",
        address=addr or "",
        description=desc,
        all_day=all_day,
    )
