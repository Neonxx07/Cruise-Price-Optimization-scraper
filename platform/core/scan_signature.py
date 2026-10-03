"""Identify a scan REQUEST, so the same one is not run twice in a row.

Neon 2026-10-01: *"the problem is the script is still scanning i do not
want it to scan because it is the same list it should not scan again it
should gave me the same results and that these bookings were already
scanned ... at least in a frame of 2 hours."*

WHAT ALREADY EXISTED, AND WHY IT WAS NOT ENOUGH. `CacheService` is a
PER-BOOKING freshness cache: the scan starts, opens a browser, walks the
list and skips individual bookings that were checked recently. That is the
right layer for a partially-changed list, and it stays.

It cannot answer the question Neon is asking, which is about the REQUEST,
not the booking: "I pressed Start on the same 723 bookings I scanned forty
minutes ago - don't open a browser at all." Nothing modelled the request,
so every Start was a fresh run by definition.

THE SIGNATURE is deterministic and order-independent:

  * booking ids are stripped, de-duplicated and SORTED, so the same set
    pasted in a different order is the same scan;
  * the cruise line is part of it - booking numbers are only unique within
    a portal;
  * so is every parameter that MATERIALLY changes what a scan does.
    `bypass_cache` ("Force live recheck") is the important one: a forced
    re-check is a different request from an ordinary scan and must never
    be suppressed by one.

WHAT IS DELIBERATELY NOT IN IT: anything cosmetic. Including, say, a window
size or a sort order would make every run unique and quietly disable the
whole feature - the classic way a cache key stops working.
"""

from __future__ import annotations

import hashlib
import json

#: Bumped only if the signature's MEANING changes, so old rows cannot be
#: matched against a new scheme and wrongly suppress a scan.
SIGNATURE_VERSION = "v1"


def normalise_booking_ids(booking_ids) -> list[str]:
    """The booking set, in canonical form.

    Stripped, blanks dropped, duplicates removed, sorted. Sorting is what
    makes `[a, b]` and `[b, a]` the same scan; de-duplication is what makes
    a list someone pasted twice the same as pasting it once.

    Sorted as STRINGS deliberately - booking ids are identifiers, not
    numbers ("0012" and "12" are different bookings to the portal), and
    numeric sorting would merge them.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in booking_ids or []:
        booking_id = str(raw).strip()
        if booking_id and booking_id not in seen:
            seen.add(booking_id)
            out.append(booking_id)
    return sorted(out)


def scan_signature(cruise_line: str, booking_ids, *,
                   bypass_cache: bool = False, **params) -> str:
    """A stable id for "this exact scan request".

    Two Starts produce the same signature only when they would do the same
    work: same portal, same set of bookings, same materially-relevant
    options. Never raises - an unhashable parameter is rendered with
    `default=str` rather than taking down a scan that was about to start.
    """
    payload = {
        "v": SIGNATURE_VERSION,
        "line": str(cruise_line or "").upper(),
        "bookings": normalise_booking_ids(booking_ids),
        # A forced re-check is a DIFFERENT request. Letting an ordinary
        # scan suppress it would make "Force live recheck" silently do
        # nothing, which is worse than a redundant scan.
        "bypass_cache": bool(bypass_cache),
    }
    for key in sorted(params):
        payload[f"p:{key}"] = params[key]

    rendered = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]


def describe_overlap(previous_ids, requested_ids) -> dict:
    """How a new request differs from a previous one.

    Used when the signatures do NOT match, to say something more useful
    than "this is a different list": how many bookings carry over, how many
    are new, how many were dropped. The per-booking freshness cache then
    skips the carried-over ones inside the run, so a list that gained two
    bookings costs two bookings, not the whole list again.
    """
    previous = set(normalise_booking_ids(previous_ids))
    requested = set(normalise_booking_ids(requested_ids))
    return {
        "requested": len(requested),
        "shared": len(previous & requested),
        "added": len(requested - previous),
        "removed": len(previous - requested),
        "identical": previous == requested and bool(previous),
    }
