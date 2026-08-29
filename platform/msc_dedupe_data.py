"""One-off (but re-runnable) cleanup: booking_data.jsonl and
rate_check_data.jsonl accumulate one line per CHECK, not per booking —
re-checking the same booking multiple times (common during iterative bug
fixing) leaves stale duplicate entries behind. msc_run_calculator.py
already reads these with last-write-wins semantics (_load_last_by_id), so
correctness was never at risk — this is purely disk/readability hygiene,
collapsing each file to its latest entry per booking_id.

Deliberately does NOT touch network_capture.jsonl or
live_check_results.jsonl — those are append-only audit logs where every
individual entry is meaningful history (e.g. the DiscountPaxTypeCmd
captures that led to the Voyagers Selection discovery, 2026-08-11), not a
"latest state per booking" snapshot. Deduping those would destroy real
history for no correctness benefit.

Usage: python msc_dedupe_data.py
"""

import json
import os

TARGETS = [
    "data/msc_control/booking_data.jsonl",
    "data/msc_control/rate_check_data.jsonl",
]


def dedupe(path: str) -> None:
    if not os.path.exists(path):
        print(f"{path}: does not exist, skipping")
        return
    with open(path, encoding="utf-8") as f:
        lines = [l for l in (line.strip() for line in f) if l]

    by_id = {}
    for line in lines:
        entry = json.loads(line)
        by_id[entry["booking_id"]] = entry

    with open(path, "w", encoding="utf-8") as f:
        for entry in by_id.values():
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"{path}: {len(lines)} lines -> {len(by_id)} (removed {len(lines) - len(by_id)} stale duplicate(s))")


if __name__ == "__main__":
    for path in TARGETS:
        dedupe(path)
