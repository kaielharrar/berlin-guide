"""Mit Vergnügen: Claude liest die Artikel aus und extrahiert konkrete Termine.

Ablauf: RSS-Feed -> Artikelseite (article.single) -> Claude (strukturierte Ausgabe).
Ergebnisse werden pro Artikel-URL in cache/mitvergnuegen.json gespeichert, damit
jeder Artikel nur einmal an Claude geht. Ohne ANTHROPIC_API_KEY wird nur der Cache genutzt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import anthropic
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel

from ..models import Event

log = logging.getLogger(__name__)

FEED = "https://mitvergnuegen.com/feed/"
CACHE = Path(__file__).resolve().parents[2] / "cache" / "mitvergnuegen.json"
MODEL = "claude-opus-5-5"
TZ = ZoneInfo("Europe/Berlin")
DELAY_S = 1.0
MAX_ARTICLE_AGE_DAYS = 60

SYSTEM = """Du extrahierst Veranstaltungen aus Artikeln des Berliner Stadtmagazins „Mit Vergnügen“.
Gib nur Einträge zurück, die einen konkreten Termin haben (Datum, ggf. Zeitraum). Orte ohne Datum
(Restaurants, Cafés, Bars, „schönste Orte“) sind keine Veranstaltungen – dann eine leere Liste.
Regeln:
- Datumsangaben ohne Jahr beziehen sich auf das Jahr der Veröffentlichung bzw. das nächste passende Datum danach.
- Mehrtägige Events (Märkte, Ausstellungen, Festivals): start_date und end_date, Uhrzeiten leer lassen.
- Uhrzeiten als HH:MM in Berliner Ortszeit; „ab 19 Uhr“ ist start_time 19:00.
- url: den Ticket- oder Info-Link des Eintrags, falls im Text angegeben, sonst leer.
- description: ein bis zwei Sätze auf Deutsch, eigene Worte, keine wörtlichen Zitate.
- Nichts erfinden: Was nicht im Text steht, bleibt leer."""


class ExtractedEvent(BaseModel):
    title: str
    start_date: str            # YYYY-MM-DD
    end_date: Optional[str]    # YYYY-MM-DD, bei mehrtägigen Events
    start_time: Optional[str]  # HH:MM
    end_time: Optional[str]
    venue: str
    address: str
    price: str
    url: str
    description: str


class Extraction(BaseModel):
    events: List[ExtractedEvent]


def fetch(days: int = 30, session: Optional[requests.Session] = None) -> List[Event]:
    s = session or requests.Session()
    cache = _load_cache()
    client = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
    if client is None:
        log.warning("mitvergnuegen: kein ANTHROPIC_API_KEY – nur bereits ausgewertete Artikel")

    r = s.get(FEED, timeout=20)
    r.raise_for_status()
    new = 0
    for item in ET.fromstring(r.content).iter("item"):
        url = (item.findtext("link") or "").strip()
        if not url or url in cache:
            continue
        if client is None:
            continue
        pub = item.findtext("pubDate")
        published = parsedate_to_datetime(pub) if pub else datetime.now(TZ)
        title = (item.findtext("title") or "").strip()
        time.sleep(DELAY_S)
        try:
            text = _article_text(s, url)
            extracted = _extract(client, title, published, text)
        except Exception as exc:  # noqa: BLE001 – ein Artikel darf den Lauf nicht abbrechen
            log.warning("mitvergnuegen: %s übersprungen (%s)", url, exc)
            continue
        cache[url] = {"title": title, "published": published.isoformat(),
                      "events": [e.model_dump() for e in extracted]}
        new += 1
        log.info("mitvergnuegen: %d Termine aus „%s“", len(extracted), title)

    _prune(cache)
    _save_cache(cache)

    today = datetime.now(TZ).date()
    horizon = today + timedelta(days=days)
    out: List[Event] = []
    for article_url, entry in cache.items():
        for raw in entry["events"]:
            ev = _to_event(ExtractedEvent(**raw), article_url)
            if ev and ev.start.date() <= horizon and (ev.end or ev.start).date() >= today:
                out.append(ev)
    log.info("mitvergnuegen: %d neue Artikel ausgewertet, %d Events im Zeitfenster", new, len(out))
    return out


def _article_text(s: requests.Session, url: str) -> str:
    r = s.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    art = soup.select_one("article.single") or soup.find("article") or soup.body
    for tag in art.select("script, style, noscript, aside, form"):
        tag.decompose()
    # Links mit Ziel behalten, damit Claude Ticket-Links zuordnen kann
    for a in art.find_all("a", href=True):
        a.replace_with(f"{a.get_text(' ', strip=True)} <{a['href']}>")
    return art.get_text("\n", strip=True)


def _extract(client: anthropic.Anthropic, title: str, published: datetime, text: str) -> List[ExtractedEvent]:
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": _schema()},
        },
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Titel: {title}\nVeröffentlicht: {published:%Y-%m-%d}\n\n<artikel>\n{text}\n</artikel>",
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude hat die Anfrage abgelehnt")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Antwort abgeschnitten (max_tokens)")
    raw = next(b.text for b in response.content if b.type == "text")
    return Extraction.model_validate_json(raw).events


def _schema() -> dict:
    """JSON-Schema für strukturierte Ausgabe: alle Felder required, keine Zusatzfelder."""
    nullable = {"type": ["string", "null"]}
    props = {f: ({"type": "string"} if f not in ("end_date", "start_time", "end_time") else nullable)
             for f in ExtractedEvent.model_fields}
    return {
        "type": "object",
        "properties": {"events": {"type": "array", "items": {
            "type": "object", "properties": props,
            "required": list(props), "additionalProperties": False}}},
        "required": ["events"],
        "additionalProperties": False,
    }


def _to_event(x: ExtractedEvent, article_url: str) -> Optional[Event]:
    try:
        sd = date.fromisoformat(x.start_date)
        ed = date.fromisoformat(x.end_date) if x.end_date else None
        st = dtime.fromisoformat(x.start_time) if x.start_time else None
        et = dtime.fromisoformat(x.end_time) if x.end_time else None
    except ValueError:
        return None
    all_day = st is None
    if all_day:
        start = datetime.combine(sd, dtime.min, TZ)
        end = datetime.combine(ed or sd, dtime.min, TZ)
    else:
        start = datetime.combine(sd, st, TZ)
        end = datetime.combine(ed or sd, et, TZ) if et else None
        if end and end <= start:
            end += timedelta(days=1)  # z. B. 22:00–04:00
    key = hashlib.sha1(f"{article_url}|{x.title}|{x.start_date}".encode()).hexdigest()[:16]
    desc = "\n".join(p for p in (x.description, f"Preis: {x.price}" if x.price else "",
                                 f"Artikel: {article_url}") if p)
    return Event(
        uid=f"mitvergnuegen:{key}",
        source="Mit Vergnügen",
        title=x.title,
        start=start,
        end=end,
        url=x.url or article_url,
        venue=x.venue,
        address=x.address,
        description=desc,
        all_day=all_day,
    )


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def _prune(cache: dict) -> None:
    """Alte Artikel entfernen – aber nur, wenn keiner ihrer Termine noch bevorsteht."""
    cutoff = datetime.now(TZ) - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    today = datetime.now(TZ).date().isoformat()

    def still_relevant(entry: dict) -> bool:
        return any((e.get("end_date") or e.get("start_date") or "") >= today for e in entry["events"])

    for url in [u for u, e in cache.items()
                if datetime.fromisoformat(e["published"]) < cutoff and not still_relevant(e)]:
        del cache[url]
