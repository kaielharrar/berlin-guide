"""Gemeinsame Claude-Extraktion: Termine aus Fließtext (Artikel, Eventseiten).

Genutzt von Mit Vergnügen und den privaten Sitemap-Quellen. Ergebnisse werden
pro Seite in einer JSON-Datei unter cache/ gespeichert.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import anthropic
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel

from . import usage
from .models import Event

MODEL = "claude-sonnet-5"
TZ = ZoneInfo("Europe/Berlin")
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
MAX_TEXT = 30000

SYSTEM = """Du extrahierst Veranstaltungen in Berlin aus Webseiten (Magazinartikel oder Eventseiten).
Gib nur Einträge zurück, die einen konkreten Termin haben (Datum, ggf. Zeitraum). Orte ohne Datum
(Restaurants, Cafés, Bars, „schönste Orte“) und reine Archiv-/Übersichtsseiten ohne Termine sind
keine Veranstaltungen – dann eine leere Liste.
Regeln:
- Datumsangaben ohne Jahr beziehen sich auf das Jahr des Stands-Datums bzw. das nächste passende Datum danach.
- Mehrere Termine desselben Angebots: je Termin ein Eintrag.
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


def client_or_none() -> Optional[anthropic.Anthropic]:
    return anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None


def page_text(s: requests.Session, url: str, selector: str = "") -> str:
    r = s.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    root = (soup.select_one(selector) if selector else None) or soup.find("main") or soup.find("article") or soup.body
    for tag in root.select("script, style, noscript, nav, header, footer, aside, form"):
        tag.decompose()
    # Links mit Ziel behalten, damit Claude Ticket-Links zuordnen kann
    for a in root.find_all("a", href=True):
        a.replace_with(f"{a.get_text(' ', strip=True)} <{a['href']}>")
    return root.get_text("\n", strip=True)[:MAX_TEXT]


def extract(client: anthropic.Anthropic, title: str, as_of: datetime, text: str,
            model: str = MODEL) -> List[ExtractedEvent]:
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _schema()}},
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Titel: {title}\nStand: {as_of:%Y-%m-%d}\n\n<seite>\n{text}\n</seite>",
        }],
    )
    usage.record(response, "extract")
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude hat die Anfrage abgelehnt")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Antwort abgeschnitten (max_tokens)")
    raw = next(b.text for b in response.content if b.type == "text")
    return Extraction.model_validate_json(raw).events


def _schema() -> dict:
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


def to_event(x: ExtractedEvent, page_url: str, source: str, uid_prefix: str,
             private: bool = False) -> Optional[Event]:
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
    key = hashlib.sha1(f"{page_url}|{x.title}|{x.start_date}|{x.start_time}".encode()).hexdigest()[:16]
    desc = "\n".join(p for p in (x.description, f"Preis: {x.price}" if x.price else "",
                                 f"Seite: {page_url}") if p)
    return Event(uid=f"{uid_prefix}:{key}", source=source, title=x.title, start=start, end=end,
                 url=x.url or page_url, venue=x.venue, address=x.address, description=desc,
                 all_day=all_day, private=private)


def in_window(ev: Event, days: int) -> bool:
    today = datetime.now(TZ).date()
    return ev.start.date() <= today + timedelta(days=days) and (ev.end or ev.start).date() >= today


def load_cache(name: str) -> dict:
    try:
        return json.loads((CACHE_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(name: str, cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / name).write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
