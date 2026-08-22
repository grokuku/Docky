# Rate limiting sur l'authentification (`POST /login`)

> Traçabilité de l'implémentation du rate limiting anti brute-force sur
> `POST /login` (orchestrateur), roadmap.md « Backlog sécurité » /
> « Phase 9 ». Baseline pytest au démarrage : **303 passed**.

## 1. Objectif et périmètre

- Protéger `POST /login` de l'**orchestrateur** contre le brute-force de
  mots de passe.
- **Zéro changement de comportement** hors du cas « IP bloquée » :
  réponses identiques avant le seuil (303 `/login?error=1`, 303
  `/dashboard`…), GET /login et GET /logout non affectés.
- Suite pytest existante verte **sans modification** ; nouveaux tests
  dédiés dans `orchestrator/tests/test_rate_limit.py`.

Hors scope (inchangés) : CSRF, rate limiting sur l'agent, limiting
multi-instances distribué (voir §8 Limitations).

## 2. Conception

### 2.1 Choix : limiteur maison en mémoire (pas de slowapi)

Limiteur en mémoire pur Python/stdlib (aucune nouvelle dépendance),
adapté au déploiement cible mono-instance (uvicorn single-process par
défaut). slowapi a été écarté : il ajoute une dépendance (wrapper autour
de `limits`) pour une mécanique triviale ici, et sa config par décorateur
est moins flexible pour notre lecture de config à chaque requête.

### 2.2 Algorithme : fenêtre glissante sur les ÉCHECS uniquement

- Clé = IP client résolue (voir §2.3).
- On ne compte que les **échecs** de connexion (mauvais mot de passe,
  utilisateur inconnu, hash vide, erreur bcrypt). Un **succès remet le
  compteur de l'IP à zéro** (l'utilisateur légitime qui se trompe une
  fois n'est jamais pénalisé au-delà du seuil).
- Fenêtre glissante : chaque échec est horodaté (`time.monotonic()`,
  immunisé aux sauts d'horloge murale) ; seuls les échecs plus récents
  que `window_seconds` comptent. Bloqué tant que
  `nb_échecs_fenêtre >= max_attempts`.
- Pendant un blocage, les requêtes reçoivent 429 **avant** toute vérif
  d'identifiants et ne prolongent PAS le blocage (les échecs ne sont pas
  enregistrés tant que bloqué) → déblocage purement temporel quand le
  plus vieil échec de la fenêtre sort de celle-ci. Pas de lock-out
  permanent possible.
- Mémoire bornée : purge des timestamps expirés à chaque écriture +
  sweep global de la table dès que le nombre d'IP suivies dépasse
  `MAX_TRACKED_CLIENTS` (4096), avec éviction FIFO en dernier recours.

### 2.3 Clé client et X-Forwarded-For

- Par défaut : `request.client.host` (IP socket directe).
- `X-Forwarded-For` (premier IP de la liste) pris en compte **uniquement
  si `security.rate_limit.trust_proxy: true`** — sinon l'en-tête est
  ignoré, ce qui empêche un client direct de spoofer des IPs pour
  contourner la limite.
- Pas de client socket (cas limite TestClient/scope exotique) → clé
  `"unknown"`.

### 2.4 Comportement quand bloqué : HTTP 429 (+ Retry-After)

Choix documenté : **429** plutôt qu'un redirect 303 vers
`/login?error=ratelimit`.

Raisons :
1. Sémantique HTTP correcte (le problème n'est pas « identifiants
   incorrects » mais « trop de requêtes ») — exploitable par monitoring,
   reverse proxy et tests.
2. Le template actuel affiche « Identifiants incorrects » pour tout
   `error=*` ; rediriger vers `/login?error=ratelimit` afficherait un
   message mensonger sans toucher au template (hors scope « zéro
   changement »).
3. Les bots de brute-force ne suivent pas les redirects : le redirect
   n'apporterait rien côté défense.

La réponse 429 embarque un petit corps HTML autonome (message FR,
cohérent avec l'UI) et l'en-tête `Retry-After` (secondes restantes,
plafond = fenêtre).

### 2.5 Placement

Appels explicites dans `login_submit` (et non middleware global) :
- `check_login_rate_limit(request)` en tête du handler POST /login →
  renvoie la réponse 429 ou `None` ;
- `register_login_failure(request)` sur chaque chemin d'échec ;
- `register_login_success(request)` avant le redirect de succès.

GET /login et GET /logout sont délibérément hors limiter (le GET sert
aussi à afficher l'erreur après redirect ; bloquer logout n'a aucun sens).

Le handler lit la config **à chaque requête** (résolution tardive via
`get_setting("security.rate_limit")`) : cohérent avec le pattern projet
(`jwt_utils`, tests qui réécrivent `settings.yaml` à chaud), aucune
variable d'env supplémentaire nécessaire pour les tests.

## 3. Configuration

Section ajoutée sous `security` dans `settings.yaml` :

```yaml
security:
  jwt_secret: "..."
  jwt_algorithm: "HS256"
  jwt_expire_minutes: 1440
  rate_limit:            # anti brute-force sur POST /login
    enabled: true        # false = désactive complètement le limiteur
    max_attempts: 5      # nb max d'échecs consécutifs par IP dans la fenêtre
    window_seconds: 300  # taille de la fenêtre glissante (s)
    trust_proxy: false   # true = faire confiance à X-Forwarded-For (derrière reverse proxy)
```

- Défauts sûrs appliqués si section/clés absentes :
  `enabled=True, max_attempts=5, window_seconds=300, trust_proxy=False`.
- Valeurs invalides (type non numérique, <= 0 pour max_attempts /
  window_seconds) → retour au défaut correspondant.
- `ensure_config_files()` crée désormais les installations fraîches avec
  cette section (auto-documentation).
- L'API `/settings/*` existante fait load→mutate→save : elle préserve
  les clés inconnues, donc `rate_limit` survit aux éditions via l'UI.

Fichiers mis à jour pour la documentation des clés :
`data/settings.yaml` (config exemple/dev du dépôt, avec commentaires) et
défauts dans `ensure_config_files()` (orchestrator/app/config.py).

## 4. Thread-safety

Un unique `threading.Lock` protège toutes les lectures/mutations du
store. Uvicorn est mono-process async par défaut (inutile en théorie),
mais le lock coûte négligeable et rend le module sûr si l'orchestrateur
est lancé avec plusieurs threads/workers. NB : en multi-process
(workers uvicorn > 1), chaque process garde son propre compteur —
limite intrinsèque d'un limiteur mémoire, documentée en §8.

## 5. Fichiers

| Fichier | Action |
|---|---|
| `orchestrator/app/auth/rate_limit.py` | **créé** — limiteur + helpers |
| `orchestrator/app/auth/router.py` | modifié — branchement POST /login |
| `orchestrator/app/config.py` | modifié — section `rate_limit` dans les défauts `ensure_config_files()` |
| `data/settings.yaml` | modifié — clés documentées (commentaires) |
| `orchestrator/tests/test_rate_limit.py` | **créé** — nouveaux tests |
| `docs/rate-limiting.md` | créé (ce fichier) |

Aucun test existant modifié. Aucune dépendance ajoutée.

## 6. Nouveaux tests (`orchestrator/tests/test_rate_limit.py`, 12 tests)

Isolation : fixture autouse `clean_rate_limiter` qui reset le singleton
avant/après chaque test (le store est process-wide) ; limites basses
injectées en réécrivant `settings.yaml` (lecture de la config à chaque
requête). Vérification supplémentaire : exécuter `test_rate_limit.py`
AVANT `test_auth_router.py` reste vert (ordre-indépendance).

| Test | Cas couvert |
|---|---|
| `test_failures_under_threshold_behave_normally` | sous le seuil → 303 error=1 identiques, succès OK juste avant seuil |
| `test_threshold_reached_returns_429` | seuil atteint → 429 même avec BONS identifiants, `Retry-After` ≥ 1, pas de cookie |
| `test_blocked_ip_cannot_extend_block_by_retrying` | hammering pendant blocage non enregistré → déblocage purement temporel (t=1000+100, pas 1010+100) |
| `test_get_login_not_rate_limited` | GET /login reste 200 quand l'IP est bloquée |
| `test_success_resets_counter` | succès → remise à zéro du compteur de l'IP |
| `test_window_expiry_unblocks` | fenêtre expirée (horloge factice monkeypatchée sur `rl.monotonic`) → débloqué à t₀+W, pas avant |
| `test_distinct_ips_are_independent` | deux IPs indépendantes (trust_proxy + XFF) ; bloquer l'une n'affecte pas l'autre |
| `test_forwarded_for_ignored_when_trust_proxy_disabled` | sans trust_proxy, XFF spoofé ne permet PAS de contourner (clé = socket peer) + assertion directe sur `get_client_key` |
| `test_forwarded_for_first_ip_used_when_trusted` | trust_proxy → premier IP de la liste XFF uniquement |
| `test_disabled_limiter_never_blocks` | `enabled: false` → jamais de 429, état non touché |
| `test_config_invalid_values_fall_back_to_defaults` | valeurs invalides (`"abc"`, `-5`) → défauts sûrs, pas de crash |
| `test_missing_rate_limit_section_uses_safe_defaults` | section absente → défauts documentés (5/300/false/enabled) |

### Compatibilité avec les tests existants

**Aucun test existant modifié.** Raisonnement : les seeds de test
(`make_settings`) n'ont pas de clé `rate_limit` → défauts permissifs
(5 échecs / 300 s) ; toute la suite existante produit au maximum **3**
échecs consécutifs depuis l'IP unique `testclient`
(`test_login_wrong_password_redirects_error`,
`test_login_unknown_user_redirects_error`,
`test_login_empty_password_hash_redirects_error`), sous le seuil de 5.
Les nouveaux tests remettent le singleton à zéro autour de chaque test,
donc aucune pollution croisée dans un ordre ou l'autre.

## 7. Résultats pytest

| Exécution | Résultat |
|---|---|
| Baseline avant travaux | **303 passed** |
| Suite complète après implémentation | **315 passed** (303 existants inchangés + 12 nouveaux), 0 failed |
| Ordre inversé (`test_rate_limit` avant `test_auth_router` + `test_api_routes`) | **60 passed** |

Commandes :

```
python -m pytest -q                     # 315 passed
python -m pytest orchestrator/tests/test_rate_limit.py orchestrator/tests/test_auth_router.py orchestrator/tests/test_api_routes.py -q   # 60 passed
```

Avertissements résiduels = dépréciations préexistantes (starlette/
on_event), non liées à ce changement.

## 8. Limitations connues (documentées, acceptées)

- **Mono-process** : limiteur mémoire par processus. Avec plusieurs
  workers uvicorn, chaque worker compte ses propres échecs (seuil
  effectif × nb workers). Le déploiement Docky standard est mono-worker ;
  pour du multi-instance, poser la limite au niveau reverse proxy.
- **Redémarrage = perte des compteurs** (pas de persistance) : acceptable
  pour de l'anti brute-force.
- **IPv6 / NAT** : beaucoup de clients derrière une même IP partagent le
  quota ; le reset-sur-succès et Retry-After limitent l'impact légitime.
- Les requêtes avec formulaire invalide (422 FastAPI, champs manquants)
  passent avant le check du handler et ne sont donc pas comptées — sans
  impact sécurité réel (elles n'émettent aucun essai d'identifiants).

## 9. Rapport final

- **Fichiers créés** : `orchestrator/app/auth/rate_limit.py`,
  `orchestrator/tests/test_rate_limit.py`, `docs/rate-limiting.md`.
- **Fichiers modifiés** : `orchestrator/app/auth/router.py` (branchement,
  +`request: Request` injecté par FastAPI), `orchestrator/app/config.py`
  (section `rate_limit` dans `ensure_config_files()`),
  `data/settings.yaml` (clés documentées).
- **Design** : fenêtre glissante d'échecs par IP, `time.monotonic()`,
  `threading.Lock`, purge à l'écriture + sweep au-delà de 4096 IP ;
  réponse **429 + Retry-After** (choix documenté §2.4) ; config relue à
  chaque requête via `security.rate_limit.{enabled,max_attempts,
  window_seconds,trust_proxy}` (défauts `true/5/300/false`).
- **Dépendances ajoutées** : aucune.
- **Tests existants modifiés** : aucun.
- **pytest** : 315 passed (voir §7).
