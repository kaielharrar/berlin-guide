"""iCal-Export: ein abonnierbarer Feed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from icalendar import Calendar
from icalendar import Event as VEvent

from .models import Event

PRODID = "-//berlin-guide//DE"


def _vevent(ev: Event, personal: bool = False) -> VEvent:
    v = VEvent()
    v.add("uid", f"{ev.uid.replace(':', '-')}@berlin-guide")
    v.add("dtstamp", datetime.now(timezone.utc))
    if ev.all_day:
        v.add("dtstart", ev.start.date())
        v.add("dtend", (ev.end or ev.start).date() + timedelta(days=1))  # DTEND ist exklusiv
        v.add("transp", "TRANSPARENT")  # blockiert den Kalender nicht als „belegt“
    else:
        v.add("dtstart", ev.start)
        v.add("dtend", ev.end or ev.start + timedelta(hours=2))
    v.add("summary", ev.title)
    v.add("url", ev.url)
    if ev.address or ev.venue:
        v.add("location", ev.address or ev.venue)
    if ev.lat is not None and ev.lon is not None:
        v.add("geo", (ev.lat, ev.lon))
    lines = [l for l in (
        f"★ {ev.score}/10 – {ev.score_reason}" if personal and ev.score is not None else "",
        f"Preis: {ev.price_text}" if ev.price_text else "",
        f"Veranstalter: {ev.organizer}" if ev.organizer else "",
        ev.description,
        ev.url,
        f"Quelle: {ev.source}",
    ) if l]
    v.add("description", "\n".join(lines))
    if ev.tags:
        v.add("categories", ev.tags)
    return v


def write_feed(path: Path, events: Iterable[Event], name: str, desc: str,
               personal: bool = False) -> int:
    """personal=True schreibt die Bewertung in die Beschreibung – nur für den privaten Feed."""
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-caldesc", desc)
    cal.add("x-wr-timezone", "Europe/Berlin")
    cal.add("refresh-interval;value=duration", "PT12H")
    cal.add("x-published-ttl", "PT12H")
    n = 0
    for ev in sorted(events, key=lambda e: e.start):
        cal.add_component(_vevent(ev, personal))
        n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cal.to_ical())
    return n
