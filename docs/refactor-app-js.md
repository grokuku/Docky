# Refactor `orchestrator/app/static/js/app.js` — découpage en modules (v0.0.4)

Trace complète du refactor JS (dernier monolithe après les 4 refactors Python
validés : docker_manager, llm-client, agent-manager, routes-api).

> Méthode imposée : approche PRUDENTE et INCRÉMENTALE (pas de tests JS
> automatisés). ZÉRO changement de comportement. Chaque étape est vérifiée
> (syntaxe `node --check`, globales préservées, pas de doublons).

---

## Étape 1 — Analyse + état des lieux (aucune modification)

### 1.1 Inventaire des fichiers

| Fichier | Rôle | Lignes |
|---|---|---|
| `orchestrator/app/static/js/app.js` | Monolithe JS du dashboard (objet global `DockyApp`) | 4405 |
| `orchestrator/app/static/js/settings.js` | JS de la page Settings (objet global `SettingsApp`) | 480 |
| `orchestrator/templates/dashboard.html` | Seule page qui charge `app.js` | 394 |
| `orchestrator/templates/settings.html` | Charge `settings.js` (indépendant de app.js) | 194 |
| `orchestrator/templates/login.html` | Aucun JS | 36 |
| `orchestrator/templates/logs.html` | JS inline (`PopupLogs`, `escapeHtml`, `formatLogLine`) | 992 |
| `orchestrator/templates/console.html` | JS inline (xterm + WebSocket exec) | 153 |

- **Pas de bundler** : aucun `package.json`, aucun outil de build (projet vanilla).
- **Node disponible** : `node v22.23.2` → `node --check` utilisable pour la
  validation syntaxique.
- **Serveur statique** : `orchestrator/app/main.py` mount `/static` →
  `StaticFiles(directory=.../app/static)` → tout fichier ajouté dans
  `static/js/` est servi automatiquement (aucune modif serveur nécessaire).

### 1.2 Comment `app.js` est chargé / quelles globales les templates appellent

- `dashboard.html` inclut **un seul** `<script src="/static/js/app.js"></script>`
  en fin de `<body>` (script classique, synchrone, sans `defer`/`module`).
- Le HTML appelle les **méthodes globales `DockyApp.*`** via `onclick`,
  `onchange`, `oninput` (36 références uniques — voir liste §1.4).
- `app.js` lui-même boote via `document.addEventListener("DOMContentLoaded",
  () => DockyApp.init())` (fin du fichier).
- `settings.html` utilise `SettingsApp.*` (settings.js) : **aucun partage avec
  app.js**. Les pages logs/console utilisent du JS inline indépendant.
- Conclusion : le contrat à préserver = **l'objet global `DockyApp`** avec
  exactement les mêmes méthodes/propriétés, défini avant `DOMContentLoaded`.

### 1.3 Structure de l'objet `DockyApp`

Objet littéral global unique. 226 entrées recensées : 62 propriétés d'état +
164 méthodes. Organisation interne par **sections commentées** (repères
fiables pour un découpage fidèle) :

| Lignes | Section | Contenu |
|---|---|---|
| 6–70 | `State` | propriétés d'état (stacks, caches, WS, chat, tri…) |
| 72–154 | `Utilities` | `apiFetch`, `apiPost`, `showToast`, `escapeHtml`, `formatBytes`, `icon`, `agentQueryParam`, `agentQuery` |
| 156–341 | `Multi-agent management` | `loadAgents`, `renderAgentSelector`, `updateStatsBar`, `toggleAgentFilter`, refresh agents |
| 343–634 | `Stacks` | `refreshStacks`, `renderStacks`, `loadContainers`, `renderContainers`, badges |
| 636–849 | `Grid Dashboard (Option B)` | `renderGridDashboard` |
| 850–870 | `View Mode Toggle` | `toggleViewMode`, `renderCurrentView` |
| 872–1048 | `Table Dashboard (Option C)` | `renderTableDashboard`, `renderTableRow` |
| 1050–1463 | `Colonnes redimensionnables` + sélection | resizers colonnes, `hashString`, `stackColor`, `containerStatusDot`, `renderGridContainerCard`, `selectContainerInGrid`, `showStackContextPanel`, `clearStackSelection`, dialog non-sauvegardé, `_debouncedGridRender` |
| 1465–1517 | `Stats / Resources` | `loadContainerStats`, `renderStats` |
| 1519–1669 | `Activity modal` | `_openActivity`, `_appendActivity`, `_finishActivity`, `_parseSSEBlock`, `_streamAction`, `closeActivity` |
| 1671–1730 | `Actions` | `containerAction`, `stackAction` |
| 1732–1903 | `Update check` | `checkUpdate`, `checkStackUpdate`, cache update |
| 1905–1919 | `Logs` | `openLogs`, `openStackLogs` |
| 1921–1941 | `Console (exec)` | `openConsole`, `closeConsole` |
| 1943–2178 | `Container Edit Modal` | `openContainerEdit`, formulaire, `applyContainerEdit` |
| 2180–2221 | `Ports` | `togglePorts`, `loadPorts` |
| 2223–2241 | `Auto-refresh` | `startAutoRefresh`, `stopAutoRefresh` |
| 2243–2317 | `Events WebSocket + Heartbeat` | `connectEvents`, `disconnectEvents`, `_debouncedEventRefresh`, heartbeat |
| 2318–2859 | `Compose editor (Phase 3)` | état éditeur + `loadEditor`, `renderEditor`, sauvegarde, onglets |
| 2861–2945 | `.env` + toggle fichiers | `createEnvFile`, `toggleShowAllStackFiles` |
| 2947–2984 | `New stack` | `DEFAULT_COMPOSE_TEMPLATE`, `openNewStackModal`, `createStack` |
| 2986–3273 | `Import stack` | `openImportModal`, preview, `doImport` |
| 3275–3322 | `Delete stack` | `openDeleteStackModal`, `confirmDeleteStack` |
| 3324–3369 | `Permissions` | `openPermsModal`, `applyPermissions` |
| 3371–3661 | `Chat LLM (Phase 4)` | `sendChatMessage`, rendu chat, validation humaine |
| 3663–3735 | `Chat panel toggle` | `toggleChat`, `applyChatVisibility` |
| 3737–3793 | `SOUL.md editor` | `openSoulEditor`, `saveSoul` |
| 3795–3902 | `Panel resizers` | `initResizers`, `restorePanelSizes` |
| 3904–4031 | `Git History` | `openHistory`, preview/restore |
| 4033–4171 | `Sort & Group` | `onSortChange`, `onSearchInput`, `_sortContainers`, `_groupStacks` |
| 4173–4397 | `init()` | bootstrap complet (localStorage, WS, timers, backdrop modales, ESC…) |

Chaque section est bornée par deux bandeaux `// ------` et une ligne vide.

### 1.4 Méthodes `DockyApp.*` appelées par les templates (contrat à préserver)

Recensées depuis `dashboard.html` (attributs `on*`) :

`_openVersionMismatchModal`, `_onUnsavedCancel`, `_onUnsavedDiscard`,
`_onUnsavedSave`, `applyContainerEdit`, `applyPermissions`, `clearChat`,
`closeActivity`, `closeConsole`, `closeContainerEdit`, `closeDeleteStackModal`,
`closeHistory`, `closeImportModal`, `closeImportPreview`, `closeNewStackModal`,
`closePermsModal`, `closeSoulEditor`, `closeVersionMismatch`, `confirmDeleteStack`,
`confirmImport`, `createStack`, `doImport`, `onChatKeydown`, `onGroupChange`,
`onSearchInput`, `onSortChange`, `onStackSelect`, `openImportModal`,
`openNewStackModal`, `openSoulEditor`, `refreshStacks`, `saveSoul`,
`sendChatMessage`, `toggleChat`, `togglePorts`, `toggleViewMode`.

(La liste complète des méthodes appelées depuis le HTML généré dynamiquement
par app.js — `selectStackFromDashboard`, `containerAction`, `stackAction`,
`selectFile`, etc. — est préservée car le code qui génère ces `onclick` est
déplacé tel quel avec les méthodes.)

### 1.5 Décision technique (choix de la solution la moins risquée)

**Chargement multi-scripts classiques, dans l'ordre, dans `dashboard.html`** :

- Les templates référencent des **globales** (`DockyApp.*`) via `onclick` :
  les ES modules (`<script type="module">`) isolent leur portée et ne
  produisent **pas** de globales → solution écartée (risque de casser les 37
  références HTML + tout le HTML généré dynamiquement).
- Un seul `<script src="app.js">` qui charge les modules par injection
  dynamique (`document.write` / append séquentiel) → dépend du parseur, des
  timings, peut générer des warnings console (`document.write`) → plus risqué.
- **Solution retenue** : remplacer l'unique balise `<script>` par une série de
  balises classiques synchromes dans le même ordre, toutes exécutées **avant**
  `DOMContentLoaded` (donc avant le boot). Comportement équivalent (scripts
  classiques = blocage + exécution en ordre + portée globale partagée).

**Assemblage** :
- `app.js` reste **le point d'entrée / la façade** : il définit
  `window.DockyApp` (propriétés d'état + `init()`) et enregistre le boot
  `DOMContentLoaded`. Il est chargé **en premier**.
- Les modules (`api.js`, `events.js`, `dashboard.js`, `editor.js`, `chat.js`,
  `modals.js`) rattachent leurs méthodes via
  `Object.assign(window.DockyApp, { … })`, chargés ensuite.
- Toutes les méthodes restent sur le même objet → les appels croisés
  (`this.showToast`, `this.refreshStacks`, …) fonctionnent à l'identique.
- Aucun fichier n'exécute de code avant le DOM ; le boot unique reste dans
  app.js.

**Découpage par module** (sur la base des sections réelles) :

| Module | Sections déplacées |
|---|---|
| `api.js` | Utilities |
| `events.js` | Events WebSocket + Heartbeat |
| `dashboard.js` | Multi-agent, Stacks, Grid, View Mode, Table, Colonnes/sélection, Stats, Actions, Update check, Logs, Console, Ports, Auto-refresh, Resizers, Sort & Group |
| `editor.js` | Compose editor, .env+toggle, New stack, Import stack, Delete stack, Permissions, Git History |
| `chat.js` | Chat LLM, Chat panel toggle, SOUL.md editor |
| `modals.js` | Activity modal, Container Edit Modal |
| `app.js` | État (`State`), `init()`, boot (façade) |

**Changement de template (unique, documenté)** : `dashboard.html` remplace la
balise unique `<script src="/static/js/app.js">` par la série de 7 balises
(voir Étape 6). Aucun autre template modifié.

### 1.6 Vérifications de l'étape 1

- [x] app.js lu en entier (4405 lignes, 7 lectures séquentielles).
- [x] Templates lus (5 fichiers) ; inventaire des globales fait.
- [x] Pas de bundler, pas de package.json ; node v22 disponible.
- [x] `/static` sert tout le dossier `static` (ajout de fichiers OK).
- [x] Seul `dashboard.html` charge app.js.
- [x] `node --check` sur le monolithe actuel : à faire en baseline.

---

## Étape 2 — Extraction `api.js` (Utilities)

(à compléter)

---

## Étape 2 — Extraction `api.js` (Utilities)

**Méthodes déplacées** (section `Utilities`, verbatim) : `apiFetch`,
`apiPost`, `showToast`, `escapeHtml`, `formatBytes`, `icon`.
(Note : `agentQueryParam` / `agentQuery` figurent dans la section
`Multi-agent management` → déplacés dans `dashboard.js`, voir Étape 4.)

**Fichier créé** : `orchestrator/app/static/js/api.js` (99 lignes) —
`Object.assign(window.DockyApp, { … })`.

**Vérifications** : `node --check api.js` OK ; noms présents/absents dans
l'inventaire exacts.

## Étape 3 — Extraction `events.js` (WebSocket)

**Méthodes déplacées** (section `Events WebSocket + Heartbeat`, verbatim) :
`connectEvents`, `disconnectEvents`, `_debouncedEventRefresh`,
`startHeartbeat`, `stopHeartbeat`.

**Fichier créé** : `orchestrator/app/static/js/events.js` (90 lignes).

**Vérifications** : `node --check events.js` OK ; pas de doublon.

## Étape 4 — Extraction `dashboard.js` (rendu principal)

**Méthodes déplacées** (sections verbatim) :
- Multi-agent : `loadAgents`, `refreshAgents`, `renderAgentSelector`,
  `updateStatsBar`, `toggleAgentFilter`, `startAgentsRefresh`,
  `stopAgentsRefresh`, `agentQueryParam`, `agentQuery`.
- Stacks : `refreshStacks`, `updateStackSelector`, `renderStacks`,
  `statusBadge`, `containerStatusBadge`, `toggleStack`, `loadContainers`,
  `renderContainers`.
- Grid / Table / Vues : `renderGridDashboard`, `toggleViewMode`,
  `renderCurrentView`, `renderTableDashboard`, `renderTableRow`,
  `_tableColWidthsKey`, `_legacyTableColWidthsKey`, `_tableColDefaults`,
  `_tableColMinPx`, `_tableContainerWidth`, `_getTableColWidths`,
  `_migrateTableColWidths`, `_applyTableColWidths`, `_saveTableColWidth`,
  `attachTableColumnResizers`, `hashString`, `stackColor`,
  `containerStatusDot`, `renderGridContainerCard`, `selectContainerInGrid`.
- Sélection / panel contexte / dialog : `showStackContextPanel`,
  `clearStackSelection`, `_forceDeselect`, `_saveAndDeselect`,
  `showUnsavedDialog`, `_onUnsavedSave`, `_onUnsavedDiscard`,
  `_onUnsavedCancel`, `_debouncedGridRender`.
- Stats : `loadContainerStats`, `renderStats`.
- Actions : `containerAction`, `stackAction`.
- Update check : `_containerUpdateCacheKey`, `_stackUpdateCacheKey`,
  `_updateBadgeClass`, `_countCachedUpdates`, `_pruneUpdateCache`,
  `_invalidateContainerUpdateCache`, `checkUpdate`, `checkStackUpdate`.
- Logs / Console : `openLogs`, `openStackLogs`, `openConsole`, `closeConsole`.
- Ports : `togglePorts`, `loadPorts`.
- Auto-refresh : `startAutoRefresh`, `stopAutoRefresh`.
- Resizers : `initResizers`, `restorePanelSizes`.
- Sort & Group : `onSortChange`, `onGroupChange`, `onSearchInput`,
  `_filterContainers`, `_emptyViewMessage`, `_sortStacks`, `_sortContainers`,
  `_groupStacks`.

**Fichier créé** : `orchestrator/app/static/js/dashboard.js` (1954 lignes).

**Vérifications** : `node --check dashboard.js` OK ; pas de doublon.

## Étape 5 — Extraction `editor.js`, `chat.js`, `modals.js`

### `editor.js` (1197 lignes)
Sections `Compose editor (Phase 3)`, `.env + toggle`, `New stack`,
`Import stack`, `Delete stack`, `Permissions`, `Git History`.
Méthodes : `onStackSelect`, `_scrollToStackInDashboard`,
`selectStackFromDashboard`, `loadEditor`, `toggleComposeEdit`, `selectFile`,
`renderEditorPlaceholder`, `_setEditorFileContent`, `renderEditorLoading`,
`isModified`, `anyModified`, `renderEditor`, `updateLineNumbers`,
`updateReadonlyLineNumbers`, `syncLineScroll`, `onEditorInput`,
`updateModifiedIndicators`, `onEditorKeydown`, `saveCurrentFile`,
`saveAndDeploy`, `createEnvFile`, `toggleShowAllStackFiles`,
`DEFAULT_COMPOSE_TEMPLATE` (propriété), `openNewStackModal`,
`closeNewStackModal`, `createStack`, `openImportModal`,
`openImportModalForStack`, `closeImportModal`, `importExternal`,
`_doImportPreview`, `showImportPreview`, `closeImportPreview`,
`confirmImport`, `doImportDirect`, `doImport`, `openDeleteStackModal`,
`closeDeleteStackModal`, `confirmDeleteStack`, `openPermsModal`,
`closePermsModal`, `applyPermissions`, `openHistory`, `closeHistory`,
`_selectHistory`, `_previewHistory`, `_restoreHistory`.

### `chat.js` (439 lignes)
Sections `Chat LLM (Phase 4)`, `Chat panel toggle`, `SOUL.md editor`.
Méthodes : `sendChatMessage`, `onChatKeydown`, `renderChatMessage`,
`formatChatContent`, `renderToolCalls`, `renderValidationRequest`,
`authorizeExec`, `refuseExec`, `authorizeClean`, `clearChat`, `toggleChat`,
`applyChatVisibility`, `showChatLoading`, `setChatInputEnabled`,
`scrollChatToBottom`, `openSoulEditor`, `closeSoulEditor`, `saveSoul`.

### `modals.js` (404 lignes)
Sections `Activity modal`, `Container Edit Modal`.
Méthodes : `_openActivity`, `_appendActivity`, `_finishActivity`,
`_parseSSEBlock`, `_streamAction`, `closeActivity`, `openContainerEdit`,
`_attachEditScrollSpy`, `_renderContainerEditForm`, `_addEditRow`,
`applyContainerEdit`, `closeContainerEdit`.

**Vérifications** : `node --check` sur les 3 fichiers OK ; aucun doublon.

## Étape 6 — Façade `app.js` + template + validation complète

### Façade `app.js` (322 lignes)
- Définit `window.DockyApp = { … }` avec **toutes les propriétés d'état**
  (section `State`, inchangée) et `init()` (déplacé verbatim).
- Boot : `document.addEventListener("DOMContentLoaded", () => DockyApp.init())`
  (inchangé).
- Chargé **en premier** dans le template ; les modules rattachent ensuite leurs
  méthodes via `Object.assign(window.DockyApp, { … })`. Tous ces scripts sont
  classiques et synchrones → exécutés **avant** `DOMContentLoaded` → l'objet
  est complet au moment du boot (même garantie qu'avant).

### Template `dashboard.html` (modification unique, documentée)
La balise unique `<script src="/static/js/app.js"></script>` est remplacée par
7 balises dans l'ordre : `app.js`, `api.js`, `events.js`, `dashboard.js`,
`editor.js`, `chat.js`, `modals.js`. Aucun autre template modifié
(settings/login/logs/console intacts).

### Validation complète
1. **Syntaxe** : `node --check` sur les 7 fichiers docky + `settings.js` → OK.
2. **Inventaire** : 164 méthodes + 62 propriétés (226 entrées) identiques entre
   le monolithe (HEAD git) et l'ensemble découpé — aucune perdue, aucune ajoutée,
   aucun doublon.
3. **Code verbatim** : les 30 sections + `init()` apparaissent à l'identique
   (byte-for-byte, whitespace final normalisé) dans leur module de destination.
4. **Globales templates** : les 36 références `DockyApp.*` de `dashboard.html`
   sont toutes définies.
5. **Smoke Node** (harnais avec DOM/fetch/WS/localStorage mockés) : chargement
   dans l'ordre, assemblage de `DockyApp` complet (226 clés), exécution réelle
   d'`init()` **sans exception**, timers de fond posés (refreshAgents,
   autoRefresh, versionCheck, heartbeat), appels croisés inter-modules OK.
6. **Smoke FastAPI/TestClient** : `/dashboard` rendu avec les 7 balises dans
   le bon ordre ; `/settings` garde `settings.js` ; `/login` sans JS docky ;
   `/popup/logs` et `/popup/console` rendus OK ; tous les fichiers JS servis en
   HTTP 200.
7. **Suite pytest** : **303/303 passés** (aucune régression).

### Choix technique et justification
- **Scripts multiples classiques dans le template** (plutôt qu'ES modules ou
  injection dynamique) : les templates et le HTML généré dynamiquement
  référencent des **globales** `DockyApp.*` (onclick/onchange/oninput). Les
  ES modules isolent la portée et ne produisent pas de globales → écartés.
  L'injection dynamique (`document.write`/append séquentiel) dépend du parseur
  et peut générer des warnings console → écartée.
- **Namespace global partagé `window.DockyApp`** : `app.js` (façade) définit
  l'objet + état + `init()` + boot ; chaque module fait
  `Object.assign(window.DockyApp, { … })`. Les appels croisés (`this.showToast`,
  `this.refreshStacks`, …) restent valides car toutes les méthodes vivent sur le
  même objet.

### Fichiers créés / modifiés
- Créés : `orchestrator/app/static/js/api.js`, `events.js`, `dashboard.js`,
  `editor.js`, `chat.js`, `modals.js`.
- Modifiés : `orchestrator/app/static/js/app.js` (façade), 
  `orchestrator/templates/dashboard.html` (série de `<script>`).
- Intacts : `settings.js`, `settings.html`, `login.html`, `logs.html`,
  `console.html`, côté serveur (aucune modification).

### Limites
- Pas de tests navigateur automatisés (pas de Playwright/Selenium dans le
  projet) : la validation comportementale passe par le smoke Node (init() réel)
  + le smoke TestClient (rendu templates + services HTTP) + vérification
  manuelle recommandée (ouvrir /dashboard, cliquer, vérifier la console).
- Le harnais Node simule le DOM : les branches DOM riches (rendus grille/table)
  ne sont pas toutes exécutées à l'écran, mais le code est déplacé verbatim et
  l'assemblage de l'objet est prouvé complet.
- `settings.js` définit un objet `SettingsApp` distinct avec des noms de
  méthodes homonymes (`init`, `apiFetch`, `showToast`, …) : aucun conflit (objets
  séparés, jamais chargés sur la même page).
