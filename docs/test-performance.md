# Performance de la suite de tests

## Symptôme

Suite complète (542 tests) : **~47 s**. Cause dominante : le **cost bcrypt 12**
(~70–100 ms par appel `gensalt`/`checkpw`) dans les suites qui font des logins
et rotations répétés (`test_rate_limit`, `test_csrf`, `test_password_rotation`,
setup `auth_client`). Le top 15 des lenteurs représentait ~18,5 s (~39 % du
total), presque uniquement du hashing/vérification bcrypt.

## Fix (test-only)

Le code de production n'utilise pas passlib : il appelle directement
`bcrypt.gensalt()` (défaut 12) dans :

- `orchestrator/app/config.py` (hash du mot de passe bootstrap),
- `orchestrator/app/auth/router.py` (rotation),
- `orchestrator/app/routes/settings.py` (changement de mot de passe),
- `orchestrator/app/auth/password_policy.py` (`DEFAULT_PASSWORD_HASH`).

Le **conftest racine** (`/projects/Docky/conftest.py`) — chargé uniquement par
pytest, jamais par l'application en production — remplace `bcrypt.gensalt` par
un wrapper qui force le cost à **4** (minimum bcrypt) :

```python
_ORIG_GENSALT = bcrypt.gensalt
_TEST_BCRYPT_ROUNDS = 4

def _test_gensalt(rounds: int = 12, prefix: bytes = b"2b") -> bytes:
    return _ORIG_GENSALT(_TEST_BCRYPT_ROUNDS, prefix)

bcrypt.gensalt = _test_gensalt
```

**Pourquoi aucune assertion n'est affaiblie :** le cost est encodé dans le hash
lui-même (`$2b$04$...`). Tous les hash générés pendant les tests (`BCRYPT_DOCKY123`,
`make_users`, hash au fil des tests via les endpoints de rotation) utilisent
cost 4, et `bcrypt.checkpw` les vérifie exactement comme avant — juste plus
vite. Toutes les vérifications `bcrypt.checkpw` des tests restent intactes.

**Sécurité production garantie :** le fichier conftest n'est jamais importé en
dehors de pytest ; les appels prod `bcrypt.gensalt()` gardent leur défaut
**cost 12** (vérifié dans le code : `config.py`, `auth/router.py`,
`routes/settings.py`, `auth/password_policy.py` — aucun de ces fichiers n'a été
modifié).

## Résultats

| Métrique                     | Avant  | Après  |
|------------------------------|-------:|-------:|
| Suite complète (542 tests)   | 46,9 s | **18,5 s** (pytest) / 21,2 s (réel) |
| `orchestrator/tests`         | 45,7 s | **18,1 s** |
| `agent/tests`                | ~1 s   | 0,8 s  |

Nouveau top lenteurs (`--durations=10`) : plus **aucun** test bcrypt ; le plus
lent est `test_api_requires_auth` (~1,5 s de *setup* : premier import de
`app.main` + lifespan FastAPI/MCP du process).

## Piste non retenue (bonus)

Mutualiser le `TestClient`/lifespan de `test_api_requires_auth` en fixture
module/session économiserait ~1,5 s, mais la fixture `orchestrator_client`
monkeypatche `agent_manager.start_background_refresh` **par test** et partage
état (cookies, fichiers de config réécrits par certains tests). Le partage
casserait l'isolation pour un gain marginal (~3 %) : non fait, volontairement.
