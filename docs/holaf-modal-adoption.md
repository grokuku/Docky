# Adoption de la brique HolafModal (holaf-lib)

## Résumé

La brique [`HolafModal`](../orchestrator/app/static/vendor/holaf/holaf-modal.js)
provient du dépôt [`/projects/holaf-lib`](/projects/holaf-lib) (brique `modal`,
version pinnée dans `holaf-manifest.json`). Elle remplace les modales HTML
statiques : le code appelant utilise `HolafModal.open({...})` (voir
`orchestrator/app/static/js/settings.js`).

**Version installée : v0.2.1** (bibliothèque de thèmes : préréglages
`dark` / `light` / `midnight` / `slate`, registre `HolafModal.themes`, thème
global volatil `HolafModal.setTheme() / clearTheme()`).

## Changelog v0.2.1 (finition, non-breaking)

Passage de **v0.2.0 → v0.2.1** (upgrade via `./scripts/holaf upgrade modal`).
Aucune API supprimée ni renommée — toutes les API utilisées par Docky
(`open`, `confirm`, `setTheme`, `themes.register/get/list`, `busy`) restent
identiques. Changements de finition :

- **Opt-out silencieux `theme: ""`** : équivalent de `null` (aucun thème,
même global), sans `console.warn`.
- **Warning unique par nom de thème inconnu** : un nom inconnu ne déclenche
qu'UN SEUL `console.warn`, même si la résolution échoue à chaque `open()`
(le `Set` est réinitialisé quand le nom redevient valide ou par
`clearTheme()`).
- **`themes.register` renvoie une copie protégée** (comme `themes.get`) :
muter le retour ne corrompt plus le registre.
- **Clés `--hm-*` racine d'un `{ preset, … }` fusionnées dans les surcharges**
(avant, elles étaient ignorées) ; en cas de doublon, `vars` (champ officiel)
garde la priorité.

Aucune adaptation des call sites Docky n'a été nécessaire : le thème `docky`
(`holaf-docky-theme.js`) et toutes les fenêtres migrées fonctionnent à
l'identique.

## Thème Docky (Proposition 4)

Le thème Docky est **générique côté brique, spécifique côté Docky** : la brique
ne contient aucune couleur de projet — Docky les déclare dans un petit module
autonome [`orchestrator/app/static/js/holaf-docky-theme.js`](../orchestrator/app/static/js/holaf-docky-theme.js)
toujours chargé **après** la brique :

1. il enregistre le thème `docky` dans `HolafModal.themes.register("docky", {...})`
   avec la palette validée « Proposition 4 »
   (fond `#151527`, surfaces `#1d1d36`, bordures `#262643`, texte
   `#f0f0f8`/`#b4b4cf`, accent `#e94560`→`#ff5e7a`, danger `#ef4444`, rayon
   `16px`, overlay `rgba(13,13,27,.7)` — voir `--hm-*`) ;
2. il appelle `HolafModal.setTheme("docky")` pour que TOUTES les modales Docky
   héritent du thème, sans répéter l'option `theme` à chaque `open()`.

Ce module est fail-safe silencieux : si la brique `HolafModal` n'est pas
chargée, il se retire sans erreur.

Charge dans `settings.html` **et** `dashboard.html` (même ordre :
brique puis thème) :

```html
<script type="module" src="/static/vendor/holaf/holaf-modal.js"></script>
<script type="module" src="/static/js/holaf-docky-theme.js"></script>
```

> Les deux fichiers sont des modules ES (la brique étant elle-même un module),
> ce qui garantit l'ordre d'exécution : `holaf-docky-theme.js` s'exécute bien
> après que `window.HolafModal` est prêt.

## Fenêtres migrées

| # | Fenêtre | Ancien code | Nouveau code |
|---|---------|-------------|--------------|
| 1 | Supprimer l'agent (settings) | `#delete-agent-modal` (statique) | `settings.js::deleteAgent → HolafModal.open` |
| 2 | Supprimer la stack (éditeur) | `#delete-stack-modal` (statique) | `editor.js::openDeleteStackModal → HolafModal.open` |
| 3 | Down de la stack (dashboard) | `#stack-down-modal` (statique) | `dashboard.js::confirmStackDown → HolafModal.open` |
| 4 | Recréer un container en cours d'exécution (édition container) | `window.confirm` | `modals.js::applyContainerEdit → HolafModal.confirm` (empilé) |
| 5 | Restaurer l'historique (éditeur) | `window.confirm` | `editor.js::_restoreHistory → HolafModal.confirm` |
| 6 | Supprimer un container (dashboard) | `window.confirm` | `dashboard.js::confirmContainerDelete → HolafModal.confirm` |
| 7 | Update all (repli, modale absente) | `window.confirm` | `dashboard.js::updateAllContainers → HolafModal.confirm` (empilé) |
| 8 | Régénérer la clé MCP (settings) | `window.confirm` | `settings.js::regenerateMcpKey → HolafModal.confirm` |
| 9 | Désactiver Docker Hub (settings) | `window.confirm` | `settings.js::clearDockerhub → HolafModal.confirm` |
| 10 | Nouvelle stack (éditeur) | `#new-stack-modal` (statique) | `editor.js::openNewStackModal → HolafModal.open` |
| 11 | Import d'une stack (éditeur) | `#import-modal` (statique) | `editor.js::openImportModal → HolafModal.open` |
| 12 | Permissions du fichier (éditeur) | `#perms-modal` (statique) | `editor.js::openPermsModal → HolafModal.open` |
| 13 | SOUL.md (chat) | `#soul-modal` (statique) | `chat.js::openSoulEditor → HolafModal.open` |
| 14 | Formulaire agent ADD/EDIT (settings) | `#agent-modal` (statique) | `settings.js::showAgentForm → HolafModal.open` |
| 15 | Preview de l'import (éditeur) | `#import-preview-modal` (statique) | `editor.js::showImportPreview → HolafModal.open` |
| 16 | Historique git (éditeur) | `#history-modal` (statique) | `editor.js::openHistory → HolafModal.open` |
| 17 | Accès Web multi-adresses (dashboard) | `#webui-modal` (statique) | `dashboard.js::_renderWebUIModal → HolafModal.open` |
| 18 | Update all (dashboard) | `#update-all-modal` (statique) | `dashboard.js::_renderUpdateAllModal → HolafModal.open` |
| 19 | Versions désynchronisées (dashboard) | `#version-mismatch-modal` (statique) | `dashboard.js::_openVersionMismatchModal → HolafModal.open` |
| 20 | Éditer un container (dashboard) | `#container-edit-modal` (statique) | `modals.js::_renderContainerEditForm → HolafModal.open` (formulaire à onglets) |
| 21 | Activity (progression des commandes) | `#activity-modal` (statique) | `modals.js::_openActivity → HolafModal.open` (élément persistant, streaming) |

Convention commune aux migrations : pas d'option `theme` inline (thème
global « docky » hérité), boutons `cancel`/`danger`, `onClose` qui purge
l'état cible, et la fonction `close*` conservée en stub (purge d'état
uniquement) pour préserver l'API existante — notamment le handler global
« Échap » de `app.js`.

**Bilan : 21/21** — toutes les modales statiques de `dashboard.html` sont
migrées. `console-modal`, qui était du **code mort**, a depuis été **supprimé**
(voir section dédiée ci-dessous) : la console passe exclusivement par la popup.


> `stack-down-modal` a été migré (ligne 3) et son bloc HTML mort supprimé de
> `dashboard.html`. Les 7 `window.confirm()` natifs restants ont été convertis
> en `HolafModal.confirm()` (lignes 4-9) avec conversion `async/await` des call
> sites, messages et actions strictement identiques. La brique gère l'empilement
> (pile d'overlays + z-index global) : les confirms imbriqués (édition container,
> update-all) sont supportés — vérifié en headless Chromium (z-index 100001/100002).

## Lot « 5 formulaires » (lignes 10-14)

Les 5 formulaires ont été migrés vers `HolafModal.open` avec `content` en
**string HTML** (contenu de confiance, rendu via `innerHTML`). Les IDs des
inputs sont **conservés** car les callbacks (`createStack`, `doImport`,
`applyPermissions`, `saveSoul`, `submitAgentForm`/`collectPathMappings`) lisent
les valeurs depuis le DOM de la modale ouverte. Les boutons de la modale
(`onClick`) appellent ces callbacks ; les stubs `close*` sont conservés
(purge d'état uniquement) pour préserver l'API existante — notamment le
handler global « Échap » de `app.js`.

- **new-stack** (`editor.js::openNewStackModal`) : champs/callbacks identiques,
  sélecteur d'agent peuplé dans `onOpen`, Entrée = créer (comportement
  historique conservé via `onOpen`).
- **import** (`editor.js::openImportModal`) : idem ; l'étape preview séparée
  (`#import-preview-modal`) reste pour le lot suivant.
- **perms** (`editor.js::openPermsModal`) : 1 champ permissions, validation
  conservée dans `applyPermissions()`, Entrée = appliquer.
- **soul** (`chat.js::openSoulEditor`) : textarea SOUL.md + sauvegarde, taille
  confortable (`size: "lg"` + `width: 720` — l'existant était large, 720px).
- **agent** (`settings.js::showAgentForm`) : le plus complexe — formulaire en
  mode ADD **et** EDIT. Content dynamique (string), handlers de mappings
  (add/remove) fonctionnels via `onclick` inline dans le DOM de la modale,
  pré-remplissage EDIT + rendu des mappings dans `onOpen`, `escapeHtml` sur
  les valeurs des mappings (`renderPathMappings`).

## Lot « 5 listes dynamiques » (lignes 15-19)

Les 5 modales « liste » de `dashboard.html` ont été migrées vers `HolafModal.open`
avec `content` en **string HTML** (contenu de confiance). Les blocs HTML morts
ont été **supprimés** de `dashboard.html`. Les stubs `close*` sont conservés
(purge d'état + fermeture du handle stocké) pour préserver l'API existante —
notamment le handler global « Échap » de `app.js`.

**Ce que `open()` retourne** : un contrôleur `ctrl` avec `el` (racine), `body`,
`overlay`, `footer`, `close(value)`, `setTitle(t)`, `setContent(c)`,
`setBusy(on,msg)`, `bringToFront()`. C'est ce handle qui permet le **re-rendu
dynamique dans la modale ouverte** : on garde `ctrl` dans un champ d'état et on
appelle `ctrl.setContent(html)` pour remplacer le corps sans recréer le footer.

- **import-preview** (`editor.js::showImportPreview`) : contenu construit en
  string (conversions, warnings, compose converti), bouton « Confirmer l'import »
  → `confirmImport()`. Handle stocké dans `_importPreviewModalCtrl` ;
  `closeImportPreview()` ferme via le handle.
- **history** (`editor.js::openHistory`) : contenu chargé **de façon asynchrone**
  puis injecté via `ctrl.setContent()` (re-rendu dynamique). Les handlers
  (`_selectHistory`/`_previewHistory`/`_restoreHistory`) lisent le DOM de la
  modale (présente dans le document) comme avant. La restauration passe par le
  `HolafModal.confirm` déjà migré (ligne 5). Handle stocké dans `_historyModalCtrl`.
- **webui** (`dashboard.js::_renderWebUIModal`) : liste des adresses WebUI, liens
  cliquables (`target=_blank rel=noopener noreferrer`), résolution des adresses
  relatives contre l'URL de l'agent conservée (`_resolveWebUIUrl`). Handle stocké
  dans `_webUIModalCtrl`.
- **update-all** (`dashboard.js::_renderUpdateAllModal`) : **la plus délicate**.
  Approche de re-rendu dynamique :
  1. `HolafModal.open()` retourne `ctrl` ; on le stocke dans `_updateAllModalCtrl`.
  2. Le bouton « Mettre à jour » vit dans le **footer** de la brique (créé une
     seule fois à l'ouverture). `ctrl.setContent()` ne remplace QUE le corps — le
     footer (et donc le bouton) **persiste** entre deux re-rendus.
  3. À chaque exclusion ✕ (`excludeFromUpdateAll`), on re-rend le corps via
     `ctrl.setContent(html)` et on bascule l'état `disabled` du bouton via
     `ctrl.footer.querySelector('.holaf-modal-btn-primary')`
     (`_setUpdateAllConfirmDisabled`).
  4. État « vide » (tous exclus) : corps = message clair + bouton confirm
     `disabled=true` ; sinon bouton réactivé.
  Le repli `HolafModal.confirm` (ligne 7) a été **supprimé** : la modale est
  désormais toujours disponible. Le confirm imbriqué (édition container /
  update-all) fonctionne AU-DESSUS grâce à l'empilement de la brique (multi-
  instances OK, z-index distincts — vérifié en headless Chromium).
- **version-mismatch** (`dashboard.js::_openVersionMismatchModal`) : liste des
  agents/versions en désaccord, rendu identique. Handle stocké dans
  `_versionMismatchModalCtrl`.

> Vérifié en headless Chromium : webui avec 2 adresses (résolution relative +
> absolue, noopener), update-all avec exclusion ✕ (re-rendu correct, compteur
> [i/N] dans l'Activity, bouton confirm désactivé à l'état vide), empilement
> update-all + confirm (z-index 100006/100007), history et import-preview.

## La fenêtre « la plus utilisée » : édition de container (ligne 20)

La modale d'édition de container (`#container-edit-modal`) était la dernière
grande fenêtre statique du dashboard — et la plus utilisée (clic droit sur un
container → « Edit »). Elle est migrée vers `HolafModal.open` avec un
particularité : c'est un **formulaire riche à onglets**, pas un formulaire
simple.

- **`modals.js::openContainerEdit`** : le fetch de l'edit-spec et le refus des
  containers externes (`managed === false`) sont inchangés ; le rendu passe par
  `_renderContainerEditForm(spec, containerId)`.
- **`_renderContainerEditForm`** : construit la même string HTML qu'avant
  (section WebUI en tête, onglets Infos/Ports/Volumes/Env/Réseau, tables avec
  lignes ajoutables/suppressibles, réseaux en lecture seule) et l'ouvre dans la
  brique : `id: "container-edit-modal"` (anti-doublon multi-clics),
  `size: "xl"` (860px, l'ancienne modale faisait 750px max — le formulaire
  riche y gagne), footer `Annuler` / `💾 Appliquer` avec
  `close: false` sur « Appliquer » : la fermeture reste décidée par
  `applyContainerEdit` (validation WebUI, confirm de recréation empilé, POST,
  erreurs). Les icônes Lucide sont créées APRÈS injection du contenu
  (`onOpen`) — l'ancien code appelait `createIcons()` avant `innerHTML`, les
  icônes du formulaire n'étaient donc jamais rendues.
- **Stratégie content** : string HTML (contenu de confiance, comme le lot
  « 5 formulaires »). Les IDs des champs sont conservés :
  `applyContainerEdit`, `_addEditRow`, `_attachWebUIRowListener` et
  `_addWebUIRow` lisent le DOM du document, où la modale est rattachée.
- **`_switchEditTab`** : bascule d'onglets scopée au corps de la modale ouverte
  (handle `ctrl.body` stocké dans `_containerEditModalCtrl`, repli sur
  `btn.closest('.holaf-modal-body')`). Un seul panneau visible après clic ; la
  section WebUI reste toujours visible au-dessus des onglets.
- **Menu contextuel « ports libres »** (`openHostPortContextMenu`) : la cible
  `#host-port-context-menu` reste un élément statique de `dashboard.html`
  (HORS de la modale). Son z-index est relevé à 110000 (CSS) pour passer
  AU-DESSUS des overlays HolafModal (base 100000+) — positionnement clampé et
  fermeture (clic extérieur, Échap, scroll) inchangés. Au passage, correction
  d'un bug PRÉEXISTANT (présent depuis l'origine du menu, commit `ed27800`,
  vérifié sur le code d'avant migration en headless Chromium) : le `onclick`
  des propositions appelait `closeHostPortContextMenu()` AVANT
  `_setHostPort()` — or `close()` nullise `_hostPortMenuTarget`, donc la
  proposition n'était jamais appliquée au champ. Ordre inversé :
  `_setHostPort(...)` puis `closeHostPortContextMenu()`.
- **`applyContainerEdit`** : strictement inchangé (collecte, validation
  http/https des adresses WebUI, confirm « Recréer le container » empilé si
  running, POST `/api/containers/{id}/update`, puis `closeContainerEdit()` +
  `refreshStacks(true)` — voir docs/container-edit-refresh-backup.md).
- **`closeContainerEdit`** : stub conservé (fermeture via le handle stocké +
  purge d'état) pour préserver l'API existante — notamment le handler global
  « Échap » de `app.js`, dont les handlers restent null-safe (le bloc HTML
  mort `#container-edit-modal` a été supprimé de `dashboard.html`).

> Vérifié en headless Chromium : ouverture sur un faux container, largeur 860px
> (`holaf-modal-xl`), thème docky (`--hm-*`), onglets (clics successifs → un
> seul panneau visible, section WebUI toujours présente), ajout/suppression de
> lignes WebUI, clic droit sur un port hôte (menu rendu au-dessus de la modale,
> remplissage du champ au clic sur une proposition), POST d'update (succès →
> fermeture + purge ; échec → modale maintenue), confirm empilé si running,
> Échap et clic sur le fond.

## La plus risquée : Activity modal (ligne 21, streaming terminal)

L'Activity modal affiche la progression **en streaming** des commandes
(`_streamAction` consomme un flux SSE et écrit ligne à ligne dans la sortie
terminal). C'est la plus délicate à migrer car HolafModal `close()` **détruit**
l'élément (retire l'overlay du DOM) — alors que l'ancien code gardait la modale
attachée (simple `hidden`) et continuait d'écrire dans `#activity-output` même
fermée.

**Approche retenue : « élément persistant »** — le contenu (statut + sortie
terminal) est un **Node créé une seule fois** (`_getActivityContent`) et passé à
`HolafModal.open({ content })`. À la fermeture, l'élément se détache du DOM mais
la **référence JS persiste** : le streaming continue d'écrire sans crash, et la
consolidation des lignes de progression se fait **par référence** (la garde
`isConnected` de `_appendProgressLine` est supprimée). À la réouverture,
`_openActivity` réinitialise la sortie et rouvre une modale fraîche sur le même
élément.

- **`_openActivity(title)`** : réinitialise la sortie (`terminal-empty`) et le
  statut (`status-running`), puis `HolafModal.open({ id: "activity-modal",
  content, width: 650, buttons: [Fermer] })`. Anti-doublon par `id` : un second
  appel pendant qu'elle est ouverte ramène au premier plan sans recréer.
- **`_appendActivity` / `_scrollActivity`** : écrivent dans `_activityOutputEl`
  (référence persistante) au lieu de `document.getElementById("activity-output")`.
- **`_appendProgressLine`** : consolidation par référence (`lastProgressEl`
  non-null) — plus de garde `isConnected`, donc la réécriture en place continue
  même quand la modale est fermée/détachée.
- **`_finishActivity`** : met à jour le statut (`Terminé`/`Échec`) toujours ;
  n'écrit le résumé (`✓ Terminé` / `✗ Échec`) **que si la modale est encore
  ouverte** (`_activityModalCtrl` non-null) — on ne la rouvre pas si l'utilisateur
  l'a fermée pendant le streaming. Le fallback JSON (résultat complet) est
  conservé.
- **`closeActivity`** : stub conservé — ferme via le handle stocké
  (`_activityModalCtrl.close()`), null-safe pour le handler global « Échap » de
  `app.js`. Le handler « clic sur le fond » de `app.js` est supprimé (géré par
  `closeOnOverlay` de la brique).
- **CSS** : `#activity-modal .holaf-modal-body { padding:0; display:flex;
  flex-direction:column }` + statut aligné en haut ; la sortie garde les classes
  `.terminal-output` / `.terminal-line(.progress)` / `.terminal-summary`.
- Le bloc HTML mort `#activity-modal` est **supprimé** de `dashboard.html`.

> Vérifié en headless Chromium : ouverture (racine `holaf-modal-root`), fake
> stream avec lignes de progression (consolidation en place → 1 seule ligne
> `progress`, dernière trame « Downloading 100% »), **fermeture en plein
> streaming** → l'élément se détache mais l'accumulation continue sans crash,
> `_finishActivity` après fermeture → pas de réouverture ni de résumé, statut
> « Terminé » ; réouverture + done → résumé `✓ Terminé` écrit ; thème docky
> (`--hm-bg #151527`, `--hm-accent #e94560`, `--hm-radius 16px`) ; Échap ferme.

## `console-modal` : code mort — supprimé ✅

`console-modal` (`#console-modal` dans `dashboard.html`) était **du code mort** :
`openConsole()` (dashboard.js) ouvre une **fenêtre popup** `/popup/console`
(xterm.js) et n'ouvrait jamais la modale ; `closeConsole()` ne faisait que cacher
la modale et fermer un `consoleWs` jamais initialisé.

**Nettoyage réalisé** (la recommandation initiale de tout supprimer, `openConsole`
compris, était erronée — `openConsole` est le chemin **vivant** de la console) :

- bloc HTML `#console-modal` supprimé de `dashboard.html` ;
- `closeConsole()` supprimée de `dashboard.js` (plus aucun call site) ;
- références `app.js` nettoyées : handler « Échap » et clic backdrop, plus les
  propriétés d'état orphelines `consoleWs`, `consoleContainerId`,
  `consoleContainerAgent`, `consoleHistory` ;
- CSS orphelin spécifique supprimé (`.modal-console`, `.console-input-area`,
  `#console-prompt`, `#console-input`) — `.terminal-output` conservé (utilisé
  par la modale Activity via `modals.js`).

**Conservé** : `openConsole()` (popup `window.open`), la route `/popup/console`
et son template xterm.js, et tout le chemin exec/terminal (WebSocket
`/api/containers/{id}/exec`).

## Une seule copie, servie

⚠️ **Une seule copie de la brique est maintenue dans Docky** :

```
orchestrator/app/static/vendor/holaf/holaf-modal.js      ← la brique
orchestrator/app/static/vendor/holaf/holaf-manifest.json ← versions installées
orchestrator/app/static/js/holaf-docky-theme.js          ← thème Docky (module autonome)
```

Elle est servie par FastAPI via le montage `/static` et référencée dans les
templates :

```html
<script type="module" src="/static/vendor/holaf/holaf-modal.js"></script>
```

Le contexte de build Docker étant `./orchestrator`, tout ce qui doit finir dans
l'image doit vivre sous `orchestrator/`. Une ancienne copie racine
(`<racine>/vendor/holaf/`) avait été installée en parallèle lors de l'adoption
initiale : elle n'était jamais servie (doublon) et a été supprimée.

**Règle d'arrêt** : la brique est une copie pinnée — ne JAMAIS modifier
`holaf-modal.js` directement dans Docky. Toute amélioration se fait dans la lib
puis se propage par `upgrade` (voir ci-dessous).

## Installer / mettre à jour une brique

Le script `holaf` de la lib copie la brique dans `<DEST>/vendor/holaf/` (avec
le manifest local `holaf-manifest.json`). **Pour Docky, la cible `<DEST>` est le
répertoire servi `orchestrator/app/static`**, pas la racine du projet :

```bash
# Installation initiale d'une brique
cd /projects/holaf-lib && ./scripts/holaf install <brique> /projects/Docky/orchestrator/app/static

# Vérifier si les copies de Docky sont à jour
cd /projects/holaf-lib && ./scripts/holaf check /projects/Docky/orchestrator/app/static

# Tirer une amélioration de la lib
cd /projects/holaf-lib && ./scripts/holaf upgrade <brique> /projects/Docky/orchestrator/app/static

# Remonter une amélioration (interdite en direct ici : passer par la lib)
cd /projects/holaf-lib && ./scripts/holaf adopt <brique> /projects/Docky/orchestrator/app/static
```

Après installation, committer `orchestrator/app/static/vendor/holaf/` dans Docky.

## Vérification

- La copie servie est strictement identique à la lib
  (`diff js/holaf-modal.js` → aucun écart).
- Le smoke test HTTP 200 sur `/static/vendor/holaf/holaf-modal.js` couvre la
  copie servie (v0.2.1).
- Thème : `HolafModal.version === "0.2.1"`, `HolafModal.themes.list()` contient
  `docky`, et une modale ouverte sans option `theme` porte les variables
  `--hm-*` de la palette Docky (vérifié en headless Chromium).