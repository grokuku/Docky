# Settings — Redesign en « cards » + fix scroll

## Objectif
Redessiner la page Settings de Docky en grille responsive de cartes, et corriger
le bug de scroll qui coupait le contenu sous la topbar.

## Fichiers modifiés
- `orchestrator/templates/settings.html`
- `orchestrator/app/static/css/style.css`
- `docs/settings-cards-redesign.md` (ce document)

## Changements HTML (`settings.html`)
Ajout de la classe `settings-card-wide` sur les deux sections pleine largeur :
- **Configuration LLM** (`<section class="panel settings-panel settings-card-wide">`)
- **Agents** (`<section class="panel settings-panel settings-card-wide">`)

Les sections **Historique**, **Sécurité** et **API MCP** restent des cartes
compactes (une colonne de la grille).

Aucun `id` n'a été modifié → `settings.js` reste fonctionnel (aucune régression JS).

## Changements CSS (`style.css`)
`.settings-layout` passe d'une colonne étroite centrée à une grille responsive :

```css
.settings-layout {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
    gap: 20px;
    align-items: start;
    padding: 24px 20px;
}

.settings-card-wide {
    grid-column: 1 / -1;
}
```

- `grid-template-columns: repeat(auto-fit, minmax(340px, 1fr))` → les cartes
  compactes s'empilent automatiquement selon la largeur d'écran.
- `.settings-card-wide { grid-column: 1 / -1; }` → LLM et Agents occupent toute
  la largeur de la grille.
- Le thème sombre et les styles des panneaux (`.panel`, `.panel-header`,
  `.panel-body`, `.settings-body`, `.settings-actions`, etc.) sont conservés.

## Fix scroll
Le parent `.app-body` a `height: 100vh; overflow: hidden;` (layout dashboard).
Le contenu de Settings dépassait 100vh et était clippé, sans possibilité de
scroller.

Le fix est appliqué sur `.settings-layout` (le conteneur direct sous la topbar,
dans le flux flex de `.app-body`) :
- `flex: 1` → occupe l'espace restant sous la topbar (56px).
- `min-height: 0` → autorise le conteneur flex à rétrécir sous sa taille de
  contenu (prérequis flexbox pour le scroll).
- `overflow-y: auto` → le contenu défile dans la zone sous la topbar au lieu
  d'être clippé par `overflow: hidden` de `.app-body`.

`.settings-layout` n'est utilisé que dans `settings.html` → le dashboard n'est
pas affecté.

## Vérifications
- `node --check orchestrator/app/static/js/settings.js` → OK (aucune régression JS).
- `pytest -q` → **492 passed** (zéro régression).
- Smoke TestClient : `GET /settings` (avec cookie JWT) → **200**, contient
  `settings-layout`, 2 cartes `settings-card-wide`, et les IDs `llm-status` /
  `mcp-status` intacts.
- Dashboard non cassé : `.settings-layout` n'existe que sur la page Settings.
