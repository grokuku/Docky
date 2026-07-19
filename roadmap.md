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
│  Dashboard (bin-packing, cards, couleurs) │
│  Chat LLM (29 tools, Firecrawl, SOUL.md)  │
│  Éditeur Compose (proxy vers l'agent)     │
│  Settings (LLM, agents, mot de passe)     │
│  Popups logs + console                     │
│                                            │
│  Cache en mémoire (états containers)       │
│  Config des agents (URL + clé API)         │
│  SOUL.md (mémoire LLM)                     │
│  compose_reference.md (référence LLM)     │
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
- **Communication** : REST API (JSON) + WebSocket (logs, console — TODO: proxy WS)
- **LLM** : Client API compatible OpenAI (Ollama, Deepseek, Ollama Cloud, etc.)
- **Recherche web** : Firecrawl API (search + scrape + map)
- **Stockage Orchestrateur** :
  - `settings.yaml` : paramètres globaux (endpoint LLM, modèle, clé Firecrawl, agents configurés)
  - `users.yaml` : utilisateurs (login + hash bcrypt)
  - `api_keys.yaml` : clés API + whitelist IP (pour agent Discord)
  - `soul.md` : mémoire persistante du LLM
  - `compose_reference.md` : référence de syntaxe docker-compose pour le LLM (bundlé dans l'image)
- **Stockage Agent** :
  - `/data/stacks/` : un dossier par stack avec docker-compose.yml + .env + fichiers de config
- **Bootstrap** : l'orchestrateur crée les fichiers de config par défaut au démarrage s'ils n'existent pas
- **Images Docker** : publiées sur ghcr.io (multi-arch amd64 + arm64)

### Cache (orchestrateur)

- Cache en mémoire (dict Python) des états des containers et stacks par agent
- Rafraîchi à chaque cycle de refresh du dashboard
- Pas de cache pour les fichiers (toujours fetch frais)

---

## 🖥️ Interface Web (Orchestrateur)

### Layout

Le dashboard est composé de panneaux redimensionnables (click'n'drag, sauvegardé en localStorage) :

```
┌─────────────────────────────┬──────────────────────┐
│  Top bar : Login / Settings │                      │
│  Sélecteur d'agent (tous/A) │  Panel contextuel    │
│  Chat toggle (💬)           │  (apparait au clic    │
├─────────────────────────────┤   sur un container)  │
│                             │                      │
│  Dashboard (bin-packing)    │  - Nom du stack       │
│  Cards de containers        │  - Badge Docky/Ext.  │
│  groupées par stack (couleur)│  - Boutons stack     │
│  Bordure + fond coloré      │  - Éditeur compose   │
│                             │                      │
├─────────────────────────────┤                      │
│  Chat LLM (toggle 💬)       │                      │
└─────────────────────────────┴──────────────────────┘
```

### Dashboard (bin-packing)

- **Algorithme skyline bottom-left** : chaque stack est un bloc rectangulaire (ceil(n/2) × ceil(n/ceil(n/2))), placé sur une grille globale sans trous
- **Tri par taille** pour le packing (gros blocs d'abord, petits remplissent les trous)
- **Ordre d'affichage alphabétique** (stable entre re-renders)
- **Boustrophedon** dans chaque bloc (serpent) pour garantir la connexion entre containers
- **Cards de containers** : nom, statut (dot coloré), image, CPU/RAM (barres), ports, boutons (▶⏹🔄📋🖥)
- **Couleur par stack** : bordure + fond semi-transparent, déterministe (hash du nom)
- **Clic sur un container** : assombrit les autres stacks + affiche le panel contextuel à droite
- **Clic dans le vide** : désélectionne

### Panel contextuel (clic sur container)

- Nom du stack + badge (Docky/Externe/standalone)
- Boutons de commande du stack (▶⏹🔄⬆📥📝)
- Éditeur compose (si managed) avec onglets de fichiers, sauvegarde, déploiement
- Message "Stack externe" si non managed

### Popups (fenêtres séparées)

- **Logs** : fenêtre popup, polling auto toutes les 3s, boutons Pause/Clear/Refresh, select 50-500 lignes
- **Console** : fenêtre popup, input commande, historique (↑/↓), exécution one-shot via API

### Chat LLM (toggle 💬)

- Interface texte simple, peut être masqué pour gagner de la place
- Le LLM a accès à l'état de tous les agents en temps réel
- 29 tools disponibles (voir section LLM)
- Validation humaine pour exec dans un container et clean_agent
- Tool calls visibles ("🔧 Actions effectuées: ...")
- SOUL.md éditable via l'interface
- Warning : recommandation d'utiliser un LLM local

### Page Settings

- **Configuration LLM** : endpoint, API key (masquée), modèle (dropdown avec scan des modèles disponibles), clé Firecrawl, bouton tester
- **Agents** : liste des agents (statut online/offline), ajouter/modifier/supprimer, bouton tester
- **Sécurité** : changement de mot de passe (ancien + nouveau + confirmation)
- **Warning LLM local** : bandeau jaune recommandant un LLM local pour éviter les fuites de données

### Authentification

- Page de login (username + mot de passe)
- Session via token JWT (cookie httpOnly, 24h)
- Un seul utilisateur prévu dans un premier temps
- Mot de passe changeable dans Settings

---

## 🤖 Intégration LLM

### Configuration
- Endpoint configurable (compatible OpenAI API) dans settings.yaml
- Modèle configurable (dropdown avec scan automatique des modèles disponibles)
- Paramètres (temperature, max_tokens, etc.)
- Scan des modèles via GET /v1/models de l'API

### Tools du LLM (29 tools)

**Containers :**
- start_container(agent_name, container_id)
- stop_container(agent_name, container_id)
- restart_container(agent_name, container_id)
- get_container_details(agent_name, container_id)
- get_container_stats(agent_name, container_id)
- get_container_logs(agent_name, container_id, tail)
- exec_in_container(agent_name, container_id, command) — ⚠️ validation humaine
- list_containers(agent_name) — agent_name="all" pour tous les agents

**Stacks :**
- start_stack(agent_name, stack_name)
- stop_stack(agent_name, stack_name)
- restart_stack(agent_name, stack_name)
- update_stack(agent_name, stack_name) — docker compose pull + up -d
- deploy_stack(agent_name, stack_name) — docker compose down + up -d
- create_stack(agent_name, name, compose_content, env_content)
- modify_stack_file(agent_name, stack_name, filename, content)
- delete_stack(agent_name, stack_name)
- get_stack_files(agent_name, stack_name)
- read_stack_file(agent_name, stack_name, filename)
- get_stack_status(agent_name, stack_name)

**Fichiers :**
- set_file_permissions(agent_name, stack_name, filename, mode)

**Ports :**
- get_used_ports(agent_name)
- check_ports_available(agent_name, ports)

**Maintenance :**
- clean_agent(agent_name) — docker system prune — ⚠️ validation humaine

**Web :**
- web_search(query) — Firecrawl search
- web_scrape(url) — Firecrawl scrape
- web_map(url) — Firecrawl map

**Référence :**
- read_compose_reference() — lit compose_reference.md
- read_soul() — lit soul.md
- update_soul(content) — met à jour soul.md

### SOUL.md
- Mémoire persistante du LLM
- Mis à jour par le LLM pour les instructions persistantes
- Éditable manuellement via l'interface web

### compose_reference.md
- Documentation de référence pour la création de docker-compose.yml
- Bundlé dans l'image Docker (dans app/)
- Copié vers /data/ au premier démarrage (bootstrap)
- Règles : pas de champ `version:` (déprécié), tag `latest` par défaut, métadonnées Docky obligatoires

### Métadonnées Docky dans les compose

Chaque docker-compose.yml créé par Docky commence par un bloc de métadonnées en commentaires :

```yaml
# ============================================
# Docky Stack Metadata
# @name: nom-de-la-stack
# @category: ai|database|monitoring|media|network|security|dev|web|storage|other
# @description: Description courte
# @source: URL du repo ou de la doc
# @hardware: Requirements hardware
# @ports: 8080, 11434
# @created: 2025-01-15
# @updated: 2025-01-15
# ============================================
```

Ces métadonnées sont parsées et affichées dans le contexte du LLM.

---

## 📡 Agent (service distant)

### Rôle
- Service léger déployé sur chaque serveur
- Expose une API REST sécurisée par clé API
- Accès direct à Docker via docker.sock
- Gère les stacks locales (fichiers stockés sur le serveur)
- Toutes les fonctions Docker SDK sont wrappées avec asyncio.to_thread() pour ne pas bloquer l'event loop

### Endpoints de l'agent
| Méthode | Route | Description |
|---------|-------|-------------|
| GET | /agent/health | Statut (pas d'auth, pour ping) |
| GET | /agent/containers | Liste des containers |
| GET | /agent/containers/{id} | Détails d'un container |
| GET | /agent/containers/{id}/stats | CPU/RAM |
| GET | /agent/containers/{id}/logs | Logs (param tail) |
| WS | /agent/containers/{id}/logs/stream | Stream logs temps réel |
| WS | /agent/containers/{id}/exec | Console interactive |
| POST | /agent/containers/{id}/start | Démarrer |
| POST | /agent/containers/{id}/stop | Arrêter |
| POST | /agent/containers/{id}/restart | Redémarrer |
| POST | /agent/containers/{id}/exec | Exec one-shot |
| GET | /agent/containers/{id}/update-check | Vérif update image |
| GET | /agent/stacks | Liste des stacks (managed + externes) |
| GET | /agent/stacks/{name}/files | Liste des fichiers |
| GET | /agent/stacks/{name}/files/{filename} | Contenu d'un fichier |
| PUT | /agent/stacks/{name}/files/{filename} | Sauvegarder un fichier |
| POST | /agent/stacks | Créer une stack |
| DELETE | /agent/stacks/{name} | Supprimer une stack |
| POST | /agent/stacks/{name}/deploy | Déployer (down + up -d) |
| POST | /agent/stacks/{name}/start | Démarrer une stack |
| POST | /agent/stacks/{name}/stop | Arrêter une stack |
| POST | /agent/stacks/{name}/restart | Redémarrer une stack |
| POST | /agent/stacks/{name}/update | Mettre à jour (pull + up -d) |
| POST | /agent/stacks/import | Importer une stack externe (avec dry_run) |
| PUT | /agent/stacks/{name}/files/{filename}/permissions | Changer chmod |
| GET | /agent/ports | Liste des ports utilisés |
| POST | /agent/system/prune | Docker system prune |

### Stacks externes
- Détection automatique via les labels Docker Compose (com.docker.compose.project)
- Actions start/stop/restart fonctionnent avec --project-name (sans fichier compose)
- Import possible (copie du compose + .env, conversion des chemins relatifs → absolus)
- Preview avant import (dry-run)

### Authentification
- Clé API configurée via DOCKY_AGENT_API_KEY (env var)
- Toutes les requêtes doivent inclure Authorization: Bearer {api_key}
- /agent/health n'a pas d'auth (pour le ping)

---

## 📥 Import de stacks

- Import depuis un dossier externe (Dockge, etc.)
- Copie du docker-compose.yml + .env + fichiers de config
- Conversion automatique des chemins relatifs → absolus
- Preview (dry-run) avant import : affiche le compose converti + conversions + warnings
- Détection automatique du chemin source via les labels Docker
- Bouton 📥 en un clic sur les stacks externes

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
| GET | /api/v1/agents/{id}/containers | Containers d'un agent |
| GET | /api/v1/agents/{id}/containers/{cid}/logs | Logs d'un container |

---

## 📁 Structure du projet

```
/projects/Docky/
├── orchestrator/           # Code orchestrateur
│   ├── app/               # Application Python (FastAPI)
│   │   ├── main.py         # App FastAPI + startup bootstrap
│   │   ├── config.py        # Chargement/sauvegarde config + ensure_config_files()
│   │   ├── compose_reference.md  # Référence docker-compose (bundlé)
│   │   ├── auth/           # Authentification JWT
│   │   ├── agent_manager/  # Communication avec agents distants
│   │   ├── llm/            # Client LLM + 29 tools + Firecrawl
│   │   ├── routes/         # Routes API + dashboard
│   │   └── static/         # JS, CSS
│   ├── templates/          # Templates HTML (login, dashboard, settings, popups)
│   ├── Dockerfile
│   ├── docker-compose.yml  # Orchestrateur seul (dev)
│   ├── requirements.txt    # Dépendances (versions pinnées)
│   └── .dockerignore
├── agent/                  # Code agent
│   ├── main.py             # App FastAPI (port 8080)
│   ├── routes.py           # Endpoints /agent/*
│   ├── docker_manager.py   # Docker SDK (async avec asyncio.to_thread)
│   ├── auth.py             # Auth par clé API
│   ├── config.py           # DOCKY_DATA_DIR
│   ├── Dockerfile          # Avec Docker CLI + compose plugin
│   ├── docker-compose.yml  # Agent seul
│   ├── requirements.txt    # Dépendances (versions pinnées)
│   └── .dockerignore
├── data/                   # Config partagée (montée en volume)
│   ├── settings.yaml       # Config globale (ignoré par git)
│   ├── users.yaml          # Utilisateurs (ignoré par git)
│   ├── api_keys.yaml       # Clés API (ignoré par git)
│   ├── soul.md             # Mémoire LLM (commité)
│   ├── compose_reference.md # Référence (commité)
│   └── stacks/             # Stacks (ignoré par git)
├── .github/workflows/      # GitHub Actions
│   ├── release.yml         # Build + push images (multi-arch) + bump version
│   └── test-build.yml      # Build + push images tag "test"
├── docker-compose.yml      # Exemple: orchestrateur + agent ensemble
├── .env.example            # Template de configuration
├── .gitignore
├── version.txt             # 0.0.1
└── roadmap.md
```

---

## 🔒 Sécurité

### Points actuels
- Login + mot de passe (hashé bcrypt) pour l'interface web
- JWT pour les sessions web (cookie httpOnly, 24h)
- Clé API par agent (authentification orchestrator → agent)
- API key + whitelist IP pour l'agent externe (Discord)
- Validation humaine pour exec dans un container et clean_agent
- Agent et orchestrateur en containers séparés
- .gitignore protège les fichiers sensibles (settings.yaml, users.yaml, api_keys.yaml, .env)
- Warning sur la page Settings : recommandation d'utiliser un LLM local

### Points à définir plus tard
- Rate limiting
- HTTPS géré par reverse proxy externe
- Chiffrement de la communication orchestrator ↔ agent (TLS)
- Proxy WebSocket pour logs/console (actuellement 501 Not Implemented)

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
- [x] Affichage des logs (popup avec polling)
- [x] Console (popup avec exec one-shot)
- [x] Scan des ports utilisés
- [x] Détection des stacks externes (Dockge, etc.)
- [x] Boutons update pour les stacks

### Phase 3 — Éditeur Compose ✅
- [x] Affichage du docker-compose.yml et .env
- [x] Édition directe (textarea)
- [x] Sauvegarde + redéploiement
- [x] Création de nouvelle stack
- [x] Suppression de stack (avec confirmation)
- [x] Gestion des permissions (chmod)

### Phase 4 — Chat LLM ✅
- [x] Client API compatible OpenAI
- [x] Interface de chat (texte, toggle 💬)
- [x] Injection du contexte (état containers + soul.md + métadonnées)
- [x] 29 tools disponibles
- [x] Validation humaine pour exec dans container
- [x] Validation humaine pour clean_agent
- [x] Mise à jour automatique de soul.md par le LLM
- [x] Édition manuelle de soul.md via l'interface
- [x] Intégration Firecrawl (search + scrape + map, sans restriction)
- [x] Création/édition de fichiers arbitraires par le LLM
- [x] Gestion des permissions (chmod) par le LLM
- [x] Vérification des ports par le LLM
- [x] read_compose_reference tool
- [x] Scan des modèles disponibles (dropdown dans Settings)
- [x] Historique de conversation (avec tool calls mémorisés)
- [x] Warning LLM local sur la page Settings

### Phase 5 — Refactoring multi-containers ✅
- [x] Créer le service Agent (FastAPI léger, API REST sécurisée par clé API)
- [x] Docker CLI installé dans l'agent (docker-ce-cli + compose-plugin)
- [x] Refactorer l'Orchestrateur (agent_manager au lieu de docker_manager direct)
- [x] Gestion multi-agents (config dans settings.yaml)
- [x] Cache en mémoire des états
- [x] Dashboard avec vue globale + filtre par agent
- [x] Statut online/offline des agents
- [x] Proxy de l'éditeur compose vers l'agent
- [x] LLM tools adaptés pour cibler un agent précis
- [x] Page Settings pour gérer les agents
- [x] Bootstrap : création automatique des fichiers de config au démarrage
- [x] Toutes les fonctions Docker SDK wrappées avec asyncio.to_thread()
- [x] Route /agent/stacks optimisée (2 appels Docker au lieu de 1+3N)

### Améliorations post-Phase 5 ✅
- [x] Import de stacks externes (Dockge, etc.) avec dry-run/preview
- [x] Détection automatique du chemin source des stacks externes
- [x] Conversion des chemins relatifs → absolus lors de l'import
- [x] Dashboard avec bin-packing (skyline bottom-left) sans trous
- [x] Cards de containers groupées par stack avec couleurs distinctes
- [x] Boustrophedon dans les blocs pour la connexion des containers
- [x] Clic sur un container → panel contextuel (stack info + compose + actions)
- [x] Assombrissement des autres stacks au clic
- [x] Panneaux redimensionnables (click'n'drag, sauvegardé en localStorage)
- [x] Toggle du chat (💾 masquable)
- [x] Popups logs + console (fenêtres séparées)
- [x] Changement de mot de passe dans Settings
- [x] Métadonnées Docky dans les compose (parsing + contexte LLM)
- [x] compose_reference.md (référence docker-compose pour le LLM)
- [x] Images Docker multi-arch (amd64 + arm64) sur ghcr.io
- [x] GitHub Actions (release + test-build)
- [x] .env.example pour la configuration
- [x] .gitignore (protection des secrets)
- [x] Versioning (version.txt)
- [x] Scripts supprimés (install.sh, update.sh) — utilisation des images Docker

### Phase 6 — API Agent externe (Discord)
- [ ] Endpoints REST orchestrateur (agents, containers, stacks, logs)
- [ ] Système de clé API + whitelist IP
- [ ] Validation humaine à la première connexion
- [ ] Documentation de l'API

### Phase 7 — Polish et Sécurité
- [ ] Proxy WebSocket pour logs/console (actuellement popups HTTP)
- [ ] Design final et cohérent (refonte UI)
- [ ] Gestion des erreurs et notifications
- [ ] Tests de sécurité
- [ ] Documentation utilisateur
- [ ] Affichage de la version dans l'interface
- [ ] Script d'installation de l'agent (one-liner)

---

## 📝 Notes

- Le projet est hébergé sur GitHub : https://github.com/grokuku/Docky
- Le reverse proxy et le domaine sont déjà prêts (HTTPS géré en externe)
- L'interface est en HTML/JS/CSS vanilla, pas de framework lourd
- Images Docker publiées sur ghcr.io (multi-arch amd64 + arm64)
- Installation via docker pull + docker-compose (plus besoin de scripts d'install)
- L'outil est destiné à un usage personnel dans un premier temps, mais conçu pour pouvoir évoluer
- Les Phases 1-5 et améliorations post-Phase 5 ont été réalisées