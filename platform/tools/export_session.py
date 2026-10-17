"""Turn a Claude Code session transcript into something a person can read.

A transcript (`~/.claude/projects/<slug>/<session-id>.jsonl`) is mostly
tool calls and tool output - this session's is 172 MB. That is useless as
a handoff: too big to re-read, and the signal is a few hundred messages
buried in it.

This extracts the CONVERSATION - what was asked and what was answered -
and drops the tool traffic.

REDACTION IS NOT OPTIONAL. Transcripts contain whatever was typed into
them, and this project's transcripts contain a real portal password that
was pasted in September. The output of this script goes in the project
folder, so anything that looks like a credential is masked before it is
written. Patterns are deliberately broad; a false positive costs a `****`
in a document, a false negative leaks a live password.

Usage:
    python tools/export_session.py <transcript.jsonl> <out.md>
    python tools/export_session.py --latest <out.md>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"

#: Masked before anything is written. Broad on purpose - see the module
#: docstring.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(?:pass(?:word|wd)?|pwd|secret|token|api[-_ ]?key)\b"
               r"\s*(?:is|:|=)?\s*\S+"),
    re.compile(r"(?i)\bWelcome@\S*"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{10,}"),
    re.compile(r"\bpat-[A-Za-z0-9\-]{10,}\b"),          # HubSpot tokens
    re.compile(r"\bsk-[A-Za-z0-9\-_]{10,}\b"),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _text_of(message: dict) -> str:
    """The human-readable text of one message, or "" for tool traffic."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text", ""))
    return "\n".join(out)


def export(src: Path, dest: Path) -> tuple[int, int]:
    asked = answered = 0
    with (src.open(encoding="utf-8", errors="replace") as fh,
          dest.open("w", encoding="utf-8") as out):
        out.write(f"# Session transcript — {src.stem}\n\n")
        out.write(
            "Conversation only; tool calls and tool output are omitted. "
            "Credentials are masked — see `tools/export_session.py`.\n\n---\n\n")
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _text_of(message).strip()
            if not text:
                continue
            # System reminders and pasted-content wrappers are machinery,
            # not conversation.
            if text.startswith("<system-reminder>") or text.startswith("Caveat:"):
                continue
            text = redact(text)
            if role == "user":
                asked += 1
                out.write(f"## Neon\n\n{text}\n\n")
            else:
                answered += 1
                out.write(f"### Claude\n\n{text}\n\n")
    return asked, answered


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    if argv[0] == "--latest":
        candidates = sorted(PROJECTS.rglob("*.jsonl"),
                            key=lambda p: p.stat().st_mtime)
        if not candidates:
            print(f"no transcripts under {PROJECTS}")
            return 1
        src, dest = candidates[-1], Path(argv[1])
    else:
        src, dest = Path(argv[0]), Path(argv[1])

    if not src.exists():
        print(f"not found: {src}")
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    asked, answered = export(src, dest)
    size_mb = src.stat().st_size / 1024 / 1024
    print(f"{src.name}  ({size_mb:,.1f} MB)")
    print(f"  -> {dest}  ({dest.stat().st_size / 1024:,.0f} KB)")
    print(f"  {asked} asked, {answered} answered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
