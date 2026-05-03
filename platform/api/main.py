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

    # CORS — allow all for development, restrict in production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    return app


# For `uvicorn api.main:app`
app = create_app()
