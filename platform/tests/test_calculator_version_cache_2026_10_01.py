"""A cached verdict the current calculator disagrees with is not fresh.
(Roadmap P1.2)

THE PROBLEM, hit twice in two days. A verdict is cached for up to 24 hours
(ESPRESSO 12h, NCL 6h, GoCCL 24h) and both TRAP and NO_SAVING are cacheable.
So when the calculator changes, every stored row is an answer the code no
longer gives - and it keeps being served until the TTL runs out. Silently,
looking perfectly normal.

    2026-09-30  ALL INC 2PK NRD double count   8 entries cleared BY HAND
    2026-10-01  booking 3001014, a price INCREASE shown as a green
                OPTIMIZATION                   1 entry cleared BY HAND

Clearing by hand is a step someone will forget, and the failure is invisible.

THE DESIGN. Each entry stores a fingerprint of the calculator's LOGIC; a
mismatch on read is a miss.

  * Derived from the code, not a hand-bumped constant - a constant someone
    must remember to increment is the same forgettable step relocated.
  * Taken from the AST, not the file bytes. This codebase comments heavily
    and revises long incident write-ups; hashing the source would throw the
    whole cache away for a typo, which is how a safety mechanism gets a
    reputation for being annoying and ends up switched off.
  * Fails toward RE-SCANNING. A redundant scan costs a page load; a stale
    verdict costs a client's price drop.
"""

import pathlib

import pytest

from core.calculator_version import CALCULATOR_FINGERPRINT, compute_fingerprint

CALCULATOR = pathlib.Path("core/calculator.py")


@pytest.fixture
def source():
    return CALCULATOR.read_text(encoding="utf-8")


@pytest.fixture
def tmp_py(tmp_path):
    def _write(text, name="c.py"):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path
    return _write


# -- what the fingerprint must and must not notice ----------------------


def test_a_comment_does_not_change_the_fingerprint(source, tmp_py):
    """Otherwise a typo fix discards the entire cache."""
    before = compute_fingerprint(tmp_py(source))
    after = compute_fingerprint(tmp_py(source + "\n# explaining something\n", "d.py"))
    assert before == after


def test_a_docstring_edit_does_not_change_the_fingerprint(tmp_py):
    """This project's docstrings carry incident histories that get revised
    and change no behaviour."""
    a = tmp_py('def f():\n    """One."""\n    return 1\n', "a.py")
    b = tmp_py('def f():\n    """A much longer explanation."""\n    return 1\n', "b.py")
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_a_docstring_only_function_still_parses(tmp_py):
    """Stripping the docstring must not leave an empty body."""
    path = tmp_py('def f():\n    """Only a docstring."""\n', "only.py")
    assert compute_fingerprint(path) != "unknown"


def test_a_logic_change_does_change_the_fingerprint(source, tmp_py):
    before = compute_fingerprint(tmp_py(source))
    changed = source.replace("def round2(", "def round2_renamed(", 1)
    assert compute_fingerprint(tmp_py(changed, "d.py")) != before


def test_a_changed_constant_changes_the_fingerprint(tmp_py):
    """The 3001014 bug was a threshold, not a function name."""
    a = tmp_py("X = 1.0\n", "a.py")
    b = tmp_py("X = 2.0\n", "b.py")
    assert compute_fingerprint(a) != compute_fingerprint(b)


def test_an_unreadable_calculator_expires_everything(tmp_path):
    """Fails toward re-scanning, never toward serving a stale verdict."""
    assert compute_fingerprint(tmp_path / "missing.py") == "unknown"


def test_unknown_matches_no_real_fingerprint():
    assert CALCULATOR_FINGERPRINT != "unknown"


def test_the_live_fingerprint_is_a_short_stable_hash():
    assert len(CALCULATOR_FINGERPRINT) == 12
    assert CALCULATOR_FINGERPRINT == compute_fingerprint()


# -- the cache honours it ----------------------------------------------


@pytest.fixture
def cache(tmp_path, monkeypatch):
    import models.database as db
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _build():
        async with engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all)
        monkeypatch.setattr(db, "async_session", factory)
        import services.cache_service as mod
        monkeypatch.setattr(mod, "async_session", factory)
        return mod.CacheService()
    return _build


@pytest.mark.asyncio
async def test_a_fresh_entry_from_this_calculator_is_served(cache):
    service = await cache()
    await service.set_result("ESPRESSO", "b1", status="NO_SAVING",
                             payload={"old_total": 100.0})

    found = await service.get_many("ESPRESSO", ["b1"])
    assert "b1" in found
    assert found["b1"]["status"] == "NO_SAVING"


@pytest.mark.asyncio
async def test_an_entry_from_a_different_calculator_is_a_miss(cache, monkeypatch):
    service = await cache()
    await service.set_result("ESPRESSO", "b1", status="NO_SAVING",
                             payload={"old_total": 100.0})

    # The calculator changes under the cache.
    import services.cache_service as mod
    monkeypatch.setattr(mod, "CALCULATOR_FINGERPRINT", "ffffffffffff")

    assert await service.get_many("ESPRESSO", ["b1"]) == {}


@pytest.mark.asyncio
async def test_an_entry_predating_the_fingerprint_is_a_miss(cache):
    """Rows written before this existed carry no `calc` key. Nothing knows
    which calculator produced them, so they expire once."""
    import json

    import models.database as db
    from datetime import datetime, timedelta

    service = await cache()
    async with db.async_session() as session:
        session.add(db.CacheEntry(
            key="cache_ESPRESSO_old",
            value_json=json.dumps({"status": "NO_SAVING", "old_total": 1.0}),
            expires_at=datetime.utcnow() + timedelta(hours=6)))
        await session.commit()

    assert await service.get_many("ESPRESSO", ["old"]) == {}


@pytest.mark.asyncio
async def test_the_write_path_stamps_the_fingerprint(cache):
    import json

    import models.database as db
    from sqlalchemy import select

    service = await cache()
    await service.set_result("ESPRESSO", "b1", status="NO_SAVING", payload={})

    async with db.async_session() as session:
        entry = (await session.execute(
            select(db.CacheEntry).where(
                db.CacheEntry.key == "cache_ESPRESSO_b1"))).scalar_one()
        assert json.loads(entry.value_json)["calc"] == CALCULATOR_FINGERPRINT


@pytest.mark.asyncio
async def test_a_rewrite_refreshes_the_fingerprint(cache, monkeypatch):
    """A re-scan after a calculator change must make the entry usable
    again, not leave it permanently stale."""
    service = await cache()
    await service.set_result("ESPRESSO", "b1", status="NO_SAVING", payload={})

    import services.cache_service as mod
    monkeypatch.setattr(mod, "CALCULATOR_FINGERPRINT", "ffffffffffff")
    assert await service.get_many("ESPRESSO", ["b1"]) == {}

    await service.set_result("ESPRESSO", "b1", status="NO_SAVING", payload={})
    assert "b1" in await service.get_many("ESPRESSO", ["b1"])
