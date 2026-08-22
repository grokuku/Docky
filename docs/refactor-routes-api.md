# Refactor `orchestrator/app/routes/api.py` → routeurs par domaine

Objectif : découper `orchestrator/app/routes/api.py` (1 962 lignes, ~60
endpoints `/api/*`) en routeurs par domaine + module de helpers partagés, SANS
changement de comportement (chemins URL, payloads, statuts, helpers), en
gardant la suite pytest verte après chaque étape.

**Règle critique** (même principe que les refactors `agent/docker_manager.py`,
`app/llm/client.py` et `app/agent_manager/client.py` réussis) : les tests
monkeypatchent des symboles DANS le namespace `app.routes.api` :
- `app.routes.api.agent_manager` (conftest `mock_agent_manager`, avec un mock
  dont `agents` est un vrai dict — `_resolve_agent` doit donc le voir) ;
- `app.routes.api.LLMClient` (conftest `mock_llm_client`) ;
- `app.routes.api._check_agent_error(...)` est APPELÉE directement par les
  tests (`test_api_routes`), elle doit rester exposée dans la façade.

Stratégie choisie = **(a) façade ré-export + résolution tardive** : `api.py`
reste la façade (l'objet `router = APIRouter(prefix="/api")` utilisé par
`app.main`), inclut tous les sous-routeurs, injecte le callback broadcast
(`agent_manager.broadcast_agent_event`) et ré-exporte tous les symboles
publics. Les sous-routeurs et les helpers résolvent les symboles
monkeypatchables (`agent_manager`, `LLMClient`) via le namespace de la façade
au moment de l'appel (helper `_api()`, résolution tardive — aucun cycle
d'import). Aucun fichier de test existant n'est modifié.

## Rappels / dépendances vérifiées au départ
- `app.main` importe `router` depuis `app.routes.api` (unique import
  applicatif).
- `app.routes.dashboard` n'importe RIEN depuis `app.routes.api`.
- Les tests importent `app.routes.api` (module) et accèdent à `agent_manager`,
  `LLMClient`, `_check_agent_error`, `router` (via `app.main`).
- `app.routes.api` importe depuis `app.llm.client` : `LLMClient`, `run_chat`,
  `read_soul`, `update_soul`, `execute_tool`, `build_system_prompt`, `TOOLS`,
  `HUMAN_VALIDATION_MARKER`.
- `app.routes.api` importe `agent_manager` depuis `app.agent_manager.client`
  (singleton unique).
- `pyproject.toml` : `pythonpath = ["orchestrator", "."]` ; testpaths
  `orchestrator/tests`, `agent/tests` ; asyncio_mode auto.

## État initial (baseline)
- `python -m pytest -q` → **296 passed** (9.25s)

## Découpage cible (adapté au code réel)
1. `api_helpers.py` : helpers partagés (`_events_clients`, `_broadcast_agent_event`,
   `_find_version_path`, `_VERSION_PATH`, `_check_auth`, `_check_auth_ws`,
   `_unauthorized`, `_agent_bad_request`, `_agent_not_found`, `_agent_offline`,
   `_agent_unreachable`, `_resolve_agent`, `_check_agent_error`, `_sse_response`,
   `_sse_action_response`, `_mask_api_key`) + helper `_api()` de résolution
   tardive.
2. `agents.py` : `/agents*` (5 endpoints).
3. `settings.py` : `/settings/llm*`, `/settings/agents*`, `/settings/password`,
   `/settings/git-history*`, `/version`, `/version-check` + `_save_agents`.
4. `containers.py` : `/containers*`, `/ports`, `/containers/{id}/update-check`,
   `/events` (ws), `/presence/heartbeat`.
5. `stacks.py` : `/stacks*`.
6. `chat.py` : `/chat`, `/chat/validate-exec`, `/soul`, `/chat/stream` (ws).
7. `api.py` (façade) : `router` principal + inclusion des sous-routeurs +
   injection broadcast + ré-exports.

## Étapes (chaque étape : `python -m pytest -q` vert)

### Étape 1 — extraction des helpers (api_helpers.py) (TERMINÉE)

### Fichier créé
- `orchestrator/app/routes/api_helpers.py` : `_events_clients`,
  `_broadcast_agent_event`, `_find_version_path`, `_VERSION_PATH`, `_check_auth`,
  `_check_auth_ws`, `_unauthorized`, `_agent_bad_request`, `_agent_not_found`,
  `_agent_offline`, `_agent_unreachable`, `_resolve_agent`, `_check_agent_error`,
  `_sse_response`, `_sse_action_response`, `_mask_api_key` + helper `_api()`
  (résolution tardive de `app.routes.api`).

### Fichier modifié
- `orchestrator/app/routes/api.py` : suppression des définitions de helpers
  (remplacées par un import depuis `app.routes.api_helpers`) ; l'injection
  `agent_manager.broadcast_agent_event = _broadcast_agent_event` est conservée
  (le callback vient désormais de `api_helpers`). `_save_agents` reste dans
  `api.py` pour l'instant (déplacé à l'étape 2).

### Points critiques traités
- `_resolve_agent` et `_sse_action_response` résolvent `agent_manager` via
  `_api().agent_manager` au moment de l'appel (monkeypatch des tests
  `app.routes.api.agent_manager` toujours effectif).
- La façade ré-exporte tous les helpers (imports explicites) → le namespace
  `app.routes.api` est inchangé (`_check_agent_error` toujours appelable par
  les tests).
- Aucun cycle d'import : `api_helpers` n'importe `app.routes.api` que dans
  `_api()` (appel).

### Tests
- `python -m pytest -q` → **296 passed** ✓

### Étape 2 — routeurs agents.py + settings.py (TERMINÉE)

### Fichiers créés
- `orchestrator/app/routes/agents.py` : `/agents` GET, `/agents/refresh` POST,
  `/agents/{name}/containers`, `/agents/{name}/stacks`, `/agents/{name}/ports`
  (5 endpoints). Résout `agent_manager` via `_api().agent_manager`.
- `orchestrator/app/routes/settings.py` : `/settings/llm*` (4), `/settings/agents*`
  (5), `/settings/password`, `/settings/git-history*` (2), `/version`,
  `/version-check` (14 endpoints) + `_save_agents`. Résout `agent_manager` /
  `LLMClient` via `_api().agent_manager` / `_api().LLMClient`.

### Fichier modifié
- `orchestrator/app/routes/api.py` : suppression des sections Agents management,
  Settings-LLM, Settings-Agents, Settings-Password, Version, Settings-Git-history
  (497 lignes, script Python sur marqueurs de section) ; ajout des imports
  `from app.routes import agents, settings` et `from app.routes.settings import
  _save_agents` ; inclusion `router.include_router(agents.router)` et
  `router.include_router(settings.router)`.

### Vérifications
- `/api` HTTP methods total = **61** (identique au compte d'origine) via
  `app.openapi()`.
- Tous les chemins `/api/agents*`, `/api/settings/*`, `/api/version*` présents.

### Tests
- `python -m pytest -q` → **296 passed** ✓

### Étape 3 — routeur containers.py (TERMINÉE)

### Fichier créé
- `orchestrator/app/routes/containers.py` : `/containers` GET,
  `/containers/{id}` GET, `/containers/{id}/start|stop|restart` POST,
  `/containers/{id}/edit-spec`, `/containers/{id}/update`,
  `/containers/{id}/update-image` (SSE), `/containers/{id}/logs`,
  WS `/containers/{id}/logs/stream`, WS `/events`, `/presence/heartbeat`,
  WS `/containers/{id}/exec`, `/containers/{id}/exec`, `/containers/{id}/stats`,
  `/ports`, `/containers/{id}/update-check` (17 endpoints + 3 WS).
  Résout `agent_manager` via `_api().agent_manager` ; `_events_clients` importé
  de `api_helpers` (partagé avec `_broadcast_agent_event`).

### Fichier modifié
- `orchestrator/app/routes/api.py` : suppression de la section Containers
  (499 lignes, script sur marqueurs) ; import `containers` +
  `router.include_router(containers.router)`.

### Vérifications
- `/api` HTTP methods total = **61** (identique).

### Tests
- `python -m pytest -q` → **296 passed** ✓

### Étape 4 — routeur stacks.py (TERMINÉE)

### Fichier créé
- `orchestrator/app/routes/stacks.py` : `/stacks` GET, `/stacks/{name}/containers`,
  `/stacks/{name}/start|stop|restart|update` (SSE), `/stacks/{name}/update-check`,
  `/stacks/{name}/logs`, `/stacks/{name}/files`, `/stacks/{name}/files-with-content`,
  `/stacks/{name}/files/{filename:path}` GET/PUT, `/stacks/{name}/files/{filename}/permissions`,
  `/stacks/{name}/compose` GET/PUT, `/stacks/{name}/env` GET/PUT, `/stacks` POST,
  `/stacks/import`, `/stacks/{name}` DELETE, `/stacks/{name}/deploy`,
  `/stacks/{name}/history`, `/stacks/{name}/history/{hash}`,
  `/stacks/{name}/history/restore/{hash}` (24 endpoints). Résout `agent_manager`
  via `_api().agent_manager`.

### Fichier modifié
- `orchestrator/app/routes/api.py` : suppression de la section Stacks (425 lignes) ;
  import `stacks` + `router.include_router(stacks.router)`.

### Vérifications
- `/api` HTTP methods total = **61** (identique).

### Tests
- `python -m pytest -q` → **296 passed** ✓

### Étape 5 — routeur chat.py (TERMINÉE)

### Fichier créé
- `orchestrator/app/routes/chat.py` : `/chat` POST, `/chat/validate-exec` POST,
  `/soul` GET, `/soul` PUT, WS `/chat/stream` (5 endpoints). Résout `LLMClient`
  et `agent_manager` via `_api().LLMClient` / `_api().agent_manager` ; les
  autres symboles `app.llm` (`run_chat`, `read_soul`, `update_soul`,
  `execute_tool`, `build_system_prompt`, `TOOLS`, `HUMAN_VALIDATION_MARKER`)
  sont importés directement depuis `app.llm.client` (non monkeypatchés sur
  `app.routes.api`).

### Fichier modifié
- `orchestrator/app/routes/api.py` : suppression de la section LLM Chat
  (303 lignes) — le fichier passe à la façade pure (68 lignes). Import `chat` +
  `router.include_router(chat.router)`.

### Vérifications
- `/api` HTTP methods total = **61** (identique).
- Les 4 WebSocket routes (`/api/events`, `/api/containers/{id}/logs/stream`,
  `/api/containers/{id}/exec`, `/api/chat/stream`) sont enregistrées et
  rejettent les connexions non authentifiées (close 4401) via TestClient.

### Tests
- `python -m pytest -q` → **296 passed** ✓

### Étape 6 — nettoyage final + smoke tests (TERMINÉE)

### Nettoyage
- `orchestrator/app/routes/settings.py` : suppression de `import json` et du
  logger inutilisé.
- `orchestrator/app/routes/chat.py` : suppression du logger inutilisé.
- `orchestrator/app/routes/api.py` (façade) : réécriture en 64 lignes — imports
  nettoyés (asyncio/json/urllib/pathlib/httpx/bcrypt/fastapi.* inutiles retirés),
  ré-exports conservés (`agent_manager`, `LLMClient`, helpers, symboles llm,
  `router`, `_save_agents`), docstring mise à jour.

### Fichier créé
- `orchestrator/tests/test_routes_modules_import.py` (NOUVEAU, 7 tests smoke) :
  vérifie l'exposition des symboles de la façade, le préfixe `/api` + inclusion
  des 5 routeurs, l'unicité du singleton `agent_manager`, l'injection du
  callback broadcast, l'absence de cycle (`_api()`), la résolution patchable de
  `_resolve_agent`, et le partage des symboles llm.

### Tests
- `python -m pytest -q` → **303 passed** (296 historiques + 7 smoke)

## État final / rapport

### Fichiers créés
| Fichier | Contenu |
|---|---|
| `orchestrator/app/routes/api_helpers.py` | helpers partagés + `_api()` |
| `orchestrator/app/routes/agents.py` | `/api/agents*` (5 endpoints) |
| `orchestrator/app/routes/settings.py` | `/api/settings/*`, `/api/version*`, `_save_agents` (14 endpoints) |
| `orchestrator/app/routes/containers.py` | `/api/containers*`, `/api/ports`, `/api/events`, `/api/presence/heartbeat` (17 endpoints + 3 WS) |
| `orchestrator/app/routes/stacks.py` | `/api/stacks*` (24 endpoints) |
| `orchestrator/app/routes/chat.py` | `/api/chat`, `/api/chat/validate-exec`, `/api/soul`, WS `/api/chat/stream` (4 endpoints + 1 WS) |
| `orchestrator/tests/test_routes_modules_import.py` | 7 tests smoke |
| `docs/refactor-routes-api.md` | cette trace |

### Fichiers modifiés
- `orchestrator/app/routes/api.py` : **1 962 → 64 lignes** (façade pure : router
  `APIRouter(prefix="/api")` + inclusion des 5 routeurs + injection broadcast +
  ré-exports).

### Symboles déplacés par module
- **api_helpers** : `_events_clients`, `_broadcast_agent_event`,
  `_find_version_path`, `_VERSION_PATH`, `_check_auth`, `_check_auth_ws`,
  `_unauthorized`, `_agent_bad_request`, `_agent_not_found`, `_agent_offline`,
  `_agent_unreachable`, `_resolve_agent`, `_check_agent_error`, `_sse_response`,
  `_sse_action_response`, `_mask_api_key` + `_api()`.
- **agents** : `api_list_agents`, `api_refresh_agents`, `api_agent_containers`,
  `api_agent_stacks`, `api_agent_ports`.
- **settings** : `_save_agents`, `api_get_llm_settings`, `api_update_llm_settings`,
  `scan_llm_models`, `api_test_llm`, `api_get_settings_agents`,
  `api_add_settings_agent`, `api_update_settings_agent`, `api_delete_settings_agent`,
  `api_test_settings_agent`, `api_change_password`, `api_version`,
  `api_version_check`, `api_get_git_history_settings`, `api_update_git_history_settings`.
- **containers** : `api_list_containers`, `api_get_container`, `api_start_container`,
  `api_stop_container`, `api_restart_container`, `api_get_container_edit_spec`,
  `api_update_container`, `api_update_container_image`, `api_container_logs`,
  `ws_container_logs_stream`, `ws_events`, `api_presence_heartbeat`,
  `ws_container_exec`, `api_container_exec`, `api_container_stats`, `api_get_ports`,
  `api_update_check`.
- **stacks** : `api_list_stacks`, `api_stack_containers`, `api_stack_start`,
  `api_stack_stop`, `api_stack_restart`, `api_stack_update`, `api_stack_update_check`,
  `api_stack_logs`, `api_list_stack_files`, `api_list_stack_files_with_content`,
  `api_get_stack_file`, `api_put_stack_file`, `api_set_file_permissions`,
  `api_get_compose`, `api_put_compose`, `api_get_env`, `api_put_env`,
  `api_create_stack`, `api_import_stack`, `api_delete_stack`, `api_deploy_stack`,
  `api_stack_history`, `api_stack_version`, `api_restore_stack`.
- **chat** : `chat_endpoint`, `validate_exec_endpoint`, `get_soul_endpoint`,
  `update_soul_endpoint`, `chat_stream_ws`.

### Tests modifiés
Aucun test existant modifié (objectif zéro atteint). Un nouveau fichier de smoke
test ajouté (7 tests).

### Résultats pytest (étape par étape)
| Étape | suite complète |
|---|---|
| Baseline | 296 ✓ |
| 1 helpers | 296 ✓ |
| 2 agents + settings | 296 ✓ |
| 3 containers | 296 ✓ |
| 4 stacks | 296 ✓ |
| 5 chat | 296 ✓ |
| 6 nettoyage + smoke | **303 ✓** (296 + 7) |

### Ce qui RESTE dans api.py (documenté, volontaire)
La façade : `router = APIRouter(prefix="/api")`, les 5
`router.include_router(...)`, l'injection
`agent_manager.broadcast_agent_event = _broadcast_agent_event`, et les ré-exports
(`agent_manager`, `LLMClient`, helpers, symboles llm, `_save_agents`).

### Points de blocage / incidents
1. **Script de suppression** : première version comparait `lines[i+1].strip() ==
   target` (bug : le titre contient `# `). Corrigé en comparaison de sous-chaîne
   (`target in lines[i+1]`).
2. **FastAPI 0.141.1** : les `include_router` créent des `_IncludedRouter`
   paresseux (pas de liste de routes directe) — validé via `app.openapi()`
   (61 méthodes HTTP /api, identique au compte d'origine) et TestClient
   (y compris les 4 WebSocket, close 4401 sans auth).
3. Aucun cycle d'import : vérifié en important les sous-modules dans plusieurs
   ordres (api en premier / en dernier) et par le smoke test.

### Vérifications finales
- 61 méthodes HTTP `/api` via `app.openapi()` (identique à l'origine) ✓
- 4 WebSocket routes opérationnelles ✓
- `agent_manager.broadcast_agent_event is api._broadcast_agent_event` ✓
- `_resolve_agent` / `_sse_action_response` lisent `agent_manager` via la façade
  (monkeypatch des tests effectif) ✓
- `python -m pytest -q` → **303 passed** ✓

La présente trace (`docs/refactor-routes-api.md`) contient l'historique complet :
chaque étape, fichiers créés/modifiés, symboles déplacés, tests et résultats
pytest, points de blocage.
