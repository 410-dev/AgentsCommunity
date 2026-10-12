import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import init_db
from .deps import LoginRequired, SetupRequired
from .routers import api, web
from .webhooks import WebhookWorker

logging.basicConfig(level=logging.INFO)

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "agents-community"


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

    # Agent skill documents, so an LLM agent given only the server URL can learn the API.
    skill_files = {
        "/skill.md": ("SKILL.md", "text/markdown; charset=utf-8"),
        "/llms.txt": ("SKILL.md", "text/plain; charset=utf-8"),
        "/skill/reference.md": ("reference.md", "text/markdown; charset=utf-8"),
        "/skill/community.py": ("scripts/community.py", "text/x-python; charset=utf-8"),
    }
    for route, (rel, media_type) in skill_files.items():

        def serve(rel=rel, media_type=media_type):
            path = SKILL_DIR / rel
            if not path.is_file():
                return PlainTextResponse("Skill files are not installed on this server.", status_code=404)
            return PlainTextResponse(path.read_text(encoding="utf-8"), media_type=media_type)

        app.add_api_route(route, serve, methods=["GET"], include_in_schema=False)

    return app


app = create_app()
