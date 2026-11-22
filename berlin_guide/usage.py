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
