# Bouton CACHER/AFFICHER l'éditeur Compose

## Objectif
Permettre à l'utilisateur de masquer/afficher le panneau de l'éditeur Compose
(la colonne de droite du dashboard) afin de laisser le panneau principal
(containers/stacks) prendre toute la largeur.

## Conteneur de l'éditeur localisé
- **HTML** : `orchestrator/templates/dashboard.html`
  - `.right-column` → contient le panneau de l'éditeur Compose.
  - `.compose-panel` → le panneau lui-même (en-tête + `#compose-body`).
  - `#resizer-vertical` → la poignée de redimensionnement entre la colonne
    gauche (`.left-column`) et la colonne droite (`.right-column`).
- **CSS** : `orchestrator/app/static/css/style.css`
  - `.app-layout` est un `flex-direction: row` ; `.left-column` et
    `.right-column` sont les deux colonnes, séparées par `#resizer-vertical`.
- **JS** : `orchestrator/app/static/js/editor.js` (`loadEditor`,
  `renderEditorPlaceholder`, `renderEditor`, `#compose-body`).

## Bouton toggle ajouté
- **Emplacement** : barre supérieure (`topbar-right`), juste avant le bouton
  du chat (`#chat-toggle`).
- **Icône** : `panel-right` (lucide) — représente un panneau latéral.
- **ID** : `#editor-toggle`, classe `topbar-btn active`.
- **Action** : `DockyApp.toggleEditor()`.

## Persistance (localStorage)
- Clé : `docky-editor-visible` (`'1'` = visible, `'0'` = masqué).
- État initial chargé dans `app.js` → `init()` via `applyEditorVisibility()`.
- Écrit à chaque bascule dans `editor.js` → `toggleEditor()`.

## Comportement à la sélection d'une stack
- Si l'éditeur est **masqué** et qu'on sélectionne une stack, il **reste
  masqué**. La sélection continue de charger le contenu en arrière-plan
  (`loadEditor`), mais le panneau n'est pas réaffiché. L'utilisateur le rouvre
  via le bouton de la topbar. (Choix documenté : ne pas rouvrir automatiquement,
  pour ne pas perturber la vue plein écran du dashboard.)

## Adaptation du layout
- `applyEditorVisibility()` (dans `editor.js`) :
  - masque/affiche `.right-column` et `#resizer-vertical` via `display`.
  - quand masqué : `.left-column` passe en `flex: 1` (pleine largeur).
  - quand affiché : restaure la largeur sauvegardée (`docky-left-width`) si
    elle existe, sinon revient au défaut flex.
  - met à jour l'état `active` du bouton `#editor-toggle`.

## Fichiers modifiés
- `orchestrator/templates/dashboard.html` — bouton `#editor-toggle` ajouté.
- `orchestrator/app/static/js/app.js` — état `editorVisible` + chargement
  initial dans `init()`.
- `orchestrator/app/static/js/editor.js` — `toggleEditor()` et
  `applyEditorVisibility()`.

## Validation
- `node --check` sur `editor.js` et `app.js` : OK.
- `pytest -q` : **492 passed** (zéro régression).
- Smoke `TestClient GET /dashboard` : **200**.
