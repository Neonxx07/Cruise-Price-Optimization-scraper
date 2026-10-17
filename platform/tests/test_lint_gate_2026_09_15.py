"""The lint gate - the cheapest guard this project has.

Neon, 2026-09-15, mid-run: NCL failed on every booking with
"cannot access local variable 'balance_is_all_commission'".

`scraper/ncl.py` read that name three lines above its assignment. ruff
reports it as F821 in MILLISECONDS - and no linter was installed in this
project at all. The 145 NCL unit tests passed the whole time, because they
exercise `calculate_ncl` directly and never run the scraper function that
held the broken ordering.

So the outage was not a hard bug. It was a bug nothing was looking for.

Scope is deliberately narrow (see ruff.toml): "F" is pyflakes - undefined
names, unused imports, broken f-strings - plus a handful of real-defect
bugbear rules. No style rules. A gate that argues about formatting gets
switched off; one that only fires on genuine defects stays on.
"""
import pathlib
import subprocess
import sys

import pytest

PLATFORM = pathlib.Path(__file__).resolve().parent.parent


def _ruff(*args):
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args],
        cwd=PLATFORM, capture_output=True, text=True, timeout=180,
    )


def _have_ruff():
    try:
        return _ruff("--version").returncode == 0
    except Exception:
        return False


requires_ruff = pytest.mark.skipif(
    not _have_ruff(),
    reason="ruff not installed - run: python -m pip install ruff",
)


@requires_ruff
def test_the_project_passes_the_lint_gate():
    """The gate itself. If this fails, read the output - every rule
    selected in ruff.toml is a real defect, not a style opinion."""
    done = _ruff("check", ".")
    assert done.returncode == 0, (
        "lint gate failed:\n" + done.stdout + done.stderr
    )


@requires_ruff
def test_the_gate_catches_the_exact_ncl_outage(tmp_path):
    """THE NEGATIVE CONTROL, and the reason to trust the test above.

    A gate that passes proves nothing unless it is shown to fail on the
    real defect. This is the NCL code as it actually shipped.
    """
    sample = tmp_path / "ncl_bug_sample.py"
    sample.write_text(
        "def check_booking(self, booking_id, logger, amount_due, com_due):\n"
        "    commission_rate = None\n"
        "    logger.info('ncl.commission', booking_id=booking_id,\n"
        "                commission_rate=commission_rate,\n"
        "                balance_is_all_commission=balance_is_all_commission)\n"
        "    balance_is_all_commission = amount_due is not None\n"
        "    return balance_is_all_commission\n",
        encoding="utf-8",
    )
    done = _ruff("check", "--isolated", "--select", "F", str(sample))
    assert done.returncode != 0, "the gate did NOT catch the NCL outage"
    assert "F821" in done.stdout
    assert "balance_is_all_commission" in done.stdout


@requires_ruff
def test_the_gate_catches_a_missing_import(tmp_path):
    """The second shape that bit this session: a name CALLED while its
    import was silently skipped by a faulty patch script. The module
    imported fine; the NameError only fired mid-run, after real work."""
    sample = tmp_path / "missing_import.py"
    sample.write_text(
        "def main():\n"
        "    return msc_occupancy_is_trustworthy({}, None)\n",
        encoding="utf-8",
    )
    done = _ruff("check", "--isolated", "--select", "F", str(sample))
    assert "F821" in done.stdout, done.stdout


@requires_ruff
def test_the_gate_does_not_fire_on_correct_code(tmp_path):
    """It must not cry wolf, or it gets switched off."""
    sample = tmp_path / "fine.py"
    sample.write_text(
        "import json\n"
        "\n"
        "\n"
        "def main(raw):\n"
        "    parsed = json.loads(raw)\n"
        "    return [item for item in parsed if item]\n",
        encoding="utf-8",
    )
    done = _ruff("check", "--isolated", "--select", "F", str(sample))
    assert done.returncode == 0, done.stdout


def test_the_config_exists_and_stays_defect_only():
    """If someone widens this to style rules it will start failing on
    formatting, and the first response will be to disable it - losing the
    F-rules that actually matter."""
    config = (PLATFORM / "ruff.toml").read_text(encoding="utf-8")
    assert 'select = [' in config
    assert '"F",' in config, "pyflakes rules are the whole point"
    for style_rule in ('"E501"', '"W291"', '"D1'):
        assert style_rule not in config, (
            f"{style_rule} is a style rule - keep this gate defect-only"
        )
