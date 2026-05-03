"""Pydantic schemas for API request/response validation."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Requests ────────────────────────────────────────────────────


class ScanRequest(BaseModel):
    """Request to start a batch scan."""

    booking_ids: list[str] = Field(..., min_length=1, max_length=100)
    cruise_line: str = Field(default="ESPRESSO", pattern="^(ESPRESSO|NCL)$")


class StopScanRequest(BaseModel):
    """Request to stop a running scan."""

    job_id: str


class ExportRequest(BaseModel):
    """Request to export results as CSV."""

    job_id: Optional[str] = None
    cruise_line: Optional[str] = None


# ── Responses ───────────────────────────────────────────────────


class BookingResponse(BaseModel):
    booking_id: str
    cruise_line: str
    status: str
    net_saving: float = 0
    old_total: float = 0
    new_total: float = 0
    confidence: int = 0
    price_category: Optional[str] = None
    new_price_category: Optional[str] = None
    note: str = ""
    error: Optional[str] = None
    lost_pkg_names: list[str] = Field(default_factory=list)
    checked_at: Optional[str] = None


class ScanJobResponse(BaseModel):
    job_id: str
    status: str
    cruise_line: str
    progress_done: int = 0
    progress_total: int = 0
    current_booking_id: Optional[str] = None
    results: list[BookingResponse] = Field(default_factory=list)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class PriceHistoryEntry(BaseModel):
    total: float
    category: Optional[str] = None
    cruise_line: str
    checked_at: Optional[str] = None


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    uptime_seconds: float
