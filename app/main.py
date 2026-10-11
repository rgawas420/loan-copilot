import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.accounts import router as accounts_router
from app.api.billing import router as billing_router
from app.api.ads import router as ads_router
from app.api.kb_admin import router as kb_admin_router
from app.api.rag_admin import router as rag_admin_router
from app.api.routes import router
from app.core.config import BASE_DIR, get_settings
from app.core.logging import setup_logging
from app.services import accounts, ads, billing, feedback, rag_debug, recommendations, usage
from app.services.audit import init_db

setup_logging()
init_db()
accounts.init_db()
billing.init_db()
usage.init_db()
rag_debug.init_db()
feedback.init_db()
ads.init_db()
recommendations.init_db()
log = logging.getLogger(__name__)


def _warmup():
    """Load the embedding model and build the KB in the background so the first search is not slow.
    With Pinecone, also sync the Urban Money folder (new / changed / deleted files); the in-memory store
    loads that folder while it is built."""
    try:
        from app.services import aliases
        aliases.load()                                     # admin-added bank / product / city names
    except Exception:
        log.exception("Could not load name aliases")
    try:
        from app.rag.vectorstore import get_vectorstore, is_pinecone
        get_vectorstore()
        from app.rag import rerank
        rerank.warmup()                                    # before the sync, which can take minutes
        report = None
        if is_pinecone() and get_settings().kb_sync_on_startup:
            from app.services import knowledge_base
            report = knowledge_base.sync()
        log.info("Knowledge base ready")
        from app.services import kb_admin, kb_staging
        if kb_staging.open_ids():                          # uploads whose check was cut short by a restart
            kb_admin.enqueue("stage", kb_staging.open_ids())
        # Golden tests: re-run when the configuration (models, prompt, reranker, chunking...) or documents changed.
        if report and (report.get("indexed") or report.get("removed")):
            rag_debug.autorun_after_documents_changed(report)
        else:
            rag_debug.autorun_if_config_changed()
    except Exception:
        log.exception("Warm-up failed; the KB will load on first use")


@asynccontextmanager
async def lifespan(_app):
    import anyio.to_thread
    anyio.to_thread.current_default_thread_limiter().total_tokens = get_settings().threadpool_size
    if get_settings().warmup:
        threading.Thread(target=_warmup, daemon=True).start()
    yield


app = FastAPI(title="Urban Money Loan Copilot", lifespan=lifespan)
app.include_router(router)
app.include_router(accounts_router)
app.include_router(billing_router)
app.include_router(kb_admin_router)
app.include_router(rag_admin_router)
app.include_router(ads_router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# React app (frontend/, built with `npm run build`) is served at / when its build exists.
# The original HTML/JS page stays available at /classic and is the fallback when there is no build.
REACT_DIST = BASE_DIR / "frontend" / "dist"
if (REACT_DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=REACT_DIST / "assets"), name="react-assets")
if (REACT_DIST / "icons").is_dir():
    app.mount("/icons", StaticFiles(directory=REACT_DIST / "icons"), name="react-icons")

# Progressive web app files (frontend/public, copied into the build). The service worker must be served from the
# root to control the whole app, and never from a stale HTTP cache, or a deploy would not reach installed apps.
NO_CACHE = {"Cache-Control": "no-cache"}
PWA_FILES = {"sw.js": "text/javascript", "manifest.webmanifest": "application/manifest+json",
             "offline.html": "text/html", "favicon.ico": "image/x-icon"}


@app.get("/classic")
def classic(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/")
def index(request: Request):
    if (REACT_DIST / "index.html").exists():
        return FileResponse(REACT_DIST / "index.html", headers=NO_CACHE)     # always the current build's assets
    return templates.TemplateResponse(request, "index.html")


# Last: a catch-all path segment must not shadow the routes above (/classic, /docs, ...).
@app.get("/{name}", include_in_schema=False)
def pwa_file(name: str):
    path = REACT_DIST / name
    if name not in PWA_FILES or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type=PWA_FILES[name], headers=NO_CACHE if name != "favicon.ico" else None)
