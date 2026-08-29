"""Every page.evaluate() JavaScript body must actually PARSE.

CONFIRMED REAL PRODUCTION BREAKAGE, 2026-08-27. A live NCL scan failed 9
bookings with:

    page.evaluate: SyntaxError: Unexpected token '{'

Cause: brace-heavy JavaScript inside Python f-strings. In an f-string every
literal `{` must be written `{{`, so the SAME helper text needs two
different escapings depending on whether it is injected into an f-string or
a plain string. A `.replace(..., count=1)` put the brace-doubled copy into
the plain-string block, and `{{err: ...}}` reached the browser verbatim.

Two hand-rolled checks BOTH passed while the code was broken, because they
un-escaped the string by hand instead of letting Python render it. This test
renders each argument the way Python really does (via ast) and hands the
result to Node for a genuine parse.

`scraper/ncl.py` no longer uses f-strings for JS at all — plain strings plus
explicit `__PLACEHOLDER__` tokens — so braces mean what they say.
"""
import ast
import os
import subprocess
import sys
import tempfile

import pytest

_NODE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))),
    "Lib", "site-packages", "playwright", "driver", "node.exe",
)

pytestmark = pytest.mark.skipif(
    not os.path.exists(_NODE),
    reason="Playwright's bundled node is required to parse JS",
)

_PLATFORM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRAPERS = [
    os.path.join(_PLATFORM, "scraper", "ncl.py"),
    os.path.join(_PLATFORM, "scraper", "espresso.py"),
    os.path.join(_PLATFORM, "scraper", "goccl.py"),
    os.path.join(_PLATFORM, "scraper", "base.py"),
]


def _known_constants(path):
    """Module-level JS-fragment constants that get .replace()d in."""
    out = {}
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = node.value.value
    return out


def _render(arg, consts):
    """Reproduce exactly what Python hands to page.evaluate."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) else "PLACEHOLDER"
            for v in arg.values
        )
    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) \
            and arg.func.attr == "replace" and len(arg.args) == 2:
        base = _render(arg.func.value, consts)
        if base is None:
            return None
        needle = _render(arg.args[0], consts)
        repl = arg.args[1]
        if isinstance(repl, ast.Name):
            value = consts.get(repl.id, "PLACEHOLDER")
        else:
            value = _render(repl, consts) or "PLACEHOLDER"
        return base.replace(needle, value)
    return None


def _js_blocks():
    for path in _SCRAPERS:
        if not os.path.exists(path):
            continue
        consts = _known_constants(path)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("evaluate", "eval_on_selector")
                    and node.args):
                continue
            js = _render(node.args[0], consts)
            if js and "(" in js:
                yield os.path.basename(path), node.lineno, js


BLOCKS = list(_js_blocks())


def test_there_are_js_blocks_to_check():
    """Guard against the collector silently finding nothing and the whole
    suite passing vacuously — the failure mode of the two checks that
    already gave a false pass here."""
    assert len(BLOCKS) >= 10, f"only found {len(BLOCKS)} JS blocks"


@pytest.mark.parametrize("path,lineno,js", BLOCKS,
                         ids=[f"{p}:{n}" for p, n, _ in BLOCKS])
def test_evaluate_js_parses(path, lineno, js):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write("(async()=>{" + js + "})();")
        tmp = f.name
    try:
        result = subprocess.run([_NODE, "--check", tmp],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (
            f"{path}:{lineno} JavaScript does not parse:\n"
            f"{(result.stderr or '')[:600]}"
        )
    finally:
        os.unlink(tmp)


def test_no_f_string_javascript_in_ncl():
    """f-strings and brace-heavy JS must not be mixed — that is the defect
    itself, not just a symptom."""
    src = open(os.path.join(_PLATFORM, "scraper", "ncl.py"), encoding="utf-8").read()
    assert 'evaluate(f"""' not in src, (
        "an f-string JS body was reintroduced in scraper/ncl.py — use a plain "
        "string with __PLACEHOLDER__ tokens instead"
    )
