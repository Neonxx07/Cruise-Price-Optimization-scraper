"""Core data models for the Cruise Intelligence System.

All data structures used across the system — booking results, invoice items,
scan jobs. Uses Pydantic for validation and serialization.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ───────────────────────────────────────────────────────


class CruiseLine(str, Enum):
    ESPRESSO = "ESPRESSO"
    NCL = "NCL"


class BookingStatus(str, Enum):
    OPTIMIZATION = "OPTIMIZATION"
    TRAP = "TRAP"
    NO_SAVING = "NO_SAVING"
    ERROR = "ERROR"
    WLT = "WLT"
    CHECKING = "CHECKING"
    PAID_IN_FULL = "PAID_IN_FULL"
    SKIPPED_TODAY = "SKIPPED_TODAY"


class ScanJobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


# ── Invoice ─────────────────────────────────────────────────────


class InvoiceItem(BaseModel):
    """A single line item from a cruise invoice."""

    pax_id: str = ""
    type: str = ""
    name: str = ""
    normalized_name: str = ""
    amount: float = 0.0


class Invoice(BaseModel):
    """Full invoice with line items."""

    items: list[InvoiceItem] = Field(default_factory=list)


# ── NCL Addon ───────────────────────────────────────────────────


class NclAddon(BaseModel):
    """An addon scraped from the NCL summary page."""

    name: str
    qty: int = 1


# ── Booking Result ──────────────────────────────────────────────


class BookingResult(BaseModel):
    """The complete result of analyzing a single booking."""

    cruise_line: CruiseLine
    status: BookingStatus
    note: str = ""
    error: Optional[str] = None

    booking_id: str
    price_category: Optional[str] = None
    new_price_category: Optional[str] = None

    old_total: float = 0.0
    new_total: float = 0.0
    price_drop: float = 0.0
    obc_change: float = 0.0
    net_saving: float = 0.0

    lost_pkg_value: float = 0.0
    lost_pkg_names: list[str] = Field(default_factory=list)
    lost_fares: list[str] = Field(default_factory=list)
    re_addable_fares: list[str] = Field(default_factory=list)
    gained_fares: list[str] = Field(default_factory=list)

    confidence: int = 0
    old_cruise_fare: float = 0.0
    new_cruise_fare: float = 0.0
    fare_change_pct: float = 0.0

    checked_at: datetime = Field(default_factory=datetime.utcnow)


# ── NCL Category ────────────────────────────────────────────────


class NclCategory(BaseModel):
    """A category from the NCL SeaWeb grid (VX._form_12)."""

    category: str
    res_total: float = 0.0
    status: str = ""
    has_availability: bool = False
    cabin_available: int = 0
    current_promo: str = ""
    description: str = ""


# ── Scan Job ────────────────────────────────────────────────────


class ScanJob(BaseModel):
    """Tracks a batch scan request."""

    job_id: str
    booking_ids: list[str]
    cruise_line: CruiseLine
    status: ScanJobStatus = ScanJobStatus.PENDING
    results: list[BookingResult] = Field(default_factory=list)

    progress_done: int = 0
    progress_total: int = 0
    current_booking_id: Optional[str] = None

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Price History ───────────────────────────────────────────────


class PriceSnapshot(BaseModel):
    """A single price check recorded over time."""

    booking_id: str
    cruise_line: CruiseLine
    total: float
    category: Optional[str] = None
    checked_at: datetime = Field(default_factory=datetime.utcnow)
