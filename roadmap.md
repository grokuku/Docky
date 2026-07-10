# Docky — Roadmap

## 🎯 Vision

Docky est un outil de gestion de stacks Docker Compose assisté par LLM. Il s'exécute dans un container Docker et permet, via une interface web, de visualiser, gérer et interagir avec les stacks Docker Compose présentes sur le serveur. Un chat intégré permet de piloter les stacks via un LLM (API compatible OpenAI). Une API REST permet à un agent externe de récupérer des informations en lecture seule.

---

## 📐 Architecture

### Stack technique
- **Backend** : Python + FastAPI
- **Frontend** : HTML / JS / CSS (vanilla, léger, moderne)
- **Docker** : Docker SDK for Python (gestion via docker.sock)
- **LLM** : Client API compatible OpenAI (Ollama, Deepseek, Ollama Cloud, etc.)
- **Recherche web** : Firecrawl API (search + scrape) pour permettre au LLM d'accéder à internet
- **Stockage** : Fichiers texte (pas de base de données)
  - `settings.yaml` : paramètres globaux (endpoint LLM, modèle, clé API Firecrawl, etc.)
  - `users.yaml` : utilisateurs (login + hash du mot de passe)
  - `api_keys.yaml` : clés API + whitelist IP
  - `soul.md` : instructions/contexte persistant pour le LLM
  - `/stacks/` : un dossier par stack avec `docker-compose.yml` + `.env`

### Schéma
```
Container Docky (Python/FastAPI)
  ├── Interface Web (HTML/JS/CSS)
  │     ├── Dashboard (containers, ressources, updates, actions)
  │     ├── Éditeur Compose (docker-compose.yml + .env)
  │     └── Chat LLM (texte)
  ├── API REST (read-only, pour agent externe)
  ├── Docker SDK ← /var/run/docker.sock (monté)
  ├── LLM Client → API OpenAI-compatible
  └── Fichiers de config (/data/)
        ├── stacks/
        ├── soul.md
        ├── settings.yaml
        ├── users.yaml
        └── api_keys.yaml
```

---

## 🖥️ Interface Web

### Layout
```
┌──────────────────────────────────┬──────────────────────┐
│  Top bar : Login / Settings      │                      │
├──────────────────────────────────┤   Éditeur Compose    │
│                                  │   (colonne droite)   │
│         Dashboard                │                      │
│    Liste des stacks/containers   │   - docker-compose   │
│    Ressources (CPU/RAM)          │     .yml             │
│    Indicateur update dispo       │   - .env             │
│    Boutons: start/stop/restart/  │                      │
│    console/logs                  │   Édition directe    │
│                                  │                      │
├──────────────────────────────────┤                      │
│         Chat LLM                 │                      │
│    (texte uniquement)            │                      │
│    Actions via langage naturel   │                      │
└──────────────────────────────────┴──────────────────────┘
```

### Dashboard (haut gauche, zone principale)
- Liste des stacks avec leur état global
- Pour chaque stack : liste des containers
- Indicateurs visuels :
  - État (running / stopped / error)
  - CPU et RAM consommés
  - Indicateur "update disponible" (image plus récente sur le registry)
  - Ports utilisés par les containers
- Boutons d'action par stack et par container :
  - Start / Stop / Restart
  - Logs (affichage dans un panel)
  - Console (accès terminal au container)
  - Update (pull + up -d)

### Éditeur Compose (colonne droite)
- Affichage du `docker-compose.yml` de la stack sélectionnée
- Affichage du `.env` associé
- Édition directe en texte
- Sauvegarde → redéploiement possible

### Chat LLM (bas gauche)
- Interface texte simple (style chat)
- Le LLM a accès à l'état des containers en temps réel
- Le LLM peut effectuer des actions Docker (start, stop, update, clean, créer, modifier, supprimer stack, lire logs)
- **Validation humaine requise** pour l'exécution de commandes dans un container (exec)
- Le LLM met à jour `soul.md` quand l'utilisateur donne une instruction persistante
- L'utilisateur peut aussi éditer `soul.md` manuellement via l'interface

### Authentification
- Page de login (username + mot de passe)
- Session gérée via token JWT (cookie)
- Un seul utilisateur prévu dans un premier temps, mais système multi-utilisateurs ready

---

## 🤖 Intégration LLM

### Configuration
- Endpoint configurable (compatible OpenAI API)
- Modèle configurable
- Paramètres (temperature, max_tokens, etc.) dans `settings.yaml`

### Capacités du LLM
Le LLM reçoit en contexte :
- L'état actuel de tous les containers (via Docker SDK)
- Le contenu de `soul.md` (instructions persistantes)
- L'historique récent de la conversation (session courante)

Le LLM peut :
- **Démarrer / arrêter / redémarrer** une stack ou un container
- **Update** : docker compose pull && docker compose up -d
- **Clean** : supprimer les containers/images/volumes inutilisés
- **Créer** une nouvelle stack (génère le docker-compose.yml + .env + n'importe quel autre fichier de config nécessaire)
- **Modifier** une stack existante (édite le docker-compose.yml + .env + n'importe quel autre fichier de config)
- **Supprimer** une stack
- **Lire les logs** d'un container
- **Exécuter une commande** dans un container (avec **validation humaine**)
- **Créer / éditer n'importe quel fichier** dans le dossier d'une stack (nginx.conf, scripts d'init, configs personnalisées, etc.)
- **Gérer les permissions** (chmod) sur les fichiers créés via Python os.chmod()
- **Vérifier les ports utilisés** sur le serveur pour éviter les conflits (scan via Docker SDK + système) et proposer des ports alternatifs automatiquement

### Recherche web (via Firecrawl)
- Le LLM peut **rechercher** sur internet (Firecrawl search) — ex: "dernière version d'une image", "comment déployer X", "solution à une erreur"
- Le LLM peut **lire le contenu d'une page web** (Firecrawl scrape) — ex: documentation officielle, GitHub, Docker Hub
- Le LLM peut **découvrir les URLs** d'un site (Firecrawl map)
- **Aucune restriction** sur les sites visitables — le LLM est autonome
- Cas d'usage : recherche de configs, résolution d'erreurs, découverte de nouvelles stacks, vérification de versions d'images
- La clé API Firecrawl est stockée dans `settings.yaml`

### SOUL.md
- Fichier texte qui sert de "mémoire persistante" au LLM
- Contient les instructions et préférences de l'utilisateur accumulées au fil du temps
- Mis à jour par le LLM quand l'utilisateur donne une directive persistante
- Éditable manuellement via l'interface web
- Pas de personnalité poussée — c'est un outil

---

## 🔌 API REST (pour agent externe)

### Principe
- API REST simple (JSON)
- **Lecture seule** dans un premier temps
- Consommée par un agent externe (ex: Hermes, OpenClaw) qui communique via Discord

### Authentification
- L'agent possède une **clé API**
- À la première utilisation de la clé, le serveur demande une **validation humaine** (notification dans l'interface Docky)
- Si validé, l'**adresse IP** de l'agent est enregistrée dans une **whitelist**
- Les requêtes suivantes sont acceptées uniquement si : clé API valide + IP dans la whitelist

### Endpoints (phase 1)
| Méthode | Route | Description |
|---------|-------|-------------|
| GET | /api/v1/containers | Liste des containers avec leur état |
| GET | /api/v1/stacks | Liste des stacks |
| GET | /api/v1/stacks/{name}/containers | Containers d'une stack spécifique |
| GET | /api/v1/containers/{id}/logs | Logs d'un container |

### Endpoints (phase 2 — à étoffer plus tard)
- Métriques (CPU/RAM détaillées)
- Version des images
- Historique des actions
- etc.

---

## 📁 Gestion des stacks

### Structure des dossiers
```
/data/stacks/
├── stack1/
│   ├── docker-compose.yml
│   └── .env
├── stack2/
│   ├── docker-compose.yml
│   └── .env
└── ...
```

- Chaque stack a son propre dossier
- Le `.env` est géré au même niveau que le `docker-compose.yml`
- Docky scanne `/data/stacks/` pour découvrir les stacks existantes
- Création d'une stack = création d'un nouveau dossier + fichiers
- Suppression d'une stack = suppression du dossier (avec confirmation)
- Le LLM et l'utilisateur peuvent créer/éditer **n'importe quel type de fichier** dans le dossier d'une stack (pas seulement docker-compose.yml et .env)
- Les permissions des fichiers (chmod) peuvent être gérées via l'interface ou par le LLM
- Vérification automatique des ports utilisés lors de la création/modification d'une stack pour éviter les conflits

---

## 🔒 Sécurité

### Points actuels
- Login + mot de passe (hashé, stocké dans `users.yaml`)
- JWT pour les sessions web
- API key + whitelist IP pour l'agent externe
- Validation humaine pour exec dans un container
- Docky tourne en container avec docker.sock monté

### Points à définir plus tard
- Limitation des actions possibles (certaines stacks protégées ?)
- Rate limiting sur l'API
- HTTPS géré par le reverse proxy externe (déjà en place)

---

## 🗺️ Plan de réalisation

### Phase 1 — Fondations
- [ ] Initialiser le projet Python + FastAPI
- [ ] Dockerfile pour Docky (avec mount docker.sock)
- [ ] Système d'authentification (login + JWT)
- [ ] Connexion Docker SDK via docker.sock
- [ ] Structure des fichiers de config (settings.yaml, users.yaml, api_keys.yaml, soul.md)
- [ ] Page de login (HTML/CSS)

### Phase 2 — Dashboard
- [ ] Liste des stacks (scan de /data/stacks/)
- [ ] Liste des containers par stack (Docker SDK)
- [ ] Affichage de l'état (running/stopped/error)
- [ ] Indicateurs de ressources (CPU/RAM)
- [ ] Indicateur d'update disponible
- [ ] Scan des ports utilisés (Docker SDK + système)
- [ ] Boutons d'action (start/stop/restart)
- [ ] Affichage des logs
- [ ] Console (exec dans container)

### Phase 3 — Éditeur Compose
- [ ] Affichage du docker-compose.yml
- [ ] Affichage du .env
- [ ] Édition directe (textarea avec coloration syntaxique si possible)
- [ ] Sauvegarde + redéploiement
- [ ] Création de nouvelle stack
- [ ] Suppression de stack (avec confirmation)

### Phase 4 — Chat LLM
- [ ] Client API compatible OpenAI
- [ ] Interface de chat (texte)
- [ ] Injection du contexte (état containers + soul.md)
- [ ] Actions Docker via le LLM (start/stop/update/clean/create/modify/delete/logs)
- [ ] Validation humaine pour exec dans container
- [ ] Mise à jour automatique de soul.md par le LLM
- [ ] Intégration Firecrawl (search + scrape + map)
- [ ] Recherche web par le LLM (sans restriction de site)
- [ ] Scraping de pages web par le LLM pour lecture de documentation
- [ ] Création/édition de fichiers arbitraires dans le dossier d'une stack par le LLM
- [ ] Gestion des permissions (chmod) par le LLM
- [ ] Vérification des ports par le LLM (détection de conflits + proposition de ports alternatifs)
- [ ] Édition manuelle de soul.md via l'interface

### Phase 5 — API Agent
- [ ] Endpoints REST (containers, stacks, logs)
- [ ] Système de clé API
- [ ] Validation humaine à la première connexion
- [ ] Whitelist IP
- [ ] Documentation de l'API

### Phase 6 — Polish & Sécurité
- [ ] Design moderne et léger de l'interface
- [ ] Gestion des erreurs et notifications
- [ ] Tests de sécurité
- [ ] Documentation utilisateur
- [ ] Docker Compose pour déployer Docky lui-même

---

## 📝 Notes

- Le projet est hébergé sur GitHub : https://github.com/grokuku/Docky
- Le reverse proxy et le domaine sont déjà prêts (HTTPS géré en externe)
- L'interface doit être moderne mais légère : HTML/JS/CSS vanilla, pas de framework lourd
- L'outil est destiné à un usage personnel dans un premier temps, mais conçu pour pouvoir évoluer
