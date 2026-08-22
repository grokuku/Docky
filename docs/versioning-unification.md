# Unification du versioning Docky

> **Objectif** : une seule source de vérité — `version.txt` à la racine du dépôt.
> Plus aucune version codée en dur divergente ; orchestrateur et agent exposent
> tous deux exactement la version du dépôt. Zéro changement fonctionnel hors
> affichage de version.

---

## 1. État des lieux (analyse initiale)

Baseline de tests : **315 passed** (`python -m pytest -q`, suite racine,
`pythonpath=["orchestrator","."]`).

### 1.1 Où sont les versions aujourd'hui

| Emplacement | Valeur | Rôle / Problème |
|---|---|---|
| `version.txt` (racine) | `0.0.4` | ✅ Source de vérité (lue par les workflows CI) |
| `.github/workflows/release.yml` | lit `version.txt` → build-arg `VERSION` (+ `GIT_COMMIT`) puis **incrémente et commite** `version.txt` après build | Pattern existant : la version voyage vers Docker via build-arg |
| `.github/workflows/test-build.yml` | idem (sans bump) | Tag `test` |
| `orchestrator/Dockerfile` + `agent/Dockerfile` | `RUN echo "${VERSION}-${GIT_COMMIT}" > /app/version.txt` | ⚠️ Le fichier embarqué contient `<version>-<sha>` (ex. `0.0.4-a1b2c3d`) ≠ contenu exact de `version.txt`. Le sha reste disponible via le label d'image `git_commit` |
| `orchestrator/app/main.py:20` | `FastAPI(title="Docky", version="0.1.0")` | ❌ Hardcodé, divergent |
| `orchestrator/app/__init__.py:3` | `__version__ = "0.1.0"` | ❌ Hardcodé, divergent |
| `agent/main.py:7` | `FastAPI(title="Docky Agent", version="1.0.0")` | ❌ Hardcodé, divergent |
| `orchestrator/app/routes/api_helpers.py` | `_find_version_path()` / `_VERSION_PATH` | Résout `version.txt` à l'import : `<base>/version.txt` ou `<repo>/version.txt` (`get_base_dir()` = dossier parent du package `app`) |
| `orchestrator/app/routes/settings.py` — `GET /api/version` | lit `_VERSION_PATH`, fallback **hardcodé `"0.0.1"`** si lecture impossible | ❌ Fallback divergent ; ignore toute env |
| `orchestrator/app/routes/settings.py` — `GET /api/version-check` | compare version locale (`_VERSION_PATH`) vs `/agent/health` de chaque agent ; liste les mismatches | Logique métier correcte, conservée telle quelle |
| `agent/routes.py` — `GET /agent/health` | lit `Path(__file__).parent.parent/'version.txt'`, fallback `"unknown"` | Fonctionne mais logique dupliquée, fallback différent |
| `roadmap.md:4` (en-tête) | « Version courante : 0.0.3 » | ⚠️ Obsolète (= 0.0.4) |
| `roadmap.md:485` (arborescence) | `version.txt # 0.0.3` | ⚠️ Obsolète |
| `roadmap.md:493-496` (section Versioning) | décrit l'état courant + ⚠️ « FastAPI(version="0.1.0") … À réconcilier » | Sera actualisé après correction (documentation d'état, pas d'historique) |
| `docker-compose.yml` | `DOCKY_VERSION` = **tag d'image uniquement** (`ghcr.io/grokuku/docky:${DOCKY_VERSION:-latest}`) | N'est PAS une variable runtime de version affichée |

### 1.2 Qui lit quoi (chemin de lecture actuel)

- **Orchestrateur en checkout source** : `_find_version_path()` → `orchestrator/version.txt`
  n'existe pas → retombe sur `<racine dépôt>/version.txt` = `0.0.4`.
- **Orchestrateur en conteneur** : `WORKDIR /app`, code copié dans `/app/app/`,
  `get_base_dir() = /app` → `/app/version.txt` existe (écrit au build) =
  `"${VERSION}-${GIT_COMMIT}"` (ex. `0.0.4-abc1234`).
- **Agent en conteneur** : code copié dans `/app/agent/`,
  `Path(agent/routes.py).parent.parent = /app` → `/app/version.txt` (même contenu).
- **Agent en checkout source** : `agent/../version.txt` = racine du dépôt.

### 1.3 Contrainte packaging identifiée

Les workflows construisent avec `context: ./orchestrator` et `context: ./agent` :
le `version.txt` racine **n'est pas dans le contexte de build**, un simple
`COPY version.txt …` casserait les builds CI sans toucher aux workflows.
Le pattern existant (build-arg `VERSION` lu depuis `version.txt` par la CI)
est donc retenu comme mécanisme d'injection dans les images.

---

## 2. Décisions

### 2.1 Ordre de résolution retenu (identique côté orchestrateur et agent)

```
1. Variable d'environnement DOCKY_VERSION   (si définie et non vide après strip)
2. Fichier version.txt                      (candidats par layout, premier existant)
3. Défaut sûr : "0.0.0"                     (jamais de crash au démarrage)
```

- La priorité env permet de forcer/écraser la version affichée au déploiement
  (ex. `docker-compose.yml` pourrait définir `DOCKY_VERSION`), tout en restant
  inerte par défaut.
- Un fichier vide ou illisible est traité comme absent → défaut.
- `DEFAULT_VERSION = "0.0.0"` remplace les trois fallbacks divergents
  (`"0.0.1"` côté `/api/version`, `"unknown"` côté `/agent/health`,
  rien côté `FastAPI(version=…)`).

### 2.2 Packaging conteneur : build-arg (pattern existant) plutôt que COPY

**Choix** : conserver l'injection par `ARG VERSION` (déjà passée par
`release.yml`/`test-build.yml` depuis `version.txt` — aucune modification de la
logique des workflows), et écrire **`${VERSION}` seul** dans `/app/version.txt`
(suppression du suffixe `-GIT_COMMIT`).

**Justification** :
- Les contextes de build étant `./orchestrator` et `./agent`, `COPY version.txt`
  exigerait de modifier les workflows (hors périmètre) ;
- Le suffixe `-<sha>` faisait diverger la version exposée en conteneur
  (`0.0.4-abc1234`) de celle du dépôt (`0.0.4`) — objectif contraire ;
- Le hash git reste disponible via le label d'image `git_commit`.

**Chaîne complète résultante** :

```
version.txt (racine du dépôt)
   └─(release.yml / test-build.yml : cat version.txt)→ build-arg VERSION
        └─(Dockerfiles : RUN echo "${VERSION}" > /app/version.txt)→ image
             └─(runtime : app/version.py & agent/version.py)→ get_version()
                  ├─ orchestrator : FastAPI(version=…) + GET /api/version + /api/version-check
                  └─ agent        : FastAPI(version=…) + GET /agent/health
                       └─ UI : badge de version + alerte mismatch (/api/version-check)
```

### 2.3 Duplication volontaire du helper

`orchestrator/app/version.py` et `agent/version.py` implémentent le même motif
autorisé à être dupliqué : les deux services sont des images indépendantes sans
package partagé (aucune dépendance nouvelle). La logique canonique de
`_find_version_path` migre de `app.routes.api_helpers` vers
`app.version`, qui est ré-importée par `api_helpers` (ré-export conservé pour
la façade `app.routes.api` et ses tests).

---
## 3. Implémentation

### 3.1 Fichiers créés

| Fichier | Contenu |
|---|---|
| `orchestrator/app/version.py` | Helper de résolution : `get_version()` (env → fichier → défaut), `_find_version_path()`, `_VERSION_PATH`, `DEFAULT_VERSION = "0.0.0"`. Autonome (aucun import `app.*`) pour éviter tout cycle depuis `app/__init__.py`. |
| `agent/version.py` | Même contrat, dupliqué volontairement (services/images indépendants, zéro dépendance nouvelle). |
| `orchestrator/tests/test_version.py` | 13 tests (résolution + smoke orchestrateur). |
| `agent/tests/test_version.py` | 14 tests (résolution + smoke agent). |
| `docs/versioning-unification.md` | Ce document. |

### 3.2 Fichiers modifiés

| Fichier | Changement |
|---|---|
| `orchestrator/app/main.py` | `FastAPI(title="Docky", version=get_version())` au lieu de `"0.1.0"` en dur. |
| `orchestrator/app/__init__.py` | `__version__ = get_version()` au lieu de `"0.1.0"` en dur. |
| `orchestrator/app/routes/api_helpers.py` | La logique de `_find_version_path` migre vers `app.version` ; les noms `_find_version_path` / `_VERSION_PATH` restent ré-exportés ici → façade `app.routes.api`, `settings.py` et tests inchangés. Import `pathlib.Path` devenu inutile retiré. |
| `orchestrator/app/routes/settings.py` | `/api/version` et `/api/version-check` utilisent `get_version()` ; fallback hardcodé `"0.0.1"` supprimé ; import `_VERSION_PATH` devenu inutile retiré. **Logique métier du version-check inchangée** (comparaison version locale vs `/agent/health` de chaque agent). |
| `agent/main.py` | `FastAPI(title="Docky Agent", version=get_version())` au lieu de `"1.0.0"` en dur. |
| `agent/routes.py` | `/agent/health` utilise `get_version()` au lieu d'une lecture inline avec fallback `"unknown"`. Import `pathlib.Path` devenu inutile retiré. |
| `orchestrator/Dockerfile` | `RUN echo "${VERSION}" > /app/version.txt` (suppression du suffixe `-${GIT_COMMIT}`) ; `ARG VERSION=0.0.1` → `0.0.0` (défaut build local uniquement, aligné sur `DEFAULT_VERSION`). Labels et ARGs conservés. |
| `agent/Dockerfile` | Idem. |
| `roadmap.md` | Corrections factuelles minimales : en-tête « Version courante » 0.0.3 → 0.0.4, arborescence `version.txt # 0.0.4`, section « 🔖 Versioning » réécrite à minima pour décrire le nouvel état (l'historique v0.0.3 est intact). |

### 3.3 Fichiers NON modifiés (vérifiés, sans changement nécessaire)

- `.github/workflows/release.yml` / `test-build.yml` : lisent déjà `version.txt`
  (`cat version.txt | tr -d '[:space:]'`) → build-args `VERSION` + `GIT_COMMIT`.
  Aucune modification de logique. (Note : `release.yml` incrémente `version.txt`
  et commite APRÈS le build — comportement existant conservé.)
- `docker-compose.yml` : `DOCKY_VERSION` y sert exclusivement de tag d'image ;
  ce n'est pas une variable runtime. Les utilisateurs peuvent néanmoins ajouter
  `DOCKY_VERSION=<x.y.z>` dans la section `environment:` pour forcer la version
  affichée (priorité env du helper).
- `.env.example`, UI statique : aucune version fonctionnelle codée en dur
  (les mentions `v0.0.4` dans les commentaires JS sont des notes de refactor).

---

## 4. Tests

### 4.1 Nouveaux tests (27)

**`orchestrator/tests/test_version.py` (13)** :
- env prioritaire (`DOCKY_VERSION` gagne même si le fichier existe), strip des espaces ;
- env vide/blanche ignorée → lecture fichier ;
- fichier lu (`tmp_path`), fallback `"0.0.0"` si absent, vide, blanc ou illisible (OSError) ;
- `_find_version_path()` pointe un fichier existant (racine du dépôt en checkout) ;
- smoke : `get_version() == version.txt`, `app.main.app.version == version.txt`,
  `app.__version__ == version.txt`,
  `GET /api/version == version.txt` (fixture `auth_client`),
  `GET /api/version` honore l'override env,
  `GET /api/version-check` rapporte `orchestrator_version == version.txt`.

**`agent/tests/test_version.py` (14)** :
- mêmes cas unitaires (env prioritaire, strip, env vide, fichier lu, absents/vide/illisible → défaut) ;
- smoke : `get_version() == version.txt`, `agent.main.app.version == version.txt`,
  `GET /agent/health == version.txt` (fixture `agent_client`),
  `/agent/health` honore l'override env.

### 4.2 Tests existants

**Aucun test existant modifié.** Vérifications de compatibilité effectuées :
- `test_api_routes.py::test_get_version` (assert `== "0.0.4"`) : passe tel quel —
  en checkout source, la résolution retombe sur la racine du dépôt.
- `test_routes_modules_import.py::test_facade_exposes_expected_symbols` :
  `_VERSION_PATH` reste exposé par la façade via le ré-export d'`api_helpers`.
- `test_agent_routes.py::test_health_requires_no_auth` : ne vérifie que la
  présence de la clé `version` — compatible.

### 4.3 Validation conteneur (simulation hors Docker)

Layout `/app/{app,agent}` + `/app/version.txt=0.0.4` reproduisant les COPY des
Dockerfiles : orchestrateur et agent résolvent tous deux `0.0.4` ; override env
OK ; `version.txt` supprimé → `"0.0.0"` sans crash.

### 4.4 Résultats pytest

```
Baseline avant changements : 315 passed
Après changements          : 342 passed (315 + 27 nouveaux), 0 failed
Commande                   : python -m pytest -q  (racine, .venv)
```

---

## 5. Écarts restants / points documentés

- **Historique `roadmap.md`** : les mentions historiques de la v0.0.3
  (« Améliorations récentes (v0.0.3) », badge v0.0.3 dans le bilan…) sont
  conservées telles quelles — historique, non corrigé par design.
- **Comportement d'affichage modifié (voulu, limité à la version affichée)** :
  - en conteneur, `/api/version`, `/agent/health` et le badge UI affichent
    désormais `0.0.4` au lieu de `0.0.4-<sha>` (le sha reste dans le label
    d'image `git_commit`) ;
  - `FastAPI(version=…)` OpenAPI : `0.1.0`/`1.0.0` → version du dépôt ;
  - fallbacks unifiés : `"0.0.1"` (orchestrateur) et `"unknown"` (agent)
    → `"0.0.0"` (jamais atteint en pratique, fichiers toujours présents dans
    les images).
- **`release.yml`** bump `version.txt` après le build et committe : les images
  d'un tag donné embarquent bien la version qui était dans `version.txt` au
  moment du build (comportement existant inchangé).

## 6. Conclusion

La chaîne complète est vérifiée de bout en bout :

```
version.txt (racine, 0.0.4)
  → CI (release.yml/test-build.yml lisent le fichier → build-arg VERSION)
  → Dockerfiles (/app/version.txt = ${VERSION}, label git_commit = sha)
  → runtime (orchestrator/app/version.py & agent/version.py : DOCKY_VERSION > fichier > "0.0.0")
  → API (FastAPI(version=…), GET /api/version, GET /api/version-check, GET /agent/health)
  → UI (badge de version + alerte mismatch)
```

Une seule source de vérité, zéro version codée en dur divergente restante,
fallback sûr, aucune nouvelle dépendance, aucun test existant modifié.
