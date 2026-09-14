"""Docky Agent — FastAPI application entry point."""

from fastapi import FastAPI

from agent import registries, stats_stream
from agent.routes import router as agent_router
from agent.version import get_version


# Version résolue depuis version.txt (source de vérité du dépôt) — voir
# agent/version.py et docs/versioning-unification.md.
app = FastAPI(title="Docky Agent", version=get_version())
app.include_router(agent_router)


@app.on_event("startup")
async def startup_event():
    """Réinjecte la config docker persistée (registres) au démarrage.

    Si ``<data_dir>/.docker/config.json`` existe (credentials poussés par
    l'orchestrateur lors d'une exécution précédente — une clé ``auths`` par
    registre), ``DOCKER_CONFIG`` est remis dans l'environnement pour que tous
    les subprocess docker (``docker compose pull``, ``docker pull``…) restent
    authentifiés après un redémarrage de l'agent. Voir docs/dockerhub-auth.md.

    Démarre aussi l'infrastructure de streaming des stats (writer SQLite +
    sweeper du watch set). Aucun watch n'est restauré au boot : les streamers
    ne reprennent que si un orchestrateur se reconnecte (voir
    docs/stats-streaming.md).
    """
    registries.apply_persisted_config()
    stats_stream.start()


@app.on_event("shutdown")
async def shutdown_event():
    """Arrêt propre : ferme les streamers stats, le sweeper et la persistance."""
    stats_stream.shutdown()


@app.get("/")
def root():
    return {"service": "Docky Agent", "status": "running"}
