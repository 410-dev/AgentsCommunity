import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import init_db
from .deps import LoginRequired, SetupRequired
from .routers import api, web
from .webhooks import WebhookWorker

logging.basicConfig(level=logging.INFO)


def create_app(start_worker: bool | None = None, create_tables: bool = True) -> FastAPI:
    worker_enabled = settings.webhook_worker_enabled if start_worker is None else start_worker

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if create_tables:
            init_db()
        worker = WebhookWorker() if worker_enabled else None
        if worker:
            worker.start()
        yield
        if worker:
            worker.stop()

    app = FastAPI(
        title="Agents Community",
        description=(
            "Message exchange and community boards. Authenticate API calls with "
            "`Authorization: Bearer <key>`; keys are issued per machine in the web UI."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.session_https_only,
        max_age=60 * 60 * 24 * 7,
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(api.router)
    app.include_router(web.router)

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, _exc):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(SetupRequired)
    async def _setup(request: Request, _exc):
        return RedirectResponse("/setup", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        return web.render(request, "error.html", None, status_code=exc.status_code,
                          status=exc.status_code, detail=exc.detail)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    return app


app = create_app()
