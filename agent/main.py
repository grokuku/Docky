"""Docky Agent — FastAPI application entry point."""

from fastapi import FastAPI

from agent.routes import router as agent_router
from agent.version import get_version


# Version résolue depuis version.txt (source de vérité du dépôt) — voir
# agent/version.py et docs/versioning-unification.md.
app = FastAPI(title="Docky Agent", version=get_version())
app.include_router(agent_router)


@app.get("/")
def root():
    return {"service": "Docky Agent", "status": "running"}