"""Gemeinsames Datenmodell für Events aus allen Quellen."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

# Ticket-Status, so wie ihn die Adapter aus den Quellen ableiten.
SOLD_OUT = "sold_out"          # ausverkauft (evtl. mit Warteliste)
WAITLIST = "waitlist"          # nur noch Warteliste
FEW_LEFT = "few_left"          # wenige Plätze übrig
NEAR_CAPACITY = "near_capacity"
APPROVAL = "approval"          # Anmeldung muss bestätigt werden
OPEN = "open"


@dataclass
class Event:
    uid: str                       # global eindeutig, z. B. "luma:evt-123"
    source: str                    # "luma", ...
    title: str
    start: datetime                # zeitzonenbewusst
    end: Optional[datetime]
    url: str
    venue: str = ""
    address: str = ""
    district: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    organizer: str = ""
    description: str = ""
    image: str = ""
    is_free: Optional[bool] = None
    price_eur: Optional[float] = None
    ticket_status: str = OPEN
    spots_remaining: Optional[int] = None
    guest_count: Optional[int] = None
    has_waitlist: bool = False
    all_day: bool = False          # mehrtägige Events (Festivals, Märkte): nur Datum
    tags: List[str] = field(default_factory=list)

    @property
    def price_text(self) -> str:
        if self.is_free:
            return "kostenlos"
        if self.price_eur is not None:
            return f"{self.price_eur:.0f} €" if self.price_eur == int(self.price_eur) else f"{self.price_eur:.2f} €"
        return ""

    def urgency(self) -> Tuple[int, str]:
        """(Stufe 0–3, Begründung) – wie dringend man sich ein Ticket sichern sollte."""
        s, left = self.ticket_status, self.spots_remaining
        if s == SOLD_OUT:
            return 3, "Ausverkauft – nur noch Warteliste" if self.has_waitlist else "Ausverkauft"
        if s == WAITLIST:
            return 3, "Nur noch Warteliste"
        if left is not None and 0 < left <= 10:
            return 2, f"Nur noch {left} Plätze"
        if s in (FEW_LEFT, NEAR_CAPACITY):
            return 2, "Fast ausgebucht"
        if s == APPROVAL:
            return 1, "Anmeldung mit Freigabe – früh bewerben"
        if self.guest_count and self.guest_count >= 150:
            return 1, f"Beliebt ({self.guest_count} Anmeldungen)"
        return 0, ""

    def worth_securing(self) -> bool:
        """Noch buchbar, aber knapp oder mit Freigabe -> jetzt handeln lohnt sich."""
        if self.ticket_status == SOLD_OUT:
            return False
        level, _ = self.urgency()
        return level >= 2 or self.ticket_status == APPROVAL


@dataclass
class Article:
    """Redaktioneller Tipp (z. B. Mit Vergnügen) – noch kein strukturiertes Event."""
    source: str
    title: str
    url: str
    published: datetime
    summary: str = ""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]
