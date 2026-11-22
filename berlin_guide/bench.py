"""Kostenmessung: gleiche Stichprobe mit mehreren Modellen, Ausgabe nur als Zahlen.

Aufruf: python -m berlin_guide.bench   (braucht ANTHROPIC_API_KEY und INTEREST_PROFILE)
Bewertet 25 Luma-Events und liest 3 Magazinseiten aus – ohne Cache, ohne Feeds zu schreiben.
"""
from __future__ import annotations

import logging
from datetime import datetime
from statistics import mean

import requests

from . import extract as ex
from . import score, usage
from .__main__ import USER_AGENT
from .sources import luma

MODELS = ["claude-sonnet-5"]  # weitere Modelle nur bewusst ergänzen – jeder Lauf kostet
PAGES = [
    ("https://www.the-berliner.com/berlin/what-to-do-this-weekend-best-events/", ""),
    ("https://www.the-berliner.com/events/whats-on-this-week-daily-tips-berlin/", ""),
    ("https://mitvergnuegen.com/2026/weihnachtsmaerkte-berlin/", "article.single"),
]
log = logging.getLogger("bench")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    events = luma.fetch(days=14, session=s)[:25]
    profile = score.load_profile()
    system = [{"type": "text", "text": score.INSTRUCTIONS.format(profile=profile),
               "cache_control": {"type": "ephemeral"}}]
    texts = []
    for url, sel in PAGES:
        try:
            texts.append(ex.page_text(s, url, sel))
        except Exception as exc:  # noqa: BLE001
            log.info("Seite nicht abrufbar (%s)", type(exc).__name__)
    client = ex.client_or_none()
    results = {}
    for model in MODELS:
        usage.reset()
        ratings = {r["uid"]: r["score"] for r in score._rate(client, system, events, model=model)}
        found = [len(ex.extract(client, "bench", datetime.now(ex.TZ), t, model=model)) for t in texts]
        tot = usage.totals()
        sc = sum(usage.cost(model, t) for k, t in tot.items() if k.startswith("score:"))
        xc = sum(usage.cost(model, t) for k, t in tot.items() if k.startswith("extract:"))
        results[model] = ratings
        log.info("%s: Bewertung $%.4f/Event (%d Events) | Auslesen $%.4f/Seite (%d Seiten, Termine je Seite %s) | %s",
                 model, sc / max(len(ratings), 1), len(ratings), xc / max(len(texts), 1), len(texts), found,
                 usage.summary())
    if len(MODELS) < 2:
        return
    a, b = results[MODELS[0]], results[MODELS[1]]
    common = [u for u in a if u in b]
    if common:
        diffs = [abs(a[u] - b[u]) for u in common]
        agree8 = sum((a[u] >= 8) == (b[u] >= 8) for u in common)
        log.info("Vergleich: mittlere Abweichung %.2f Punkte, gleiche Entscheidung „Für dich“ (≥8) bei %d/%d Events",
                 mean(diffs), agree8, len(common))


if __name__ == "__main__":
    main()
