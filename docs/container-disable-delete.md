# Désactiver / Supprimer un container

Ce document décrit deux nouvelles actions du menu contextuel (clic droit) des
containers dans Docky :

1. **« Désactiver »** — arrête le container **et** l'empêche de démarrer
   automatiquement (restart policy → `no`).
2. **« Supprimer »** — supprime le container (force).

Les deux actions fonctionnent sur les containers **dans un compose** (gérés ou
externes) **et** sur les containers **externes / standalone**, sans modifier le
fichier compose.

## Commandes Docker exactes

| Action | Commandes |
| --- | --- |
| Désactiver | `docker update --restart=no <id>` puis `docker stop <id>` |
| Supprimer | `docker rm -f <id>` |

Côté agent, ces commandes sont exécutées via le SDK Docker (équivalents
docker-py) :

- `disable_container` → `c.update(restart_policy={"Name": "no"})` puis
  `c.stop(timeout=10)`.
- `delete_container` → `c.remove(force=True)`.

## Gestion compose vs externe

Les deux fonctions ciblent le container par son **id** exact et n'utilisent
**jamais** `docker compose down` ni `docker compose up`. Elles ne touchent donc
ni le fichier compose ni les autres containers du projet :

- **Compose** (géré ou externe) : le container est désactivé/supprimé
  individuellement. Le compose n'est pas modifié ; un `up` ultérieur recréera
  le service (pour « Désactiver », le restart policy `no` est appliqué au
  container existant).
- **Standalone** : le container est désactivé/supprimé directement.

## Agent

### Fonctions (`agent/docker_manager.py`)

- `disable_container(container_id) -> bool` — `docker update --restart=no` puis
  `docker stop`. Retourne `True` en cas de succès.
- `delete_container(container_id) -> bool` — `docker rm -f`. Retourne `True` en
  cas de succès.

### Routes (`agent/routes.py`)

- `POST /agent/containers/{id}/disable` — simple POST JSON (non streamé),
  cohérent avec `start` / `stop` / `restart`.
- `POST /agent/containers/{id}/delete` — simple POST JSON (non streamé).

## Orchestrateur

### Pass-through (`orchestrator/app/agent_manager/client.py`)

- `disable_container(agent_name, container_id) -> bool`
- `delete_container(agent_name, container_id) -> bool`

### Routes (`orchestrator/app/routes/containers.py`)

- `POST /api/containers/{id}/disable?agent=...`
- `POST /api/containers/{id}/delete?agent=...`

Cohérentes avec les routes existantes : auth (`_check_auth`), résolution de
l'agent (`_resolve_agent`), retour `{"success": bool}`.

## Frontend (`orchestrator/app/static/js/dashboard.js`)

Le menu contextuel `openContainerContextMenu` gagne un groupe séparé (après
Update) :

- **« Désactiver »** — appelle `containerAction(id, 'disable', agent)`.
  **Direct, sans confirmation** : l'action n'est pas destructrice (le container
  peut être redémarré / réactivé).
- **« Supprimer »** — appelle `confirmContainerDelete(id, agent)` qui demande
  une **confirmation** (`window.confirm`) car l'action est destructrice, puis
  lance `containerAction(id, 'delete', agent)`.

`containerAction` traite `disable` et `delete` comme des POST JSON simples
(non streamés), comme `start` / `stop` / `restart`. Un style `ctx-menu-danger`
(coloration rouge) est appliqué à l'entrée « Supprimer ».

## Tests

- **Agent** (`agent/tests/test_docker_manager_container_disable_delete.py`) :
  `disable` → `update(restart_policy=no)` + `stop` ; `delete` →
  `remove(force=True)` ; cas compose et standalone ; container manquant →
  `False`.
- **Agent routes** (`agent/tests/test_agent_routes.py`) : routes
  `/disable` et `/delete` (succès + auth requise).
- **Orchestrateur client** (`orchestrator/tests/test_agent_manager.py`) :
  pass-through `disable_container` / `delete_container` (succès + échec).
- **Orchestrateur routes** (`orchestrator/tests/test_api_routes.py`) : routes
  `/api/containers/{id}/disable` et `/delete` (pass-through + validation
  agent).

## Fichiers modifiés

- `agent/docker_manager.py` — `disable_container`, `delete_container`.
- `agent/routes.py` — routes `/disable`, `/delete`.
- `orchestrator/app/agent_manager/client.py` — `disable_container`,
  `delete_container`.
- `orchestrator/app/routes/containers.py` — routes `/disable`, `/delete`.
- `orchestrator/app/static/js/dashboard.js` — entrées du menu contextuel,
  `confirmContainerDelete`, labels `containerAction`.
- `orchestrator/app/static/css/style.css` — style `ctx-menu-danger`.
- `agent/tests/conftest.py` — `FakeContainer.stop/update/remove`.
- `orchestrator/tests/conftest.py` — méthodes async mockées.
