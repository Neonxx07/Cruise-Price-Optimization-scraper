"""A local read before it is assigned - the bug that took NCL down entirely.

Neon, 2026-09-15, mid-run: "the ncl is not working at all and it is showing
errors for all the bookings and this line cannot access local variable
'balance_is_all_commission' where it is not associated with a value".

scraper/ncl.py passed `balance_is_all_commission` into a `logger.info(...)`
call three lines ABOVE where it was assigned. Python raises UnboundLocalError
the moment that log line runs, which is before any booking result can be
produced - so EVERY booking failed, not some. The calculation itself was
correct the whole time; one misplaced logging argument took the whole cruise
line offline.

Nothing caught it. The module imports fine, and all 145 NCL unit tests
passed, because they exercise `calculate_ncl` directly and never run the
scraper function that held the broken ordering.
"""
import ast
import pathlib

import pytest

MODULES = [
    "core/calculator.py", "core/calculator_msc.py", "core/price_scope.py",
    "core/models.py", "core/confidence.py", "msc_commands.py",
    "msc_run_calculator.py", "services/booking_service.py",
    "services/msc_live_service.py", "scraper/espresso.py", "scraper/ncl.py",
    "scraper/goccl.py", "scraper/base.py", "gui/windows.py",
]


def _find_use_before_assignment(source: str) -> list[str]:
    """Names whose first READ sits above their first WRITE in source order.

    Deliberately narrow - it models the exact shape that broke NCL (a plain
    assignment below its own use) and nothing cleverer. Three exclusions,
    because without them the first version reported 17 false positives and
    would have been switched off as noise:

      * comprehension targets - `[x for item in items]` puts the element
        expression at or before the target's line;
      * anything a NESTED scope owns, including a nested def's parameters -
        a closure reads its parent's locals by design;
      * a read and write inside the same loop - the read on iteration two
        legitimately precedes the write in source order.
    """
    tree = ast.parse(source)
    loops = [
        (n.lineno, max(getattr(c, "lineno", n.lineno) for c in ast.walk(n)))
        for n in ast.walk(tree)
        if isinstance(n, (ast.For, ast.While, ast.AsyncFor))
    ]

    def in_one_loop(a: int, b: int) -> bool:
        return any(s <= a <= e and s <= b <= e for s, e in loops)

    problems = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        params = {a.arg for a in
                  fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
        for extra in (fn.args.vararg, fn.args.kwarg):
            if extra:
                params.add(extra.arg)

        nested: set[str] = set()
        for inner in ast.walk(fn):
            if inner is fn:
                continue
            if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                if not isinstance(inner, ast.Lambda):
                    nested.add(inner.name)
                a = inner.args
                for arg in a.posonlyargs + a.args + a.kwonlyargs:
                    nested.add(arg.arg)
                for extra in (a.vararg, a.kwarg):
                    if extra:
                        nested.add(extra.arg)
            elif isinstance(inner, ast.ClassDef):
                nested.add(inner.name)
            elif isinstance(inner, ast.comprehension):
                for t in ast.walk(inner.target):
                    if isinstance(t, ast.Name):
                        nested.add(t.id)

        # MUST be a true minimum, not "first seen". ast.walk is
        # breadth-first, so nodes arrive out of source order and
        # setdefault() records whichever line the traversal happened to
        # reach first - which made the scanner both miss the real NCL bug
        # and flag four loop variables that were fine.
        # Names inside a NESTED scope belong to that scope, not this one.
        # A closure defined early and called late legitimately reads a
        # parent local assigned after it - real in scraper/espresso.py,
        # where `_attempt()` reads `api_result` that `retry_async` only
        # assigns on the following line.
        inner_nodes = set()
        for inner in ast.walk(fn):
            if inner is fn:
                continue
            if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                for sub in ast.walk(inner):
                    inner_nodes.add(id(sub))

        first_store: dict[str, int] = {}
        first_load: dict[str, int] = {}
        for n in ast.walk(fn):
            if not isinstance(n, ast.Name) or id(n) in inner_nodes:
                continue
            bucket = (first_store if isinstance(n.ctx, ast.Store)
                      else first_load if isinstance(n.ctx, ast.Load) else None)
            if bucket is None:
                continue
            if n.id not in bucket or n.lineno < bucket[n.id]:
                bucket[n.id] = n.lineno

        for name, store_line in first_store.items():
            if name in params or name in nested:
                continue
            load_line = first_load.get(name)
            if (load_line is not None and load_line < store_line
                    and not in_one_loop(load_line, store_line)):
                problems.append(
                    "{}() {!r}: read at line {}, assigned at line {}".format(
                        fn.name, name, load_line, store_line)
                )
    return problems


# -- the scanner must actually catch the real bug -------------------

_BROKEN = (
    "def check_booking(self, booking_id):\n"
    "    commission_rate = None\n"
    "    logger.info('ncl.commission', booking_id=booking_id,\n"
    "                commission_rate=commission_rate,\n"
    "                balance_is_all_commission=balance_is_all_commission)\n"
    "    balance_is_all_commission = amount_due is not None\n"
    "    return balance_is_all_commission\n"
)

_FIXED = (
    "def check_booking(self, booking_id):\n"
    "    commission_rate = None\n"
    "    balance_is_all_commission = amount_due is not None\n"
    "    logger.info('ncl.commission', booking_id=booking_id,\n"
    "                commission_rate=commission_rate,\n"
    "                balance_is_all_commission=balance_is_all_commission)\n"
    "    return balance_is_all_commission\n"
)


def test_the_scanner_catches_the_exact_ncl_failure():
    """The negative control. Without it, a scanner that finds nothing
    proves nothing - it could simply be broken."""
    found = _find_use_before_assignment(_BROKEN)
    assert any("balance_is_all_commission" in f for f in found), found


def test_the_scanner_accepts_the_fixed_ordering():
    assert _find_use_before_assignment(_FIXED) == []


_CLOSURE = (
    "def outer():\n"
    "    async def attempt():\n"
    "        return api_result.get('data')\n"
    "    api_result = run(attempt)\n"
    "    return api_result\n"
)
_COMPREHENSION = (
    "def f(items):\n"
    "    return [item.name for item in items]\n"
)
_LOOP = (
    "def f(rows):\n"
    "    total = 0\n"
    "    for r in rows:\n"
    "        total = total + r\n"
    "    return total\n"
)
_NESTED_PARAM = (
    "def f(mgr):\n"
    "    def on_state_change(snapshot):\n"
    "        return snapshot.queued\n"
    "    snapshot = mgr.get_snapshot()\n"
    "    return snapshot\n"
)


@pytest.mark.parametrize("legitimate", [
    _CLOSURE, _COMPREHENSION, _LOOP, _NESTED_PARAM,
])
def test_legitimate_patterns_are_not_flagged(legitimate):
    """All four of these really occur in this codebase, and the first
    version of the scanner reported every one of them."""
    assert _find_use_before_assignment(legitimate) == []


# -- the real modules ----------------------------------------------


@pytest.mark.parametrize("rel_path", MODULES)
def test_no_module_reads_a_local_before_assigning_it(rel_path):
    """Runs over every production module, because this bug is invisible to
    both an import check and a unit test: `import scraper.ncl` succeeded,
    and all 145 NCL tests passed, while every real booking was failing."""
    path = pathlib.Path(rel_path)
    if not path.exists():
        pytest.skip(rel_path + " not present")
    problems = _find_use_before_assignment(path.read_text(encoding="utf-8"))
    assert not problems, (
        rel_path + " reads a local before assigning it — UnboundLocalError "
        "at runtime:\n  " + "\n  ".join(sorted(problems))
    )


def test_ncl_assigns_the_commission_flags_before_logging_them():
    """Pins the specific ordering, so a future edit that moves the log line
    back above the assignment fails here by name, rather than by taking the
    whole cruise line offline mid-run."""
    src = pathlib.Path("scraper/ncl.py").read_text(encoding="utf-8")
    assert (src.index("balance_is_all_commission = (")
            < src.index('"ncl.commission"')), (
        "balance_is_all_commission is logged before it is assigned — this is "
        "the 2026-09-15 outage that failed every NCL booking"
    )
