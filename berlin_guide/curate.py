"""Phase 1: regelbasiertes Taggen, Filtern und Dedupe. (Phase 2: Claude.)"""
from __future__ import annotations

import os
import re
from datetime import datetime, time as dtime, timedelta
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, List

import yaml

from .models import Event, slug

BERLIN = ZoneInfo("Europe/Berlin")


def load_profile(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def tag(events: List[Event], profile: dict) -> None:
    cats: Dict[str, dict] = profile.get("categories", {})
    for ev in events:
        hay = f" {ev.title} {ev.organizer} {ev.description} ".lower()
        ev.tags = [key for key, c in cats.items()
                   if any(_match(kw.lower(), hay) for kw in c.get("keywords", []))]


def _match(kw: str, hay: str) -> bool:
    if kw != kw.strip():           # Stichwort mit Leerzeichen -> wörtlich suchen
        return kw in hay
    return re.search(rf"\b{re.escape(kw)}\b", hay) is not None


def exclude(events: List[Event], profile: dict) -> List[Event]:
    bad = [k.lower() for k in profile.get("exclude_keywords") or []]
    return [e for e in events if not any(b in e.title.lower() for b in bad)]


def normalize_multiday(events: List[Event]) -> None:
    """Termine mit Uhrzeit über mehr als 48 h (z. B. Markt „16.11.–30.12., 17–22 Uhr“)
    werden ganztägig – sonst blockieren sie den Kalender wochenlang am Stück.
    Die Uhrzeiten bleiben als Öffnungszeiten in der Beschreibung."""
    for ev in events:
        if ev.all_day or not ev.end or ev.end - ev.start <= timedelta(hours=48):
            continue
        s, e = ev.start.astimezone(BERLIN), ev.end.astimezone(BERLIN)
        hours = f"Öffnungszeiten: {s:%H:%M}–{e:%H:%M} Uhr"
        ev.description = f"{hours}\n{ev.description}" if ev.description else hours
        ev.start = datetime.combine(s.date(), dtime.min, BERLIN)
        ev.end = datetime.combine(e.date(), dtime.min, BERLIN)
        ev.all_day = True


def dedupe(events: List[Event]) -> List[Event]:
    """Gleiches Event mehrfach (auch quellenübergreifend): gleicher Starttag und
    ein Titel steckt im anderen oder sie sind sehr ähnlich. Bevorzugt den Eintrag
    mit Uhrzeit, sonst den ausführlicheren."""
    by_day: Dict[object, List[Event]] = {}
    for ev in events:
        by_day.setdefault(ev.start.date(), []).append(ev)
    out: List[Event] = []
    for day_events in by_day.values():
        kept: List[Event] = []
        for ev in sorted(day_events, key=lambda e: (e.all_day, -len(e.description))):
            if not any(_same(ev.title, k.title) or _same_source_variant(ev, k) for k in kept):
                kept.append(ev)
        out += kept
    return sorted(out, key=lambda e: e.start)


def _same_source_variant(a: Event, b: Event) -> bool:
    """Gleiche Quelle, gleicher Tag, Titel beginnen gleich – z. B. eine Workshop-Seite
    und die Programmseite desselben Veranstalters („Surrender“ / „Surrender mit Andy“)."""
    if a.source != b.source:
        return False
    na, nb = _norm(a.title), _norm(b.title)
    short = min(len(na), len(nb))
    common = len(os.path.commonprefix([na, nb]))
    return common >= 8 and (common == short or common >= 15)


def _norm(title: str) -> str:
    return " ".join(slug(title).split("-"))


def _same(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    short, long_ = sorted((na, nb), key=len)
    # Enthaltensein nur bei ausreichend langen Titeln („Yoga“ ≠ „Yoga im Park“)
    if len(short) >= 12 and short in long_:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.9
