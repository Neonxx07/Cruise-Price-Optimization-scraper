"""FastAPI route definitions."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from api.schemas import (
    BookingResponse,
    ExportRequest,
    HealthResponse,
    PriceHistoryEntry,
    ScanJobResponse,
    ScanRequest,
    StopScanRequest,
)
from config.settings import settings
from core.models import CruiseLine
from services.booking_service import BookingService
from services.csv_export import export_results_csv

router = APIRouter(prefix="/api")

_start_time = time.time()
_booking_service = BookingService()


# ── Health ──────────────────────────────────────────────────────


@router.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return HealthResponse(
        status="ok",
        version=settings.app_version,
        uptime_seconds=round(time.time() - _start_time, 1),
    )


# ── Scan ────────────────────────────────────────────────────────


@router.post("/scan", response_model=ScanJobResponse)
async def start_scan(req: ScanRequest):
    """
    Start a batch scan of booking IDs.

    The scan runs in the background. Poll GET /api/scan/{job_id} for progress.
    """
    cruise_line = CruiseLine(req.cruise_line)
    job = await _booking_service.start_scan(req.booking_ids, cruise_line)
    return _job_to_response(job)


@router.get("/scan/{job_id}", response_model=ScanJobResponse)
async def get_scan(job_id: str):
    """Get the status and results of a scan job."""
    job = _booking_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scan job not found")
    return _job_to_response(job)


@router.post("/scan/stop")
async def stop_scan(req: StopScanRequest):
    """Stop a running scan after the current booking completes."""
    ok = await _booking_service.stop_scan(req.job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not running")
    return {"ok": True, "message": "Stop signal sent"}


# ── Bookings ────────────────────────────────────────────────────


@router.get("/bookings", response_model=list[BookingResponse])
async def list_bookings(cruise_line: str | None = None, limit: int = 100):
    """List all checked bookings from the database."""
    records = await _booking_service.get_all_bookings(cruise_line=cruise_line, limit=limit)
    return records


@router.get("/bookings/{booking_id}", response_model=list[BookingResponse])
async def get_booking(booking_id: str):
    """Get all check results for a specific booking ID."""
    records = await _booking_service.get_all_bookings()
    filtered = [r for r in records if r["booking_id"] == booking_id]
    if not filtered:
        raise HTTPException(status_code=404, detail="Booking not found")
    return filtered


@router.get("/bookings/{booking_id}/history", response_model=list[PriceHistoryEntry])
async def get_price_history(booking_id: str):
    """Get price history over time for a booking."""
    history = await _booking_service.get_price_history(booking_id)
    if not history:
        raise HTTPException(status_code=404, detail="No price history found")
    return history


# ── Export ──────────────────────────────────────────────────────


@router.post("/export/csv")
async def export_csv(req: ExportRequest):
    """Export scan results as CSV."""
    if req.job_id:
        job = _booking_service.get_job(req.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        csv_content = export_results_csv(job.results)
    else:
        raise HTTPException(status_code=400, detail="Provide a job_id")

    return PlainTextResponse(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cruiseintel_export.csv"},
    )


# ── Helpers ─────────────────────────────────────────────────────


def _job_to_response(job) -> ScanJobResponse:
    return ScanJobResponse(
        job_id=job.job_id,
        status=job.status.value,
        cruise_line=job.cruise_line.value,
        progress_done=job.progress_done,
        progress_total=job.progress_total,
        current_booking_id=job.current_booking_id,
        results=[
            BookingResponse(
                booking_id=r.booking_id,
                cruise_line=r.cruise_line.value,
                status=r.status.value,
                net_saving=r.net_saving,
                old_total=r.old_total,
                new_total=r.new_total,
                confidence=r.confidence,
                price_category=r.price_category,
                new_price_category=r.new_price_category,
                note=r.note,
                error=r.error,
                lost_pkg_names=r.lost_pkg_names,
                checked_at=r.checked_at.isoformat() if r.checked_at else None,
            )
            for r in job.results
        ],
        started_at=job.started_at.isoformat() if job.started_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )
