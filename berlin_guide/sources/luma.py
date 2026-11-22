"""Luma (lu.ma/berlin): öffentliche Discover-Liste für Berlin.

Nutzt denselben JSON-Endpunkt, den die öffentliche Seite lu.ma/berlin lädt.
Seitenweise, gedrosselt, stoppt sobald der Zeithorizont erreicht ist.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from dateutil import parser as dtparse

from ..models import (APPROVAL, NEAR_CAPACITY, OPEN, SOLD_OUT, WAITLIST, Event)

log = logging.getLogger(__name__)

API = "https://api.lu.ma/discover/get-paginated-events"
BERLIN_PLACE_ID = "discplace-gCfX0s3E9Hgo3rG"  # lu.ma/berlin
PAGE_SIZE = 50
MAX_PAGES = 12
DELAY_S = 1.0


def fetch(days: int = 14, session: Optional[requests.Session] = None) -> List[Event]:
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", "berlin-guide/0.1 (private event digest)")
    horizon = datetime.now(timezone.utc) + timedelta(days=days)
    events: List[Event] = []
    cursor = None
    for page in range(MAX_PAGES):
        params = {"discover_place_api_id": BERLIN_PLACE_ID, "pagination_limit": PAGE_SIZE}
        if cursor:
            params["pagination_cursor"] = cursor
        r = s.get(API, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        entries = data.get("entries", [])
        for entry in entries:
            ev = _parse(entry)
            if ev and ev.start <= horizon:
                events.append(ev)
        log.info("luma: Seite %d, %d Einträge", page + 1, len(entries))
        last_start = _dt(entries[-1].get("start_at")) if entries else None
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor or (last_start and last_start > horizon):
            break
        time.sleep(DELAY_S)
    return events


def _dt(value) -> Optional[datetime]:
    return dtparse.isoparse(value) if value else None


def _parse(entry: dict) -> Optional[Event]:
    e = entry.get("event") or {}
    start = _dt(e.get("start_at") or entry.get("start_at"))
    if not start or e.get("location_type") == "online":
        return None
    geo = e.get("geo_address_info") or {}
    de = (geo.get("localized") or {}).get("de") or {}
    coord = geo.get("place_coordinate") or e.get("coordinate") or {}
    t = entry.get("ticket_info") or {}
    price = t.get("price") or {}
    cal = entry.get("calendar") or {}
    hosts = entry.get("hosts") or []

    waitlist = bool(entry.get("waitlist_active"))
    if t.get("is_sold_out"):
        status = SOLD_OUT
    elif waitlist:
        status = WAITLIST
    elif t.get("is_near_capacity"):
        status = NEAR_CAPACITY
    elif t.get("require_approval"):
        status = APPROVAL
    else:
        status = OPEN
    spots = t.get("spots_remaining")

    ev = Event(
        uid=f"luma:{e.get('api_id') or entry.get('api_id')}",
        source="luma",
        title=(e.get("name") or "").strip(),
        start=start,
        end=_dt(e.get("end_at")),
        url=f"https://lu.ma/{e.get('url')}",
        venue=geo.get("address") or "",
        address=de.get("full_address") or geo.get("full_address") or "",
        district=de.get("sublocality") or geo.get("sublocality") or "",
        lat=coord.get("latitude"),
        lon=coord.get("longitude"),
        organizer=cal.get("name") or (hosts[0].get("name") if hosts else ""),
        image=e.get("cover_url") or "",
        is_free=t.get("is_free"),
        price_eur=(price["cents"] / 100) if price.get("cents") is not None else None,
        ticket_status=status,
        # spots_remaining ist bei ausverkauften Events teils unplausibel -> nur bei offenen nutzen
        spots_remaining=spots if status not in (SOLD_OUT, WAITLIST) else None,
        guest_count=entry.get("guest_count"),
        has_waitlist=waitlist,
    )
    return ev
