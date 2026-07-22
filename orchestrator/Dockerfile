ARG GIT_COMMIT=unknown
ARG VERSION=0.0.1

FROM python:3.12-slim

# Redeclare ARGs after FROM so they are available in this build stage
ARG GIT_COMMIT
ARG VERSION
LABEL git_commit=${GIT_COMMIT}
LABEL version=${VERSION}
RUN echo "${VERSION}-${GIT_COMMIT}" > /app/version.txt

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/

# Le dossier data sera monté en volume
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]