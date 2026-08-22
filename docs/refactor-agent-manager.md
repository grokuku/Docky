# Refactor `orchestrator/app/agent_manager/client.py` → sous-modules cohésifs

Objectif : découper `orchestrator/app/agent_manager/client.py` (1 202 lignes)
en modules cohésifs sous `orchestrator/app/agent_manager/`, SANS changement de
comportement, en gardant la suite de tests verte après chaque étape.

**Règle critique** (même principe que les refactors `agent/docker_manager.py`
et `app/llm/client.py` réussis) : les tests monkeypatchent des symboles DANS le
namespace `app.agent_manager.client` :
- `app.agent_manager.client.time.time` (`test_agent_manager._patch_time`) pour
  piloter l'horloge du cache stale-while-revalidate ;
- `load_settings` est appelé au moment de l'appel par `translate_path` /
  `_load_agents` (les tests réécrivent `settings.yaml` et monkeypatchent
  `DOCKY_DATA_DIR` plutôt que `load_settings` lui-même) ;
- `app.routes.api.agent_manager` et `app.llm.client.agent_manager` sont
  remplacés par un mock dans `conftest.mock_agent_manager`.

Stratégie choisie = **(a) façade ré-export + méthodes extraites assignées sur
la classe** : les méthodes déplacées sont définies comme fonctions `self, ...`
dans les sous-modules puis ré-affectées à la classe `AgentManager` dans la
façade `client.py`. Le singleton `agent_manager = AgentManager()` reste défini
**dans** `client.py` (même emplacement), donc l'instance est unique. Les
appels internes entre méthodes passent par `self.*` (résolution tardive
naturelle) ; l'horloge monkeypatchée (`app.agent_manager.client.time.time`) est
résolue au moment de l'appel via le helper `cache._time()`. Aucun fichier de
test existant ne doit être modifié.

## Rappels / dépendances vérifiées au départ
- `app.routes.api` importe `agent_manager` depuis `app.agent_manager.client`.
- `app.llm.client` importe `agent_manager` depuis `app.agent_manager.client`.
- `app.main` importe `agent_manager` depuis `app.agent_manager.client`.
- Les tests importent `AgentManager` et `agent_manager` depuis
  `app.agent_manager.client`.
- `pyproject.toml` : `pythonpath = ["orchestrator", "."]` (ne pas revenir en
  arrière).

## État initial (baseline)
- `python -m pytest -q` → **292 passed** (9.17s)

## Étapes

---

## Étape 1 — paths.py (translate_path + path mappings) (TERMINÉE)

### Fichiers créés
- `orchestrator/app/agent_manager/paths.py` — `translate_path`.

### Fichiers modifiés
- `orchestrator/app/agent_manager/client.py` : suppression de la méthode
  `translate_path`, import de `app.agent_manager.paths` (alias `_paths`) et
  ré-affectation `AgentManager.translate_path = _paths.translate_path` dans le
  corps de la classe.

### Tests
- `python -m pytest -q` → **292 passed**

---

## Étape 2 — cache.py (stale-while-revalidate + load/save + rebuild) (TERMINÉE)

### Fichiers créés
- `orchestrator/app/agent_manager/cache.py` — `_AGENT_CACHE_TTL`,
  `_load_cache`, `_save_cache`, `refresh_cache`, `invalidate_cache`,
  `_get_cached_containers`, `_get_cached_stacks`, `_get_cached_ports`,
  `_get_cached_or_refresh`, `_refresh_cache_entry`, `_rebuild_aggregate_cache`,
  `ensure_cache`, `get_cached_containers`, `get_cached_stacks`,
  `get_cached_ports`, `refresh_all_caches`.

### Choix horloge (time.time patchable)
Le test `test_agent_manager._patch_time` patch
`app.agent_manager.client.time.time`. Comme `client.time` EST le module `time`
(singleton), patché globalement, tout `time.time()` d'un sous-module verrait
aussi le patch. Pour rester explicite et robuste, `cache.py` lit l'horloge via
`_time()` qui résout tardivement `app.agent_manager.client.time.time()` dans le
namespace de la façade (aucun cycle d'import, patch toujours effectif).

### Fichiers modifiés
- `orchestrator/app/agent_manager/client.py` : suppression des méthodes cache
  (`_load_cache`, `_save_cache`, `refresh_cache`, `invalidate_cache`,
  `_get_cached_*`, `_get_cached_or_refresh`, `_refresh_cache_entry`,
  `_rebuild_aggregate_cache`, `ensure_cache`, `get_cached_*`,
  `refresh_all_caches`) ; import `app.agent_manager.cache` (alias `_cache`) ;
  ré-affectation de chaque méthode sur la classe ; `_AGENT_CACHE_TTL` ré-exporté
  depuis `_cache._AGENT_CACHE_TTL` (namespace façade préservé).

### Tests
- `python -m pytest -q` → **292 passed**

---

## Étape 3 — events.py + résolution de la circularité (TERMINÉE)

### Fichiers créés
- `orchestrator/app/agent_manager/events.py` — `start_background_refresh`,
  `_connect_agent_events`, `_handle_agent_event`, `_incremental_refresh`.

### Solution de circularité adoptée
**Callback de broadcast injecté.** `AgentManager.__init__` initialise
`self.broadcast_agent_event = None`. `app.routes.api` définit
`_broadcast_agent_event(agent_name, action)` (qui itère `_events_clients`) et
l'injecte sur le singleton `agent_manager` au moment de son import, juste après
la définition de `_events_clients`. `events._handle_agent_event` n'importe plus
`app.routes.api` : il appelle `self.broadcast_agent_event(agent_name, action)`
s'il est callable. Le package agent_manager n'a donc plus aucune référence
(import) vers `app.routes`.

### Fichiers modifiés
- `orchestrator/app/agent_manager/client.py` : import `app.agent_manager.events`
  (alias `_events`) ; ajout `self.broadcast_agent_event = None` dans `__init__` ;
  suppression des méthodes `start_background_refresh`, `_connect_agent_events`,
  `_handle_agent_event`, `_incremental_refresh` et ré-affectation depuis
  `_events`.
- `orchestrator/app/routes/api.py` : ajout de `_broadcast_agent_event` et
  injection `agent_manager.broadcast_agent_event = _broadcast_agent_event`.

### Tests
- `python -m pytest -q` → **292 passed**

---

## Étape 4 — nettoyage final de la façade + smoke tests (TERMINÉE)

### Fichiers modifiés
- `orchestrator/app/agent_manager/client.py` : docstring mis à jour (façade +
  description des sous-modules) ; suppression de `import os` (devenu inutile
  après extraction du cache) ; le reste des imports est soit utilisé par la
  façade, soit un ré-export volontaire (`_AGENT_CACHE_TTL`).

### Fichier créé
- `orchestrator/tests/test_agent_manager_modules_import.py` (NOUVEAU, 4 tests
  smoke) : vérifie l'exposition des symboles de la façade, la ré-affectation
  des méthodes extraites sur `AgentManager`, l'unicité du singleton et du hook
  `broadcast_agent_event`, et l'absence de cycle d'import (appel de
  `cache._time()`).

### Tests
- `python -m pytest -q` → **296 passed** (292 historiques + 4 smoke)

---

## État final / rapport

### Fichiers créés
| Fichier | Contenu |
|---|---|
| `orchestrator/app/agent_manager/paths.py` | `translate_path` |
| `orchestrator/app/agent_manager/cache.py` | `_AGENT_CACHE_TTL`, `_load_cache`, `_save_cache`, `refresh_cache`, `invalidate_cache`, `_get_cached_containers`, `_get_cached_stacks`, `_get_cached_ports`, `_get_cached_or_refresh`, `_refresh_cache_entry`, `_rebuild_aggregate_cache`, `ensure_cache`, `get_cached_containers`, `get_cached_stacks`, `get_cached_ports`, `refresh_all_caches` + helper `_time()` |
| `orchestrator/app/agent_manager/events.py` | `start_background_refresh`, `_connect_agent_events`, `_handle_agent_event`, `_incremental_refresh` |
| `orchestrator/tests/test_agent_manager_modules_import.py` | 4 tests smoke (imports + ré-affectation + singleton + pas de cycle) |
| `docs/refactor-agent-manager.md` | cette trace |

### Fichiers modifiés
- `orchestrator/app/agent_manager/client.py` : **1 202 → 826 lignes** (façade +
  `AgentManager` HTTP/streaming + ré-affectations + singleton). Imports retirés :
  `os`. Docstring de module mis à jour.
- `orchestrator/app/routes/api.py` : ajout de `_broadcast_agent_event` +
  injection `agent_manager.broadcast_agent_event = _broadcast_agent_event`
  (résolution de la circularité).

### Symboles déplacés par module
- **paths** : `translate_path`.
- **cache** : `_AGENT_CACHE_TTL`, `_load_cache`, `_save_cache`, `refresh_cache`,
  `invalidate_cache`, `_get_cached_containers`, `_get_cached_stacks`,
  `_get_cached_ports`, `_get_cached_or_refresh`, `_refresh_cache_entry`,
  `_rebuild_aggregate_cache`, `ensure_cache`, `get_cached_containers`,
  `get_cached_stacks`, `get_cached_ports`, `refresh_all_caches`.
- **events** : `start_background_refresh`, `_connect_agent_events`,
  `_handle_agent_event`, `_incremental_refresh`.

### Tests modifiés
Aucun test existant modifié. Un nouveau fichier de smoke test ajouté
(4 tests).

### Résultats pytest (étape par étape)
| Étape | suite complète |
|---|---|
| Baseline | 292 ✓ |
| 1 paths | 292 ✓ |
| 2 cache | 292 ✓ |
| 3 events + circularité | 292 ✓ |
| 4 nettoyage + smoke | **296 ✓** (292 + 4) |

### Solution de circularité adoptée
Callback de broadcast injecté (décrite à l'étape 3) :
- `AgentManager.__init__` → `self.broadcast_agent_event = None`.
- `app.routes.api` définit `_broadcast_agent_event(agent_name, action)` après
  `_events_clients` et l'injecte sur le singleton importé.
- `events._handle_agent_event` appelle ce callback s'il est callable ; il
  n'y a plus aucun `import app.routes` dans le package `app.agent_manager`.

### Ce qui RESTE dans client.py (documenté, volontaire)
`AgentManager` et toutes ses méthodes HTTP/streaming : `__init__`, `_load_agents`,
`reload`, `list_agents`, `ping_agent`, `ping_all`, `_request`, `_parse_sse_event`,
`_stream_request`, `_consume_stream`, `_agent_error`, tous les `get_*`/`stream_*`
conteneurs/stacks/ports/git, et les agrégats `get_all_containers`,
`get_all_stacks`, `get_all_ports`. Le singleton `agent_manager = AgentManager()`
reste défini en fin de façade. `STREAM_TIMEOUT` reste dans la façade (utilisé par
`_stream_request`). `_AGENT_CACHE_TTL` est ré-exporté depuis `cache`.

### Points de blocage / incidents
1. **Horloge du cache** : le test patch `app.agent_manager.client.time.time`.
   Vérifié que `client.time` est le module `time` singleton (le patch est
   global). Choix d'un helper `cache._time()` résolvant la façade au moment de
   l'appel pour rester explicite, robuste et sans cycle.
2. Aucun cycle d'import : vérifié en important les sous-modules dans plusieurs
   ordres et par le smoke test (`cache._time()` résout la façade sans
   `ImportError`).

### Vérifications finales
- `AgentManager.translate_path is paths.translate_path` ✓
- Les 15 méthodes cache et 4 méthodes events sont ré-affectées sur
  `AgentManager` (smoke test) ✓
- `agent_manager` singleton unique, instance d'`AgentManager` ✓
- Plus aucun `from app.routes import api` dans `app.agent_manager` ✓
- `python -m pytest -q` → **296 passed** ✓

La présente trace (`docs/refactor-agent-manager.md`) contient l'historique
complet : chaque étape, fichiers créés/modifiés, symboles déplacés, tests et
résultats pytest, solution de circularité et points de blocage.
