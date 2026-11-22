"""Phase 1: regelbasiertes Taggen, Filtern und Dedupe. (Phase 2: Claude.)"""
from __future__ import annotations

import re
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
    """Gleiches Event auf mehreren Plattformen: gleicher Tag + ähnlicher Titel."""
    seen: Dict[str, Event] = {}
    for ev in sorted(events, key=lambda e: e.start):
        key = f"{ev.start.date()}|{slug(ev.title)[:30]}"
        if key not in seen:
            seen[key] = ev
    return list(seen.values())
