# Docker Compose - Référence de syntaxe (2024-2025)

## IMPORTANT
- Le champ `version:` est DEPRÉCIÉ. Ne PAS l'inclure dans les fichiers docker-compose.yml.
- Docker Compose v2 est la version actuelle (plugin `docker compose`, pas `docker-compose`).
- Les fichiers s'appellent `docker-compose.yml` ou `compose.yaml`.

## Structure de base

```yaml
services:
  nom-du-service:
    image: image:tag
    container_name: mon-container
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/data
    environment:
      - KEY=value
    networks:
      - mon-reseau

networks:
  mon-reseau:
    driver: bridge

volumes:
  data:
```

## Options courantes

### Image et build
- `image: nginx:latest` — utilise une image existante
- `build: ./mon-dossier` — build depuis un Dockerfile
- `build: { context: ./mon-dossier, dockerfile: Dockerfile }` — build avancé

### Ports
- `"8080:8080"` — host:container
- `"8080"` — port container seulement (port aléatoire sur l'hôte)
- `"127.0.0.1:8080:8080"` — lié à localhost seulement

### Volumes
- `./data:/data` — montage de dossier local
- `data:/data` — volume nommé
- `./config.yml:/etc/config.yml:ro` — montage en lecture seule

### Environment
- `KEY=value` — format liste
- `{ KEY: value }` — format map
- `env_file: .env` — charge depuis un fichier .env

### Restart policies
- `no` — ne jamais redémarrer (défaut)
- `always` — toujours redémarrer
- `unless-stopped` — redémarrer sauf si arrêté manuellement (RECOMMANDÉ)
- `on-failure` — redémarrer seulement en cas d'erreur

### Healthcheck
```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
  interval: 30s
  timeout: 10s
  retries: 3
```

### Depends on
```yaml
depends_on:
  - service-a
  - service-b
```

### Networks
```yaml
networks:
  frontend:
  backend:
    internal: true  # non accessible depuis l'extérieur
```

### Resource limits
```yaml
deploy:
  resources:
    limits:
      memory: 512M
      cpus: '0.5'
```

### GPU passthrough (pour AI/ML)
```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

## Bonnes pratiques
- Toujours utiliser `restart: unless-stopped` sauf raison spécifique
- Utiliser le tag `latest` par défaut pour permettre les updates via `docker compose pull`
- Utiliser un tag précis (`nginx:1.25`) seulement si l'utilisateur demande une version spécifique ou si la stabilité est critique
- Pour les mises à jour: `docker compose pull` récupère la dernière version du tag configuré
- Utiliser des volumes nommés pour les données persistantes
- Mettre les variables sensibles dans `.env` (avec `env_file`)
- Nommer les containers avec `container_name` pour faciliter la gestion
- Utiliser des networks dédiés pour isoler les services

## Métadonnées Docky

Chaque fichier docker-compose.yml créé par Docky DOIT commencer par un bloc de métadonnées en commentaires:

```yaml
# ============================================
# Docky Stack Metadata
# @name: nom-de-la-stack
# @category: ai|database|monitoring|media|network|security|dev|web|storage|other
# @description: Description courte de ce que fait la stack
# @source: URL du repo ou de la doc (si applicable)
# @hardware: Requirements hardware (ex: "GPU recommended, min 8GB RAM")
# @ports: 8080, 11434
# @created: 2025-01-15
# @updated: 2025-01-15
# ============================================
```

Ces métadonnées sont des commentaires YAML, ignorées par Docker Compose mais utilisées par Docky pour le contexte.