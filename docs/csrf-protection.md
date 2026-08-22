# Protection CSRF (défense en profondeur) — Docky

> Statut : **terminé** — suite complète au vert (392 passed), 20 tests CSRF dédiés.
> Chantier : protection CSRF par *double-submit cookie* sur l'orchestrateur,
> sans casser les 372 tests existants (mécanisme de bypass dédié aux tests).

## 1. Menace et positionnement

Le cookie de session `docky_token` est posé avec `httponly`, `samesite=lax`.
`SameSite=Lax` bloque déjà l'envoi du cookie sur les requêtes cross-site
« non-top-level » (POST cross-site classiques, WebSocket cross-site) : c'est la
première ligne de défense. La protection CSRF ajoutée ici est une **défense en
profondeur** (navigateurs/clients ne respectant pas SameSite, cookies tournés
en `SameSite=None` derrière un reverse-proxy, etc.).

## 2. Design retenu : double-submit cookie

Pattern **double-submit cookie** :

1. À chaque rendu de page HTML (`GET /login`, `GET /dashboard`,
   `GET /settings`, `GET /popup/logs`, `GET /popup/console`,
   `GET /change-password`), le serveur génère un token aléatoire
   (`secrets.token_urlsafe(32)`) et :
   - le pose en cookie **`csrf_token`** — volontairement **NON-httpOnly**
     (le JS doit le lire), `samesite=lax`, `path=/`, durée 24 h,
     attribut `Secure` dès que HTTPS est détecté (schéma de la requête ou
     `X-Forwarded-Proto: https`) ;
   - l'injecte dans le template (champ caché `_csrf_token` pour les formulaires).
2. Toute requête **mutante** doit renvoyer cette valeur et le serveur vérifie
   que **cookie == valeur soumise** (comparaison en temps constant via
   `hmac.compare_digest`) :

| Cible                              | Méthodes            | Transport du token                | Échec                          |
|------------------------------------|---------------------|-----------------------------------|--------------------------------|
| `/api/*`                           | POST/PUT/PATCH/DELETE | en-tête `X-CSRF-Token` uniquement | `403 JSON {"detail": "CSRF"}` |
| `POST /login`, `POST /change-password` | POST           | champ de formulaire `_csrf_token` **ou** en-tête | redirect `/login?error=csrf` / formulaire ré-affiché avec message |

3. Les méthodes sûres (`GET/HEAD/OPTIONS/TRACE`) ne sont jamais bloquées.

### Rotation post-authentification

Le token est **régénéré à chaque login réussi** (et après un changement de
mot de passe forcé), comme le veut la bonne pratique : un token éventuellement
connu avant l'authentification devient inutilisable ensuite. L'ancien token,
comparé au nouveau cookie, produit bien un `403`.

## 3. Implémentation backend

- **Module** : `orchestrator/app/auth/csrf.py`
  - `generate_csrf_token()` — `secrets.token_urlsafe(32)`
  - `set_csrf_cookie(request, response, token)` — pose le cookie (Secure auto)
  - `verify_csrf(request, submitted=None)` — activé ? + comparaison temps constant
  - `check_request_csrf(request)` — politique pure (testable) → `403` ou `None`
  - `CSRFMiddleware` — middleware ASGI pur (aucun buffering des flux SSE),
    branché dans `app.main` via `app.add_middleware(...)`. Les scopes
    non-HTTP (WebSocket) passent sans modification.
- **Config** (relue tardivement à chaque requête, même pattern que le rate
  limiting) : section `security.csrf.enabled` (défaut `true`, défauts sûrs).

```yaml
security:
  csrf:
    enabled: true          # false désactive toute la vérification
```

### Exemptions

Liste **minimale et documentée** :

- méthodes sûnes (GET/HEAD/OPTIONS/TRACE) — par définition non-CSRF ;
- `POST /login` et `POST /change-password` : protégés aussi, mais validés
  *dans le handler* (le corps form-urlencoded est déjà parsé par FastAPI,
  ce qui évite de consommer le body dans le middleware) ;
- pas d'endpoint `/api/*` exempté : tous les appels mutants proviennent du
  navigateur (aucun script serveur-à-serveur n'appelle l'orchestrateur ; les
  agents, eux, sont appelés par l'orchestrateur et n'exposent pas `/api/*`).
- pas de route `/health` côté orchestrateur à exempter.

## 4. WebSocket (constat, aucune complexification)

Endpoints WS : `/api/events`, `/api/chat/stream`,
`/api/containers/{id}/logs/stream`, `/api/containers/{id}/exec`.

- Le handshake est un GET : le pattern CSRF (token pré-partagé) ne s'applique
  pas naturellement, et un handshake ne mute aucun état.
- Ces WebSockets exigent déjà le cookie de session JWT
  (`_check_auth_ws`, cf. `app/routes/api_helpers.py`) ; combiné à
  `SameSite=lax`, un site tiers ne peut pas faire accepter un handshake
  authentifié.
- **Écart documenté** : pas de vérification `Origin` dédiée aujourd'hui ;
  considéré acceptable dans le modèle de menaces actuel (auth cookie +
  SameSite=lax). À revoir si le déploiement passe en `SameSite=None`.

## 5. Frontend

- `orchestrator/app/static/js/api.js` expose `DockyApp.getCookie(name)` /
  `DockyApp.csrfToken()` et installe **un wrapper unique autour de
  `window.fetch`** qui ajoute automatiquement l'en-tête `X-CSRF-Token`
  (lu depuis le cookie `csrf_token`) à toute méthode mutante.
  → `apiFetch`/`apiPost` ET tous les `fetch(...)` directs existants
  (`dashboard.js`, `editor.js`, `chat.js`, `modals.js`, `settings.js`,
  `events.js`) sont couverts par UNE centralisation, y compris le code futur.
- Templates `login.html` / `change_password.html` : champ caché
  `<input type="hidden" name="_csrf_token" value="{{ csrf_token }}">`.

## 6. Compatibilité tests (impératif : zéro modification des 372 tests)

Les tests TestClient ne chargent ni le JS ni les templates pour extraire le
token : la suite existante ne peut pas présenter d'en-tête CSRF.

**Mécanisme retenu — variable d'environnement (option (a) de la demande)** :

- `orchestrator/tests/conftest.py` ajoute une fixture **autouse** qui pose
  `DOCKY_DISABLE_CSRF_FOR_TESTS=1` pour chaque test du dossier
  `orchestrator/tests`.
- `app.auth.csrf.csrf_enabled()` (relue tardivement à chaque requête) retourne
  `False` si cette variable est présente → toute la vérification est court-
  circuitée, comportement identique à `security.csrf.enabled: false`.
  Ce n'est PAS un flag de production : la variable ne peut être posée que par
  l'environnement du processus (même philosophie que `DOCKY_DATA_DIR` utilisé
  par la suite depuis l'origine).
- **Aucun des 372 tests existants n'est modifié** ; ils continuent d'appeler
  POST/PUT/DELETE sur `/api/*` et `/login` sans en-tête.
- Les nouveaux tests dédiés (`orchestrator/tests/test_csrf.py`) utilisent une
  fixture `csrf_on` qui **supprime** la variable (monkeypatch.delenv) pour
  activer explicitement la protection, indépendamment de la config.

## 7. Tests nouveaux (test_csrf.py)

Cas couverts (voir le fichier pour le détail) :

1. mutante sans en-tête → `403 {"detail": "CSRF"}`
2. mauvais token → `403`
3. bon couple cookie/en-tête → `200`
4. GET jamais bloqué (jamais `403`)
5. `POST /login` protégé (sans token → redirect `?error=csrf`, mauvais token idem)
6. `POST /change-password` protégé (formulaire ré-affiché avec message)
7. rotation : nouveau token émis après login, l'ancien est rejeté
8. comparaison temps constant (test fonctionnel + espion sur `hmac.compare_digest`)
9. bascule `security.csrf.enabled: true→false` relue tardivement
10. bypass env var : présent → mutations autorisées (état par défaut des tests)
11. rendu de page pose bien le cookie (non-httpOnly, samesite=lax) + champ caché

## 8. Résultats

Validation finale (`.venv/bin/python -m pytest -q` depuis `/projects/Docky`,
`asyncio_mode=auto`, `pythonpath=["orchestrator","."]`) :

- **392 passed, 3 warnings** en ~38 s (dont 20 tests dédiés CSRF). Aucun échec.
- Référence pré-CSRF : 372 passed → **+20 nouveaux tests**, aucune suppression.
- **Zéro test existant modifié** (le bypass est fourni par la fixture autouse de
  `orchestrator/tests/conftest.py`, voir §6).

### Smoke checks manuels/TestClient (comportement réel, protection activée)

| Scénario | Résultat attendu | Constaté |
|----------|------------------|----------|
| `POST /api/*` sans `X-CSRF-Token` | `403 {"detail":"CSRF"}` | ✔ |
| `POST /api/*` avec bon couple cookie/en-tête | `200` | ✔ |
| `GET` (toutes routes) | jamais `403` | ✔ |
| `POST /login` sans `_csrf_token` | redirect `/login?error=csrf` | ✔ |
| `POST /login` token valide | succès + **rotation** du token | ✔ |
| ancien token après login | `403` (rotation) | ✔ |
| `POST /change-password` protégé | formulaire ré-affiché / succès | ✔ |
| bascule `security.csrf.enabled` à chaud | relue à chaque requête | ✔ |

### Smoke frontend (harnais DOM Node)

**Écart documenté** : le dépôt ne contient **aucun harnais de test DOM Node**
(no `package.json`, no fichier `*.test.js`/`*.spec.js`/`*.mjs` de smoke). Le
refactoring frontend (``static/js/api.js`` etc.) n'a jamais mis en place de
harnais JS automatisé ; les modules JS sont des scripts classiques qui se
rattachent à `window.DockyApp` et sont couverts indirectement par les tests
pytest (rendu des templates) et par les smoke TestClient ci-dessus.

Le wrapper global `window.fetch` d'`api.js` (installation idempotente
`__dockyCsrfWrapped`, ajout de `X-CSRF-Token` sur les méthodes mutantes,
jamais sur GET/HEAD/OPTIONS/TRACE, jamais d'échec de la requête appelante en
cas d'imprévu JS) a été relu et est cohérent avec le serveur. Un harnais DOM
Node reste à créer si l'on veut une couverture JS unitaire — hors périmètre de
ce chantier, et documenté comme écart assumé.
