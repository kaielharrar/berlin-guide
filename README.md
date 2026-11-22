# Berlin Guide

Ein abonnierbarer Kalender mit Events in Berlin. GitHub Actions sammelt täglich alle Quellen und veröffentlicht `berlin.ics` auf GitHub Pages.

**Abo:** https://kaielharrar.github.io/berlin-guide/ → „Kalender abonnieren“

## Quellen

| Quelle | Weg |
|---|---|
| Luma (lu.ma/berlin) | öffentliche Discover-Liste (JSON) |
| berlin.de/events | RSS + schema.org-Daten der Detailseiten (Festivals, Märkte – ganztägig) |

Nicht möglich: Rausgegangen (robots.txt sperrt Scraper), Resident Advisor (Bot-Schutz).
Neue Quelle = Modul in `berlin_guide/sources/` mit `fetch(days, session) -> List[Event]`, dann in `SOURCES` in `berlin_guide/__main__.py` eintragen.

## Lokal

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m berlin_guide
```

Zeithorizont und Kategorien: `config/profile.yaml`. Manuell neu bauen: Actions → „Kalender aktualisieren“ → Run workflow.
