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

## Casse des noms de stack (gérée uniformément)

Docker **minusculise** toujours le label `com.docker.compose.project`, tandis
que le dossier créé par Docky peut garder sa casse d'origine (ex. le dossier
`MyApp` vs le label `myapp`). Une comparaison sensible à la casse
(classiquement `(stacks_dir / project).exists()`) reconnaissait alors la stack
comme **externe** — ou, pire, ré-écrivait le compose dans un second dossier à
la casse distincte (concaténation `stacks_dir / project` non résolue).

### Helper unique

Un helper unique `_find_managed_stack_dir(project)` dans
`agent/docker_manager.py` rapproche le label (minusculisé) des dossiers de
`/data/stacks/` **insensiblement à la casse** et renvoie le **chemin réel** du
dossier trouvé (casse d'origine). Il est utilisé par les deux flux concernés :

- `_get_container_full_spec` (modale d'édition) — le champ `stack` / `managed`
  reflète la casse réelle du dossier ;
- `update_container` (`_update_compose_container`) — le **nom réel** du dossier
  est transmis pour l'écriture du compose, le `_git_save` et le `compose_up`,
  aucune concaténation sensible à la casse ne peut donc créer un second dossier.

### Collisions de casse

Si plusieurs dossiers ne diffèrent que par la casse (`MyApp`/`myapp`), la
correspondance **exacte** gagne ; si le label ne peut être rapproché de façon
**unique** (ex. `MyApp` + `MYAPP` pour un label `myapp`), le helper renvoie
`None` et la stack est traitée comme externe (refusée proprement, message
`Les stacks externes ne peuvent pas être éditées` inchangé) — aucun crash.

### Tests

`agent/tests/test_docker_manager_managed_case.py` : dossier `MyApp` + label
`myapp` → `managed True` et édition fonctionnelle (écrit dans `MyApp`, aucun
`myapp` créé) ; label sans dossier → refusé ; collisions de casse (résolution
exacte et cas ambigu sans crash).

---

## autoFix par emplacement du compose

### Problème

Une stack **importée puis déployée depuis Docky** peut avoir un label projet qui
ne correspond **plus** au nom du dossier. C'est le cas quand le compose déclare
une surcharge de nom — clé top-level `name:` ou variable d'environnement
`COMPOSE_PROJECT_NAME` — : Docker Compose utilise alors ce nom surchargé comme
`com.docker.compose.project`, qui peut différer du dossier `MyApp` créé par
Docky. La reconnaissance par label seul (même insensible à la casse) classait
alors la stack comme **externe** et refusait son édition, alors que son compose
vit bien dans `/data/stacks/`.

### Helper unique `_resolve_managed_stack(container_labels)`

Un helper unique dans `agent/docker_manager.py` décide si un container appartient
à une stack **gérée** par Docky. Il renvoie `(stack_name, dir_path)` ou `None` :

1. **Label projet** : si `com.docker.compose.project` matche un dossier de
   `get_stacks_dir()` insensiblement à la casse (le fix récent, via
   `_find_managed_stack_dir`) → managé.
2. **Emplacement du compose** : sinon, si les labels `working_dir` /
   `config_files` pointent **dans** `get_stacks_dir()/<dossier>` (chemins
   normalisés via `Path.resolve()`) → managé, en restituant le **nom réel** du
   dossier.

Le nom renvoyé est toujours le nom **réel** du dossier (casse d'origine), donc
l'écriture du compose, le `_git_save` et le `compose_up` réutilisent le dossier
exact — aucun second dossier n'est créé.

Il est utilisé par les trois flux concernés :

- `_get_container_full_spec` (modale d'édition) — `managed` / `stack` ;
- `update_container` (`_update_compose_container`) — édition fonctionnelle ;
- `_container_compose_context` (stop / delete / `_edit_managed_compose`) —
  cohérence de l'auto-édition du compose.

### Preuve anti-doublon

`_update_compose_container` écrit le compose puis appelle `compose_up(project)`
(le nom du dossier). `compose_up` → `_resolve_stack_compose` → `_run_compose` →
`_resolve_compose_args` exécute `docker compose -f /data/stacks/<dossier>/docker-compose.yml up -d --remove-orphans`
depuis le dossier Docky. Le nom de projet est alors déterminé par le compose
lui-même (surcharge `name:` / `COMPOSE_PROJECT_NAME`, sinon le nom du dossier).
Comme le déploiement d'origine a été fait depuis le même dossier Docky
(`working_dir` dans `/data/stacks/`), le nom de projet recalculé est **identique**
à celui des containers existants → `docker compose up` retrouve le même projet et
**ne crée aucun doublon**. Le raisonnement est vérifié dans le code réel.

### Cas jamais redéployé (working_dir hors `/data/stacks`)

Si `working_dir` / `config_files` pointent **hors** de `/data/stacks/` (stack
externe jamais déployée depuis Docky), il n'y a **pas** d'auto-édition : le
message de refus 1464 est enrichi —
`Les stacks externes ne peuvent pas être éditées. Si cette stack a été importée,
déployez-la depuis Docky pour reprendre les containers.` — et le toast frontend
(`modals.js openContainerEdit`) porte le même guidage.

### Tests

`agent/tests/test_docker_manager_managed_case.py` :

- surcharge `name:` + `working_dir` dans stacks → `managed True` + édition
  fonctionnelle (écrit dans le dossier réel, aucun dossier surchargé créé) ;
- `working_dir` hors `/data/stacks` → refusé avec message enrichi ;
- sans labels compose → chemin standalone inchangé.

---

## Fichiers modifiés

| Fichier | Changement |
|---------|------------|
| `orchestrator/app/static/js/dashboard.js` | `refreshStacks(force)` contourne le garde-fou `_lastGridKey` |
| `orchestrator/app/static/js/modals.js` | `applyContainerEdit` → `refreshStacks(true)` |
| `agent/docker_manager.py` | `_update_compose_container` crée un backup git (`_git_save`) ; helper `_resolve_managed_stack` (autoFix par emplacement) ; message de refus enrichi |
| `agent/tests/test_docker_manager_container_edit.py` | nouveaux tests (backup git / standalone) |
| `agent/tests/test_docker_manager_managed_case.py` | nouveaux tests (autoFix par emplacement du compose) |
| `orchestrator/app/static/js/modals.js` | toast de refus enrichi (guidage import) |
| `orchestrator/tests/test_agent_manager.py` | nouveaux tests (invalidation de cache) |

## Validation

- `timeout 300 .venv/bin/python -m pytest -q` → **560 passed** (552 existants +
  8 nouveaux).
