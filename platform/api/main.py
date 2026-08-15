"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from config.settings import settings
from models.database import init_db
from utils.logging import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks."""
    # Startup
    setup_logging(settings.log_level, settings.log_file)
    await init_db()
    yield
    # Shutdown (cleanup if needed)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Cruise booking repricing intelligence system. "
            "Analyzes Royal Caribbean, Celebrity, and Norwegian Cruise Line bookings "
            "to find optimization opportunities."
        ),
        lifespan=lifespan,
    )

    # CONFIRMED REAL RISK, fixed 2026-08-13: this used to be
    # allow_origins=["*"] + allow_credentials=True — see the detailed
    # explanation on settings.cors_allowed_origins. Investigated first
    # (per the fix instructions: don't touch this without checking
    # actual reachability/usage): this API defaults to binding
    # 127.0.0.1 only (settings.api_host), and confirmed nothing in this
    # project's GUI/CLI/extension calls it over HTTP at all — so an
    # empty allow-list (no cross-origin access) changes no existing
    # functionality. Only enables credentialed CORS if origins are ever
    # explicitly configured, since allow_credentials=True is meaningless
    # (and FastAPI/Starlette will refuse it) paired with an empty list.
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(router)

    return app


# For `uvicorn api.main:app`
app = create_app()
