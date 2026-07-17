# Docky — Roadmap

## 🎯 Vision

Docky est une plateforme de gestion de stacks Docker Compose multi-serveurs, assistée par LLM. L'architecture est divisée en deux composants :

- **Orchestrateur** : interface web centralisée, chat LLM, API pour agent externe (Discord). Gère plusieurs agents distants.
- **Agent** : service léger déployé sur chaque serveur, avec accès direct à Docker via docker.sock. Expose une API REST sécurisée par clé API.

L'orchestrateur se connecte aux agents, centralise la configuration, et offre une vue globale de l'infrastructure tout en permettant de zoomer sur un serveur spécifique.

---

## 📐 Architecture

```
┌──────────────────────────────────────────┐
│            ORCHESTRATEUR                   │
│  (Web UI + LLM + API Discord)             │
│                                            │
│  Dashboard global ── filtre par agent      │
│  Chat LLM ── voit tous les agents          │
│  Éditeur compose ── proxy vers l'agent     │
│                                            │
│  Cache léger en mémoire (états containers) │
│  Config des agents (URL + clé API)         │
│  SOUL.md (mémoire LLM)                     │
└──────────────────────────────────────────┘
         │ REST API          │ REST API          │ REST API
         ▼                   ▼                    ▼
┌──────────┐         ┌──────────┐         ┌──────────┐
│  AGENT A │         │  AGENT B │         │  AGENT C │
│ Serveur 1│         │ Serveur 2│         │ Serveur 3│
│          │         │          │         │          │
│ docker.sock        │ docker.sock        │ docker.sock
│ /data/stacks/      │ /data/stacks/      │ /data/stacks/
│ API key   │         │ API key  │         │ API key  │
│ :8080     │         │ :8080    │         │ :8080    │
└──────────┘         └──────────┘         └──────────┘
```

### Stack technique

- **Orchestrateur** : Python + FastAPI, HTML/JS/CSS vanilla (frontend)
- **Agent** : Python + FastAPI (service léger, pas d'UI)
- **Communication** : REST API (JSON) + WebSocket pour le temps réel (logs, console, stats)
- **LLM** : Client API compatible OpenAI (Ollama, Deepseek, Ollama Cloud, etc.)
- **Recherche web** : Firecrawl API (search + scrape + map)
- **Stockage Orchestrateur** :
  - `settings.yaml` : paramètres globaux (endpoint LLM, modèle, clé Firecrawl, agents configurés)
  - `users.yaml` : utilisateurs (login + hash bcrypt)
  - `api_keys.yaml` : clés API + whitelist IP (pour agent Discord)
  - `soul.md` : mémoire persistante du LLM
- **Stockage Agent** :
  - `/data/stacks/` : un dossier par stack avec docker-compose.yml + .env + fichiers de config
  - `agent_config.yaml` : clé API de l'agent

### Cache (orchestrateur)

- Cache en mémoire (dict Python) des états des containers et stacks par agent
- Rafraîchi toutes les 5 secondes via polling de l'agent
- Pas de cache pour les fichiers (toujours fetch frais)
- Perdu au redémarrage (acceptable)

---

## 🖥️ Interface Web (Orchestrateur)

### Layout

```
┌──────────────────────────────────┬──────────────────────┐
│  Top bar : Login / Settings /    │                      │
│  Sélecteur d'agent (tous/agent A)│   Éditeur Compose    │
├──────────────────────────────────┤   (colonne droite)   │
│                                  │                      │
│         Dashboard                │   - docker-compose   │
│    Liste des agents (statut)     │     .yml             │
│    Stacks par agent              │   - .env             │
│    Containers + ressources       │   - autres fichiers  │
│    Ports, updates                │                      │
│    Boutons: start/stop/restart/  │   Édition directe    │
│    logs/console                  │   → proxy vers agent │
│                                  │                      │
├──────────────────────────────────┤                      │
│         Chat LLM                 │                      │
│    (texte, vue globale)          │                      │
└──────────────────────────────────┴──────────────────────┘
```

### Dashboard (haut gauche, zone principale)

- **Vue globale** : tous les agents, stacks et containers en une seule vue
- **Filtre par agent** : sélecteur en haut pour n'afficher qu'un agent précis
- **Statut des agents** : indicateur online/offline pour chaque agent
- Pour chaque stack :
  - Nom de la stack + agent sur lequel elle tourne
  - État global (badge coloré: vert=running, rouge=stopped, orange=partial)
  - Containers avec : état, image, CPU/RAM (barres), ports exposés, badge update dispo
  - Boutons: Start, Stop, Restart, Logs, Console, Update

### Éditeur Compose (colonne droite)

- Affichage du docker-compose.yml, .env et autres fichiers de la stack sélectionnée
- Édition directe → proxy vers l'agent pour sauvegarde
- Boutons: Sauvegarder, Sauvegarder & Déployer, Supprimer, Permissions (chmod)
- Création de nouvelle stack (sur l'agent sélectionné)

### Chat LLM (bas gauche)

- Interface texte simple
- Le LLM a accès à l'état de tous les agents en temps réel
- Le LLM peut effectuer des actions Docker sur n'importe quel agent
- Validation humaine requise pour exec dans un container
- Le LLM met à jour soul.md pour les instructions persistantes
- Recherche web via Firecrawl (search + scrape + map, sans restriction)
- SOUL.md éditable manuellement via l'interface

### Authentification

- Page de login (username + mot de passe)
- Session gérée via token JWT (cookie httpOnly)
- Un seul utilisateur prévu dans un premier temps

---

## 🤖 Intégration LLM

### Configuration
- Endpoint configurable (compatible OpenAI API) dans settings.yaml
- Modèle configurable
- Paramètres (temperature, max_tokens, etc.)

### Capacités du LLM

Le LLM reçoit en contexte :
- L'état actuel de tous les agents et leurs containers
- Le contenu de soul.md
- L'historique récent de la conversation

Le LLM peut :
- **Démarrer / arrêter / redémarrer** une stack ou un container (sur n'importe quel agent)
- **Update** : docker compose pull et docker compose up -d
- **Clean** : supprimer les containers/images/volumes inutilisés
- **Créer** une nouvelle stack (docker-compose.yml + .env + n'importe quel autre fichier de config)
- **Modifier** une stack existante (édite le docker-compose.yml + .env + n'importe quel autre fichier)
- **Supprimer** une stack
- **Lire les logs** d'un container
- **Exécuter une commande** dans un container (avec validation humaine)
- **Créer / éditer n'importe quel fichier** dans le dossier d'une stack
- **Gérer les permissions** (chmod) sur les fichiers créés via Python os.chmod()
- **Vérifier les ports utilisés** sur le serveur pour éviter les conflits et proposer des ports alternatifs
- **Rechercher sur le web** via Firecrawl (search + scrape + map, sans restriction de site)
- **Mettre à jour soul.md** quand l'utilisateur donne une directive persistante

### SOUL.md
- Mémoire persistante du LLM
- Mis à jour par le LLM pour les instructions persistantes
- Éditable manuellement via l'interface web

---

## 📡 Agent (service distant)

### Rôle
- Service léger déployé sur chaque serveur
- Expose une API REST sécurisée par clé API
- Accès direct à Docker via docker.sock
- Gère les stacks locales (fichiers stockés sur le serveur)

### Endpoints de l'agent
| Méthode | Route | Description |
|---------|-------|-------------|
| GET | /agent/containers | Liste des containers avec état |
| GET | /agent/containers/{id} | Détails d'un container |
| GET | /agent/containers/{id}/stats | CPU/RAM d'un container |
| GET | /agent/containers/{id}/logs | Logs d'un container (param tail) |
| WS | /agent/containers/{id}/logs/stream | Stream logs temps réel |
| WS | /agent/containers/{id}/exec | Console interactive (exec) |
| POST | /agent/containers/{id}/start | Démarrer un container |
| POST | /agent/containers/{id}/stop | Arrêter un container |
| POST | /agent/containers/{id}/restart | Redémarrer un container |
| GET | /agent/containers/{id}/update-check | Vérif update image |
| GET | /agent/stacks | Liste des stacks |
| GET | /agent/stacks/{name}/files | Liste des fichiers d'une stack |
| GET | /agent/stacks/{name}/files/{filename} | Contenu d'un fichier |
| PUT | /agent/stacks/{name}/files/{filename} | Sauvegarder un fichier |
| POST | /agent/stacks | Créer une stack |
| DELETE | /agent/stacks/{name} | Supprimer une stack |
| POST | /agent/stacks/{name}/deploy | Déployer (down + up -d) |
| POST | /agent/stacks/{name}/start | Démarrer une stack |
| POST | /agent/stacks/{name}/stop | Arrêter une stack |
| POST | /agent/stacks/{name}/restart | Redémarrer une stack |
| PUT | /agent/stacks/{name}/files/{filename}/permissions | Changer chmod |
| GET | /agent/ports | Liste des ports utilisés |

### Authentification
- Clé API configurée dans le docker-compose de l'agent
- Toutes les requêtes doivent inclure Authorization: Bearer {api_key}
- L'orchestrateur connaît la clé API de chaque agent (configuré dans settings.yaml)

### Configuration de l'agent (docker-compose.yml)
```yaml
services:
  docky-agent:
    build: .
    container_name: docky-agent
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./data:/data
    environment:
      - DOCKY_AGENT_API_KEY=your-secret-key-here
      - DOCKY_DATA_DIR=/data
```

---

## 🌐 API REST Orchestrateur (pour agent externe Discord)

### Principe
- API REST simple (JSON), lecture seule
- Consommée par un agent externe (Hermes, OpenClaw) via Discord

### Authentification
- Clé API + whitelist IP avec validation humaine à la première connexion

### Endpoints (phase 1)
| Méthode | Route | Description |
|---------|-------|-------------|
| GET | /api/v1/agents | Liste des agents avec statut |
| GET | /api/v1/containers | Tous les containers (tous agents) |
| GET | /api/v1/stacks | Toutes les stacks (tous agents) |
| GET | /api/v1/agents/{id}/containers | Containers d'un agent spécifique |
| GET | /api/v1/agents/{id}/containers/{cid}/logs | Logs d'un container |

---

## 📁 Gestion des agents (Orchestrateur)

### Configuration
- Les agents sont configurés dans settings.yaml :
```yaml
agents:
  - name: "Serveur Principal"
    url: "http://192.168.1.10:8080"
    api_key: "agent-api-key-1"
  - name: "Serveur Backup"
    url: "http://192.168.1.20:8080"
    api_key: "agent-api-key-2"
```

- L'orchestrateur ping chaque agent régulièrement pour vérifier le statut (online/offline)
- Le statut est affiché dans le dashboard

### Cache
- États des containers et stacks mis en cache en mémoire
- Rafraîchi toutes les 5 secondes
- Pas de cache pour les fichiers (fetch frais à chaque édition)

---

## 🔒 Sécurité

### Points actuels
- Login + mot de passe (hashé bcrypt) pour l'interface web
- JWT pour les sessions web
- Clé API par agent (authentification orchestrator vers agent)
- API key + whitelist IP pour l'agent externe (Discord)
- Validation humaine pour exec dans un container
- Agent et orchestrateur en containers séparés

### Points à définir plus tard
- Rate limiting
- HTTPS géré par reverse proxy externe
- Chiffrement de la communication orchestrator et agent (TLS)
- Limitation des actions possibles par agent

---

## 🗺️ Plan de réalisation

### Phase 1 — Fondations ✅
- [x] Initialiser le projet Python + FastAPI
- [x] Système d'authentification (login + JWT)
- [x] Connexion Docker SDK via docker.sock
- [x] Structure des fichiers de config
- [x] Page de login (HTML/CSS)
- [x] Dockerfile

### Phase 2 — Dashboard ✅
- [x] Liste des stacks et containers
- [x] Affichage de l'état (running/stopped/error)
- [x] Indicateurs de ressources (CPU/RAM)
- [x] Indicateur d'update disponible
- [x] Boutons d'action (start/stop/restart)
- [x] Affichage des logs (statique + stream)
- [x] Console (exec dans container)
- [x] Scan des ports utilisés

### Phase 3 — Éditeur Compose ✅
- [x] Affichage du docker-compose.yml et .env
- [x] Édition directe (textarea)
- [x] Sauvegarde + redéploiement
- [x] Création de nouvelle stack
- [x] Suppression de stack (avec confirmation)
- [x] Gestion des permissions (chmod)

### Phase 4 — Chat LLM ✅
- [x] Client API compatible OpenAI
- [x] Interface de chat (texte)
- [x] Injection du contexte (état containers + soul.md)
- [x] Actions Docker via le LLM
- [x] Validation humaine pour exec dans container
- [x] Mise à jour automatique de soul.md par le LLM
- [x] Édition manuelle de soul.md via l'interface
- [x] Intégration Firecrawl (search + scrape + map)
- [x] Création/édition de fichiers arbitraires par le LLM
- [x] Gestion des permissions (chmod) par le LLM
- [x] Vérification des ports par le LLM

### Phase 5 — Refactoring multi-containers
- [ ] Créer le service Agent (FastAPI léger, API REST sécurisée par clé API)
  - [ ] Endpoints containers (list, start/stop/restart, logs, exec, stats, update-check)
  - [ ] Endpoints stacks (list, files, create/delete, deploy, start/stop/restart, permissions)
  - [ ] Endpoints ports
  - [ ] Authentification par clé API (Bearer token)
  - [ ] Dockerfile + docker-compose pour l'agent
- [ ] Refactorer l'Orchestrateur
  - [ ] Remplacer docker_manager direct par agent_manager (comm avec agents distants)
  - [ ] Gestion multi-agents (config dans settings.yaml)
  - [ ] Cache en mémoire des états (polling 5s)
  - [ ] Dashboard avec vue globale + filtre par agent
  - [ ] Statut online/offline des agents
  - [ ] Proxy de l'éditeur compose vers l'agent
  - [ ] LLM tools adaptés pour cibler un agent précis
  - [ ] Settings : interface pour ajouter/modifier/supprimer des agents
- [ ] Tests de communication orchestrator et agent

### Phase 6 — API Agent externe (Discord)
- [ ] Endpoints REST orchestrateur (agents, containers, stacks, logs)
- [ ] Système de clé API + whitelist IP
- [ ] Validation humaine à la première connexion
- [ ] Documentation de l'API

### Phase 7 — Polish et Sécurité
- [ ] Design final et cohérent
- [ ] Gestion des erreurs et notifications
- [ ] Tests de sécurité
- [ ] Documentation utilisateur
- [ ] Docker Compose pour déployer l'orchestrateur
- [ ] Script d'installation de l'agent (one-liner)

---

## 📝 Notes

- Le projet est hébergé sur GitHub : https://github.com/grokuku/Docky
- Le reverse proxy et le domaine sont déjà prêts (HTTPS géré en externe)
- L'interface doit être moderne mais légère : HTML/JS/CSS vanilla, pas de framework lourd
- L'outil est destiné à un usage personnel dans un premier temps, mais conçu pour pouvoir évoluer
- Les Phases 1-4 ont été réalisées en architecture mono-container et doivent être refactorées en Phase 5
