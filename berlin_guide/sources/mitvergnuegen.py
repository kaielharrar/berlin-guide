"""Mit Vergnügen: Claude liest die Artikel aus und extrahiert konkrete Termine.

RSS-Feed -> Artikelseite (article.single) -> Claude. Jeder Artikel geht nur
einmal an Claude (Cache: cache/mitvergnuegen.json).
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import List, Optional

import requests

from .. import extract as ex
from .. import usage
from ..models import Event

log = logging.getLogger(__name__)

FEED = "https://mitvergnuegen.com/feed/"
CACHE_FILE = "mitvergnuegen.json"
DELAY_S = 1.0
MAX_ARTICLE_AGE_DAYS = 60


def fetch(days: int = 30, session: Optional[requests.Session] = None) -> List[Event]:
    s = session or requests.Session()
    cache = ex.load_cache(CACHE_FILE)
    client = ex.client_or_none()
    if client is None:
        log.warning("mitvergnuegen: kein ANTHROPIC_API_KEY – nur bereits ausgewertete Artikel")

    r = s.get(FEED, timeout=20)
    r.raise_for_status()
    new = 0
    for item in ET.fromstring(r.content).iter("item"):
        url = (item.findtext("link") or "").strip()
        if not url or url in cache or client is None:
            continue
        if not usage.allow("extract"):
            break  # Tagesbudget erreicht – Rest beim nächsten Lauf
        pub = item.findtext("pubDate")
        published = parsedate_to_datetime(pub) if pub else datetime.now(ex.TZ)
        title = (item.findtext("title") or "").strip()
        time.sleep(DELAY_S)
        try:
            text = ex.page_text(s, url, "article.single")
            extracted = ex.extract(client, title, published, text)
        except Exception as exc:  # noqa: BLE001 – ein Artikel darf den Lauf nicht abbrechen
            log.warning("mitvergnuegen: %s übersprungen (%s)", url, exc)
            continue
        cache[url] = {"title": title, "published": published.isoformat(),
                      "events": [e.model_dump() for e in extracted]}
        new += 1
        log.info("mitvergnuegen: %d Termine aus „%s“", len(extracted), title)

    _prune(cache)
    ex.save_cache(CACHE_FILE, cache)

    out: List[Event] = []
    for article_url, entry in cache.items():
        for raw in entry["events"]:
            ev = ex.to_event(ex.ExtractedEvent(**raw), article_url, "Mit Vergnügen", "mitvergnuegen")
            if ev and ex.in_window(ev, days):
                out.append(ev)
    log.info("mitvergnuegen: %d neue Artikel ausgewertet, %d Events im Zeitfenster", new, len(out))
    return out


def _prune(cache: dict) -> None:
    """Alte Artikel entfernen – aber nur, wenn keiner ihrer Termine noch bevorsteht."""
    cutoff = datetime.now(ex.TZ) - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    today = datetime.now(ex.TZ).date().isoformat()

    def still_relevant(entry: dict) -> bool:
        return any((e.get("end_date") or e.get("start_date") or "") >= today for e in entry["events"])

    for url in [u for u, e in cache.items()
                if datetime.fromisoformat(e["published"]) < cutoff and not still_relevant(e)]:
        del cache[url]
