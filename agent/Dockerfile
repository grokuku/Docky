ARG GIT_COMMIT=unknown
ARG VERSION=0.0.1

FROM python:3.12-slim

# Redeclare ARGs after FROM so they are available in this build stage
ARG GIT_COMMIT
ARG VERSION
LABEL git_commit=${GIT_COMMIT}
LABEL version=${VERSION}
# Installer Docker CLI et docker compose plugin
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        docker-ce-cli \
        docker-compose-plugin \
        git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN echo "${VERSION}-${GIT_COMMIT}" > /app/version.txt

COPY requirements.txt ./agent/
RUN pip install --no-cache-dir -r agent/requirements.txt

COPY . ./agent/

VOLUME ["/data"]

EXPOSE 8080

CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8080"]