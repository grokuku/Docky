# Correctif CI : build PyYAML depuis le sdist (échec des tests GitHub Actions)

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
