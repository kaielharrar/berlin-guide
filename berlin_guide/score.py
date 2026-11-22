"""Persönliche Relevanz: Claude bewertet jedes Event (0–10) anhand des Interessenprofils.

Das Profil ist privat: lokal in config/interests.md (nicht in Git), in GitHub Actions
als Secret INTEREST_PROFILE. Bewertungen werden pro Event + Profil-Version gecacht,
damit jedes Event nur einmal an Claude geht. Geloggt werden nur Zahlen – die
Actions-Logs des öffentlichen Repos sind öffentlich.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import anthropic
from pydantic import BaseModel

from . import usage
from .models import Event

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PROFILE_FILE = ROOT / "config" / "interests.md"
CACHE = ROOT / "cache" / "scores.json"
MODEL = "claude-sonnet-5-5"
BATCH = 25
TZ = ZoneInfo("Europe/Berlin")

INSTRUCTIONS = """Du bewertest Veranstaltungen in Berlin danach, wie gut sie zu der Person passen,
deren Interessenprofil unten steht. Vergib pro Event eine Punktzahl von 0 bis 10:
- 9–10: trifft ein Kerninteresse substanziell, hochwertiges/interaktives Format – unbedingt hingehen
- 7–8: passt klar zu einem Interesse oder ist ein sehr gutes gemeinsames Erlebnis als Paar
- 5–6: angrenzend/inspirierend (die ca. 10 % Horizont-Erweiterung) oder solide, aber unspezifisch
- 0–4: passt nicht, oberflächliches Networking, reine Verkaufsveranstaltung, Kinder-/Familienevent
Bewerte nach Titel, Ort, Veranstalter und Beschreibung; wenn Infos fehlen, schätze vorsichtig.
reason: ein kurzer deutscher Satz (max. 15 Wörter), warum es passt oder nicht.

<interessenprofil>
{profile}
</interessenprofil>"""


class Rating(BaseModel):
    id: str
    score: int
    reason: str


class Ratings(BaseModel):
    ratings: List[Rating]


SCHEMA = {
    "type": "object",
    "properties": {"ratings": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "score": {"type": "integer"},
                       "reason": {"type": "string"}},
        "required": ["id", "score", "reason"], "additionalProperties": False}}},
    "required": ["ratings"],
    "additionalProperties": False,
}


def load_profile() -> Optional[str]:
    text = os.environ.get("INTEREST_PROFILE")
    if not text and PROFILE_FILE.exists():
        text = PROFILE_FILE.read_text(encoding="utf-8")
    return text.strip() if text and text.strip() else None


def score(events: List[Event]) -> bool:
    """Setzt ev.score / ev.score_reason. False, wenn kein Profil oder kein API-Key."""
    profile = load_profile()
    if not profile:
        log.warning("score: kein Interessenprofil – persönlicher Feed wird nicht erzeugt")
        return False
    version = hashlib.sha256(profile.encode()).hexdigest()[:12]
    cache = _load_cache()
    if cache.get("profile_version") != version:
        cache = {"profile_version": version, "scores": {}}  # Profil geändert -> neu bewerten
    scores: Dict[str, dict] = cache["scores"]

    todo = [e for e in events if e.uid not in scores]
    client = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
    if todo and client is None:
        log.warning("score: kein ANTHROPIC_API_KEY – %d Events unbewertet", len(todo))
        todo = []
    system = [{"type": "text", "text": INSTRUCTIONS.format(profile=profile),
               "cache_control": {"type": "ephemeral"}}]
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        try:
            for r in _rate(client, system, batch):
                scores[r["uid"]] = {"score": r["score"], "reason": r["reason"]}
        except Exception as exc:  # noqa: BLE001 – Batch beim nächsten Lauf erneut versuchen
            log.warning("score: Batch %d fehlgeschlagen (%s)", i // BATCH + 1, type(exc).__name__)

    current = {e.uid for e in events}
    cache["scores"] = {k: v for k, v in scores.items() if k in current}
    _save_cache(cache)
    for e in events:
        s = cache["scores"].get(e.uid)
        if s:
            e.score, e.score_reason = s["score"], s["reason"]
    log.info("score: %d neu bewertet, %d gesamt bewertet", len(todo), sum(e.score is not None for e in events))
    return True


def _rate(client: anthropic.Anthropic, system: list, batch: List[Event],
          model: str = MODEL) -> List[dict]:
    ids = {str(n): e for n, e in enumerate(batch)}
    lines = []
    for n, e in ids.items():
        start = e.start.astimezone(TZ)
        when = f"{start:%a %d.%m.}" + ("" if e.all_day else f" {start:%H:%M}")
        parts = [f"[{n}] {e.title}", when, e.venue or e.address, e.organizer, e.price_text,
                 (e.description or "")[:400].replace("\n", " ")]
        lines.append(" | ".join(p for p in parts if p))
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=system,
        messages=[{"role": "user", "content": "Bewerte diese Events:\n" + "\n".join(lines)}],
    )
    usage.record(response, "score")
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(response.stop_reason)
    raw = next(b.text for b in response.content if b.type == "text")
    out = []
    for r in Ratings.model_validate_json(raw).ratings:
        if r.id in ids:
            out.append({"uid": ids[r.id].uid, "score": max(0, min(10, r.score)), "reason": r.reason})
    return out


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
