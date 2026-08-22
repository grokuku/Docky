# Rotation forcée du mot de passe par défaut (`docky123`)

Statut : **TERMINÉE** — implémentation, tests et nettoyage secrets validés
(372/372 pytest verts).

## Problème

`ensure_config_files()` (orchestrator/app/config.py) crée au premier
démarrage un compte `admin` / `docky123`. Rien n'obligeait à le changer :
un Docky déployé sans reconfiguration reste accessible avec des
identifiants connus publiquement.

## Objectifs

1. Un compte dont le mot de passe est encore le mot de passe par défaut ne
   peut **rien faire d'autre** que changer son mot de passe.
2. Deux mécanismes de détection complémentaires :
   - flag explicite `must_change_password: true` écrit au bootstrap ;
   - filet de sécurité bcrypt : le mot de passe soumis au login est
     comparé (via un hash pré-calculé une seule fois, cf.
     `app/auth/password_policy.py`) à la valeur connue `"docky123"`,
     ce qui couvre les déploiements créés avant l'introduction du flag.
3. Aucun JWT complet n'est émis tant que la rotation n'est pas faite : le
   login émet uniquement un JWT court (10 min) à portée restreinte
   (`purpose="password_change"`), stocké dans le cookie `docky_pwreset`
   (httpOnly, samesite=lax). `verify_token()` rejette tout token porteur
   d'un claim `purpose`, donc les endpoints API/pages protégés refusent
   ce token restreint.
4. Zéro régression sur la suite existante (342 tests), adaptations de
   tests listées plus bas.

## Design retenu

| Élément | Choix |
|---|---|
| Détection | `rotation_required(user, submitted_password)` = flag `must_change_password` OU `is_default_password(submitted)` |
| Coût bcrypt | hash du mot de passe par défaut pré-calculé **une fois** au chargement du module (`DEFAULT_PASSWORD_HASH`) ; chaque vérification = 1 × `bcrypt.checkpw` ≈ 70 ms (coût 12), exécutée uniquement après une authentification réussie |
| Token restreint | JWT HS256, claims `sub`, `iat`, `exp` (+10 min), `purpose="password_change"` |
| Cookies | `docky_pwreset` (token restreint, max-age 600 s) ; `docky_token` (session normale) jamais émis avant rotation |
| Refus global | `verify_token()` retourne `None` si le payload contient un claim `purpose` → `_check_auth`, garde du dashboard, WebSocket : tous refusent le token restreint |
| Page dédiée | `GET /change-password` (formulaire si token restreint valide, sinon redirection `/login`) ; `POST /change-password` applique les mêmes règles que `PUT /api/settings/password` (longueur ≥ 6) + confirmation + interdiction de réutiliser l'ancien mot de passe |
| Persistance | `users.yaml` : `password_hash` mis à jour + `must_change_password: false`, puis émission du JWT normal et redirection `/dashboard` |
| Rate limiting | inchangé sur `POST /login` ; un login « succès mais rotation requise » réinitialise bien le compteur (authentification réussie) |

## Fichiers

### Créés
- `orchestrator/app/auth/password_policy.py` — constantes + helpers de détection
- `orchestrator/templates/change_password.html` — page de rotation (style login)
- `orchestrator/tests/test_password_rotation.py` — nouveaux tests
- `data/users.yaml.example`, `data/settings.yaml.example`,
  `data/api_keys.yaml.example` — exemples neutres suivis dans git

### Modifiés
- `orchestrator/app/auth/jwt_utils.py` — `purpose`, token restreint,
  durcissement de `verify_token`
- `orchestrator/app/auth/router.py` — branchement du flux de rotation +
  routes `/change-password`
- `orchestrator/app/config.py` — `ensure_config_files()` écrit
  `must_change_password: true` pour l'admin par défaut
- `conftest.py` (racine) — `BCRYPT_DOCKY123` généré dynamiquement (le vrai
  hash n'est plus codé en dur dans aucun fichier suivi) ; `make_users`
  accepte un `password` non-défaut
- `orchestrator/tests/test_auth_router.py` — adaptations (voir ci-dessous)
- `orchestrator/tests/test_rate_limit.py` — adaptations (voir ci-dessous)

### Git (secrets committés)
- `git rm --cached data/users.yaml data/api_keys.yaml data/settings.yaml`
  (détracking **effectué** ; fichiers conservés sur disque, déjà ignorés par
  `.gitignore`)
- Remplacement du hash réel de `docky123` présent dans `conftest.py`
  (fichier suivi) par une génération à l'import de session de test
  (`BCRYPT_DOCKY123 = bcrypt.hashpw(b"docky123", bcrypt.gensalt(12))`).
- Création des exemples neutres **suivis** :
  - `data/users.yaml.example` — hash placeholder **non fonctionnel**
    (`$2b$12$CHANGEMOIhashPLACEHOLDER...`, 60 caractères, n'est le hash
    d'aucun mot de passe) + commentaire explicite ;
  - `data/settings.yaml.example` — `jwt_secret: "CHANGE_ME"` + consigne de
    remplacement (`python -c "import os; print(os.urandom(32).hex())"`) ;
  - `data/api_keys.yaml.example` — structure `api_keys: {}` + exemple de
    clé fictive en commentaire.

## Nettoyage secrets — vérifications effectuées

- `git grep` du vrai hash (`$2b$12$dU1o...dNtm`) sur `git ls-files` :
  **0 occurrence** après détracking.
- Scan de toutes les chaînes au format bcrypt (`$2[aby]$NN$…`) dans tout
  fichier suivi ou trackable : **aucune ne valide `docky123`** via
  `bcrypt.checkpw` (la seule trouvée est le placeholder invalide de
  `users.yaml.example`, qui lève `Invalid salt`).
- Mentions en clair de `docky123` restantes, toutes légitimes :
  `orchestrator/app/config.py` (création du compte par défaut au bootstrap —
  par design), `orchestrator/app/auth/password_policy.py` (constante de
  détection — le filet de sécurité doit connaître la valeur publique),
  `conftest.py` (génération dynamique), tests (assertions), docs/roadmap.
- `ensure_config_files()` vérifié sur répertoire vide : crée les 5 fichiers
  (`users.yaml` avec `must_change_password: true` + hash docky123 frais,
  `settings.yaml` avec `jwt_secret` aléatoire 64 hex, `api_keys.yaml`,
  `soul.md`, `compose_reference.md`) ; appel idempotent.

## Tests existants modifiés (adaptation à la rotation)

Le hash `docky123` utilisé par les fixtures déclenche désormais la rotation
au login. Les tests qui testent le flux **normal** post-login reçoivent donc
explicitement un compte à mot de passe **non-défaut**
(`make_users(data_dir, password=...)`) :

- `orchestrator/tests/test_auth_router.py`
  - `test_login_ok_sets_cookie`
  - `test_logout_clears_cookie`
- `orchestrator/tests/test_rate_limit.py`
  - `test_failures_under_threshold_behave_normally`
  - `test_success_resets_counter`
  - `test_distinct_ips_are_independent`
  - `test_forwarded_for_first_ip_used_when_trusted`
  - `test_disabled_limiter_never_blocks`

Aucun autre test existant n'est modifié (les tests `change-password` de
`test_api_routes.py` passent par un JWT direct, pas par le formulaire de
login).

## Validation finale

- Suite complète : `python -m pytest -q` → **372 passed** (~32 s), soit les
  342 tests préexistants + 30 nouveaux (`test_password_rotation.py`, tous
  verts en < 13 s isolément) — zéro régression, aucun test lent ni bloquant.
- `git status` : seuls les changements attendus (feature + détracking des
  3 fichiers data + exemples non suivis à ajouter).
- Aucun fichier suivi ne contient le vrai hash de `docky123` ; le conftest
  racine génère le sien dynamiquement (`BCRYPT_DOCKY123`).

## Journal

- [x] Lecture du code concerné (config, auth, routes, templates, tests)
- [x] Design arrêté (token restreint, cookies, détection)
- [x] Backend : password_policy, jwt_utils, router, config
- [x] Template change_password.html
- [x] Adaptation fixtures/conftest + tests existants
- [x] Nouveaux tests (30 dans test_password_rotation.py)
- [x] pytest vert (372 = 342 + 30)
- [x] Détracking git + fichiers *.example + vérification grep
