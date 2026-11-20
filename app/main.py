import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db
from .config import get_settings
from .ingest import worker
from .routes import auth as auth_routes
from .routes import chat, health, manuals, notes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("manualagerie")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    db.init_storage()
    if not s.openrouter_api_key:
        log.warning("OPENROUTER_API_KEY is not set: chat and metadata extraction are disabled")
    if s.auth_disabled:
        log.warning("AUTH_DISABLED=true: anyone who can reach this server can use it")
    if s.start_worker:
        worker.start()
    log.info("Manualagerie ready (model=%s, data=%s)", s.openrouter_model, s.data_dir)
    yield
    worker.stop()


def create_app() -> FastAPI:
    s = get_settings()
    auth.check_config()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Manualagerie", lifespan=lifespan, docs_url=None, redoc_url=None)
    # Middleware order: last added runs first, so Session wraps Auth.
    app.add_middleware(auth.AuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=auth.resolve_secret_key(), session_cookie="manualagerie",
                       max_age=60 * 60 * 24 * 90, same_site="lax", https_only=s.app_url.startswith("https"))
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(manuals.router)
    app.include_router(notes.router)
    app.include_router(chat.router)
    return app


app = create_app()
