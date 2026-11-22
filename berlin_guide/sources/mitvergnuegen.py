"""Mit Vergnügen: redaktionelle Berlin-Tipps über den öffentlichen RSS-Feed.

Phase 1: Artikel als „Tipps“ listen. Phase 2: Claude extrahiert daraus konkrete Events.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from ..models import Article

log = logging.getLogger(__name__)

FEED = "https://mitvergnuegen.com/feed/"


def fetch(max_age_days: int = 30, session: Optional[requests.Session] = None) -> List[Article]:
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", "berlin-guide/0.1 (private event digest)")
    r = s.get(FEED, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    out: List[Article] = []
    for item in root.iter("item"):
        pub = item.findtext("pubDate")
        published = parsedate_to_datetime(pub) if pub else datetime.now(timezone.utc)
        if published < cutoff:
            continue
        desc = BeautifulSoup(item.findtext("description") or "", "html.parser").get_text(" ", strip=True)
        out.append(Article(
            source="mitvergnuegen",
            title=(item.findtext("title") or "").strip(),
            url=(item.findtext("link") or "").strip(),
            published=published,
            summary=desc[:280],
        ))
    log.info("mitvergnuegen: %d Artikel", len(out))
    return out
