"""MSC status match - prepare one customer's Carnival evidence.

Built 2026-10-08 (MSC status-match branch). A human supplies, one at a
time, an MSC booking number and that customer's Carnival Guest Number.
This then:

  1. loads the MSC booking as already scanned - its passenger names and
     dates of birth (data/msc_control/booking_data.jsonl, written by
     msc_commands._lookup_one_booking)
  2. looks the guest up in GoCCL and reads the real Carnival record
  3. screenshots GoCCL's guest window, unedited - that image is the
     evidence MSC asks for
  4. requires the record to match exactly one passenger on the MSC
     booking (name + date of birth, see core.vifp_guest)
  5. only then clicks Add Number and sets State of Residency
  6. appends the outcome to data/status_match/results.jsonl

A record that does not match is reported, with the screenshot kept, and
goes no further. Nothing here submits anything to MSC; the application is
a human's decision.

Captured evidence is never deleted (project hard rule 1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.vifp_guest import CarnivalGuestInfo, match_msc_passenger, normalise_guest_number
from utils.logging import get_logger

logger = get_logger(__name__)

MSC_BOOKING_DATA = Path("data/msc_control/booking_data.jsonl")
EVIDENCE_DIR = Path("data/status_match")


@dataclass
class StatusMatchCandidate:
    msc_booking_id: str
    guest_number: str
    ready: bool
    reason: str
    guest: CarnivalGuestInfo | None = None
    passenger: dict | None = None
    residency_code: str | None = None
    screenshot_path: str | None = None
    msc_discounts: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "msc_booking_id": self.msc_booking_id,
            "guest_number": self.guest_number,
            "ready": self.ready,
            "reason": self.reason,
            "guest": self.guest.as_dict() if self.guest else None,
            "passenger": self.passenger,
            "residency_code": self.residency_code,
            "screenshot_path": self.screenshot_path,
            "msc_discounts": self.msc_discounts,
        }


def latest_msc_booking(booking_id: str, path: Path = MSC_BOOKING_DATA) -> dict | None:
    """The most recent MSC scan of this booking, or None if never scanned."""
    if not path.exists():
        return None
    latest = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("booking_id")) == str(booking_id):
                latest = row
    return latest


async def prepare_status_match(scraper, msc_booking_id: str, guest_number: str, *,
                               msc_record: dict | None = None,
                               evidence_dir: Path = EVIDENCE_DIR) -> StatusMatchCandidate:
    """Run steps 1-6 above for one customer. `scraper` is a started,
    signed-in GoCCLScraper. `msc_record` overrides the stored MSC scan
    (used by tests and by a caller that has just scanned the booking)."""
    msc_booking_id = str(msc_booking_id).strip()
    number = normalise_guest_number(guest_number)
    if not number:
        raise ValueError(f"not a Carnival Guest Number: {guest_number!r}")

    record = msc_record if msc_record is not None else latest_msc_booking(msc_booking_id)
    if record is None:
        raise LookupError(f"MSC booking {msc_booking_id} has not been scanned yet - "
                          f"scan it on MSC first so its passengers are known")
    passengers = record.get("passengers") or []
    discounts = record.get("current_discounts") or []

    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    shot = evidence_dir / f"{msc_booking_id}_{number}_{stamp}.png"

    await scraper.open_vifp_lookup()
    guest = await scraper.lookup_vifp_guest(number, screenshot_path=str(shot))
    match = match_msc_passenger(guest, passengers)

    candidate = StatusMatchCandidate(
        msc_booking_id=msc_booking_id, guest_number=number,
        ready=False, reason=match.reason, guest=guest,
        passenger=match.passenger, screenshot_path=str(shot),
        msc_discounts=discounts,
    )
    if match.passenger is not None:
        candidate.residency_code = await scraper.add_vifp_guest(guest)
        candidate.ready = True

    _record(candidate, evidence_dir)
    log = logger.info if candidate.ready else logger.warning
    log("status_match.prepared", msc_booking_id=msc_booking_id,
        ready=candidate.ready, reason=candidate.reason, tier=guest.tier)
    return candidate


def _record(candidate: StatusMatchCandidate, evidence_dir: Path) -> None:
    entry = {"recorded_at": datetime.now().isoformat(), **candidate.as_dict()}
    with (evidence_dir / "results.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
