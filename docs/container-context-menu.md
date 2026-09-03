# Éditeur de container : modale alignée mockup, onglets réparés, menu contextuel

Ce document décrit trois améliorations de l'UI de l'éditeur de container de
Docky :

1. La modale réelle est alignée sur le mockup validé (section WebUI en haut,
   formulaire Infos complet).
2. Les onglets Infos/Ports/Volumes/Env/Réseau fonctionnent (bascule de panneau).
3. L'ouverture de l'éditeur passe du double-clic au clic droit via un menu
   contextuel personnalisé.

## 1. Modale alignée sur le mockup

### Pourquoi la modale réelle différait du mockup (cause)

Le rendu réel de `_renderContainerEditForm` (modals.js) était déjà proche du
mockup, mais deux écarts subsistaient :

- **Absence du lien « + Ajouter une adresse »** dans la section WebUI. Le
  mockup prévoit une ligne vide en bas qui se duplique dès qu'une adresse est
  remplie **et** un lien explicite « + Ajouter une adresse ». Le lien manquait.
- **Absence du champ « Command »** dans le formulaire Infos. Le mockup liste
  Nom, Image, Stack, Restart **et Command** ; le champ Command n'était pas
  rendu ni collecté à la sauvegarde.

### Correction apportée

- Ajout du bouton « + Ajouter une adresse » (`_addWebUIRow`) sous la section
  WebUI, qui insère une ligne vide `[Libellé (optionnel)] [Adresse] [✕]`.
- Ajout du champ « Commande » (`#edit-container-command`) dans le formulaire
  Infos, pré-rempli depuis `spec.command` s'il existe, et collecté dans le
  payload de `applyContainerEdit` (champ `command`).
- La section WebUI est bien pré-remplie depuis le spec (`spec.webui`) et la
  sauvegarde (`applyContainerEdit`) collecte/valide les lignes non vides :
  seules les lignes avec une adresse non vide sont envoyées, et l'adresse est
  validée (`http://`, `https://`, `:port` ou `/chemin`).

## 2. Onglets de la modale réparés

### Cause des onglets cassés

Les onglets Infos/Ports/Volumes/Env/Réseau étaient implémentés comme des
**ancres de scroll** : chaque clic appelait `scrollIntoView` et un « scroll
spy » (`_attachEditScrollSpy`) mettait à jour l'onglet actif au scroll. Tous
les panneaux restaient affichés (`display: block`), donc le clic ne « changeait
pas de panneau » — il ne faisait que scroller, et l'état actif dépendait du
scroll plutôt que du clic.

### Correction apportée

- Les onglets basculent désormais par **clic** via `_switchEditTab(sectionId,
  btn)` : seuls les panneaux `.edit-tab-panel` sont affichés (les autres
  reçoivent la classe `hidden`), et l'onglet cliqué est marqué `active`.
- La section WebUI (en haut) n'a pas la classe `edit-tab-panel` : elle reste
  toujours visible, conformément au mockup.
- Le scroll spy (`_attachEditScrollSpy`) a été supprimé (plus nécessaire).
- Chaque panneau (Ports, Volumes, Env, Réseau) se rend correctement via les
  tables éditables existantes.

## 3. Menu contextuel (clic droit)

### Choix double-clic vs clic droit

Le double-clic (`ondblclick`) a été **retiré** au profit du clic droit
(`oncontextmenu`). Les deux coexistaient sans conflit technique, mais le
double-clic est une interaction peu découverte et sujette aux déclenchements
accidentels ; le clic droit est l'interaction standard pour un menu
contextuel. On garde donc uniquement le clic droit.

### Structure

Le menu est un élément fixe `#container-context-menu` (dans dashboard.html),
rempli dynamiquement par `openContainerContextMenu(event, containerId,
stackName, agent)` (dashboard.js). Il est positionné à la position du clic
droit, clampé aux bords de la fenêtre.

Entrées, dans l'ordre :

1. **WebUI** — une entrée par adresse `webui` du container, affichant le
   **libellé** (`name`) s'il existe, sinon l'URL brute. Clic → ouvre l'adresse
   (résolue via `_resolveWebUIUrl`). Aucune section WebUI n'est affichée si le
   container n'en a pas.
2. **Edit** — ouvre l'éditeur de container (`openContainerEdit`).
3. **Start / Stop / Restart / Update** — réutilisent `containerAction`
   (`start`, `stop`, `restart`, `update-image`).

### Fermeture

Le menu se ferme :

- au clic ailleurs (hors menu),
- à la touche Échap,
- au scroll (fenêtre et conteneurs scrollables).

Le clic droit sur un container appelle `preventDefault()` pour ne pas déclencher
le menu navigateur par défaut.

## Fichiers modifiés

- `orchestrator/app/static/js/modals.js` — section WebUI (+ bouton), champ
  Command, bascule d'onglets, collecte de la commande.
- `orchestrator/app/static/js/dashboard.js` — remplacement `ondblclick` →
  `oncontextmenu`, méthodes du menu contextuel.
- `orchestrator/app/static/js/app.js` — appel de `_attachContextMenuListeners()`
  dans `init()`.
- `orchestrator/app/static/css/style.css` — styles du menu contextuel.
- `orchestrator/templates/dashboard.html` — élément `#container-context-menu`.
- `orchestrator/tests/test_api_routes.py` — test du champ `command` en
  pass-through.

## Tests

- `node --check` sur les JS modifiés : OK.
- `pytest` : 435 passed (434 existants + 1 nouveau pour le champ `command`).
