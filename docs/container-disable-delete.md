# Désactiver / Supprimer un container

Ce document décrit deux nouvelles actions du menu contextuel (clic droit) des
containers dans Docky :

1. **« Désactiver »** — arrête le container **et** l'empêche de démarrer
   automatiquement (restart policy → `no`).
2. **« Supprimer »** — supprime le container (force).

Les deux actions fonctionnent sur les containers **dans un compose** (gérés ou
externes) **et** sur les containers **externes / standalone**. Pour un
container d'une stack **gérée par Docky** (dossier dans ``/data/stacks/``),
l'action **modifie aussi le fichier compose** (voir section « Comportement
compose » ci-dessous).

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
ni les autres containers du projet ni le cycle de vie global :

- **Stack gérée par Docky** (`/data/stacks/<nom>`) : en plus de l'action Docker,
  le fichier compose est modifié (voir ci-dessous), puis un backup git de la
  stack est créé.
- **External** (projet Compose non géré) ou **standalone** : le container est
désactivé/supprimé individuellement. Le compose n'est pas modifié (les
  fichiers d'une stack externe vivent hors de ``/data/stacks/`` et Docky ne les
  possède pas).

## Comportement compose (stacks gérées)

Pour un container d'une **stack gérée**, le service est identifié via les labels
Docker ``com.docker.compose.project`` / ``com.docker.compose.service`` (le nom
de projet, minusculisé par Docker, est rapproché du dossier ``/data/stacks`` de
manière insensible à la casse).

### Désactiver

- Le fichier compose est édité pour mettre `restart: "no"` sur le service
  concerné (la clé est **ajoutée** si absente, ou **remplacée** si présente).
- Backup git de la stack (`_git_save`).
- Puis `docker update --restart=no` + `docker stop` : l'effet est immédiat sans
  redéploiement, et le service restera désactivé au prochain `up`/déploiement.

### Supprimer

- Le service est **retiré** de la section `services:` du fichier compose.
- Backup git de la stack (`_git_save`).
- Puis `docker rm -f`.

### Cas limites

- **Service introuvable** dans le compose (fichier présent mais le service a
déjà été retiré ou renommé) : le container est quand même désactivé/supprimé
  (action Docker), le fichier n'est pas modifié, et un `WARNING` est logué.
- **Service = dernier du compose** : le retrait laisse une section `services:`
vide — le fichier reste un YAML valide.
- **Compose invalide / illisible / non éditable** : le fichier n'est **jamais**
écrasé ni corrompu ; l'action Docker est exécutée seule et un `WARNING` est
logué.

### Choix d'édition du compose (format préservé)

L'édition est **textuelle et ciblée** (mode préféré) : seules les lignes
nécessaires sont touchées (remplacement/ajout de la ligne `restart:` ou
retrait du bloc de service), tout le reste — commentaires, ordre des clés,
lignes vides — est préservé **octet pour octet**. Un simple round-trip
`yaml.safe_load`/`yaml.dump` détruirait en effet tous les commentaires du
fichier.

Cette édition textuelle ne sait gérer que les services **bloc** classiques sous
une clé top-level `services:` (les noms de services doivent être sur leur
propre ligne). Dès qu'une structure exotique est détectée (syntaxe *flow*
`web: { image: nginx }`, ancres/merge, mise en page inhabituelle), un **fallback
PyYAML round-trip** est utilisé : il préserve le contenu sémantique et le bloc
d'en-tête `# @metadata` de Docky, mais formate le reste (les commentaires des
lignes sont perdus — même limitation que l'édition de container existante
`_update_compose_container`). En cas d'échec de ces deux approches, le fichier
n'est pas modifié.

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
- **Agent** (`agent/tests/test_docker_manager_compose_disable_delete.py`) :
  comportement des stacks gérées (compose `restart:no` + git, service retiré +
  git), cas limites (service introuvable, dernier service, compose invalide) et
aucune modification pour external / standalone.
- **Agent routes** (`agent/tests/test_agent_routes.py`) : routes
  `/disable` et `/delete` (succès + auth requise).
- **Orchestrateur client** (`orchestrator/tests/test_agent_manager.py`) :
  pass-through `disable_container` / `delete_container` (succès + échec).
- **Orchestrateur routes** (`orchestrator/tests/test_api_routes.py`) : routes
  `/api/containers/{id}/disable` et `/delete` (pass-through + validation
  agent).

## Fichiers modifiés

- `agent/docker_manager.py` — `disable_container`, `delete_container` et leurs
  helpers d'édition compose (`_container_compose_context`,
  `_edit_managed_compose`, `_text_edit_compose`, `_yaml_roundtrip_edit`).
- `agent/routes.py` — routes `/disable`, `/delete`.
- `orchestrator/app/agent_manager/client.py` — `disable_container`,
  `delete_container`.
- `orchestrator/app/routes/containers.py` — routes `/disable`, `/delete`.
- `orchestrator/app/static/js/dashboard.js` — entrées du menu contextuel,
  `confirmContainerDelete`, labels `containerAction`.
- `orchestrator/app/static/css/style.css` — style `ctx-menu-danger`.
- `agent/tests/conftest.py` — `FakeContainer.stop/update/remove`.
- `orchestrator/tests/conftest.py` — méthodes async mockées.
