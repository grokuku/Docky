# Refactor `agent/docker_manager.py` → sous-modules cohésifs

Objectif : découper `agent/docker_manager.py` (3 827 lignes) en modules cohésifs
sous `agent/docker/`, SANS changement de comportement, en gardant la suite de
tests verte après chaque étape.

**Règle critique** : les tests monkeypatchent `agent.docker_manager.<symbole>`.
Stratégie choisie = **(a) façade ré-export** : chaque symbole déplacé est
ré-exporté dans le namespace `agent.docker_manager`, et le code interne des
sous-modules qui appelle un symbole potentiellement monkeypatché le résout via
le namespace `docker_manager` (helper `_dm()` à résolution tardive) afin que les
patches des tests continuent de prendre effet. Aucun fichier de test n'est modifié.

## État initial (baseline)
- `python -m pytest agent/tests -q` → **116 passed** (0.49s)
- `python -m pytest -q` → non encore vérifié à l'étape 1 (278 attendus)

## Étapes

---

## Étape 1 — Validation + chemins (TERMINÉE)

### Blocage rencontré et résolu : collision `agent/docker/` ↔ SDK `docker`
Créer un sous-package `agent/docker/` masque le SDK docker-py dès que le
dossier `agent` est un chemin direct de `sys.path` (cas de la config pytest
`pythonpath = ["orchestrator", "agent"]`) : `import docker` résolvait alors
`agent/docker/__init__.py` au lieu de `site-packages/docker` →
`ModuleNotFoundError: No module named 'docker.errors'`.

**Résolution** : `pyproject.toml` → `pythonpath = ["orchestrator", "."]`
(la racine du dépôt). Ainsi `agent`/`orchestrator` sont résolus via la racine,
`import docker` retrouve le SDK (aucun dossier `docker/` à la racine), et
`import agent.docker.*` vise nos sous-modules. Vérifié : `pytest -q` → 278 verts
avec cette config, agent seul → 116 verts. En production (uvicorn `agent.main:app`,
cwd `/app`, code sous `/app/agent`), `agent` n'est PAS sur sys.path → aucun risque.

### Fichiers créés
- `agent/docker/__init__.py` — docstring package.
- `agent/docker/validation.py` — `validate_stack_name`, `validate_filename`,
  `safe_join`, `_stack_dir`, `get_stacks_dir`, `_STACK_NAME_RE`, `_SAFE_FILENAME_RE`.

### Fichiers modifiés
- `agent/docker_manager.py` : suppression des définitions déplacées + des
  alias `_validate_stack_name`/`_validate_filename`/`_stack_file_path`
  (prouvés inutilisés hors docker_manager) ; appels internes remplacés par
  `validate_stack_name` / `safe_join` ; `import re` top-level retiré (devenu
  inutile) ; import + ré-export depuis `agent.docker.validation`.
- `pyproject.toml` : `pythonpath` corrigé (voir blocage).

### Tests
- `python -m pytest agent/tests -q` → **116 passed**.

---

## Étape 2 — Update-check (TERMINÉE)

### Fichier créé
- `agent/docker/update_check.py` — toutes les fonctions update-check :
  `_UPDATE_CHECK_TTL`, `_update_check_cache`, `_split_image_reference`,
  `_canonical_repository`, `_update_cache_key`, `_update_check_cache_info`,
  `_clean_update_check_cache`, `_extract_remote_digests`, `_dedupe_preserve_order`,
  `_short_digest`, `_short_digests`, `_remote_distribution_info`,
  `_remote_manifest_check`, `_remote_manifest_digests`, `_invalidate_update_check`,
  `_invalidate_stack_update_cache`, `_local_repo_digests`,
  `_local_repo_digests_for_image`, `check_image_update`.

### Stratégie (a) appliquée
`docker_manager.py` ré-exporte tous ces symboles (namespace identique).
Les fonctions de `update_check.py` appellent via `_dm()` (résolution tardive du
namespace `agent.docker_manager`) les symboles monkeypatchés par les tests :
- `get_docker_client` (patché via fixture `mock_docker_client` et
  `_patch_container_lookup`) ;
- `_remote_manifest_check` (patché par `_patch_container_lookup`) ;
- `_local_repo_digests` (patché par `_patch_container_lookup`) ;
- `_compose_project_services` (reste dans docker_manager — appelé par
  `_invalidate_stack_update_cache`, pas patché mais résolu via `_dm()`).

`_update_check_cache` est le MÊME objet partagé (`dm._update_check_cache is
update_check._update_check_cache == True`) : le fixture `_clear_update_check_cache`
des tests continue de le vider. `subprocess.run` est patché au niveau du module
`subprocess` partagé → inchangé. Ajout de l'import `docker.errors` (manquant à la
1re écriture, corrigé immédiatement).

### Fichiers modifiés
- `agent/docker_manager.py` : suppression du bloc « Update check » (551 lignes),
  remplacé par l'import/ré-export depuis `agent.docker.update_check`.

### Tests
- `python -m pytest agent/tests -q` → **116 passed**
- `python -m pytest -q` → **278 passed**

---

## Étape 3 — Streaming compose (TERMINÉE)

### Fichier créé
- `agent/docker/compose_stream.py` — `STREAM_IDLE_TIMEOUT`, `STREAM_EVENT_OUTPUT`,
  `STREAM_EVENT_RESULT`, `StreamCommandError`, `_run_compose`, `_run_command_stream`,
  `_stream_compose`, `_stream_compose_step`, `_stream_command_step`,
  `_compose_up_command`, `_compose_down_command`, et les générateurs publics
  `stream_start_stack`, `stream_stop_stack`, `stream_restart_stack`,
  `stream_update_stack`, `stream_deploy_stack`.

### Décision (documentée)
Les `stream_*` publics + helpers de streaming sont déplacés car leurs seules
dépendances restantes (`_resolve_compose_args`, `_resolve_stack_compose`,
`_invalidate_stack_update_cache`) sont simples et résolues via `_dm()` à
l'appel. Les routes patchent `dm.stream_*` en bloc (jamais l'intérieur) → la
façade suffit. **Non déplacés** (restent dans docker_manager) :
`_resolve_compose_args`, `_compose_file_path`, `_resolve_stack_compose`,
`_compose_project_services`, `_compose_service_update_plan`,
`_stream_compose_container_update` (dépend d'une chaîne de fonctions
containers/update) — documenté ici, c'est un choix de sécurité.

### Fichiers modifiés
- `agent/docker_manager.py` : suppression des définitions déplacées
  (constantes + 13 fonctions), remplacées par l'import/ré-export depuis
  `agent.docker.compose_stream`.

### Tests
- `python -m pytest agent/tests -q` → **116 passed**
- `python -m pytest -q` → **278 passed**

---

## Étape 4 — Git + import (TERMINÉE)

### Incident & correction (important)
Le script de suppression de l'étape 4 triait les plages de lignes par **nom**
au lieu de la **position** : les indices de lignes devenaient invalides pendant
les suppressions et le fichier `docker_manager.py` s'est corrompu (section
« Ports » endommagée). **Correction** : reconstruction de `docker_manager.py`
à partir de la version git de référence (`git show HEAD:agent/docker_manager.py`)
en appliquant toutes les suppressions/ré-exports de manière **triée par
position décroissante** (sûre), puis vérification complète (116 + 278 verts).
Les sous-modules `agent/docker/*` n'ont pas été affectés (déjà écrits).

### Fichiers créés
- `agent/docker/git_history.py` — `_git_init`, `_git_save`, `_get_git_history`,
  `_get_git_version`, `_git_restore`, `_git_cleanup`, `get_history_settings`,
  `set_history_settings`. Autonome (utilise `agent.config.get_data_dir`,
  `subprocess`, `yaml`) → aucun cycle.
- `agent/docker/import_stack.py` — `import_stack`. Dépend de
  `agent.docker.validation.validate_stack_name` et de `agent.config.get_data_dir` ;
  `_git_init`/`_git_save` appelés via `_dm()` (monkeypatchés par
  `test_docker_manager_import.py` sur `agent.docker_manager`).

### Fichiers modifiés
- `agent/docker_manager.py` : suppression de `import_stack` + des 8 fonctions
  git ; ré-exports ajoutés (bloc git_history + import_stack).

### Tests
- `python -m pytest agent/tests -q` → **116 passed**
- `python -m pytest -q` → **278 passed**

---

## Étape 5 — Ports + events (TERMINÉE)

### Fichiers créés
- `agent/docker/ports.py` — `get_used_ports`, `_scan_system_ports`,
  `_parse_ss_output`, `_parse_netstat_output`, `_parse_proc_net`.
  `get_used_ports` appelle `list_containers` via `_dm()` (test routes le
  monkeypatche : `dm.get_used_ports`).
- `agent/docker/events.py` — `watch_docker_events`, qui appelle
  `get_docker_client` via `_dm()`.

### Fichiers modifiés
- `agent/docker_manager.py` : suppression des 6 fonctions, ajout des ré-exports
  ports + events.

### Smoke test
- `agent/tests/test_docker_modules_import.py` (NOUVEAU) : importe les 7
  sous-modules, vérifie que tous les symboles attendus sont ré-exportés dans
  `agent.docker_manager`, et que `_update_check_cache` est bien un objet partagé
  unique.

### Tests
- `python -m pytest agent/tests -q` → **125 passed** (116 + 9 smoke)
- `python -m pytest -q` → **278 passed** (suite historique, inchangée)

---

## État final / rapport

### Fichiers créés
| Fichier | Contenu |
|---|---|
| `agent/docker/__init__.py` | docstring package |
| `agent/docker/validation.py` | validate_stack_name, validate_filename, safe_join, _stack_dir, get_stacks_dir, regexes |
| `agent/docker/update_check.py` | tout le bloc update-check (check_image_update, _remote_manifest_check, _extract_remote_digests, _dedupe_preserve_order, _local_repo_digests, cache partagé, etc.) |
| `agent/docker/compose_stream.py` | StreamCommandError, _run_compose, _run_command_stream, _stream_compose(_step/_command_step), _compose_up/down_command, stream_start/stop/restart/update/deploy_stack, constantes STREAM_* |
| `agent/docker/git_history.py` | _git_init/_git_save/_get_git_history/_get_git_version/_git_restore/_git_cleanup, get/set_history_settings |
| `agent/docker/import_stack.py` | import_stack |
| `agent/docker/ports.py` | get_used_ports, _scan_system_ports, _parse_ss_output, _parse_netstat_output, _parse_proc_net |
| `agent/docker/events.py` | watch_docker_events |
| `docs/refactor-docker_manager.md` | cette trace |

### Fichiers modifiés
- `agent/docker_manager.py` : **3 827 → 2 265 lignes** (façade + fonctions
  containers/stacks/files restantes).
- `pyproject.toml` : `pythonpath = ["orchestrator", "."]` (blocage collision
  `agent/docker/` ↔ SDK docker, documenté en étape 1).
- `agent/tests/test_docker_modules_import.py` : NOUVEAU (smoke test).

### Symboles déplacés par module
- **validation** : validate_stack_name, validate_filename, safe_join, _stack_dir,
  get_stacks_dir, _STACK_NAME_RE, _SAFE_FILENAME_RE (+ alias supprimés
  _validate_stack_name / _validate_filename / _stack_file_path, prouvés
  inutilisés hors docker_manager).
- **update_check** : les 19 symboles du bloc update-check.
- **compose_stream** : les 16 symboles streaming + constantes.
- **git_history** : les 8 fonctions git.
- **import_stack** : import_stack.
- **ports** : les 5 fonctions.
- **events** : watch_docker_events.

### Tests mis à jour
Aucun test existant modifié (stratégie a). Un nouveau fichier de smoke test ajouté.

### Résultats pytest (étape par étape)
| Étape | agent/tests | suite complète |
|---|---|---|
| Baseline | 116 ✓ | — |
| 1 validation | 116 ✓ | 278 ✓ |
| 2 update-check | 116 ✓ | 278 ✓ |
| 3 compose-stream | 116 ✓ | 278 ✓ |
| 4 git+import | 116 ✓ | 278 ✓ |
| 5 ports+events (+smoke) | **125 ✓** | **278 ✓** |

### Ce qui RESTE dans docker_manager.py (documenté, volontaire)
Fonctions containers/stacks/files fortement interdépendantes : get_docker_client,
_container_to_dict, list_containers/get_container/start/stop/restart,
_get_container_full_spec, logs/stats/exec (dont exec_interactive_start),
compose_* (start/up/down/stop/restart/pull), update_container + helpers de spec,
_recreate_container, _update_compose_container(_image), _stream_compose_container_update,
update_container_image, stream_update_container_image, update_stack,
check_stack_update, system_prune, is_editable_stack_file/get_stack_files/
get_stack_file/save_stack_file, create_stack, delete_stack, deploy_stack,
set_file_permissions, et les helpers de résolution compose (_resolve_stack_compose,
_compose_file_path, _resolve_compose_args, _compose_project_services,
_compose_service_update_plan). NON extraits : ces fonctions se référencent en
cascade (spec→attrs, recreate→spec, compose→resolve, update→invalidate) ; les
extraire demanderait de nombreuses injections via `_dm()` sans bénéfice de
cohésion immédiat, pour un risque accru — conformément à la consigne « ne force
pas une extraction risquée ».

### Points de blocage
1. **Collision `agent/docker/` ↔ SDK docker** (résolu) : nécessitait la correction
   de `pythonpath` dans pyproject.toml (documentée en étape 1).
2. **Bug de tri dans un script de suppression** (résolu) : la suppression par
   plages triées par nom corrompait le fichier ; reconstruction à partir de la
   version git + tri par position décroissante. Documenté en étape 4.

### Vérifications finales
- 40 symboles utilisés par routes.py/main.py : tous présents dans le namespace
  `agent.docker_manager` ✓
- `_update_check_cache` partagé (objet unique) ✓
- `python -m pytest -q` → **278 passed** (suite historique inchangée) ✓

### Nettoyage final
Après les extractions, `import json`, `import signal` et `Generator` (typing)
étaient devenus inutilisés dans `agent/docker_manager.py` → retirés (aucun
impact, la logique n'a pas été touchée).

### Validation finale (après nettoyage)
- `python -m pytest agent/tests -q` → **125 passed**
- `python -m pytest -q` → **287 passed** (278 historiques + 9 smoke tests)

La présente trace (`docs/refactor-docker_manager.md`) contient l'historique
complet : chaque étape, fichiers créés/modifiés, symboles déplacés, tests et
résultats pytest, points de blocage.
