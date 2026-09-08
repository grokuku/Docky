# Adoption de la brique HolafFetch (holaf-lib)

## Résumé

La brique
[`HolafFetch`](../orchestrator/app/static/vendor/holaf/holaf-fetch.js) provient
du dépôt [`/projects/holaf-lib`](/projects/holaf-lib) (brique `fetch`, version
pinnée dans `holaf-manifest.json`). Elle remplace les implémentations maison
`apiFetch` (une copie dans `api.js`, une seconde dans `settings.js` — la
duplication est supprimée) : le cœur HTTP blindé de la brique (JSON vérifié
avant `res.json()`, erreurs typées `HolafFetchError { status, data, body }`,
timeout `AbortController`, messages automatiques `body.error` sinon
`body.detail` — convention FastAPI) est enveloppé par un **adaptateur fin**
[`holaf-docky-fetch.js`](../orchestrator/app/static/js/holaf-docky-fetch.js)
qui injecte la **config commune Docky**.

**Version installée : v0.1.1** (auth enfichable, `on: { status }`, timeout
défaut 30 s, retry brique opt-in — non utilisé par Docky, `raw: true`,
détection de page de login SSO).

## Stratégie : adaptateur fin + config commune

La brique est purement logique et **sans aucun couplage serveur** : l'auth est
**enfichable** (`opts.auth`) et configurée par l'hôte. L'adaptateur
(`window.DockyFetch`, script classique, lecture de la brique au moment de
l'appel — l'ordre avec le module différé n'importe pas) fusionne à chaque
requête, sans muter l'objet de l'appelant :

- **auth CSRF** (double-submit cookie) : `auth: { type: "csrf",
  cookieName: "csrf_token" }` → en-tête `X-CSRF-Token` lu **frais** à chaque
  requête (voir `docs/csrf-protection.md`) ;
- **on.status 401 → redirection `/login`** (session expirée) — un éventuel
  hook `on.status` de l'appelant reste appelé (composition) ;
- **timeout 30 000 ms par défaut**, surchargeable via `opts.timeout`
  (`0` = aucun).

Deux niveaux d'API :

| Méthode | Comportement | Utilisateurs |
|---|---|---|
| `DockyFetch.request(url, opts)` | adaptateur fin ; **lève** la `HolafFetchError` (accès `err.status` / `err.data` / `err.message`) | polling du dashboard (`err.status === 404`) |
| `DockyFetch.apiRequest(url, opts)` | **contrat Docky** : ne lève jamais — données \| **null**, toast avec le message, 401 → null silencieux (redirection déjà déclenchée) | `apiFetch`/`apiPost` (`api.js`), `apiFetch` (`settings.js`) |

## Contrat Docky préservé

Les call sites existants (`apiFetch`/`apiPost` dans tous les modules) ne
changent **pas** : données \| null, toast en cas d'erreur, redirection 401.

- **Toast avec le message** : `err.message` (contient désormais `body.detail`
  via la brique v0.1.1 — ex. `"nom d'agent déjà utilisé"` au lieu d'un
  générique), repli `err.data.detail`, puis `"erreur réseau"`.
  Conséquence assumée : les call sites qui affichaient `data.detail` sur une
  réponse d'erreur HTTP (l'ancien `apiFetch` renvoyait le corps JSON même en
  4xx/5xx) reçoivent désormais `null` et affichent leur message de repli —
  le message réel est de toute façon affiché par le toast de l'adaptateur.
- **Redirect 401** : partout (y compris polling — une session expirée rend la
  page inutilisable), via le hook `on.status` commun.

## Règle canonique « une seule notification par erreur » (correction double-toast)

`DockyFetch.apiRequest` affiche **déjà** le toast d'erreur (message dérivé de
`err.message` / `body.detail`). Certains call sites re-toastaient une erreur
déjà toastée quand `data === null` (double-toast). Règle canonique : **une
seule notification par erreur**.

Deux options ont été ajoutées à `apiRequest` (et donc à `apiFetch`/`apiPost`/
`apiPut`/`apiDelete`) pour reprendre la main sans créer de doublon :

| Option | Effet | Usage |
|---|---|---|
| `{ silent: true }` | supprime le toast de l'adaptateur | l'appelant affiche son propre message **plus précis / contextuel** sur `data === null` |
| `{ errorMessage: "…" }` | remplace le message générique de l'adaptateur | message précis sans toast local |

Les deux sont mutuellement exclusifs (`errorMessage` prime sur `silent`).

### Call sites corrigés (audit)

| Fichier:ligne | Avant | Après |
|---|---|---|
| `settings.js:155` `scanModels` | toast « Aucun modèle trouvé. » même quand `data === null` (erreur déjà toastée) | toast local **uniquement si `data` présent** (réponse sans modèle) ; `data === null` → silence (l'adaptateur a déjà toasté) |
| `dashboard.js:1425` `containerAction` | toast « Échec … container » même quand `result === null` | toast local **uniquement si `result` présent** (`success === false`) ; `result === null` → silence |
| `editor.js:674` `toggleShowAllStackFiles` | toast « Impossible de recharger la liste des fichiers » même quand `filesData === null` | toast local **uniquement si `filesData` présent** ; `null` → silence |
| `modals.js:553` `proposePorts` | double-toast (adaptateur + « Agent injoignable… ») | `{ silent: true }` à l'appel — le message local « Agent injoignable… » est **plus précis** que le générique de l'adaptateur |

### Cas silencieux légitimes conservés

- **404 polling** (`dashboard.js` `loadContainerStats`/`checkUpdate`/
  `checkStackUpdate`) : passent par `DockyFetch.request` (qui **lève**), pas
  par `apiRequest` — aucun toast, comportement inchangé.
- **Réponses vides** (`data === null` sur un GET de chargement) : les call
  sites `if (!data) return;` ne toastaient pas et ne toasteront toujours pas.
- **401** : `apiRequest` retourne `null` silencieusement (redirection déjà
  déclenchée) — inchangé.
- **Retry réseau ×1 après 500 ms, méthodes sûres uniquement** (GET/HEAD/
  OPTIONS) — implémenté **au niveau adaptateur**, PAS via l'option `retry` de
  la brique (qui reprendrait aussi les 5xx). Jamais sur les mutations, jamais
  sur les 4xx/5xx, jamais sur les timeouts (un serveur lent le resterait au
  2ᵉ essai) ni sur une annulation explicite (`opts.signal` déjà aborted).
- **Corps** : sérialisation JSON automatique par la brique (objet →
  `JSON.stringify` + `Content-Type`) ; `settings.js apiPost/apiPut` en
  profitent et ne dupliquent plus la sérialisation manuelle.

## Diff résumé

- **Ajout** : `static/vendor/holaf/holaf-fetch.js` (copie pinnée, strictement
  identique à la lib), `static/js/holaf-docky-fetch.js` (adaptateur fin).
- **Modifié** :
  - `static/js/api.js` — `apiFetch` → `DockyFetch.apiRequest` (une ligne ;
    l'ancienne implémentation fetch + retry maison est supprimée). **Le
    monkey-patch CSRF global de `window.fetch` a été retiré** : tous les
    `fetch()` directs sont désormais migrés, plus rien ne dépend du wrapper
    (voir « Migration des derniers fetch directs »).
  - `static/js/settings.js` — la 2ᵉ implémentation `apiFetch` délègue à
    `DockyFetch.apiRequest` (duplication supprimée, y compris le `getCookie`
    local devenu inutile — la page settings ne charge pas `api.js` ; c'est
    l'auth CSRF de l'adaptateur qui signe les requêtes mutantes) ;
    `apiPost/apiPut` passent l'objet tel quel (la brique sérialise).
  - `static/js/dashboard.js` — **polling migré** (voir ci-dessous).
  - `static/js/chat.js`, `static/js/modals.js`, `templates/logs.html` —
    **derniers `fetch()` directs migrés** (voir section dédiée).
  - `templates/dashboard.html` — ajout de la brique (`<script type="module">`)
    et de l'adaptateur **avant `api.js`**.
  - `templates/settings.html` — ajout de la brique + de l'adaptateur avant
    `settings.js`.
  - `templates/logs.html` — ajout de la brique + de l'adaptateur avant le
    script inline.

## Polling du dashboard migré (timeout 10 s)

Trois call sites de polling passent par `DockyFetch.request(url,
{ timeout: 10000 })` — **c'est la correction des « stats gelées »** : avant,
le `fetch` sans timeout d'un agent muet immobilisait la promesse et le drapeau
`_pendingFetches` restait `true`, gelant les stats / badges du container.

| Call site | Avant | Après |
|---|---|---|
| `loadContainerStats` | `fetch` + `resp.status === 401` | `DockyFetch.request` timeout 10 s ; erreurs ignorées (catch), `finally` libère `_pendingFetches` |
| `checkUpdate` (containers) | `fetch` + tests `resp.status` 401/404 + `resp.json()` | `DockyFetch.request` timeout 10 s ; **404 via `err.status === 404`** (purge cache + resync compteur, inchangé) ; autres erreurs → état affiché conservé |
| `checkStackUpdate` (stacks) | `fetch` + `resp.json()` sans test 404 (le corps d'erreur était mis en cache) | `DockyFetch.request` timeout 10 s ; **404 via `err.status === 404`** → cache purgé, pas de pollution |

La logique `_pendingFetches` (anti-concurrence) et le retry de polling (le
refresh auto relance les checks à chaque cycle) sont **conservés tels quels**.
Micro-améliorations de robustesse : un 404 stack ne pollue plus le cache, un
corps illisible ou un 5xx ne réécrit plus d'entrée de cache fantôme (l'état
affiché est conservé, conformément à la philosophie anti-flicker du module).

## Une seule copie, servie

Comme pour HolafModal et HolafToast, une seule copie pinnée est maintenue,
servie par FastAPI via le montage `/static` :

```
orchestrator/app/static/vendor/holaf/holaf-fetch.js       ← la brique (pinnée v0.1.1)
orchestrator/app/static/vendor/holaf/holaf-manifest.json  ← versions installées
orchestrator/app/static/js/holaf-docky-fetch.js           ← adaptateur fin (config commune)
```

**Règle d'arrêt** : ne JAMAIS modifier `holaf-fetch.js` directement dans
Docky. Toute amélioration se fait dans la lib puis se propage par
`./scripts/holaf upgrade fetch …`.

## Monkey-patch CSRF : retiré (migration terminée)

Le wrapper global `window.fetch` installé par `api.js` (double-submit pour
toute requête mutante) a été **supprimé** : tous les `fetch()` directs ont
été migrés vers l'adaptateur, plus aucun module ne dépend du wrapper. La
signature CSRF est désormais assurée **uniquement** par l'auth de la brique
(`auth: { type: "csrf", cookieName: "csrf_token" }`), qui lit le cookie
`csrf_token` frais à chaque requête et pose `X-CSRF-Token`. La protection
CSRF serveur est vérifiée : une mutation sans `X-CSRF-Token` → `403
{'detail':'CSRF'}` ; avec l'en-tête (posé par la brique) → `200`.

## Migration des derniers fetch directs (chat / modals / logs)

Les derniers `fetch()` directs de `chat.js`, `modals.js` et `logs.html` sont
migrés vers `DockyFetch.request` (adaptateur fin). Aucun `window.fetch`
direct ne subsiste dans le code applicatif (grep : 0 hors brique/adaptateur).
Timeouts adaptés par flux ; messages d'erreur préservés (`err.message` =
`body.detail`), 401 → redirection `/login` via l'adaptateur.

### chat.js (4 call sites)

| Flux | Avant | Après | Timeout |
|---|---|---|---|
| `sendChatMessage` POST `/api/chat` | `fetch` + `resp.status===400` « LLM non configuré » | `DockyFetch.request` ; détection 400 déplacée dans le `catch` (`e.status===400 && e.data.detail` contient « not configured ») | **180 000 ms** (LLM lent) |
| `validateExec` ×2 POST `/api/chat/validate-exec` | `fetch` + `resp.ok` | `DockyFetch.request` ; distinction erreur réseau vs HTTP | 30 s |
| `saveSoul` PUT `/api/soul` | `fetch` + `resp.ok` | `DockyFetch.request` ; body string tel quel (`text/plain`, la brique ne sérialise pas les chaînes) | 30 s |

### modals.js (3 call sites)

| Flux | Avant | Après | Timeout |
|---|---|---|---|
| `_streamAction` POST action container (SSE) | `fetch` + `resp.status===401` + `resp.ok` + `reader` | `DockyFetch.request` **`raw:true` + `timeout:0`** (flux SSE jamais coupé) ; logique 401/!ok/reader conservée | **0** (aucun) |
| `openContainerEdit` GET spec | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` | 30 s |
| `update` PUT spec | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` (objet sérialisé par la brique) | 30 s |

### logs.html (2 call sites)

| Flux | Avant | Après | Timeout |
|---|---|---|---|
| chargement des logs (GET) | `fetch` + `resp.ok` | `DockyFetch.request` ; erreur réseau silencieuse (état « hors ligne » conservé) vs erreur HTTP affichée | 30 s |
| rafraîchissement des logs (GET) | `fetch` + `resp.ok` | `DockyFetch.request` ; même distinction | 30 s |

`logs.html` charge désormais la brique (`<script type="module">`) et
l'adaptateur avant le script inline.

## Migration des fetch directs restants (editor / dashboard / events)

Les `fetch()` directs de `editor.js`, `dashboard.js` (reste) et `events.js`
sont migrés vers `DockyFetch.request` (adaptateur fin). Aucun `window.fetch`
direct ne subsiste dans ces trois modules. Timeouts adaptés par flux ;
comportement d'erreur conservé (toast avec `err.message` = `body.detail`,
401 → redirection `/login` via l'adaptateur).

### editor.js (16 call sites)

| Flux | Avant | Après | Timeout | 401 silencieux |
|---|---|---|---|---|
| `loadEditor` batch `files-with-content` (GET) | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` | 30 s | non (redirect) |
| `loadEditor` chargement séquentiel d'un fichier (GET text/plain) | `fetch` + `resp.ok` + `resp.text()` | `DockyFetch.request` **`raw:true`** + `resp.text()` | 30 s | non (redirect) |
| `saveCurrentFile` (PUT text/plain) | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` (body string tel quel, Content-Type préservé) | 30 s | non (redirect) |
| `saveAndDeploy` boucle PUT text/plain | `fetch` + `resp.ok` | `DockyFetch.request` (body string tel quel) | 30 s | non (redirect) |
| `createEnvFile` (PUT text/plain, body vide) | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` | 30 s | non (redirect) |
| `toggleShowAllStackFiles` chargement fichier (GET text/plain) | `fetch` + `resp.ok` + `resp.text()` | `DockyFetch.request` **`raw:true`** + `resp.text()` | 30 s | non (redirect) |
| `_doImportPreview` (POST import, dry_run) | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` (objet sérialisé par la brique) | 60 s | non (redirect) |
| `confirmImport` (POST import) | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` | 60 s | non (redirect) |
| `doImportDirect` (POST import) | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` | 60 s | non (redirect) |
| `doImport` (POST import) | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` | 60 s | non (redirect) |
| `createStack` (POST) | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` | 30 s | non (redirect) |
| `confirmDeleteStack` (DELETE) | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` | 30 s | non (redirect) |
| `applyPermissions` (PUT) | `fetch` + `resp.status===401` + `resp.ok` | `DockyFetch.request` | 15 s | non (redirect) |
| `openHistory` (GET) | `fetch` + `resp.json()` | `DockyFetch.request` | 15 s | non (redirect) |
| `_previewHistory` (GET) | `fetch` + `resp.json()` | `DockyFetch.request` | 15 s | non (redirect) |
| `_restoreHistory` (POST) | `fetch` + `resp.json()` | `DockyFetch.request` | 60 s | non (redirect) |

### dashboard.js (reste)

| Flux | Avant | Après | Timeout | 401 silencieux |
|---|---|---|---|---|
| `loadVersion` (GET `/api/version`) | `fetch` + `resp.status===401` + `resp.json()` | `DockyFetch.request` | 10 s | non (redirect) |
| `refreshStacks` liste containers (GET `/api/containers?agent=all`) | `fetch` + `resp.status===401` + `resp.json()` | `DockyFetch.request` (`.catch(()=>null)`) | 10 s | non (redirect) |
| `_collectContainersWithUpdate` fallback (GET update-check) | `fetch` + `resp.status===401` + `resp.json()` | `DockyFetch.request` | 10 s | non (redirect) |

### events.js — heartbeat de présence

| Flux | Avant | Après | Timeout | 401 silencieux |
|---|---|---|---|---|
| `startHeartbeat` (POST `/api/presence/heartbeat`) | `fetch` + `credentials` | `DockyFetch.request` **`noRedirect401:true`** | 10 s | **oui** (pas de toast, PAS de redirect 401) |

Le heartbeat de présence doit rester silencieux : un 401 ne doit jamais
déloger l'utilisateur (le polling du dashboard gère déjà la session expirée).
L'option `noRedirect401` (ajoutée à l'adaptateur) supprime la redirection
`/login` du hook `on.status` commun pour ce flux uniquement.

### Adaptateur : option `noRedirect401`

`DockyFetch.request(url, { noRedirect401: true })` désactive la redirection
`/login` sur 401 pour cet appel (le hook `on.status` de l'appelant reste
composé). Utilisé par le heartbeat de présence.

## Installer / mettre à jour

```bash
cd /projects/holaf-lib && ./scripts/holaf install fetch /projects/Docky/orchestrator/app/static
cd /projects/holaf-lib && ./scripts/holaf check /projects/Docky/orchestrator/app/static
cd /projects/holaf-lib && ./scripts/holaf upgrade fetch /projects/Docky/orchestrator/app/static
```

## Vérification

- Copie servie strictement identique à la lib (`diff` → aucun écart) ;
  `./scripts/holaf check` → tout à jour (modal 0.2.1 / toast 0.2.1 / fetch 0.1.1).
- `node --check` OK sur tous les JS modifiés (+ la brique en `.mjs`).
- `python -m pytest -q` : **542 verts**.
- Smoke HTTP authentifié : `/dashboard` 200, `/settings` 200,
  `/static/vendor/holaf/holaf-fetch.js` 200, `/static/js/holaf-docky-fetch.js` 200.
- **CSRF serveur** (TestClient, CSRF activé) : mutation `PUT /api/settings/llm`
  sans `X-CSRF-Token` → `403 {'detail':'CSRF'}` ; avec l'en-tête → `200`.
- Headless Chromium : `/dashboard` charge, les stats passent par le nouveau
  chemin (`DockyFetch.request`), un 401 redirige vers `/login`.
- **Double-toast** : headless Chromium — un `apiRequest` en échec produit
  **exactement un** toast (`toast_delta_after_error === 1`), thème docky
  appliqué, `HolafToast.version === "0.2.1"` (voir section « Règle canonique »).
- **CSRF navigateur** (headless Chromium) : mutation `PUT /api/settings/llm`
  déclenchée par le frontend → `200` (la brique pose `X-CSRF-Token`, aucun
  403) ; `window.fetch` reste natif (plus de monkey-patch).
- **Flux longs** (headless Chromium, interception CDP) :
  - `/api/chat` réponse retardée de **35 s** → rendue sans abort
    (`timeout: 180000`) ;
  - action container SSE (`_streamAction`) réponse retardée de **35 s** →
    flux complété (`{success:true}`) sans coupure (`raw:true` + `timeout:0`).
- **Grep** : aucun `window.fetch` direct hors brique/adaptateur ;
  `installCsrfFetchWrapper` / `__dockyCsrfWrapped` absents.