"""Phase 1: regelbasiertes Taggen, Filtern und Dedupe. (Phase 2: Claude.)"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List

import yaml

from .models import Event, slug


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
            if not any(_same(ev.title, k.title) for k in kept):
                kept.append(ev)
        out += kept
    return sorted(out, key=lambda e: e.start)


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
