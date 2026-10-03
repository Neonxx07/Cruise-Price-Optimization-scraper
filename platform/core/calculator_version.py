"""A fingerprint of the calculator's LOGIC, used to expire stale verdicts.

THE PROBLEM, hit twice on 2026-09-30 and again on 2026-10-01. A verdict is
cached for hours (ESPRESSO 12h, GoCCL 24h), and TRAP and NO_SAVING are both
cacheable statuses. So when the calculator changes, every cached row is a
verdict the current code **disagrees with**, and it keeps being served until
the TTL runs out.

Both times the fix was to clear cache entries by hand:

  * the `ALL INC 2PK NRD` double count - 8 entries cleared;
  * booking 3001014, a price INCREASE shown as a green OPTIMIZATION - one
    entry cleared, and the stale row would have been served again.

Doing that by hand is a step someone will forget, and the failure is silent:
the row simply looks fine and is wrong.

WHY A FINGERPRINT RATHER THAN A HAND-BUMPED VERSION. A constant someone has
to remember to increment is the same forgettable step in a different place.
This is derived from the code itself, so it cannot drift.

WHY THE AST RATHER THAN THE FILE BYTES. Hashing the source would change on
every comment and docstring edit - and this codebase comments heavily, with
long incident write-ups that get revised. That would throw away the whole
cache for a typo fix, which is how a safety mechanism earns a reputation for
being annoying and gets switched off. The AST carries the logic and nothing
else: comments are not in it at all, and docstrings are stripped below.

SCOPE. `core/calculator.py` only. It computes every verdict that reaches
this cache. MSC runs through its own service and its own calculator, so
including `calculator_msc.py` would invalidate ESPRESSO and NCL entries for
a change that cannot affect them.

FAILS TOWARD RE-SCANNING. If the fingerprint cannot be computed, it returns
a value that matches nothing, so entries are treated as stale. A redundant
scan costs a page load; a stale verdict costs a client's price drop.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib

from utils.logging import get_logger

logger = get_logger(__name__)

_CALCULATOR = pathlib.Path(__file__).with_name("calculator.py")

#: Length of the stored fingerprint. 12 hex characters is 48 bits - ample
#: for detecting "this changed", and short enough to read in a log line.
_LENGTH = 12


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Remove docstrings so prose edits do not invalidate the cache.

    Comments never reach the AST. Docstrings DO, as the first statement of
    a module, class or function, so they are dropped explicitly - this
    project's docstrings carry long incident histories that are revised
    often and change no behaviour.
    """
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            # Never leave a body empty - that is a SyntaxError shape.
            body[0] = ast.Pass() if len(body) == 1 else None
            if body[0] is None:
                body.pop(0)
    return tree


def compute_fingerprint(path: pathlib.Path | None = None) -> str:
    """Hash the calculator's logic. Never raises.

    Returns "unknown" if the source cannot be read or parsed, which
    matches no stored fingerprint and therefore expires every entry -
    the safe direction.
    """
    source_path = path or _CALCULATOR
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        rendered = ast.dump(_strip_docstrings(tree), annotate_fields=False)
    except Exception as exc:  # noqa: BLE001 - must never break a scan
        logger.warning("calculator_version.unreadable",
                       path=str(source_path), error=str(exc)[:200])
        return "unknown"
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:_LENGTH]


#: Computed once at import. Parsing a 2,000-line file on every cache read
#: would be a real cost on a 723-booking run, and the source cannot change
#: underneath a running process in any way that matters - the loaded code
#: is what produced the verdicts.
CALCULATOR_FINGERPRINT: str = compute_fingerprint()
