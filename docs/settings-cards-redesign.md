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

Grille explicite **4 colonnes** (cartes compactes du bas partagent la largeur ;
LLM et Agents sont pleine largeur) :

```css
.settings-layout {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 22px;
    align-items: stretch;
    max-width: 1180px;
    width: 100%;
    margin: 0 auto;
    padding: 28px 32px 40px;
}
```

- **> 1100 px** : 4 colonnes. **LLM** (`settings-card--wide`, `grid-column:1/-1`)
  occupe la 1re rangée entière ; **Agents** (`settings-card--wide`) la 2e rangée
  entière. **Historique**, **Sécurité**, **API MCP** et **Docker Hub**
  (`settings-card--quarter`, `grid-column:auto`) partagent la 3e rangée en 4
  colonnes égales, hauteurs égales (stretch) — Docker Hub a exactement le même
  traitement que les 3 autres (chip teintée `#fbbf24`, pill de statut, barre
  d'actions).
- **≤ 1100 px** : 2 colonnes (`grid-template-columns: repeat(2, 1fr)`), LLM et
  Agents restent pleine largeur, les 4 cartes compactes se répartissent en 2×2.
- **≤ 720 px** : 1 colonne pleine largeur ; padding `16px`, gap `16px`.

## Conformité au mockup « Proposition 4 — Éventail chromatique »

Rendu réel (vérifié au headless chromium, computed-style) :

- fond page `#0d0d1b` + **halo** `radial-gradient(1200px 400px at 50% -120px,
  rgba(233,69,96,.07), transparent)` en haut ;
- cartes `#151527`, rayon **16px**, bordure `#262643`, inset highlight
  `rgba(255,255,255,.04)`, ombre repos `0 1px 2px` + `0 6px 20px
  rgba(5,5,16,.35)` ;
- hover carte : **sans mouvement** (`transform` inchangé), `filter: brightness(1.06)`
  (éclaircissement léger), bordure `#40406e`, ombre `0 12px 32px`, transition `180ms` ;
- headers : chip 34×34 radius 10, fond teinte 14 % (`color-mix`), titre
  15px/600 `ls .01em`, pill de statut à droite ;
- callout LLM : fond `rgba(251,191,36,.12)`, barre gauche 3px `#fbbf24`, rayon
  10, texte 13px `#fbbf24` (sans bordure périphérique) ;
- barre d'actions : `margin-top: auto` + `border-top 1px rgba(255,255,255,.05)`
  + `padding-top 14px`, boutons à droite gap 10, rayon 10 (`.settings-body` est
  un flex-col pour que `margin-top:auto` épingle la barre en bas) ;
- agent-rows : mini-cartes `#1d1d36`, rayon **12**, bordure `#262643`, URL mono
  12px `#7e7e9e`, pill « En ligne » verte, actions ghost ;
- inputs : `#1d1d36`, hauteur **40**, rayon **10**, focus ring 3px
  `rgba(233,69,96,.28)` ;
- typo : titre 15px/600, labels 13px/500 `#b4b4cf`, hints 12.5px `#7e7e9e`,
  code mono `#7dd3fc`.

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
- **Hover carte** (NET, sans mouvement) : pas de `translateY`, `filter:
  brightness(1.06)`, bordure `#40406e`, ombre `0 12px 32px`, transition
  `180ms ease-out` (inclut `filter`/`border-color`/`box-shadow`/`background`,
  plus de `transform`).
- **Hover ligne agent** : bordure `#333352` + fond `#232344`.
- **Focus rings** : `outline: 2px solid #ff5e7a` sur boutons/inputs/selects.
- **Entrée en cascade** : `settings-fade-up` (opacité + `translateY(12px)`) avec
  délais progressifs par carte, désactivé sous `prefers-reduced-motion`.

## Changements HTML (`settings.html`)
- `<body class="app-body settings-page">` (fond de page scopé).
- Les sections portent leur classe de teinte + span de colonne :
  **LLM** et **Agents** en `settings-card--wide` (pleine largeur), les 4 cartes
  du bas — **Historique**, **Sécurité**, **API MCP**, **Docker Hub** — en
  `settings-card--quarter` (partagent la rangée en 4 colonnes).
- **Docker Hub** conserve son traitement complet : chip teintée `#fbbf24`, pill
  de statut `#dockerhub-status` et barre d'actions « Désactiver / Sauvegarder ».
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
- `pytest -q` → **540 passed** (zéro régression ; +1 test de régression du
  badge Docker Hub : `test_put_dockerhub_enabled_then_get_returns_enabled`).
- Smoke TestClient : `GET /settings` (cookie JWT) → **200**, contient
  `settings-layout`, les 6 classes de teinte y compris `settings-panel--dockerhub`,
  `settings-card--wide` / `settings-card--quarter`, `settings-chip`, et les IDs
  `llm-status` / `mcp-status` / `dockerhub-status` / `llm-endpoint` / `llm-model` /
  `mcp-api-key` / `git-history-retention` / `current-password` / `new-password` /
  `confirm-password` / `agents-list` intacts.
- Rendu headless (chromium) : à 1280 px, grille propre — LLM et Agents pleine
  largeur, les 4 cartes du bas en 4 colonnes égales (hauteurs égales, aucune
  intersection) ; à 900 px 2 colonnes ; à 600 px 1 colonne. Hover sans `transform`
  (`brightness(1.06)`, bordure `#40406e`).
- Dashboard non cassé : les règles settings sont scopées sous `.settings-*`.
