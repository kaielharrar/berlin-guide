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


# --- Tagesbudget --------------------------------------------------------------
# Gezählt wird über alle Läufe eines Tages (Stand in cache/budget.json, wandert mit
# dem Actions-Cache). Auslesen darf höchstens EXTRACT_SHARE des Budgets verbrauchen,
# damit fürs Bewerten neuer Events immer etwas übrig bleibt. Was wegen des Budgets
# liegen bleibt, wird nicht gecacht und am nächsten Tag nachgeholt.
import json as _json
from datetime import date as _date
from pathlib import Path as _Path

EXTRACT_SHARE = 0.7
_BUDGET_FILE = _Path(__file__).resolve().parent.parent / "cache" / "budget.json"
_budget = {"limit": None, "day": "", "spent_before": 0.0}


class BudgetExceeded(RuntimeError):
    pass


def set_daily_budget(limit_usd) -> None:
    _budget["limit"] = float(limit_usd) if limit_usd else None
    today = _date.today().isoformat()
    try:
        saved = _json.loads(_BUDGET_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    _budget["day"] = today
    _budget["spent_before"] = float(saved.get("spent", 0.0)) if saved.get("day") == today else 0.0


def spent_today() -> float:
    run = sum(cost(k.split(":", 1)[1], t) for k, t in _totals.items())
    return _budget["spent_before"] + run


def allow(kind: str) -> bool:
    limit = _budget["limit"]
    if limit is None:
        return True
    cap = limit * EXTRACT_SHARE if kind == "extract" else limit
    return spent_today() < cap


def save_budget() -> str:
    _BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BUDGET_FILE.write_text(_json.dumps({"day": _budget["day"], "spent": round(spent_today(), 4)}))
    limit = _budget["limit"]
    return f"Budget heute: ${spent_today():.3f}" + (f" von ${limit:.2f}" if limit else " (kein Limit)")
