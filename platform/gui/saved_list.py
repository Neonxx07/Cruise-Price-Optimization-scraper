"""A saved booking list per cruise line - added 2026-10-16.

Neon: "everyday i keep opening copy paste for 4 cruise lines and we are
basically checking the same booking numbers everyday can you make a saving
list for each cruise line and when i enter a new list of booking or a single
booking the gui know if it is duplicated and just does [not] do a warning or
anything but silentley dedplicate".

One plain-text file per line, data/saved_lists/<LINE>.txt, one booking per
line - readable and editable by hand. Duplicates are dropped silently (case
and surrounding spaces ignored, so "zm52l7" and "DEMO41 " are the same
booking). Order is kept: first time seen wins.

Never loses data: writes go to a temp file then replace the original, and a
removal is appended to <LINE>.removed.log with the time, so nothing taken off
the list is lost (CLAUDE.md rule 1).
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "data" / "saved_lists"


def normalize(booking_id: str) -> str:
    return (booking_id or "").strip().upper()


def parse_ids(text: str) -> list[str]:
    """Booking ids from pasted text: commas, semicolons, spaces, tabs or new
    lines all separate. Blank pieces dropped."""
    return [p for p in (normalize(x) for x in re.split(r"[,\s;]+", text or "")) if p]


class SavedList:
    def __init__(self, cruise_line: str, root: Path | str | None = None):
        self.cruise_line = str(cruise_line)
        self.root = Path(root) if root else DEFAULT_ROOT
        self.path = self.root / f"{self.cruise_line}.txt"

    def load(self) -> list[str]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            bid = normalize(line)
            if bid not in seen:
                seen.add(bid)
                out.append(bid)
        return out

    def _write(self, ids: list[str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, ids) -> list[str]:
        """Add ids; return only the ones that were NEW. Silent on duplicates."""
        current = self.load()
        seen = set(current)
        new: list[str] = []
        for raw in ids:
            bid = normalize(raw)
            if bid and bid not in seen:
                seen.add(bid)
                new.append(bid)
        if new:
            self._write(current + new)
        return new

    def remove(self, ids) -> list[str]:
        """Take ids off the list; the removal is logged, never lost."""
        drop = {normalize(i) for i in ids}
        current = self.load()
        removed = [b for b in current if b in drop]
        if removed:
            self._write([b for b in current if b not in drop])
            with open(self.root / f"{self.cruise_line}.removed.log", "a", encoding="utf-8") as fh:
                stamp = datetime.now().isoformat(timespec="seconds")
                fh.writelines(f"{stamp}\t{b}\n" for b in removed)
        return removed

    def __len__(self) -> int:
        return len(self.load())
