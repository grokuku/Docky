# Adoption de la brique HolafToast (holaf-lib)

## Résumé

La brique
[`HolafToast`](../orchestrator/app/static/vendor/holaf/holaf-toast.js) provient
du dépôt [`/projects/holaf-lib`](/projects/holaf-lib) (brique `toast`, version
pinnée dans `holaf-manifest.json`). Elle remplace l'ancien toast maison
(`#toast` + `.toast` CSS) : les call sites continuent d'appeler
`showToast(message, type)` — seule l'**implémentation** est remplacée par un
**adaptateur fin** vers la brique.

**Version installée : v0.2.1** (4 types `info`/`success`/`warning`/`error`,
empilement par position, auto-dismiss avec barre de progression **animée en
temps réel**, pause au survol, bouton ✕, actions cliquables, `aria-live`,
**registre de thèmes** `--ht-*` + `setTheme`/`configure`, **6 positions** dont
`top/bottom-center`).

## Changelog

- **v0.2.1** (upgrade) — correction de la barre de progression : elle reste
désormais **animée en temps réel** (la durée est calée sur le timer
d'auto-dismiss), au lieu d'un `scaleX` figé qui n'avançait jamais. Pause au
survol via `animation-play-state` (classe `paused`) au lieu d'un `scaleX`
inline. **Rétrocompatible** : l'API (`show`/`themes`/`setTheme`/`configure`)
et le comportement historique sont inchangés — l'adaptateur Docky n'a eu
**aucune modification** pour cette montée de version.

## Stratégie retenue : adaptateur fin

L'ancien toast était implémenté **deux fois** (une copie dans `api.js` et une
dans `settings.js`), toutes deux lisant le même `<div id="toast">`. Plutôt que
de modifier les ~114 call sites, on remplace **uniquement l'implémentation** de
`showToast(message, type = "info")` par un wrapper vers la brique, via un petit
module autonome [`holaf-docky-toast.js`](../orchestrator/app/static/js/holaf-docky-toast.js)
(script classique, fail-safe silencieux).

- **Signature inchangée** : `showToast(message, type)` — zéro modification des
  call sites.
- **Mapping des types Docky → types brique** (1:1, la brique supporte les 4) :

  | Docky | Brique |
  |-------|--------|
  | `info`    | `info`    |
  | `success` | `success` |
  | `error`   | `error`   |
  | `warning` | `warning` |

- **Comportement conservé** : durée d'affichage **3000 ms** (comme l'ancien
  toast) et position **bas** — la brique n'a pas de `bottom-center`, on utilise
  `bottom-right` (coin le plus proche de l'ancien centrage bas).
- **Signature étendue** : `DockyToast.show(message, type, opts?)` avec
  `opts { position, duration }` optionnels (défauts `bottom-right` / `3000 ms`)
  — les ~114 call sites existants (`showToast(message, type)`) restent
  inchangés.
- **Repli fail-safe** : si `window.HolafToast` n'est pas chargé au moment de
  l'appel, l'adaptateur se replie sur un `console.warn` sans casser les call
  sites.

## Inventaire des toasts actuels (avant remplacement)

| Fichier | Implémentation | Signature | Call sites |
|---------|----------------|-----------|------------|
| `static/js/api.js` | `showToast(message, type="info")` → `#toast` textContent + className, hide 3000 ms | `(message, type)` | 2 |
| `static/js/settings.js` | `showToast(message, type="info")` → idem | `(message, type)` | 39 |
| `static/js/dashboard.js` | utilise `this.showToast` (via `DockyApp`) | — | 16 |
| `static/js/editor.js` | utilise `this.showToast` | — | 44 |
| `static/js/chat.js` | utilise `this.showToast` | — | 3 |
| `static/js/modals.js` | utilise `this.showToast` | — | 10 |
| `templates/dashboard.html` | `<div id="toast" class="toast hidden">` | — | — |
| `templates/settings.html` | `<div id="toast" class="toast hidden">` | — | — |
| `static/css/style.css` | `.toast`, `.toast.info/.success/.error` | — | — |

**Total call sites : 114** (tous conservés, aucun modifié).

## Diff résumé

- **Ajout** : `static/vendor/holaf/holaf-toast.js` (copie pinnée de la lib,
  identique au `diff`), `static/js/holaf-docky-toast.js` (adaptateur fin).
- **Modifié** :
  - `static/js/api.js` — `showToast` → `window.DockyToast.show(message, type)`.
  - `static/js/settings.js` — `showToast` → `window.DockyToast.show(message, type)`.
  - `templates/dashboard.html` — ajout `<script type="module">` de la brique
    toast (après `holaf-modal.js`), ajout `<script>` de l'adaptateur (avant
    `api.js`), suppression du `<div id="toast">`.
  - `templates/settings.html` — idem (adaptateur avant `settings.js`),
    suppression du `<div id="toast">`.
  - `static/css/style.css` — suppression du bloc `.toast` orphelin, puis
    suppression de l'override `.holaf-toast-container { … !important }`
    (obsolète, remplacé par le registre de thèmes de la brique).
- **Supprimé** : le conteneur HTML `#toast` (2 templates) et le CSS `.toast`
  orphelin.

## Thème Docky (via le registre de la brique)

Depuis **v0.2.0**, la brique expose un **registre de thèmes** (miroir de
HolafModal) : `HolafToast.themes.register(name, vars)` + `setTheme(name)`.
L'adaptateur `holaf-docky-toast.js` enregistre le thème **« docky »**
(variables `--ht-*` exactes du code) puis le pose comme **thème GLOBAL**
(`setTheme("docky")`) pour que TOUS les toasts Docky héritent de la palette
sans répéter l'option `theme` à chaque `show()` :

```js
HolafToast.themes.register("docky", {
    "--ht-bg": "#1d1d36",
    "--ht-fg": "#f0f0f8",
    "--ht-border": "#262643",
    "--ht-accent-info": "#60a5fa",
    "--ht-accent-success": "#34d399",
    "--ht-accent-warning": "#fbbf24",
    "--ht-accent-error": "#f87171",
    "--ht-radius": "12px",
    "--ht-shadow": "0 6px 24px rgba(13, 13, 27, 0.7)",
});
HolafToast.setTheme("docky");
```

> **Override CSS retiré** : l'ancien bloc `.holaf-toast-container { … !important }`
> de `style.css` a été **supprimé** (obsolète). Le thème passe désormais par le
> registre de la brique — plus aucun `!important`. Vérifié en headless Chromium :
> les variables `--ht-*` calculées correspondent à la palette docky (`--ht-bg
> #1d1d36`, `--ht-fg #f0f0f8`, `--ht-radius 12px`, accents `#60a5fa`/`#34d399`/
> `#fbbf24`/`#f87171`) **sans** `!important` dans le CSS injecté.

## Une seule copie, servie

Comme pour HolafModal, une seule copie de la brique est maintenue, servie par
FastAPI via le montage `/static` :

```
orchestrator/app/static/vendor/holaf/holaf-toast.js      ← la brique
orchestrator/app/static/vendor/holaf/holaf-manifest.json ← versions installées
orchestrator/app/static/js/holaf-docky-toast.js          ← adaptateur fin (module autonome)
```

**Règle d'arrêt** : la brique est une copie pinnée — ne JAMAIS modifier
`holaf-toast.js` directement dans Docky. Toute amélioration se fait dans la lib
puis se propage par `upgrade`.

## Installer / mettre à jour

```bash
cd /projects/holaf-lib && ./scripts/holaf install toast /projects/Docky/orchestrator/app/static
cd /projects/holaf-lib && ./scripts/holaf check /projects/Docky/orchestrator/app/static
cd /projects/holaf-lib && ./scripts/holaf upgrade toast /projects/Docky/orchestrator/app/static
```

## Vérification

- La copie servie est strictement identique à la lib
  (`diff js/holaf-toast.js` → aucun écart), version **v0.2.1**
  (`HolafToast.version === "0.2.1"`).
- Smoke HTTP : `/static/vendor/holaf/holaf-toast.js` 200,
  `/static/js/holaf-docky-toast.js` 200, `/dashboard` 200, `/settings` 200
  (authentifié).
- Headless Chromium : déclenchement de 2 toasts via l'adaptateur (success +
  error) → rendu correct (classes `holaf-toast--success/error`), empilement
  (2 toasts sur la même position), rôles aria (`status`/`alert`), auto-dismiss
  après 3000 ms, thème Docky appliqué via le **registre** (`--ht-bg #1d1d36`,
  `--ht-fg #f0f0f8` calculés sur le toast, **sans** `!important`), barre de
  progression animée en temps réel (v0.2.1), positions alternatives
  (`top-center` + `bottom-left`).
