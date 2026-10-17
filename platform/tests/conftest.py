"""Shared pytest setup.

1. Makes the platform package root importable regardless of where pytest
   is invoked from.

2. **Forces every test onto a THROWAWAY database.**

   CONFIRMED REAL DATA CORRUPTION, found 2026-08-27 during the forensic
   review: `tests/test_preflight_and_file_load_2026_08_27.py` drove the
   real `BookingService._run_batch` and tried to neutralise persistence
   with `monkeypatch.setattr(service, "_persist_result", noop,
   raising=False)`. There is no `_persist_result` method — the real one is
   `_save_result_to_db` — and `raising=False` made that silently a no-op.
   The tests therefore wrote to the PRODUCTION `cruise_intel.db`: 78 junk
   `ERROR` rows for booking IDs "A", "B" and "C" (26 of each), polluting
   the same table the savings reports and forensics read from. It also
   made that one test file take 4m22s.

   A wrong method name in one monkeypatch must not be able to reach real
   client data. `settings.database_url` is read by `models/database.py` at
   IMPORT time to build the engine, so the override has to happen here —
   before any test module imports anything that touches the DB — which is
   exactly what a root conftest is for.

   `Settings` is a pydantic BaseSettings (see config/settings.py), so an
   environment variable of the same name wins over the default.
"""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must be set BEFORE config.settings / models.database are imported.
_TEST_DB = os.path.join(
    tempfile.gettempdir(), f"cruiseintel_test_{uuid.uuid4().hex}.db"
)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"

import pytest  # noqa: E402


def _real_db_paths():
    """Absolute paths of the production DB, whatever the cwd is."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return {
        os.path.normcase(os.path.abspath(os.path.join(here, "cruise_intel.db"))),
        os.path.normcase(os.path.abspath("cruise_intel.db")),
    }


@pytest.fixture(scope="session", autouse=True)
def _assert_tests_never_touch_the_real_database():
    """Hard guard: fail the run if the engine is pointed at the real DB.

    Belt AND braces. The env var above is the mechanism; this is the alarm
    that goes off if it ever stops working (a future `.env` file, a
    settings refactor, an engine rebuilt from a literal path). Without it
    the failure mode is silent — which is how 78 junk rows reached
    production in the first place.
    """
    from config.settings import settings

    url = settings.database_url
    assert _TEST_DB.replace("\\", "/") in url.replace("\\", "/"), (
        f"TESTS ARE NOT ISOLATED: settings.database_url is {url!r}, "
        f"expected the throwaway DB at {_TEST_DB!r}. Refusing to run — a "
        f"test suite must never be able to write to cruise_intel.db."
    )
    for real in _real_db_paths():
        assert os.path.normcase(os.path.abspath(_TEST_DB)) != real, (
            "the throwaway DB path resolved to the production database"
        )
    yield
    try:
        if os.path.exists(_TEST_DB):
            os.remove(_TEST_DB)
    except OSError:
        pass  # a Windows file lock on teardown must not fail the suite
