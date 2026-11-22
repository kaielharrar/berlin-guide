"""Aufruf: python -m berlin_guide [--days 30] [--out out]

Erzeugt out/berlin.ics (Abo-Feed) und out/index.html (Seite mit Abo-Link).
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import curate, ics
from .sources import berlinde, luma, mitvergnuegen

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "berlin-guide/0.1 (private event calendar; +https://github.com/kaielharrar/berlin-guide)"
log = logging.getLogger("berlin_guide")

SOURCES = {
    "luma": luma.fetch,
    "berlin.de": berlinde.fetch,
    "Mit Vergnügen": mitvergnuegen.fetch,
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Berlin Guide – Abo-Kalender erzeugen")
    ap.add_argument("--days", type=int, default=None, help="Zeithorizont in Tagen")
    ap.add_argument("--out", type=Path, default=ROOT / "out")
    ap.add_argument("--profile", type=Path, default=ROOT / "config" / "profile.yaml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    profile = curate.load_profile(args.profile)
    days = args.days or profile.get("horizon_days", 30)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    events, counts = [], {}
    # Jede Quelle einzeln absichern: fällt eine aus, laufen die anderen weiter.
    for name, fetch in SOURCES.items():
        try:
            got = fetch(days=days, session=session)
        except Exception as exc:  # noqa: BLE001
            log.error("Quelle %s fehlgeschlagen: %s", name, exc)
            got = []
        counts[name] = len(got)
        events += got

    events = curate.dedupe(curate.exclude(events, profile))
    curate.tag(events, profile)

    out: Path = args.out
    n = ics.write_feed(out / "berlin.ics", events, "Berlin Guide",
                       "Events in Berlin aus " + ", ".join(SOURCES))
    (out / "index.html").write_text(_landing(n, counts), encoding="utf-8")
    log.info("Fertig: %d Events %s -> %s", n, counts, out / "berlin.ics")
    if n == 0:
        raise SystemExit("Keine Events gefunden – Feed nicht aktualisieren.")


def _landing(n: int, counts: dict) -> str:
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    src = " · ".join(f"{k}: {v}" for k, v in counts.items())
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Berlin Guide</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;background:#faf8f5;color:#1d1b19}}
@media(prefers-color-scheme:dark){{body{{background:#151412;color:#eeeae4}}code{{background:#2a2723!important}}}}
a.btn{{display:inline-block;background:#c2410c;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600}}
code{{background:#f1ece5;padding:2px 6px;border-radius:4px;word-break:break-all}}.m{{opacity:.7;font-size:14px}}</style></head>
<body><h1>Berlin Guide</h1>
<p>{n} Events · täglich aktualisiert</p>
<p><a class="btn" id="sub" href="berlin.ics">📅 Kalender abonnieren</a></p>
<p>Oder URL kopieren: <code id="url">berlin.ics</code></p>
<p class="m">Google Kalender: Weitere Kalender → „Per URL“ → URL einfügen.<br>
Apple Kalender: Ablage → Neues Kalenderabonnement.</p>
<p class="m">Quellen: {src} · Stand {now:%d.%m.%Y %H:%M}</p>
<script>const u=new URL('berlin.ics',location.href);
document.getElementById('url').textContent=u.href;
if(u.protocol==='https:')document.getElementById('sub').href='webcal://'+u.host+u.pathname;</script>
</body></html>"""


if __name__ == "__main__":
    main()
