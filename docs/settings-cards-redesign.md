# Settings — Redesign « Éventail chromatique » + fix overlap

## Objectif
Redessiner la page Settings de Docky en une grille de cartes chromatiques
(« Proposition 4 — Éventail chromatique »), corriger le bug de chevauchement
vertical (bas de la carte LLM coupé par la carte Agents) et le scroll sous la
topbar.

## Fichiers modifiés
- `orchestrator/templates/settings.html`
- `orchestrator/app/static/css/style.css`
- `docs/settings-cards-redesign.md` (ce document)

## Layout
La grille passe d'un `auto-fit` ambigu à une grille explicite **6 colonnes** :

```css
.settings-layout {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 22px;
    align-items: stretch;
    max-width: 1180px;
    width: 100%;
    margin: 0 auto;
    padding: 28px 32px 40px;
}
```

- **> 1100 px** : 6 colonnes. **LLM** et **Agents** occupent chacune 3 colonnes
  (`settings-card--half`), côte à côte sur la première rangée. **Historique**,
  **Sécurité** et **API MCP** occupent chacune 2 colonnes (`settings-card--third`),
  côte à côte sur la seconde rangée.
- **≤ 1100 px** : une seule colonne pleine largeur (`grid-template-columns: 1fr`),
  toutes les cartes passent en `grid-column: 1 / -1`.
- **≤ 560 px** : padding `16px`, gap `16px`.

### Hauteurs égales
`align-items: stretch` (défaut) fait s'étirer les cartes d'une même rangée à la
hauteur de la plus haute. Le corps de carte (`.settings-body`) porte
`flex: 1; min-height: 0; overflow: auto` : il remplit l'espace restant et
défile en interne si le contenu dépasse, sans jamais déborder sur la carte
suivante.

## Fix du chevauchement vertical
L'ancien layout (`repeat(auto-fit, minmax(340px, 1fr))` + `align-items: start`)
pouvait laisser le contenu de la carte LLM déborder sur la carte Agents selon
la largeur d'écran. Le nouveau layout :
- remplace l'`auto-fit` par une grille explicite à colonnes fixes (pas de
  reflow ambigu) ;
- passe de `align-items: start` à `align-items: stretch` (les cartes d'une
  rangée partagent exactement la même hauteur) ;
- ajoute `min-height: 0` sur `.settings-body` (prérequis flexbox pour que le
  corps défile au lieu de pousser/déborder la carte).

Vérifié par rendu headless (chromium) à 1280, 1000, 800 et 500 px : aucune
intersection entre les boîtes englobantes des 5 cartes.

## Palette & teintes de section
Nouvelle palette sombre, scopée sous `.settings-page` / `.settings-*` (le
dashboard n'est pas affecté) :
- page `#0d0d1b`, cartes `#151527`, tertiaire `#1d1d36`, raised `#232344`
- bordures `#262643` / `#333352` (hover-focus)
- texte `#f0f0f8` / `#b4b4cf` / `#7e7e9e`
- accent `#ff5e7a`, success `#34d399`, warning `#fbbf24`, danger `#f87171`, info `#60a5fa`

Chaque section porte une teinte via une variable `--tint` :
- **LLM** `#e94560` (`settings-panel--llm`)
- **Agents** `#a78bfa` (`settings-panel--agents`)
- **Historique** `#2dd4bf` (`settings-panel--history`)
- **Sécurité** `#34d399` (`settings-panel--security`)
- **API MCP** `#38bdf8` (`settings-panel--mcp`)

La teinte alimente l'icône « chip » (34×34, radius 10 px, fond teinte à 14 %
via `color-mix`, icône 17 px) dans l'en-tête de chaque carte.

## En-têtes de carte
Les en-têtes sont restructurés en `settings-header` :
- à gauche : `settings-header-title` = chip icône + titre ;
- à droite : statut (LLM, MCP) ou bouton d'action (Agents).

Aucun `id` n'a été modifié → `settings.js` reste fonctionnel (aucune régression JS).

## Interactions
- **Hover carte** : `translateY(-2px)`, bordure `#333352`, ombre renforcée,
  transition `180ms ease-out`.
- **Hover ligne agent** : bordure `#333352` + fond `#232344`.
- **Focus rings** : `outline: 2px solid #ff5e7a` sur boutons/inputs/selects.
- **Entrée en cascade** : `settings-fade-up` (opacité + `translateY(12px)`) avec
  délais progressifs par carte, désactivé sous `prefers-reduced-motion`.

## Changements HTML (`settings.html`)
- `<body class="app-body settings-page">` (fond de page scopé).
- Les 5 sections portent leur classe de teinte + span de colonne
  (`settings-card--half` / `settings-card--third`).
- En-têtes restructurés avec `settings-header`, `settings-header-title`,
  `settings-chip`.
- L'emoji `📋` d'Historique est remplacé par l'icône lucide `history`.

## Changements CSS (`style.css`)
Toutes les nouvelles règles sont scopées sous `.settings-*` / `.settings-page`.
Les classes partagées `.panel`, `.btn`, `.status-indicator` ne sont **pas**
modifiées globalement (le dashboard en dépend) ; les variantes settings sont
déclarées sous `.settings-panel .status-indicator`, `.settings-panel .btn-primary`,
etc.

## Vérifications
- `node --check orchestrator/app/static/js/settings.js` → OK.
- `pytest -q` → **498 passed** (zéro régression).
- Smoke TestClient : `GET /settings` (cookie JWT) → **200**, contient
  `settings-layout`, les 5 classes de teinte, `settings-chip`, et les IDs
  `llm-status` / `mcp-status` / `llm-endpoint` / `llm-model` / `mcp-api-key` /
  `git-history-retention` / `current-password` / `new-password` /
  `confirm-password` / `agents-list` intacts.
- Rendu headless (chromium) : aucune intersection entre cartes à 1280, 1000,
  800 et 500 px.
- Dashboard non cassé : les règles settings sont scopées sous `.settings-*`.
