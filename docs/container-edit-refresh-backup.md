# Édition de container — refresh de l'affichage & backup git

Ce document couvre deux comportements de l'**éditeur de container individuel**
(modale « ✏ nom ») :

- **A. Refresh de l'affichage** après une édition de container (l'UI restait sur
  l'ancienne version) ;
- **B. Backup git (historique de stack)** lors d'une édition qui modifie le
  compose.

Flux concerné : `modals.js openContainerEdit` → `_renderContainerEditForm` →
`applyContainerEdit` (POST `/api/containers/{id}/update`) →
`orchestrator/.../agent_manager.update_container` →
`agent/docker_manager.update_container` (`_update_compose_container` /
`_recreate_container`).

---

## Réponse à Q3 — un backup existait-il ?

**Non.** Avant cette correction, le chemin d'édition de container
(`update_container` → `_update_compose_container`) modifiait bien le
`docker-compose.yml`, mais ne créait **aucun** commit git de la stack.

Le code qui produit l'historique git vit dans `agent/docker/git_history.py`
(`_git_init`, `_git_save`, `_git_restore`, `_git_cleanup`). Il était déjà
appelé par :

- `save_stack_file` (éditeur de fichiers de stack) ;
- `create_stack` (création d'une stack) ;
- `import_stack` (import d'une stack) ;
- `_git_restore` (restauration d'une version).

Il ne l'était **pas** par le flux d'édition de container. Le backup est donc
**ajouté** (Partie B).

---

## A — Refresh de l'affichage après édition

### Constat

`applyContainerEdit` (modals.js) appelle déjà `await this.refreshStacks()`
après un POST réussi. Côté orchestrateur, `agent_manager.update_container`
invalide le cache (`invalidate_cache(agent_name)`) sur succès, puis le
reconstruit immédiatement — un refresh ultérieur renvoie donc la nouvelle
config.

Cependant, `refreshStacks()` (dashboard.js) possède un **garde-fou de
déduplication** `_lastGridKey` :

```js
const gridKey = JSON.stringify(stacksResp) + '|' + JSON.stringify(this._allContainersCache);
if (this._lastGridKey === gridKey) return;   // ← peut masquer le re-render
```

Après une édition, si la sérialisation des données fraîches coïncide avec la
dernière clé rendue (ou si un refresh auto/événementiel a déjà posé cette clé),
le re-render est sauté : la vue reste sur l'ancienne version alors même que le
compose et le cache sont corrects.

### Correctif

- `refreshStacks(force = false)` (dashboard.js) : un paramètre `force` contourne
  le garde-fou `_lastGridKey` et force le re-render de la vue courante.
- `applyContainerEdit` (modals.js) appelle désormais `await this.refreshStacks(true)`
  après succès.

Ainsi, après édition, la vue (grid, tableau ou cartes de stack) reflète la
nouvelle configuration (image, ports, env, restart policy…). Le cas d'une stack
est couvert car `refreshStacks` re-rend tout le dashboard, y compris les cartes
de stack.

Le cache stale-while-revalidate est invalidé côté orchestrateur
(`update_container`), ce qui garantit que le `refreshStacks(true)` relit l'état
fraîchement recréé de l'agent.

## B. Backup git sur édition de container

### Correctif

Dans `agent/docker_manager.py`, `_update_compose_container` (chemin emprunté
pour une stack **gérée** par Docky) appelle maintenant `_git_save(project, ...)`
après l'écriture du `docker-compose.yml` modifié, **avant** le redeploy. Ceci
réutilise la mécanique existante (`_git_save` → `_git_init` + commit +
rétention) — aucune réinvention.

Le container **standalone** (sans projet compose / sans stack gérée) passe par
`_recreate_container`, qui ne touche pas de fichier de stack : il n'y a **pas**
de backup git possible, et il est **ignoré proprement** (aucun appel à
`_git_save`, aucune erreur).

### Tests

- **Agent** (`agent/tests/test_docker_manager_container_edit.py`) :
  - stack gérée → `_git_save` appelé avec la bonne stack (`myapp`) et la
    nouvelle config écrite dans le compose ;
  - édition sans changement de config → backup quand même, sans crash ;
  - container standalone → `_recreate_container` appelé, **aucun** `_git_save`,
    aucun crash.
- **Orchestrateur** (`orchestrator/tests/test_agent_manager.py`) :
  - `update_container` invalide le cache après un succès (un refresh UI reflète
    la nouvelle config) ;
  - `update_container` n'invalide **pas** le cache sur échec.

---

## Fichiers modifiés

| Fichier | Changement |
|---------|------------|
| `orchestrator/app/static/js/dashboard.js` | `refreshStacks(force)` contourne le garde-fou `_lastGridKey` |
| `orchestrator/app/static/js/modals.js` | `applyContainerEdit` → `refreshStacks(true)` |
| `agent/docker_manager.py` | `_update_compose_container` crée un backup git (`_git_save`) |
| `agent/tests/test_docker_manager_container_edit.py` | nouveaux tests (backup git / standalone) |
| `orchestrator/tests/test_agent_manager.py` | nouveaux tests (invalidation de cache) |

## Validation

- `timeout 300 .venv/bin/python -m pytest -q` → **417 passed** (412 existants +
  5 nouveaux).
