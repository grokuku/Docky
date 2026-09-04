# Validation des agents

Ce document décrit la validation appliquée aux agents lors de leur ajout ou
modification dans Docky, afin d'empêcher les erreurs d'encodage silencieuses.

## Contexte

Une clé API d'agent contenant un caractère non-ASCII (ex. `ù`) faisait échouer
**toutes** les requêtes `httpx` vers cet agent avec une erreur
`UnicodeEncodeError: 'ascii' codec can't encode` sur l'en-tête `Authorization`.
Le problème n'était détecté qu'au moment de l'appel réseau, sans message clair
pour l'utilisateur.

## Règles de validation

La validation est appliquée à l'**ajout** (`POST /api/settings/agents`) et à
l'**édition** (`PUT /api/settings/agents/{name}`) d'un agent, via le helper
`_validate_agent(url, api_key)` dans
`orchestrator/app/routes/settings.py`.

| Champ | Règle |
|-------|-------|
| **clé API** | Ne doit contenir que des caractères ASCII. |
| **URL** | Ne doit contenir que des caractères ASCII **et** commencer par `http://` ou `https://` **et** avoir un hostname valide (lettres, chiffres, tirets, points — sans accents ni underscores). |

## Messages d'erreur (français)

- Clé API non-ASCII :
  `La clé API ne doit contenir que des caractères ASCII`
- URL non-ASCII ou invalide (schéma manquant / hostname invalide) :
  `L'URL de l'agent doit être valide (http:// ou https://) et sans caractères accentués`

Ces messages sont renvoyés avec un statut HTTP `400` dans le champ `detail` du
corps JSON.

## Frontend

`orchestrator/app/static/js/settings.js` (`SettingsApp.submitAgentForm`) affiche
déjà le `detail` renvoyé par le backend dans un toast d'erreur quand l'ajout ou
l'édition échoue (`this.showToast(data.detail || "Erreur ...", "error")`). Le
message de validation est donc remonté de façon visible à l'utilisateur.

## Tests

Les tests se trouvent dans `orchestrator/tests/test_api_routes.py` :

- ajout avec clé contenant `ù` → rejeté (400) avec le bon message ;
- ajout avec URL non-ASCII → rejeté (400) ;
- ajout avec URL sans `http(s)://` → rejeté (400) ;
- ajout avec clé/URL valides → accepté (200) ;
- édition avec clé non-ASCII → rejeté (400), valeur précédente conservée ;
- édition avec URL non-ASCII → rejeté (400), valeur précédente conservée.

## Vérification

```bash
timeout 300 .venv/bin/python -m pytest -q   # 446 passed
node --check orchestrator/app/static/js/settings.js
```
