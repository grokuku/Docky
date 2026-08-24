# Bouton « vider le filtre » + « Update all containers »

Ajout de deux features frontend (Docky v0.0.4), **sans ajout backend**.

## Feature 1 — Bouton « vider le filtre » (✕)

- **Champ de filtre** : `<input id="container-search">` dans la barre du haut
  (`topbar-dropdowns` de `orchestrator/templates/dashboard.html`).
- **Méthode de filtrage** : `DockyApp.onSearchInput(value)` (débounce 150 ms)
  met à jour `DockyApp._searchQuery`, puis `_filterContainers(containers)`
  filtre par sous-chaîne insensible à la casse sur le nom. Appliqué dans les
  deux vues (grille et tableau) via `renderCurrentView()`.
- **Bouton ajouté** : un bouton `✕` (`#container-search-clear`) à l'intérieur
  d'un wrapper `.search-box` positionné autour du champ. Il est visible
  uniquement quand le champ contient une valeur (classe `.has-value`).
- **Comportement** : `DockyApp.clearSearch()` vide `_searchQuery`, réinitialise
  le champ, persiste `localStorage('docky_container_search')`, puis re-rend la
  vue courante (`renderCurrentView()`) et `updateStatsBar()`. État vide =
  affichage complet. Fonctionne de façon identique en grille et en tableau
  (les deux rendus passent par `_filterContainers`).

### Fichiers
- `orchestrator/templates/dashboard.html` — wrapper `.search-box` + bouton `✕`.
- `orchestrator/app/static/js/dashboard.js` — `clearSearch()` +
  `updateSearchClearUI()`; `onSearchInput` appelle désormais
  `updateSearchClearUI()`.
- `orchestrator/app/static/js/app.js` — `init()` appelle `updateSearchClearUI()`
  au chargement pour restaurer l'état du bouton.
- `orchestrator/app/static/css/style.css` — styles `.search-box`, `.search-clear`.

## Feature 2 — « Update all containers »

### Où est l'info « update disponible » ?
L'état « update dispo » **n'est pas chargé** dans la liste `/api/containers`.
Il est connu via l'endpoint par-container **GET `/api/containers/{id}/update-check`**
(JSON `{ update_available: bool, ... }`), appelé de façon asynchrone par
`DockyApp.checkUpdate()` et mis en cache dans `_updateCheckCache`
(source de vérité des badges). Il n'existe **aucun endpoint bulk**.

**Où vit réellement la donnée côté frontend** : dans l'objet `DockyApp`, propriété
`_updateCheckCache` (init à `{}` dans `app.js`), clé `c:<containerId>` pour les
containers, `s:<stack>@<agent>` pour les stacks. C'est ce cache qui pilote les
badges « Update dispo » via `_updateBadgeClass()` et `_countCachedUpdates()`.

**Fraîcheur réelle** : le cache est (re)rempli par `checkUpdate()`/`checkStackUpdate()`
à chaque rendu, et le **refresh auto** (`refreshTimer = 5 s` dans `app.js:34`, actif par
`autoRefresh`) relance un check à chaque tick → une entrée est normalement < 5 s.
Le résultat stocké n'était pas horodaté : on a ajouté `_checkedAt` (timestamp) à
l'écriture via `_tagUpdateCache()`. Le TTL retenu est **30 s** (`_UPDATE_CACHE_TTL_MS`),
raisonnable même si l'auto-refresh est désactivé.

### Décision backend : aucun endpoint ajouté
Comme l'info est récupérable par l'endpoint `update-check` existant (un scan
léger est déjà fait par la UI pour chaque container), on a implémenté le
« update all » **en pur frontend** : itération sur les containers + appels aux
endpoints existants. Pas de nouvel endpoint, pas de nouveau test pytest,
zéro régression.

### Fonctionnement du bouton « ⬆ Update all »
Bouton `#update-all-btn` inséré dans `topbar-right` (à côté de « Ports »).

1. `DockyApp.updateAllContainers()` — désactive le bouton, affiche un toast
   « Recherche des mises à jour… », puis appelle
   `_collectContainersWithUpdate()`.
2. `_collectContainersWithUpdate()` — **réutilise** l'état « update dispo » déjà
   calculé pour l'affichage (cache `_updateCheckCache`) pour les containers des
   **agents non masqués**. Deux phases :
   - **Phase 1 (réutilisation)** : pour chaque container, si l'entrée de cache est
     présente **et** fraîche (`Date.now() - _checkedAt < _UPDATE_CACHE_TTL_MS` = 30 s),
     on l'utilise directement **sans appel réseau**.
   - **Phase 2 (fallback)** : uniquement pour les containers dont l'entrée est
     **absente ou périmée**, on relance un `update-check` **ciblé** (`Promise.all`),
     et on réécrit le cache avec un timestamp pour rester cohérent avec l'affichage.
   Renvoie `[{ id, name, agent }]` pour ceux avec `update_available === true`.
   Règle retenue : **réutiliser si < 30 s, rafraîchir (check ciblé) si périmé/absent**,
   pour ne jamais afficher un état faux.
3. **Confirmation** — si la liste est vide : toast « Aucun container à mettre à
   jour », aucune action. Sinon : ouverture d'une **modale**
   (`#update-all-modal`) listant chaque **(container, agent)** concerné, avec
   boutons « Annuler » / « Mettre à jour ». Repli `window.confirm` si le modal
   est absent.
4. `DockyApp.confirmUpdateAll()` — exécute les mises à jour **séquentiellement**
   (un container après l'autre), via l'endpoint SSE existant
   **POST `/api/containers/{id}/update-image`** consommé par `_streamAction`
   (mêmes mécanismes que le bouton ⬆ unitaire). La progression est affichée
   dans l'Activity Modal avec le compteur `[i/N]` et le nom du container.
   Après chaque succès : `_invalidateContainerUpdateCache(id)` +
   `checkUpdate(id, agent)` (anti-flicker). À la fin : résumé
   (succès/échecs), toast, puis `refreshStacks()`.

### Exclusion de containers (✕) — v0.0.5
Chaque ligne de la liste de la modale `#update-all-modal` affiche désormais un
petit bouton **✕** à droite.

- **Clic sur ✕** : `DockyApp.excludeFromUpdateAll(index)` retire le container
  de `_updateAllList` (source de vérité) puis re-rend la modale via
  `_renderUpdateAllModal()`. C'est une exclusion **définitive pour cette passe**
  (pas de restauration). `confirmUpdateAll()` n'itère plus que sur les
  containers restants → le compteur `[i/N]` reflète le N réel (N = nombre de
  containers restants après exclusions).
- **État « vide »** : si tous les containers sont exclus, la modale affiche un
  message clair (« Aucun container à mettre à jour ») et le bouton « Mettre à
  jour » (`#update-all-confirm-btn`) est désactivé (`disabled`).

### Fichiers
- `orchestrator/templates/dashboard.html` — id `update-all-confirm-btn` sur le
  bouton « Mettre à jour ».
- `orchestrator/app/static/js/dashboard.js` — `_renderUpdateAllModal()` gère
  l'état vide + rend un bouton ✕ par ligne ; nouvelle méthode
  `excludeFromUpdateAll(index)`.
- `orchestrator/app/static/css/style.css` — styles `.update-all-item`,
  `.update-all-remove` (hover rouge) et `#update-all-confirm-btn:disabled`.

### Choix séquentiel vs parallèle
**Séquentiel**, documenté : un pull + recreate à la fois. Plus sûr pour les

dépendances entre containers d'une même stack, et lisible dans les logs. Un
parallèle limité serait possible mais ajouterait de la complexité sans gain
robuste ici.

### Fichiers
- `orchestrator/templates/dashboard.html` — bouton `#update-all-btn`, modale
  `#update-all-modal` (bouton « Mettre à jour » identifié `#update-all-confirm-btn`).
- `orchestrator/app/static/js/dashboard.js` — `updateAllContainers()`,
  `_collectContainersWithUpdate()`, `_renderUpdateAllModal()`,
  `excludeFromUpdateAll()`, `closeUpdateAllModal()`, `confirmUpdateAll()`.
  Optimisation « Update all » :
  ajout de `_UPDATE_CACHE_TTL_MS` (30 s) et `_tagUpdateCache()` (horodatage du
  cache), `checkUpdate`/`checkStackUpdate` taguent désormais leurs entrées, et
  `_collectContainersWithUpdate()` réutilise le cache frais au lieu de rescanner
  tous les containers (fallback ciblé sur les entrées périmées/absentes).
- `orchestrator/app/static/js/app.js` — câblage modale (fermeture par clic
  sur le backdrop + touche Échap).
- `orchestrator/app/static/css/style.css` — styles de la liste
  `.update-all-list`, `.update-all-agent`.

## Tests
- **JS** : vérifié avec `node --check` (dashboard.js, app.js) — aucune erreur.
- **Backend** : aucun endpoint ajouté, donc aucun test ajouté.
- **pytest** : `timeout 300 python -m pytest -q` → **417 passed** (aucune
  régression).

## Note de comptage
La consigne mentionnait 406 tests ; la suite réelle en compte 417 (tous verts).
