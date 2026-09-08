# Authentification multi-registres (LOT 2)

Docky permet de configurer plusieurs registres Docker (Docker Hub, GHCR,
GitLab, Quay, lscr.io, registres privés…) côté orchestrateur et de pousser
leurs identifiants vers les agents, afin que les pulls d'images
(``docker compose pull``, ``docker pull``, fallback SDK) soient authentifiés.

Ce document décrit le **LOT 2** (orchestrateur + frontend) qui généralise le
LOT 1 « Docker Hub » (``docs/dockerhub-auth.md``) à N registres.

- Settings (orchestrateur) : ``orchestrator/app/routes/settings.py``,
  section ``registries`` de ``settings.yaml``
- Push : ``orchestrator/app/agent_manager/client.py``
  (``push_registry_to_agent``, ``push_registry_all``,
  ``logout_registry_to_agent``, ``logout_registry_all``,
  ``maybe_push_registries_on_online``) + déclencheur dans
  ``orchestrator/app/agent_manager/events.py``
- Agent : ``agent/registries.py``, endpoints ``POST /agent/registry/login``,
  ``POST /agent/registry/logout``, ``GET /agent/registries``
  (``agent/routes.py``)
- Frontend : carte « Registres » dans ``settings.html`` / ``settings.js``
- Tests : ``orchestrator/tests/test_settings_registries.py``,
  ``agent/tests/test_registries.py``

---

## 1. Configuration

``settings.yaml`` (défauts écrits par ``ensure_config_files``) :

```yaml
registries:
  - url: docker.io
    username: ""
    token: ""
    tailscale: false
    tailscale_host: ""
tailscale:
  enabled: false
  host: ""
```

Chaque entrée ``registries`` porte ``{url, username, token, tailscale,
tailscale_host}``. ``tailscale``/``tailscale_host`` sont un **placeholder
persisté sans effet** (à venir) ; la section ``tailscale`` globale porte le
placeholder de la carte (checkbox + adresse tailnet).

### Migration one-time depuis Docker Hub

Au démarrage (``ensure_config_files`` → ``config.migrate_dockerhub_to_registries``),
l'ancienne section ``dockerhub: {enabled, username, token}`` du LOT 1 est
convertie en entrée ``registries`` ``{url: "docker.io", username, token,
tailscale: false, tailscale_host: ""}`` (si ``enabled`` + token), puis la
section ``dockerhub`` est **supprimée** — la migration ne s'exécute donc
qu'une seule fois. Elle est volontairement **hors** de ``load_settings`` pour
ne pas perturber les tests qui seedent la section legacy directement.

---

## 2. API (orchestrateur)

Endpoints authentifiés par JWT (comme tout ``/api/settings/*``) :

| Route | Description |
|---|---|
| ``GET /api/settings/registries`` | Registres configurés + découverts agrégés + placeholder tailscale |
| ``PUT /api/settings/registries`` | Upsert d'un registre (ou persistance du placeholder tailscale) |
| ``DELETE /api/settings/registries/{url}`` | Retrait d'un registre + logout des agents |

### GET

```json
{
  "registries": [
    {
      "url": "ghcr.io",
      "username": "ghuser",
      "has_token": true,
      "tailscale": false,
      "tailscale_host": "",
      "push_status": { "agent1": "ok", "agent2": "offline" }
    }
  ],
  "discovered": ["docker.io", "ghcr.io", "lscr.io"],
  "tailscale": { "enabled": false, "host": "" }
}
```

- ``has_token`` : booléen — le token n'est **jamais** renvoyé (ni en clair ni
  masqué).
- ``push_status`` : état de poussée par agent (``ok`` / ``offline`` / ``error``),
  alimenté par ``AgentManager.registry_push_status(url)``.
- ``discovered`` : registres utilisés par les composes des stacks, **agrégés**
  depuis tous les agents (union de ``GET /agent/registries``) avec un **cache
  TTL ~5 min** (``AgentManager.get_aggregated_discovered_registries``).
  ``?refresh=1`` force un rescan.

### PUT

Registre : ``{url, username, token, tailscale, tailscale_host}``.

- Validation : ``url``/``username``/``token`` **ASCII uniquement** et URL un
  **hôte valide** (ex. ``docker.io``, ``ghcr.io``, ``localhost:5000``) → sinon
  ``400`` avec message français.
- Un token masqué/vidé (``****``) conserve la valeur stockée.
- Après persistance, le registre est poussé aux agents en ligne ; la réponse
  porte l'état persisté confirmé + le résumé de poussée par agent
  (``{success, registry, push, pushed, total, errors}``).

Placeholder tailscale : ``{tailscale: {enabled, host}}`` (sans ``url``) —
persisté **sans effet**.

### DELETE

``DELETE /api/settings/registries/{url}`` (url encodée) : retire l'entrée de
``settings.yaml`` et pousse ``/agent/registry/logout`` aux agents en ligne.
Les agents hors ligne sont déconnectés à leur reconnexion (voir §3).

---

## 3. Push (2 déclencheurs + anti-spam par registre×agent)

### Déclencheur 1 — sauvegarde / retrait

``PUT /api/settings/registries`` appelle ``agent_manager.push_registry_all(url)``
(``POST /agent/registry/login`` pour chaque agent en ligne) ;
``DELETE …/{url}`` appelle ``agent_manager.logout_registry_all(url)``
(``POST /agent/registry/logout``). Les agents hors ligne sont marqués
``{"success": false, "offline": true}``.

### Déclencheur 2 — reconnexion d'agent

``AgentManager.maybe_push_registries_on_online(name)`` est appelé :

- quand la connexion WebSocket d'events s'(re)établit
  (``events._connect_agent_events``, statut → ``online``) ;
- quand ``ping_agent`` fait passer un agent de offline/unknown à online.

Il itère **chaque registre configuré** et ne pousse que si : agent en ligne,
credentials présents, et pas de push en cours. Anti-spam **par registre×agent** :
le SHA-256 du token est comparé au dernier hash poussé pour ce couple
(``AgentManager._registry_pushed[url][agent]``) — identique → aucun push ; un
agent qui flappe ne reçoit pas les tokens à chaque reconnexion. Après rotation
du token d'un registre, la reconnexion pousse le nouveau.

Un registre **supprimé** pendant qu'un agent était hors ligne est enregistré
dans ``AgentManager._registry_pending_logout`` ; à la reconnexion, un unique
``logout`` est poussé pour cet agent, puis l'entrée est oubliée.

Les logs mentionnent le registre, l'agent et l'issue du push, **jamais le token**.

---

## 4. Agent

- ``POST /agent/registry/login`` ``{registry, username, token}`` →
  ``docker login <host> -u <user> --password-stdin`` (token sur **stdin**,
  jamais en argv) ; config écrite dans ``<data_dir>/.docker`` (volume
  persistant, une clé ``auths`` par registre) ; ``DOCKER_CONFIG`` exporté.
- ``POST /agent/registry/logout`` ``{registry}`` → ``docker logout`` + retrait
  chirurgical de l'entrée de ce registre (les autres registres sont conservés).
- ``GET /agent/registries`` → ``{stored, discovered}`` (hosts avec credentials
  + hosts utilisés par les composes).

Voir ``agent/registries.py`` et ``agent/tests/test_registries.py`` pour le
détail (normalisation ``docker.io``, ``get_registry_auth`` pour le fallback
SDK, persistance au redémarrage).

---

## 5. Frontend

Carte « Registres » (classe ``settings-card--quarter``, remplace « Docker Hub ») :

- **Liste défilable** des registres configurés : URL, pill de statut à 4 états,
  boutons « S'authentifier » / « Déconnecter ».
- **Registres découverts** non authentifiés : lignes discrètes, clic →
  formulaire pré-rempli.
- Boutons « Scanner » (force ``refresh=1``) et « Ajouter un registre ».
- **Placeholder Tailscale** : checkbox + champ adresse tailnet, persisté
  (``PUT`` avec ``{tailscale: {enabled, host}}``), hint « à venir », **aucun
  effet**.

### Pill de statut

| État serveur | Pill | Couleur |
|---|---|---|
| ``has_token=false`` | « Incomplet » | ambre (`status-warning`) |
| ``has_token=true``, poussée incomplète* | « Partiel » | ambre (`status-partial`) |
| ``has_token=true`` (sinon) | « Activé » | vert (`status-online`) |

\* au moins un agent n'a pas confirmé (``push_status`` ≠ ``ok``).

Le pill est calculé par ``SettingsApp.registryPillState()`` à partir de l'état
backend **confirmé** (jamais de la valeur locale du formulaire).

---

## 6. Sécurité

- **Token sur stdin** : ``--password-stdin`` partout ; jamais en argument de
  processus.
- **Masquage API** : ``GET`` renvoie ``has_token`` (booléen) uniquement ; le
  token n'est ni renvoyé, ni masqué partiellement, ni loggué.
- **Validation ASCII** : url/username/token ASCII obligatoires (400 FR),
  contrôlée aussi côté client en JavaScript.
- **Transport** : le push orchestrateur → agent réutilise ``_request`` avec la
  clé API Bearer de l'agent et sa politique TLS (``tls_verify``/``ca_cert``).

---

## 7. Limitations

- Le token est stocké **en clair** dans ``settings.yaml`` (comme les clés API
  LLM/agents) : protégez le data dir de l'orchestrateur.
- Le placeholder Tailscale est **sans effet** (à venir) : il ne configure
  aucun réseau tailnet.
- Les pulls faits par le **daemon lui-même** dépendent de la config du daemon
  hôte, pas de l'agent.
- L'orchestrateur ne vérifie pas la validité des tokens auprès des registres ;
  un token invalide remontera comme échec de pull sur l'agent.
