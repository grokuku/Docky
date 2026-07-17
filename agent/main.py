"""Docky Agent — FastAPI application entry point."""

from fastapi import FastAPI

from agent.routes import router as agent_router

app = FastAPI(title="Docky Agent", version="1.0.0")
app.include_router(agent_router)


@app.get("/")
def root():
    return {"service": "Docky Agent", "status": "running"}