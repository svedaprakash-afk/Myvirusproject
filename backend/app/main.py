from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import assert_safe_for_production, settings
from .db import Base, SessionLocal, engine
from .api import routes_core, routes_integrations, routes_ops, routes_posting

log = logging.getLogger("cashapp")


@asynccontextmanager
async def lifespan(app: FastAPI):
    assert_safe_for_production()
    from . import migrate

    migrate.ensure_schema(engine, settings.auto_migrate)
    yield


app = FastAPI(title="Cash Application & Receivables Exceptions", version="0.1.0", lifespan=lifespan,
              description="AI-assisted cash application for Indian mid-market corporates. All amounts are integer minor units (paise).")
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"], allow_headers=["*"], allow_credentials=False)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal error"}, status_code=500)


@app.get("/health/ready")
def ready():
    from sqlalchemy import text

    with SessionLocal() as db:
        db.execute(text("select 1"))
    return {"ready": True}


@app.get("/health")
def health():
    return {"status": "ok", "region": settings.deploy_region or None, "residency_strict": settings.residency_strict}


for r in (routes_core.router, routes_integrations.router, routes_ops.router, routes_posting.router):
    app.include_router(r, prefix="/api/v1")
