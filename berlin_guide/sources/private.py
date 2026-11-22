"""Private Quellen: iCal-Feeds (Meetup, Luma-Kalender, …) und Webseiten per Sitemap.

Welche Quellen das sind, steht NICHT im Repo: lokal in config/sources.private.yaml
(von Git ignoriert), in GitHub Actions im Secret PRIVATE_SOURCES. Events aus diesen
Quellen landen nur im persönlichen Feed; geloggt werden nur Nummern und Zahlen.

Format (YAML):
  ical:
    - name: Beispielgruppe
      url: https://www.meetup.com/<gruppe>/events/ical/
      berlin_only: true        # optional, Standard true
  sitemap:
    - name: Beispiel-Institut
      url: https://example.org/sitemap.xml
      max_age_days: 120        # nur Seiten, die so kürzlich geändert wurden
      exclude: [archive, draft]
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import requests
import yaml
from icalendar import Calendar

from .. import extract as ex
from ..models import Event

log = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).resolve().parents[2] / "config" / "sources.private.yaml"
CACHE_FILE = "private_pages.json"
DELAY_S = 1.0
BERLIN_RE = re.compile(r"berlin|\b1[0-4]\d{3}\b", re.I)
ONLINE_RE = re.compile(r"^\s*(https?://|online|zoom|virtual)", re.I)
SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def load_config() -> dict:
    text = os.environ.get("PRIVATE_SOURCES")
    if not text and CONFIG_FILE.exists():
        text = CONFIG_FILE.read_text(encoding="utf-8")
    return (yaml.safe_load(text) or {}) if text else {}


def fetch(days: int = 30, session: Optional[requests.Session] = None) -> List[Event]:
    s = session or requests.Session()
    cfg = load_config()
    out: List[Event] = []
    for i, src in enumerate(cfg.get("ical") or [], 1):
        try:
            got = _ical(s, src, days)
            log.info("privat ical #%d: %d Events", i, len(got))
            out += got
        except Exception as exc:  # noqa: BLE001
            log.warning("privat ical #%d fehlgeschlagen (%s)", i, type(exc).__name__)
    for i, src in enumerate(cfg.get("sitemap") or [], 1):
        try:
            got = _sitemap(s, src, days)
            log.info("privat sitemap #%d: %d Events", i, len(got))
            out += got
        except Exception as exc:  # noqa: BLE001
            log.warning("privat sitemap #%d fehlgeschlagen (%s)", i, type(exc).__name__)
    return out


# --- iCal -------------------------------------------------------------------

def _ical(s: requests.Session, src: dict, days: int) -> List[Event]:
    r = s.get(src["url"], timeout=30)
    r.raise_for_status()
    cal = Calendar.from_ical(r.content)
    feed_key = hashlib.sha1(src["url"].encode()).hexdigest()[:8]
    berlin_only = src.get("berlin_only", True)
    out = []
    for v in cal.walk("VEVENT"):
        start, all_day = _dt(v.get("dtstart"))
        if start is None:
            continue
        end, _ = _dt(v.get("dtend"))
        if end is None and v.get("duration"):
            end = start + v.get("duration").dt
        if all_day and end:
            end -= timedelta(days=1)  # ICS-DTEND ist exklusiv, intern inklusiv
        title = str(v.get("summary") or "").strip()
        location = str(v.get("location") or "").strip()
        desc = str(v.get("description") or "").strip()
        if ONLINE_RE.match(location) and not BERLIN_RE.search(location):
            location = ""
        if berlin_only and not BERLIN_RE.search(" ".join((location, title, desc))):
            continue
        url = str(v.get("url") or "") or _first_url(desc) or src["url"]
        ev = Event(uid=f"ical-{feed_key}:{v.get('uid') or title + start.isoformat()}",
                   source=src.get("name", "privat"), title=title, start=start, end=end, url=url,
                   address=location, organizer=src.get("name", ""), description=desc[:1500],
                   all_day=all_day, private=True)
        if ex.in_window(ev, days):
            out.append(ev)
    return out


def _dt(prop):
    if prop is None:
        return None, False
    v = prop.dt
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=ex.TZ)).astimezone(ex.TZ), False
    if isinstance(v, date):
        return datetime.combine(v, datetime.min.time(), ex.TZ), True
    return None, False


def _first_url(text: str) -> str:
    m = re.search(r"https?://\S+", text)
    return m.group(0).rstrip(").,") if m else ""


# --- Sitemap + Claude ---------------------------------------------------------

def _sitemap(s: requests.Session, src: dict, days: int) -> List[Event]:
    cache = ex.load_cache(CACHE_FILE)
    client = ex.client_or_none()
    r = s.get(src["url"], timeout=30)
    r.raise_for_status()
    cutoff = (datetime.now(ex.TZ) - timedelta(days=src.get("max_age_days", 120))).date().isoformat()
    excludes = [x.lower() for x in src.get("exclude") or []]
    name = src.get("name", "privat")
    new = 0
    pages = []
    for u in ET.fromstring(r.content).iter(f"{SM_NS}url"):
        loc = (u.findtext(f"{SM_NS}loc") or "").strip()
        lastmod = (u.findtext(f"{SM_NS}lastmod") or "")[:10]
        if loc and lastmod >= cutoff and not any(x in loc.lower() for x in excludes):
            pages.append((loc, lastmod))
    for loc, lastmod in pages:
        cached = cache.get(loc)
        if (cached and cached.get("lastmod") == lastmod) or client is None:
            continue  # unverändert oder kein API-Key
        time.sleep(DELAY_S)
        try:
            text = ex.page_text(s, loc)
            extracted = ex.extract(client, loc.rsplit("/", 1)[-1], datetime.now(ex.TZ), text)
        except Exception as exc:  # noqa: BLE001
            log.warning("privat sitemap: Seite übersprungen (%s)", type(exc).__name__)
            continue
        cache[loc] = {"lastmod": lastmod, "events": [e.model_dump() for e in extracted]}
        new += 1
    # Seiten, die nicht mehr in der Sitemap-Auswahl sind, aus dem Cache entfernen
    keep = {loc for loc, _ in pages}
    prefix = src["url"].rsplit("/", 1)[0]
    for loc in [k for k in cache if k.startswith(prefix) and k not in keep]:
        del cache[loc]
    ex.save_cache(CACHE_FILE, cache)

    out = []
    for loc, _ in pages:
        for raw in (cache.get(loc) or {}).get("events", []):
            ev = ex.to_event(ex.ExtractedEvent(**raw), loc, name, "privat-web", private=True)
            if ev and ex.in_window(ev, days):
                out.append(ev)
    log.info("privat sitemap: %d Seiten, %d neu ausgewertet", len(pages), new)
    return out
