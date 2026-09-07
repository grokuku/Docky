# Adoption de la brique HolafModal (holaf-lib)

## Résumé

La brique [`HolafModal`](../orchestrator/app/static/vendor/holaf/holaf-modal.js)
provient du dépôt [`/projects/holaf-lib`](/projects/holaf-lib) (brique `modal`,
version pinnée dans `holaf-manifest.json`). Elle remplace les modales HTML
statiques : le code appelant utilise `HolafModal.open({...})` (voir
`orchestrator/app/static/js/settings.js`).

**Version installée : v0.2.0** (bibliothèque de thèmes : préréglages
`dark` / `light` / `midnight` / `slate`, registre `HolafModal.themes`, thème
global volatil `HolafModal.setTheme() / clearTheme()`).

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
chargée, il se retire sans erreur. La modale « Supprimer l'agent »
(`settings.js::deleteAgent → HolafModal.open`, sans option `theme` inline)
l'hérite donc automatiquement.

Charge dans `settings.html` :

```html
<script type="module" src="/static/vendor/holaf/holaf-modal.js"></script>
<script type="module" src="/static/js/holaf-docky-theme.js"></script>
```

> Les deux fichiers sont des modules ES (la brique étant elle-même un module),
> ce qui garantit l'ordre d'exécution : `holaf-docky-theme.js` s'exécute bien
> après que `window.HolafModal` est prêt.

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
  copie servie (v0.2.0).
- Thème : `HolafModal.version === "0.2.0"`, `HolafModal.themes.list()` contient
  `docky`, et une modale ouverte sans option `theme` porte les variables
  `--hm-*` de la palette Docky (vérifié en headless Chromium).