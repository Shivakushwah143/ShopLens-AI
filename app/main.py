from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import sys

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.api.task_automation_routes import router as automation_router
from app.api.deals_routes import router as deals_router
from app.core.config import get_settings
from app.core.database import init_db
from app.core.logging import configure_logging


if sys.platform.startswith("win"):
    # Required for Playwright subprocess startup in Windows child processes (e.g. reload workers).
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


from app.core.sync_db import init_sync_db, SessionLocal

async def alert_checker_loop():
    from app.agent.deals_tracker.service import DealsService
    
    while True:
        try:
            # Use sync session for background alerts
            with SessionLocal() as session:
                service = DealsService(session)
                triggered = service.check_alerts()
                if triggered:
                    print(f"Triggered {len(triggered)} price alerts!")
        except Exception as e:
            print(f"Alert checker error: {e}")
        await asyncio.sleep(300) # Check every 5 minutes


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(settings.app_env)
    await init_db()       # Async DB for Research/Automation
    init_sync_db()        # Sync DB for Deals Tracker stability
    checker_task = asyncio.create_task(alert_checker_loop())
    try:
        yield
    finally:
        checker_task.cancel()
        try:
            await checker_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.include_router(api_router)
    app.include_router(automation_router)
    app.include_router(deals_router)
    ui_directory = Path(__file__).resolve().parent / "ui"
    app.mount("/ui", StaticFiles(directory=str(ui_directory)), name="ui")
    return app


app = create_app()
