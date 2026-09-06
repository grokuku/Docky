# Correctif CI : build PyYAML depuis le sdist (échec des tests GitHub Actions)

> **Note (2026-09-06)** : le workflow de tests `.github/workflows/tests.yml` a été
> **supprimé** (décision utilisateur : inutile dans le flux push → build → deploy
> immédiat). Les tests tournent désormais **en local** via
> `python -m pytest -q` (voir « Validation locale »). Les sections ci-dessous
> sont conservées pour **historique** ; les pins des requirements restent en
> place (ils s'appliquent aussi au build Docker).

## Symptôme

L'étape « Install dependencies » du workflow `.github/workflows/tests.yml`
échouait avec :

```
AttributeError: 'build_ext' object has no attribute 'cython_sources'
```

pip tentait de **compiler PyYAML depuis la source** (sdist) au lieu d'utiliser
un wheel précompilé. C'est une incompatibilité connue entre PyYAML 6.0.x et
les versions récentes de setuptools/Cython.

## Cause exacte

Les requirements déclaraient `pyyaml>=6.0,<7.0` (contrainte large). Le
résolveur pip évalue **toutes** les versions candidates (6.0, 6.0.1, 6.0.2)
pour construire le graphe de résolution et lire leurs métadonnées.

Or, parmi ces versions, seule **6.0.2** (et 6.0.1) fournit un wheel `cp312`.
Pour les versions sans wheel compatible (6.0), pip doit **construire le sdist**
pour en extraire les métadonnées → déclenche le build Cython → l'erreur
`cython_sources`.

Le build Docker passait car `python:3.12-slim` + `pip install` choisissait
directement le wheel `cp312` de 6.0.2 (pas de backtracking sur les autres
versions).

## Correctif appliqué

Deux protections complémentaires :

1. **Pin de PyYAML** à `pyyaml==6.0.2` (seule version avec wheel `cp312`
   précompilé) dans :
   - `orchestrator/requirements.txt`
   - `agent/requirements.txt`

   → élimine tout build source de PyYAML, quel que soit le résolveur.

2. **`--only-binary=:all:`** ajouté à la commande `pip install` du workflow
   `tests.yml` :

   ```yaml
   run: pip install --only-binary=:all: -r orchestrator/requirements.txt -r agent/requirements.txt -r requirements-dev.txt
   ```

   → interdit tout build depuis source (sdist) pour **toutes** les
   dépendances, pas seulement PyYAML. Toutes les deps (fastmcp, mcp, fastapi,
   uvicorn, etc.) disposent de wheels, donc aucun risque de régression.

## Fichiers modifiés

- `.github/workflows/tests.yml`
- `orchestrator/requirements.txt`
- `agent/requirements.txt`
- `docs/ci-fix.md` (ce document)

## Validation locale

- `pip install --dry-run --only-binary=:all: -r orchestrator/requirements.txt
  -r agent/requirements.txt -r requirements-dev.txt` → résout en téléchargeant
  le **wheel** `PyYAML-6.0.2-*.whl`, aucun sdist construit.
- `python -m pytest -q` → **492 passed, 0 échec** (aucune régression).

## Build Docker

Non cassé : les Dockerfiles (`orchestrator/Dockerfile`, `agent/Dockerfile`)
utilisent `python:3.12-slim` et `pip install -r requirements.txt`. Avec le pin
`pyyaml==6.0.2`, ils continuent d'utiliser le wheel `cp312` précompilé (aucun
build source). Aucune modification des Dockerfiles nécessaire.

## Correctif workflow YAML (syntaxe)

### Symptôme

Le workflow `tests.yml` ne se chargeait **pas du tout** dans GitHub Actions
(graph vide) : le fichier YAML était invalide, donc le workflow était ignoré
avant même d'exécuter une étape.

### Erreur YAML exacte

```
yaml.scanner.ScannerError: mapping values are not allowed here
  in ".github/workflows/tests.yml", line 34, column 44
```

La colonne 44 de la ligne 34 correspond au **second `:`** de la valeur
`--only-binary=:all:`. Ce `:` est immédiatement suivi d'un espace
(`:all: -r`), ce que YAML interprète comme un **séparateur clé/valeur** de
mapping → erreur de syntaxe.

### Correctif appliqué

La valeur de `run` a été **mise entre guillemets doubles** pour que le `: `
soit traité comme du texte littéral et non comme un séparateur :

```yaml
run: "pip install --only-binary=:all: -r orchestrator/requirements.txt -r agent/requirements.txt -r requirements-dev.txt"
```

Aucune logique du workflow n'a été modifiée (triggers, steps, commande pip
`--only-binary` + `pytest` inchangés).

### Validation

```
python -c "import yaml; yaml.safe_load(open('.github/workflows/tests.yml'))"
```

→ passe sans erreur. Les autres workflows (`release.yml`, `test-build.yml`)
restent valides.

---

# Correctif CI : `resolution-too-deep` (backtracking pip sur les plages ouvertes)

## Symptôme

L'étape « Install dependencies » de `tests.yml` échouait avec :

```
ERROR: ResolutionTooDeep: 2000000 lines of C extension calls...
```

pip n'arrivait pas à résoudre le graphe de dépendances dans le budget de
profondeur autorisé.

## Cause exacte

Les requirements utilisaient des **plages ouvertes** (`>=x,<y`) sur les
dépendances de premier niveau :

- `uvicorn[standard]>=0.34,<1.0`
- `passlib[bcrypt]>=1.7,<2.0`
- `python-jose[cryptography]>=3.3,<4.0`
- `fastapi>=0.115,<1.0`, `httpx>=0.27,<1.0`, `fastmcp>=4.0,<5.0`, …

Pour chaque plage, le résolveur pip doit **backtracker** : il évalue les
versions candidates une à une (ex. uvicorn 0.34 → 0.52 → …), reconstruit le
sous-graphe à chaque essai, et remonte en arrière dès qu'une combinaison
échoue. Avec plusieurs plages ouvertes imbriquées (uvicorn, passlib,
python-jose, fastmcp/mcp, …), le nombre de combinaisons explose →
`resolution-too-deep`.

## Correctif appliqué

**Pins EXACTS** (`package==x.y.z`) pour toutes les dépendances de premier
niveau **et leurs sous-dépendances critiques**, extraits de l'environnement
testé (`.venv`, où les 542 tests passent) via `pip freeze`. Un graphe 100 %
pinné ne laisse aucune liberté au résolveur → résolution immédiate et
déterministe, sans backtracking.

Fichiers réécrits :

- `orchestrator/requirements.txt`
- `agent/requirements.txt`
- `requirements-dev.txt`

Principaux pins :

| Paquet | Pin |
|---|---|
| fastapi | 0.141.1 |
| uvicorn[standard] | 0.52.4 |
| pyyaml | 6.0.3 (wheel cp312) |
| python-jose[cryptography] | 3.5.0 |
| passlib[bcrypt] | 1.7.4 |
| jinja2 | 3.1.6 |
| python-multipart | 0.0.32 |
| httpx | 0.28.1 |
| websockets | 17.1 |
| fastmcp | 4.0.2 |
| docker | 7.2.0 |
| pytest | 8.4.2 |
| pytest-asyncio | 0.26.0 |
| respx | 0.23.1 |

Sous-dépendances critiques pinnées : starlette, pydantic, pydantic_core,
typing_extensions, anyio, annotated-types, h11, httpcore, certifi, idna,
click, uvloop, httptools, watchfiles, cryptography, ecdsa, rsa, pyasn1,
bcrypt, MarkupSafe, mcp, mcp-types, sse-starlette, urllib3, requests, pluggy,
iniconfig, packaging, exceptiongroup.

## Validation locale

- `pip install --dry-run --only-binary=:all: -r orchestrator/requirements.txt
  -r agent/requirements.txt -r requirements-dev.txt` → résout en **~1,7 s**,
  aucun backtracking, aucun `resolution-too-deep`.
- `timeout 300 python -m pytest -q` → **542 passed, 0 échec** (aucune
  régression).

## Build Docker

Cohérent : les Dockerfiles (`orchestrator/Dockerfile`, `agent/Dockerfile`)
installent les mêmes `requirements.txt` → les pins s'appliquent aussi au build
Docker. C'est voulu : images **reproductibles** (mêmes versions exactes en CI
et en production). Aucune modification des Dockerfiles nécessaire.
