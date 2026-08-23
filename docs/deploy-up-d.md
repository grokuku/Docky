# Déploiement des stacks : `up -d` seul + action « Down »

## Contexte

Le flux « save & deploy » (bouton de l'éditeur Compose) exécutait un
`docker compose down` puis `up -d` — une coupure complète jugée trop
destructive. Ce document décrit le nouveau comportement, l'ajout d'un bouton
explicite « Down » et le statut du bouton « save & deploy ».

## 1. Nouveau comportement du deploy

Le deploy exécute désormais **uniquement** `docker compose up -d --remove-orphans`,
sans `down` préalable — aligné sur le flux d'update (`pull` puis `up -d`).

**Avant** : `docker compose down` puis `docker compose up -d --remove-orphans`.
**Après** : `docker compose up -d --remove-orphans` seul.

Pour une vraie destruction propre, l'utilisateur dispose maintenant d'une action
explicite « Down » (voir §2).

### Endpoints/fonctions impactés

- `agent/docker/compose_stream.py` — `stream_deploy_stack` : plus de `down`
  systématique, seul `up -d --remove-orphans` est émis.
- `agent/docker_manager.py` — `deploy_stack` (variante non-streaming) : alignée,
  `up -d` seul.
- `orchestrator/app/llm/tools.py` — description de l'outil LLM `deploy_stack`
  mise à jour.

### Gestion des stacks externes

Sans fichier compose localisable, `up` retombe sur `start` (via
`_compose_up_command`) — inchangé.

## 2. Bouton « Down » de stack

Nouvelle action explicite = `docker compose down` : arrête et **supprime les
containers et le réseau**, **les volumes nommés sont conservés** (pas de `-v`).

### Endpoints

- Agent : `POST /agent/stacks/{name}/down` (SSE streamé).
- Orchestrateur : `POST /api/stacks/{name}/down?agent=…` (SSE streamé, même
  pattern que `start`/`stop`/`restart`/`update`).

### Fonctions créées

- `agent/docker/compose_stream.py` — `stream_down_stack` (exécute `down` via
  `_compose_down_command` / `_resolve_compose_args` ; retombe sur `stop` pour
  une stack externe).
- `agent/docker_manager.py` — ré-export façade de `stream_down_stack`.
- `agent/routes.py` — route agent `POST /agent/stacks/{name}/down`.
- `orchestrator/app/agent_manager/client.py` — `stream_down_stack` (et helper
  JSON `down_stack`).
- `orchestrator/app/routes/stacks.py` — route `POST /api/stacks/{name}/down`.

### Frontend

- `dashboard.js` : bouton « Down » sur la carte de stack et dans le panneau de
  la stack (icône `power`). Une **confirmation** (`stack-down-modal` dans
  `dashboard.html`, plus fallback `window.confirm`) est demandée car l'action
  est destructive.
- L'action passe par le même `stackAction(name, 'down', agent)` /
  `/api/stacks/{name}/down` que les autres actions.

## 3. Bouton « save + deploy » actif en permanence

Le bouton « Sauvegarder & Déployer » (`saveAndDeploy()`) était **désactivé**
quand aucune modification locale n'était détectée
(`disabled = !this.anyModified()`). Désormais il est **toujours actif / cliquable** :

- la ligne de génération du bouton ne conditionne plus le `disabled` ;
- la mise à jour d'état ne désactive plus ce bouton.

Le flux reste **PUT des fichiers modifiés puis POST /deploy**. Un déploiement
reste utile même sans modification (reconfigurer/recréer la stack, appliquer un
`down` antérieur, etc.). Le bouton « Sauvegarder » (fichier courant) conserve,
lui, son désactivation quand le fichier n'est pas modifié.

## Tests

- **Modifiés** :
  - `agent/tests/test_agent_routes.py` — ajout de `/agent/stacks/myapp/down`
    aux tests des actions SSE (200) et à ceux d'authentification (401).
  - `agent/tests/test_docker_modules_import.py` — `stream_down_stack` ajouté aux
    ré-exports attendus de la façade.
  - `orchestrator/tests/test_api_routes.py` — tests `test_sse_stack_down` (200,
    SSE, invalidation de cache) et `test_sse_stack_down_unauthorized` (401).
- **ajoutés** :
  - `agent/tests/test_compose_stream.py` — `test_stream_deploy_stack_runs_up_only`
    (le deploy n'émet que `up`, jamais `down`) et
    `test_stream_down_stack_runs_down` (`stream_down_stack` émet `down`).

Aucun test existant ne vérifiait la séquence `down` puis `up` du deploy ; il
n'a donc pas fallu réécrire d'assertion de séquence existante.

### Résultat pytest

`timeout 300 python -m pytest -q` → **412 passed** (406 d'origine + 6 nouveaux).

### Smoke TestClient

`POST /api/stacks/{name}/down?agent=…` renvoie un flux SSE cohérent
(`text/event-stream` : `event: output` puis `event: done` avec
`{"success": true, "output": …}`), agent mocké via `agent_manager.stream_down_stack`.
