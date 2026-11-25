"""Token-Verbrauch und Kosten der Claude-Aufrufe zählen (nur Zahlen, nichts Inhaltliches)."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict

# $ pro Million Tokens: (Input, Output, Cache-Read, Cache-Write)
PRICES = {
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}

_totals: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))


def record(response, kind: str) -> None:
    """kind: 'extract' oder 'score'. Abrechnung nach dem Modell, das tatsächlich geantwortet hat."""
    u = response.usage
    t = _totals[f"{kind}:{response.model}"]
    t["calls"] += 1
    t["input"] += u.input_tokens or 0
    t["output"] += u.output_tokens or 0
    t["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
    t["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0


def cost(model: str, t: Dict[str, float]) -> float:
    pi, po, pr, pw = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (t["input"] * pi + t["output"] * po + t["cache_read"] * pr + t["cache_write"] * pw) / 1e6


def summary() -> str:
    if not _totals:
        return "Claude: keine Aufrufe"
    parts, total = [], 0.0
    for key, t in sorted(_totals.items()):
        model = key.split(":", 1)[1]
        c = cost(model, t)
        total += c
        parts.append(f"{key} {int(t['calls'])} Aufrufe, {int(t['input'] + t['cache_read'] + t['cache_write'])} in / "
                     f"{int(t['output'])} out Tokens, ${c:.3f}")
    return "Claude: " + "; ".join(parts) + f" | gesamt ${total:.3f}"


def reset() -> None:
    _totals.clear()


def totals() -> Dict[str, Dict[str, float]]:
    return _totals


# --- Budget -------------------------------------------------------------------
# Tagesbudget, abgerechnet über ein rollierendes 7-Tage-Fenster: In 7 Tagen wird höchstens
# 7 × Tagesbudget ausgegeben. So passt auch mal eine lange Artikelseite (≈0,06 $), die allein
# über einem Tagesbudget liegt – dafür wird an den Folgetagen weniger ausgegeben.
# Stand in cache/budget.json (wandert mit dem Actions-Cache). Auslesen darf höchstens
# EXTRACT_SHARE verbrauchen, damit fürs Bewerten neuer Events immer etwas übrig bleibt.
# Was wegen des Budgets liegen bleibt, wird nicht gecacht und später nachgeholt.
import json as _json
from datetime import date as _date, timedelta as _timedelta
from pathlib import Path as _Path

WINDOW_DAYS = 7
MAX_DAY_FACTOR = 1  # Tagesbudget ist harte Obergrenze pro Tag
EXTRACT_SHARE = 0.6
# Geschätzte Kosten eines Aufrufs (Sonnet 5, medium), damit ein Aufruf das Budget nicht überzieht.
ESTIMATE = {"extract": 0.03, "score": 0.015}
_BUDGET_FILE = _Path(__file__).resolve().parent.parent / "cache" / "budget.json"
_budget = {"limit": None, "day": "", "history": {}, "extract_before": 0.0}


class BudgetExceeded(RuntimeError):
    pass


def set_daily_budget(limit_usd) -> None:
    _budget["limit"] = float(limit_usd) if limit_usd else None
    today = _date.today()
    try:
        saved = _json.loads(_BUDGET_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    days = saved.get("days") or ({saved["day"]: {"all": saved.get("spent", 0.0)}} if saved.get("day") else {})
    first = (today - _timedelta(days=WINDOW_DAYS - 1)).isoformat()
    _budget["day"] = today.isoformat()
    _budget["history"] = {d: v for d, v in days.items() if d >= first}


def _run(kind: str = "") -> float:
    return sum(cost(k.split(":", 1)[1], t) for k, t in _totals.items() if k.startswith(kind))


def _window(field: str) -> float:
    return sum(v.get(field, 0.0) for d, v in _budget["history"].items() if d != _budget["day"]) \
        + _budget["history"].get(_budget["day"], {}).get(field, 0.0)


def spent_today() -> float:
    return _budget["history"].get(_budget["day"], {}).get("all", 0.0) + _run()


def allow(kind: str, estimate: float | None = None) -> bool:
    limit = _budget["limit"]
    if limit is None:
        return True
    est = ESTIMATE.get(kind, 0.0) if estimate is None else estimate
    total = WINDOW_DAYS * limit
    if spent_today() + est > MAX_DAY_FACTOR * limit:
        return False
    if kind == "extract":
        return _window("extract") + _run("extract:") + est <= total * EXTRACT_SHARE \
            and _window("all") + _run() + est <= total
    return _window("all") + _run() + est <= total


def save_budget() -> str:
    h = _budget["history"].setdefault(_budget["day"], {})
    h["all"] = round(h.get("all", 0.0) + _run(), 4)
    h["extract"] = round(h.get("extract", 0.0) + _run("extract:"), 4)
    _totals.clear()
    _BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BUDGET_FILE.write_text(_json.dumps({"days": _budget["history"]}))
    limit = _budget["limit"]
    if not limit:
        return f"Budget heute: ${h['all']:.3f} (kein Limit)"
    return (f"Budget heute: ${h['all']:.3f} | letzte {WINDOW_DAYS} Tage: ${_window('all'):.3f} "
            f"von ${WINDOW_DAYS * limit:.3f} (Ø ${limit:.3f}/Tag)")
