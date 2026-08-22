# Défaut utilisé uniquement en build local sans --build-arg ; la CI (release.yml /
# test-build.yml) injecte toujours VERSION depuis version.txt (source de vérité).
ARG GIT_COMMIT=unknown
ARG VERSION=0.0.0

FROM python:3.12-slim

# Redeclare ARGs after FROM so they are available in this build stage
ARG GIT_COMMIT
ARG VERSION
LABEL git_commit=${GIT_COMMIT}
LABEL version=${VERSION}
WORKDIR /app

# Version embarquée : ARG VERSION est injecté au build par la CI depuis
# version.txt (racine du dépôt — source de vérité). On écrit la version SEULE,
# sans suffixe commit, pour que le runtime expose exactement cette version ;
# le hash git reste disponible via le label `git_commit` ci-dessus.
# Lu au runtime par app/version.py → GET /api/version et FastAPI(version=…).
RUN echo "${VERSION}" > /app/version.txt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/

# Le dossier data sera monté en volume
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]