"""Eventbrite: Berlin-Übersichtsseiten + Detailseiten (schema.org-JSON-LD), ohne Claude.

Die Übersicht (/d/germany--berlin/all-events/?page=N) liefert je 20 Events, aber nur
mit Datum. Uhrzeit, Ende und Preis stehen im JSON-LD der Detailseite. Detailseiten
werden einmal abgerufen und gecacht (cache/eventbrite.json). robots.txt erlaubt beide
Seitentypen; wir identifizieren uns per User-Agent und rufen gedrosselt ab.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import List, Optional

import requests

from .. import extract as ex
from ..models import Event

log = logging.getLogger(__name__)

LIST_URL = "https://www.eventbrite.de/d/germany--berlin/all-events/?page={}"
PAGES = 8
MAX_DETAILS = 60  # neue Detailseiten pro Lauf, Rest beim nächsten Lauf
DELAY_S = 1.5
CACHE_FILE = "eventbrite.json"
LD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
EVENT_TYPES = {"Event", "Festival", "BusinessEvent", "SocialEvent", "MusicEvent", "EducationEvent",
               "ComedyEvent", "DanceEvent", "ExhibitionEvent", "FoodEvent", "LiteraryEvent",
               "SportsEvent", "TheaterEvent", "ScreeningEvent", "ChildrensEvent", "SaleEvent"}


def _ld(html: str) -> list:
    out = []
    for block in LD_RE.findall(html):
        try:
            out.append(json.loads(block))
        except ValueError:
            pass
    return out


def fetch(days: int = 30, session: Optional[requests.Session] = None) -> List[Event]:
    s = session or requests.Session()
    today = datetime.now(ex.TZ).date()
    horizon = today + timedelta(days=days)
    cache = ex.load_cache(CACHE_FILE)

    listed = {}
    for page in range(1, PAGES + 1):
        time.sleep(DELAY_S)
        r = s.get(LIST_URL.format(page), timeout=20)
        if r.status_code != 200:
            log.warning("eventbrite: Übersicht Seite %d -> HTTP %d", page, r.status_code)
            break
        items = [el.get("item", {}) for d in _ld(r.text) if isinstance(d, dict)
                 for el in d.get("itemListElement", []) if isinstance(el, dict)]
        items = [i for i in items if i.get("@type") in EVENT_TYPES and i.get("url")]
        if not items:
            break
        for i in items:
            listed[i["url"].split("?")[0]] = i

    fetched = 0
    for url, item in listed.items():
        if url in cache or fetched >= MAX_DETAILS:
            continue
        if (item.get("startDate") or "9999")[:10] > horizon.isoformat():
            continue
        time.sleep(DELAY_S)
        fetched += 1
        try:
            r = s.get(url, timeout=20)
            r.raise_for_status()
            detail = next((d for d in _ld(r.text) if isinstance(d, dict)
                           and d.get("@type") in EVENT_TYPES and d.get("startDate")), None)
        except Exception as exc:  # noqa: BLE001
            log.warning("eventbrite: Detailseite übersprungen (%s)", type(exc).__name__)
            continue
        cache[url] = detail or item

    # Abgelaufene Einträge aus dem Cache entfernen
    for url in [u for u, d in cache.items() if (d.get("endDate") or d.get("startDate") or "")[:10] < today.isoformat()]:
        del cache[url]
    ex.save_cache(CACHE_FILE, cache)

    out = []
    for url, data in cache.items():
        ev = _event(url, data)
        if ev and ev.start.date() <= horizon and (ev.end or ev.start).date() >= today:
            out.append(ev)
    log.info("eventbrite: %d gelistet, %d Detailseiten neu, %d Events im Zeitfenster", len(listed), fetched, len(out))
    return out


def _event(url: str, d: dict) -> Optional[Event]:
    if "Online" in (d.get("eventAttendanceMode") or "") and "Mixed" not in d.get("eventAttendanceMode", ""):
        return None
    start_s = d.get("startDate") or ""
    end_s = d.get("endDate") or start_s
    if not start_s:
        return None
    all_day = "T" not in start_s
    if all_day:
        start = datetime.combine(date.fromisoformat(start_s[:10]), dtime.min, ex.TZ)
        end = datetime.combine(date.fromisoformat(end_s[:10]), dtime.min, ex.TZ)
    else:
        start = datetime.fromisoformat(start_s).astimezone(ex.TZ)
        end = datetime.fromisoformat(end_s).astimezone(ex.TZ) if "T" in end_s else None
    loc = d.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address") or {}
    if isinstance(addr, dict):
        if addr.get("addressLocality") and "berlin" not in addr["addressLocality"].lower():
            return None
        addr = ", ".join(x for x in (addr.get("streetAddress"), addr.get("postalCode"), addr.get("addressLocality")) if x)
    geo = loc.get("geo") or {}
    org = d.get("organizer") or {}
    if isinstance(org, list):
        org = org[0] if org else {}
    offers = d.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    prices = []
    for o in offers:
        for k in ("lowPrice", "price"):
            try:
                prices.append(float(o[k]))
                break
            except (KeyError, TypeError, ValueError):
                pass
    ev_id = re.search(r"(\d+)$", url)
    return Event(
        uid=f"eventbrite:{ev_id.group(1) if ev_id else url}",
        source="Eventbrite",
        title=(d.get("name") or "").strip(),
        start=start,
        end=end,
        url=url,
        venue=loc.get("name") or "",
        address=addr or "",
        lat=float(geo["latitude"]) if geo.get("latitude") else None,
        lon=float(geo["longitude"]) if geo.get("longitude") else None,
        organizer=(org.get("name") or "") if isinstance(org, dict) else "",
        description=(d.get("description") or "").strip(),
        image=d.get("image") if isinstance(d.get("image"), str) else "",
        is_free=(min(prices) == 0) if prices else None,
        price_eur=min(prices) if prices else None,
        all_day=all_day,
    )
