"""Private Quellen: iCal-Feeds (Meetup, Luma-Kalender, …) und Webseiten per Sitemap.

Welche Quellen das sind, steht NICHT im Repo: lokal in config/sources.private.yaml
(von Git ignoriert), in GitHub Actions im Secret PRIVATE_SOURCES. Events aus diesen
Quellen landen nur im persönlichen Feed; geloggt werden nur Nummern und Zahlen.

Format (YAML):
  ical:
    - name: Beispielgruppe
      url: https://www.meetup.com/<gruppe>/events/ical/
      berlin_only: true        # optional, Standard true
  sitemap:                     # oder rss: (siehe sources/web.py)
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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import requests
import yaml
from icalendar import Calendar

from .. import extract as ex
from . import web
from ..models import Event

log = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).resolve().parents[2] / "config" / "sources.private.yaml"
DELAY_S = 1.0
BERLIN_RE = re.compile(r"berlin|\b1[0-4]\d{3}\b", re.I)
ONLINE_RE = re.compile(r"^\s*(https?://|online|zoom|virtual)", re.I)


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
    for i, src in enumerate((cfg.get("sitemap") or []) + (cfg.get("rss") or []), 1):
        try:
            out += _web(s, src, days, f"privat web #{i}")
        except Exception as exc:  # noqa: BLE001
            log.warning("privat web #%d fehlgeschlagen (%s)", i, type(exc).__name__)
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


# --- Webseiten (Sitemap/RSS) + Claude -> sources/web.py -----------------------

def _web(s: requests.Session, src: dict, days: int, label: str) -> List[Event]:
    src = dict(src)
    if "url" in src and "sitemap" not in src and "rss" not in src:
        src["sitemap"] = src.pop("url")
    _migrate_old_cache(src)
    return web.fetch_source(s, src, days, private=True, label=label)


def _migrate_old_cache(src: dict) -> None:
    """Einmalig: alten Cache (private_pages.json) ins neue Format übernehmen,
    damit bereits ausgewertete Seiten nicht erneut an Claude gehen."""
    old = ex.load_cache("private_pages.json")
    if not old:
        return
    name = f"web_{hashlib.sha1(src['sitemap'].encode()).hexdigest()[:10]}.json"
    new = ex.load_cache(name)
    for url, entry in old.items():
        new.setdefault(url, {"stamp": entry.get("lastmod", ""), "events": entry.get("events", [])})
    ex.save_cache(name, new)
    (ex.CACHE_DIR / "private_pages.json").unlink(missing_ok=True)
