# Authentification Docker Hub (anti « toomanyrequests »)

Docky permet de configurer des identifiants Docker Hub côté orchestrateur et de
les pousser automatiquement vers les agents, afin que les pulls d'images
(``docker compose pull``, ``docker pull``, fallback SDK) soient authentifiés et
n'épuisent plus le quota anonyme de Docker Hub (erreur 429
``toomanyrequests``).

- Settings (orchestrateur) : ``orchestrator/app/routes/settings.py``,
  section ``dockerhub`` de ``settings.yaml``
- Push : ``orchestrator/app/agent_manager/client.py``
  (``push_dockerhub_to_agent``, ``push_dockerhub_all``,
  ``maybe_push_dockerhub_on_online``) + déclencheur dans
  ``orchestrator/app/agent_manager/events.py``
- Agent : ``agent/dockerhub.py``, endpoint ``POST /agent/dockerhub/login``
  (``agent/routes.py``), réinjection au démarrage (``agent/main.py``)
- Frontend : carte « Docker Hub » dans ``settings.html`` / ``settings.js``
- Tests : ``orchestrator/tests/test_settings_dockerhub.py``,
  ``agent/tests/test_dockerhub.py``

---

## 1. Design

Tous les pulls de l'agent passent par la **CLI docker** (``docker compose pull``,
``docker pull`` — ``agent/docker/compose_stream.py`` et
``agent/docker_manager``). La CLI lit ses credentials dans le répertoire pointé
par ``DOCKER_CONFIG`` (``config.json``). Le mécanisme est donc :

1. L'orchestrateur stocke les credentials dans ``settings.yaml``
   (``dockerhub: {enabled, username, token}``).
2. Il les pousse aux agents via ``POST /agent/dockerhub/login``.
3. L'agent exécute ``docker --config <data_dir>/.docker login -u <user>
   --password-stdin`` : la CLI écrit ``<data_dir>/.docker/config.json``
   (le data dir est un **volume persistant**), et ``DOCKER_CONFIG`` est exporté
   dans l'environnement du process agent.
4. Tous les subprocess docker (compose/pull) héritent de ``DOCKER_CONFIG`` →
   pulls authentifiés.

### Fallback SDK (images.pull)

Deux fallbacks utilisent le SDK docker-py (``client.images.pull`` dans
``agent/docker_manager.py`` : mise à jour d'un container compose non
résolvable, et recréation d'un container standalone). Le SDK **ne lit pas** la
config CLI : ces appels reçoivent un ``auth_config`` explicite décodé depuis le
``config.json`` persisté (``agent/dockerhub.get_registry_auth``, champ
``auths["https://index.docker.io/v1/"].auth``, base64 ``user:token``). Tout le
reste (compose pull, docker pull streamés) passe par la CLI.

---

## 2. Settings (orchestrateur)

``settings.yaml`` (défauts écrits par ``ensure_config_files``) :

```yaml
dockerhub:
  enabled: false
  username: ""
  token: ""
```

Endpoints (authentifiés par JWT, comme tout ``/api/settings/*``) :

| Route | Description |
|---|---|
| ``GET /api/settings/dockerhub`` | ``{enabled, username, has_token}`` — **jamais** le token (ni en clair ni masqué) |
| ``PUT /api/settings/dockerhub`` | Persiste puis pousse vers tous les agents en ligne ; retourne ``{success, push: {agent: …}, pushed, total, errors}`` |
| ``POST /api/settings/dockerhub/clear`` | Désactive, efface username/token et pousse ``enabled=false`` (logout) aux agents en ligne |

Validation ``PUT`` : ``username`` et ``token`` doivent être **ASCII uniquement**
(sinon ``400`` avec message français, même règle que la clé API des agents — un
credential non ASCII ferait échouer ``httpx`` à la construction du header
``Authorization``). Un token masqué/vidé (``****``) conserve la valeur stockée.
``enabled=true`` exige username + token (400 sinon).

### Frontend

Carte compacte « Docker Hub » (même rangée que Historique/Sécurité/MCP, classe
``settings-card--quarter``) : toggle « Activer », username, token (input
masqué, placeholder « (configuré) » si un token est stocké), boutons
« Désactiver » et « Sauvegarder ». Après sauvegarde, le toast affiche
« Poussé sur N/M agents » (+ erreurs par agent éventuelles). Un hint rappelle
de créer un access token sur hub.docker.com (Account Settings → Security) et
que les agents hors ligne recevront la config à leur reconnexion.

### Badge de statut (pill « Activé / Partiel / Incomplet / Désactivé »)

L'en-tête de la carte porte un pill `#dockerhub-status` calculé par
`SettingsApp.dockerhubPillState()` à partir de l'état backend **confirmé**
(jamais de la valeur locale du formulaire) :

| État serveur                              | Pill          | Couleur |
|-------------------------------------------|---------------|---------|
| `enabled=false`                            | « Désactivé » | gris/rouge (`status-offline`) |
| `enabled=true`, `has_token=false`          | « Incomplet » | ambre (`status-warning`) |
| `enabled=true`, `has_token=true`, poussée incomplète* | « Partiel » | ambre (`status-partial`) |
| `enabled=true`, `has_token=true` (sinon)   | « Activé »    | vert (`status-online`) |

\* uniquement quand le résultat de poussée est connu (réponse d'un PUT) : au
moins un agent n'a pas confirmé (hors ligne — il rattrapera à sa reconnexion —
ou en erreur). Au chargement (GET), le résultat de poussée n'est pas connu : le
pill reflète la config seule (« Activé »).

Le pill est rafraîchi :

- au chargement, via `SettingsApp.loadDockerhubSettings()` (payload du GET :
  `enabled`/`has_token`) ;
- **immédiatement** après un « Sauvegarder » (PUT) ou un « Désactiver » (clear),
  depuis la **réponse de la mutation elle-même** : `PUT /api/settings/dockerhub`
  et `POST …/clear` renvoient l'état persisté confirmé (`enabled`, `has_token`,
  `username`) **plus** le résumé de poussée (`pushed`, `total`, `errors`), que
  `SettingsApp.applyDockerhubState()` applique au formulaire et au pill (avec
  un tooltip « Poussé sur N/M agent(s) »). Aucune valeur locale n'est
  réinjectée et il n'y a pas de GET de rattrapage qui pourrait écraser l'état
  « Partiel » — la source de vérité est toujours la réponse serveur.

Comportements garantis côté backend :

- `test_put_dockerhub_enabled_then_get_returns_enabled` : un `PUT` avec
  `enabled: true` renvoie `enabled: true, has_token: true` **et** le `GET`
  suivant confirme le même état (les deux payloads du pill) ;
- `test_put_dockerhub_response_carries_partial_push_results` : la réponse du
  `PUT` porte le résumé de poussée par agent (`pushed`/`total`/`errors`) ;
- `test_get_dockerhub_incomplete_config_pill_payload` : `enabled` sans token
  stocké → `GET` renvoie `enabled=true, has_token=false` (pill « Incomplet ») ;
- `test_clear_dockerhub_disables_and_pushes_logout` : la réponse du clear
  porte l'état désactivé confirmé (pill « Désactivé »).

---

## 3. Push (2 déclencheurs + anti-spam)

### Déclencheur 1 — sauvegarde des paramètres

``PUT /api/settings/dockerhub`` (et ``POST …/clear``) appelle
``agent_manager.push_dockerhub_all()`` : pour chaque agent **en ligne**,
``POST /agent/dockerhub/login`` avec ``{username, token, enabled}``. Les agents
hors ligne sont marqués ``{"success": false, "offline": true}`` dans le
résultat retourné au frontend.

### Déclencheur 2 — reconnexion d'agent

``AgentManager.maybe_push_dockerhub_on_online(name)`` est appelé :

- quand la connexion WebSocket d'events s'(re)établit
  (``events._connect_agent_events``, statut → ``online``) ;
- quand ``ping_agent`` fait passer un agent de offline/unknown à online.

Il ne pousse que si : config ``enabled`` avec username + token, agent en
ligne, et pas de push déjà en cours. Sinon il compare le SHA-256 du token au
dernier hash poussé pour cet agent (``AgentManager._dockerhub_pushed``) :
identique → **aucun push** (anti-spam : un agent qui flappe ne reçoit pas le
token à chaque reconnexion) ; différent ou jamais poussé → push. Après une
rotation de token, la reconnexion pousse le nouveau.

Si la config a été **désactivée** pendant que l'agent était hors ligne, la
reconnexion pousse une seule fois ``enabled=false`` (l'agent fait
``docker logout`` et nettoie son dossier), puis l'entrée est oubliée.

Les logs mentionnent le nom de l'agent et l'issue du push, **jamais le token**.

---

## 4. Agent

``POST /agent/dockerhub/login`` (protégé par la clé API Bearer) :

- ``enabled=true`` + credentials → ``docker login -u <user> --password-stdin``
  exécuté avec le token sur **stdin** (JAMAIS en argv — il n'apparaît ni dans
  ``ps``, ni dans les logs de commande) ; config écrite dans
  ``<data_dir>/.docker`` ; ``DOCKER_CONFIG`` exporté pour les subprocess de
  l'agent. Échec → ``502`` avec le message d'erreur assaini de la CLI.
- ``enabled=false`` → ``docker logout`` + suppression du dossier ``.docker``
  + retrait de ``DOCKER_CONFIG``.

### Persistance au redémarrage

Au démarrage de l'agent (event ``startup`` de ``agent/main.py``),
``agent/dockerhub.apply_persisted_config()`` vérifie
``<data_dir>/.docker/config.json`` : s'il existe, ``DOCKER_CONFIG`` est
réinjecté dans ``os.environ`` — tous les subprocess docker
(``docker compose pull``, ``docker pull``, ``docker compose up``) héritent de
l'environnement et restent authentifiés après un redémarrage, sans nouvel
échange avec l'orchestrateur. Le volume ``/data`` de l'agent
(``docker-compose.yml``) garantit la survie du fichier.

---

## 5. Sécurité

- **Token sur stdin** : ``--password-stdin`` partout ; le token n'apparaît
  jamais en argument de processus.
- **Masquage API** : ``GET`` renvoie ``has_token`` (booléen) uniquement ; le
  token n'est ni renvoyé, ni masqué partiellement, ni loggué.
- **Validation ASCII** : username et token ASCII obligatoires (400 FR), même
  logique que la clé API des agents (httpx ne peut pas encoder un header non
  ASCII) ; contrôlée aussi côté client en JavaScript.
- **Fichiers** : ``config.json`` est écrit dans le data dir de l'agent
  (volume privé de l'agent) avec les permissions du process ; ``clear`` /
  logout suppriment le dossier.
- **Transport** : le push orchestrateur → agent réutilise ``_request`` avec la
  clé API Bearer de l'agent et sa politique TLS (``tls_verify``/``ca_cert``).

---

## 6. Limitations

- Le token est stocké **en clair** dans ``settings.yaml`` (comme les clés API
  LLM/agents) : protégez le data dir de l'orchestrateur.
- Les pulls vers des **registres privés tiers** (GHCR, Quay…) ne sont pas
  couverts : seul Docker Hub est authentifié.
- Les pulls faits par le **daemon lui-même** (``image:`` d'un compose déployé
  par un autre outil, ``pull_policy``) dépendent de la config du daemon hôte,
  pas de l'agent.
- Si l'agent est hors ligne lors d'un ``clear``, sa config locale reste en
  place jusqu'à sa reconnexion (un logout unique est alors poussé).
- L'orchestrateur ne vérifie pas la validité du token auprès de Docker Hub ;
  un token invalide remontera comme échec de pull sur l'agent.