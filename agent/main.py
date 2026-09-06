"""Docky Agent — FastAPI application entry point."""

from fastapi import FastAPI

from agent import dockerhub
from agent.routes import router as agent_router
from agent.version import get_version


# Version résolue depuis version.txt (source de vérité du dépôt) — voir
# agent/version.py et docs/versioning-unification.md.
app = FastAPI(title="Docky Agent", version=get_version())
app.include_router(agent_router)


@app.on_event("startup")
async def startup_event():
    """Réinjecte la config docker persistée (Docker Hub) au démarrage.

    Si ``<data_dir>/.docker/config.json`` existe (credentials poussés par
    l'orchestrateur lors d'une exécution précédente), ``DOCKER_CONFIG`` est
    remis dans l'environnement pour que tous les subprocess docker
    (``docker compose pull``, ``docker pull``…) restent authentifiés après un
    redémarrage de l'agent. Voir docs/dockerhub-auth.md.
    """
    dockerhub.apply_persisted_config()


@app.get("/")
def root():
    return {"service": "Docky Agent", "status": "running"}
