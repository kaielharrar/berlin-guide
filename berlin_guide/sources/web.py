"""Webseiten als Quelle: Sitemap oder RSS -> Seiten -> Claude extrahiert Termine.

Genutzt für öffentliche Magazine (config/profile.yaml: web_sources) und private
Quellen (sources.private.yaml: sitemap/rss). Jede Seite geht nur einmal an Claude –
erneut nur, wenn sich ihr Änderungsdatum (lastmod/pubDate) ändert.

Konfiguration je Quelle:
  name: The Berliner
  sitemap: https://example.org/sitemap_index.xml   # oder: rss: https://example.org/feed/
  max_age_days: 14          # nur kürzlich geänderte/veröffentlichte Seiten
  include: [/events/]       # optional: URL muss eins davon enthalten
  exclude: [/news/]         # optional: URL darf keins davon enthalten
  max_pages: 40             # optional: Obergrenze neuer Seiten pro Lauf
"""
from __future__ import annotations

import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import List, Optional, Tuple

import requests

from .. import extract as ex
from .. import usage
from ..models import Event

log = logging.getLogger(__name__)

SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
DELAY_S = 1.0
INDEX_CHILDREN = 2   # bei Sitemap-Index: nur die letzten N Unter-Sitemaps (neueste Beiträge)


def fetch_source(s: requests.Session, src: dict, days: int, private: bool, label: str) -> List[Event]:
    """label erscheint im Log – bei privaten Quellen nur eine Nummer."""
    cache_file = f"web_{hashlib.sha1((src.get('sitemap') or src.get('rss')).encode()).hexdigest()[:10]}.json"
    cache = ex.load_cache(cache_file)
    client = ex.client_or_none()
    pages = _list_pages(s, src)
    name = src.get("name", "Web")
    new = 0
    for url, stamp in pages:
        if (cache.get(url) or {}).get("stamp") == stamp or client is None:
            continue
        if new >= src.get("max_pages", 40) or not usage.allow("extract"):
            break  # Rest beim nächsten Lauf
        time.sleep(DELAY_S)
        try:
            text = ex.page_text(s, url, src.get("selector", ""))
            extracted = ex.extract(client, url.rstrip("/").rsplit("/", 1)[-1], datetime.now(ex.TZ), text)
        except Exception as exc:  # noqa: BLE001 – eine Seite darf den Lauf nicht abbrechen
            log.warning("%s: Seite übersprungen (%s)", label, type(exc).__name__)
            continue
        cache[url] = {"stamp": stamp, "events": [e.model_dump() for e in extracted]}
        new += 1

    # Cache: Seiten behalten, solange noch Termine bevorstehen
    today = datetime.now(ex.TZ).date().isoformat()
    listed = {u for u, _ in pages}
    for url in [u for u, e in cache.items() if u not in listed and not any(
            (x.get("end_date") or x.get("start_date") or "") >= today for x in e["events"])]:
        del cache[url]
    ex.save_cache(cache_file, cache)

    out = []
    prefix = "privat-web" if private else "web"
    for url, entry in cache.items():
        for raw in entry["events"]:
            ev = ex.to_event(ex.ExtractedEvent(**raw), url, name, prefix, private=private)
            if ev and ex.in_window(ev, days):
                out.append(ev)
    log.info("%s: %d Seiten, %d neu ausgewertet, %d Events", label, len(pages), new, len(out))
    return out


def _list_pages(s: requests.Session, src: dict) -> List[Tuple[str, str]]:
    cutoff = datetime.now(ex.TZ) - timedelta(days=src.get("max_age_days", 30))
    inc = src.get("include") or []
    exc = [x.lower() for x in src.get("exclude") or []]

    def keep(url: str) -> bool:
        low = url.lower()
        return (not inc or any(i in url for i in inc)) and not any(x in low for x in exc)

    items: List[Tuple[str, str]] = []
    if src.get("rss"):
        root = ET.fromstring(_get(s, src["rss"]))
        for it in root.iter("item"):
            url = (it.findtext("link") or "").strip()
            pub = it.findtext("pubDate")
            when = parsedate_to_datetime(pub) if pub else datetime.now(ex.TZ)
            if url and when >= cutoff and keep(url):
                items.append((url, when.isoformat()))
        return items

    root = ET.fromstring(_get(s, src["sitemap"]))
    if root.tag == f"{SM}sitemapindex":
        children = [(l.findtext(f"{SM}loc") or "").strip() for l in root.iter(f"{SM}sitemap")]
        posts = [c for c in children if "post-sitemap" in c] or children
        roots = [ET.fromstring(_get(s, c)) for c in posts[-INDEX_CHILDREN:]]
    else:
        roots = [root]
    day_cutoff = cutoff.date().isoformat()
    for r in roots:
        for u in r.iter(f"{SM}url"):
            url = (u.findtext(f"{SM}loc") or "").strip()
            lastmod = (u.findtext(f"{SM}lastmod") or "").strip()
            if url and lastmod[:10] >= day_cutoff and keep(url):
                items.append((url, lastmod))
    return items


def _get(s: requests.Session, url: str) -> bytes:
    r = s.get(url, timeout=30)
    r.raise_for_status()
    return r.content


def fetch_public(days: int = 30, session: Optional[requests.Session] = None) -> List[Event]:
    """Öffentliche Webquellen aus config/profile.yaml (web_sources)."""
    from ..curate import load_profile
    from pathlib import Path
    profile = load_profile(Path(__file__).resolve().parents[2] / "config" / "profile.yaml")
    s = session or requests.Session()
    out: List[Event] = []
    for src in profile.get("web_sources") or []:
        try:
            out += fetch_source(s, src, days, private=False, label=src.get("name", "web"))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s fehlgeschlagen (%s)", src.get("name", "web"), exc)
    return out
