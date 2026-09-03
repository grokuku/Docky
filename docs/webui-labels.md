# WebUI par container (labels `docky.webui.*`)

Chaque container peut exposer une ou plusieurs adresses web via des labels
Docker. Un bouton 🌐 apparaît alors dans les actions du container (mode grille
et mode tableau) pour ouvrir l'adresse, et une section d'édition dédiée est
disponible dans la modale de container.

## Contrat de données

### Labels

- `docky.webui.<n>` = adresse web (n = 1, 2, 3, …).
- `docky.webui.<n>.name` = libellé optionnel (ex. `Admin`).

Exemple :

```yaml
services:
  web:
    image: nginx:latest
    labels:
      docky.webui.1: http://192.168.1.10:8080
      docky.webui.1.name: Admin
      docky.webui.2: :8080
```

### Valeur d'adresse — résolution de l'option B

- Si l'adresse commence par `http://` ou `https://` → utilisée telle quelle
  (**option A**).
- Sinon (ex. `:8080`, `/admin`) → préfixée par l'URL publique de l'agent
  (**option B**).

**Choix de résolution : côté frontend.** L'agent renvoie l'URL **brute**
(exactement telle que stockée dans le label) et c'est le frontend qui résout
l'option B contre l'URL de l'agent. Raison : l'agent ne connaît pas forcément
son URL publique (elle est configurée côté orchestrateur dans `settings.yaml`).
Le frontend dispose de cette URL via `/api/agents` (`agentsList`).

### Dict container exposé au frontend

Le dict container (construit par `_container_to_dict`) contient un champ
`webui` :

```json
"webui": [{"url": "http://192.168.1.10:8080", "name": "Admin"}, {"url": ":8080"}]
```

- Trié par `n` croissant.
- `name` optionnel (absent si le label `.name` n'est pas défini).
- L'URL est brute (option B non résolue côté agent).

### Sécurité

- Seuls les schémas `http` / `https` sont ouverts.
- Ouverture en `target="_blank" rel="noopener noreferrer"` (fenêtre de choix)
  ou `window.open(url, '_blank', 'noopener')` (ouverture directe).

## Parsing (agent)

`agent/docker_manager.py` :

- `_webui_label_numbers(labels)` — ensemble des `n` pour lesquels un label
  `docky.webui.<n>` existe (ignore les suffixes `.name`).
- `_parse_webui_labels(labels)` — liste ordonnée `[{url, name?}]` (URL brute).
- `_webui_labels_from_spec(webui)` — inverse : `[{url, name?}]` → labels
  `docky.webui.*`.

Le champ `webui` est intégré dans :

- `_container_to_dict` (liste des containers) ;
- `_get_container_full_spec` (spec d'édition, pré-remplissage du formulaire).

## Écriture

### Stack gérée (compose)

`_update_compose_container` écrit les labels `docky.webui.*` dans le service du
compose. Les anciens labels `docky.webui.*` sont **remplacés** ; les autres
labels du service sont **préservés** (l'UI n'expose pas les labels bruts, donc
une édition partielle ne doit pas les perdre).

### Standalone

`_recreate_container` applique les labels via `docker run --label` (le dict
`labels_dict` est déjà passé à `client.containers.run`). Les labels
`docky.webui.*` sont fusionnés dans ce dict (anciens remplacés). Le champ
`webui` est aussi ajouté à la détection de changement (`spec_changed`) pour
qu'une édition ne touchant que le WebUI déclenche bien une recréation.

## Bouton 🌐 + fenêtre de choix (frontend)

`orchestrator/app/static/js/dashboard.js` :

- `_webuiButton(c, agt)` — bouton 🌐 (icône `globe`) rendu dans
  `renderTableRow` et `renderGridContainerCard` si `container.webui` non vide.
- `openWebUI(containerId, agent)` :
  - 1 adresse → `window.open(url, '_blank', 'noopener')` ;
  - plusieurs → ouvre la **fenêtre de choix** (`#webui-modal`, pattern de la
    modale update-all) listant les adresses avec leur libellé si présent,
    sinon l'URL brute.
- `_resolveWebUIUrl(url, agent)` — résout l'option B contre l'URL de l'agent.
- `_renderWebUIModal(webui, agent)` / `closeWebUIModal()` — rendu / fermeture.

La modale `#webui-modal` est déclarée dans `orchestrator/templates/dashboard.html`.

## Édition dans la modale container (frontend)

`orchestrator/app/static/js/modals.js` :

- `_renderContainerEditForm` ajoute en **HAUT** (avant les onglets
  Infos/Ports/Volumes/Env/Réseau) une section « Accès Web (WebUI) » avec des
  lignes dynamiques `[Libellé (optionnel)] [Adresse] [✕]`.
- Pré-remplie depuis le spec (`spec.webui`).
- Une ligne vide en bas pour ajouter ; dès qu'une adresse est remplie, une
  nouvelle ligne vide apparaît (`_attachWebUIRowListener`).
- `✕` retire une ligne.
- `applyContainerEdit` n'envoie que les lignes avec une adresse non vide →
  payload `webui`. Validation http/https (ou relative `:port` / `/chemin`)
  avant envoi.

## Tests

- `agent/tests/test_docker_manager_webui.py` (12 tests) : parsing (1, plusieurs,
  avec/sans name, ordre), champ `webui` dans `_container_to_dict` et
  `_get_container_full_spec`, écriture compose (labels remplacés, non-webui
  préservés, effacement), standalone (labels appliqués au recreate), option B
  non résolue côté agent.
- `orchestrator/tests/test_api_routes.py` (2 tests) : pass-through de `webui`
  dans `edit-spec` et `update`.

Résultat : `timeout 300 python -m pytest -q` → 434 passed (420 + 14 nouveaux).
`node --check` sur `dashboard.js` et `modals.js` → OK.
