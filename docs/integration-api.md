# API d'intégration Docky ↔ Homy

Façade REST **dédiée, versionnée et server-to-server** permettant à une
intégration externe (premier consommateur : **Homy**) de lire l'inventaire de
Docky. Elle est volontairement séparée de l'API navigateur (`/api/*`,
authentifiée par le cookie de session JWT + double-submit CSRF).

- **Base URL** : `/api/integration/v1`
- **Auth** : une **clé Bearer unique et dédiée** (`security.integration_api_key`),
  header `Authorization: Bearer <clé>`, comparaison en temps constant
  (`hmac.compare_digest`).
- **Erreurs** : toujours `{"error": "...", "code": "..."}` (jamais l'enveloppe
  navigateur `{"detail": ...}`).
- **Module** : `orchestrator/app/routes/integration.py` (monté dans `app.main`).
- **Tests** : `orchestrator/tests/test_integration_api.py`.

---

## 1. Authentification

Clé **dédiée** : elle est totalement indépendante de la clé MCP (décision Q1).
Elle est visible / copiable / régénérable depuis **Settings → carte « API
d'intégration »** (`GET/POST /api/settings/integration[/regenerate]`, réservés
aux sessions JWT).

Génération et persistance : `get_integration_api_key()`
(`security.integration_api_key`) — clé auto-générée sur la première ouverture
de la carte Settings, sur le modèle de la clé MCP.

### Réponses d'erreur d'auth

| Cas | Statut | Corps |
|-----|--------|-------|
| Header `Authorization` absent | `401` | `{"error":"Invalid or missing API key","code":"unauthorized"}` |
| Token Bearer invalide | `401` | `{"error":"Invalid or missing API key","code":"unauthorized"}` |
| Header présent mais scheme ≠ `Bearer` | `403` | `{"error":"Forbidden","code":"forbidden"}` |
| Clé non configurée (`security.integration_api_key` vide) | `503` | `{"error":"Docky is not configured","code":"not_configured"}` |
| Façade désactivée (`security.integration_enabled: false`) | `503` | `{"error":"Docky is not configured","code":"not_configured"}` |

### Exemption CSRF

Tous les chemins `/api/integration/` sont **exemptés** du double-submit CSRF
(`app.auth.csrf.API_EXEMPT_PREFIXES`). Les mutations (batch health, batch
stats, actions start/stop/restart) portent
uniquement le Bearer, sans cookie/CSRF : elles ne doivent donc pas être
bloquées par un `403 CSRF`. Preuve : le test
`test_batch_health_csrf_exempt_with_bearer` (POST Bearer sans cookie → `200`,
alors qu'un POST `/api/presence/heartbeat` sans CSRF → `403 CSRF`).

---

## 2. Normalisation

Toutes les réponses de la façade sont normalisées :

- **`state`** ∈ `running | exited | paused | restarting | created | dead | unknown`.
  Les libellés Docker non listés (`removing`, `stopping`, …), vides ou inconnus
  sont ramenés à `"unknown"`.
- **`health`** ∈ `healthy | unhealthy | starting | none`. `null` (pas de
  healthcheck) → `"none"` ; toute autre valeur inconnue → `"none"`.
- **`checkedAt`** / **`lastCheck`** : ISO‑8601 UTC suffixé `Z`
  (`2026-09-13T14:55:52Z`), `null` si jamais mesuré.
- **`cpu_percent`** : normalisé **0–100 hôte** (décision Q4). L'agent renvoie
  `(cpu_delta/system_delta) * cpu_count * 100` (pourcentage multi-cœurs) ;
  la façade divise par le nombre de vCPU via
  `normalize_cpu_percent(raw, cpu_count)` puis clampe 0–100. Utilisé par les
  endpoints stats du Lot B.
- Un conteneur normalisé a la forme :
  `{id, name, image, state, health, stack, service}` (`stack` = `null` hors
  compose, `service` = chaîne vide hors compose).

Helpers réutilisables exposés par `app.routes.integration` :
`normalize_state`, `normalize_health`, `normalize_cpu_percent`,
`normalize_container`, `utc_iso_now`, `iso_from_timestamp`.

---

## 3. Endpoints (Lot A)

### `GET /api/integration/v1/agents`

Liste les agents configurés.

```json
{
  "agents": [
    {
      "name": "prod-1",
      "url": "https://agent-1:8443",
      "status": "online",
      "version": "0.0.4",
      "lastCheck": "2026-09-13T14:55:52Z"
    }
  ]
}
```

- `version` est capturée/cachée au moment du ping `/agent/health`
  (`{status, version, name}`) par `AgentManager.ping_agent`.
- `lastCheck` = horodatage du dernier ping.
- `503 {"error":"No agents configured","code":"no_agents"}` si aucun agent.

### `GET /api/integration/v1/agents/{agent}/containers`

```json
{
  "agent": "prod-1",
  "containers": [
    {"id": "a1b2c3", "name": "web", "image": "nginx:latest",
     "state": "running", "health": "healthy", "stack": "MyApp", "service": "web"}
  ]
}
```

- `404 {"code":"agent_not_found"}` si l'agent est inconnu.
- `502 {"code":"agent_unreachable"}` si l'agent est hors ligne / injoignable.

### `GET /api/integration/v1/agents/{agent}/containers/{container}`

`{container}` accepte **le nom OU l'id** Docker.

```json
{
  "agent": "prod-1",
  "container": "web",
  "id": "a1b2c3",
  "name": "web",
  "state": "running",
  "health": "none",
  "checkedAt": "2026-09-13T14:55:52Z"
}
```

- `404 {"code":"agent_not_found"}` agent inconnu.
- `404 {"code":"not_found"}` conteneur introuvable (agent en ligne).
- `503 {"code":"agent_offline"}` agent connu mais hors ligne.
- `502 {"code":"agent_unreachable"}` agent en ligne mais injoignable.

### `POST /api/integration/v1/containers/health`

Vérification santé par lots. Corps :

```json
{"targets": [{"agent": "prod-1", "container": "web"}]}
```

Réponse **toujours `200`**, ordre des `targets` préservé, erreur **par cible** :

```json
{
  "checkedAt": "2026-09-13T14:55:52Z",
  "results": [
    {"agent": "prod-1", "container": "web", "found": true,
     "id": "a1b2c3", "name": "web", "state": "running", "health": "healthy"},
    {"agent": "prod-1", "container": "missing", "found": false, "state": "unknown",
     "error": {"code": "not_found", "message": "Container 'missing' not found"}},
    {"agent": "prod-2", "container": "x", "found": false, "state": "unknown",
     "error": {"code": "agent_unreachable", "message": "Agent 'prod-2' is offline"}}
  ]
}
```

- Un agent injoignable / hors ligne / inconnu donne `found:false` +
  `error.code:"agent_unreachable"` **dans la réponse 200** (pas de 502 global).
- Matching cible : nom exact, id exact, ou préfixe d'id (≥ 6 caractères,
  sémantique Docker).
- Implémentation : **groupement par agent**, une seule liste de conteneurs par
  agent (cache SWR, fraîcheur ~5 s), puis matching name/id.

Limites (sinon `400 {"error": "...", "code": "invalid_request"}`) :

- `targets` non-liste, JSON invalide, ou `targets` absents ;
- **plus de 100 cibles** (`MAX_BATCH_TARGETS`) ;
- corps **> 256 Kio** (`MAX_BODY_BYTES`).

---

## 4. Endpoints (Lot B) — stats, batch stats, actions

### Décisions stats

- **CPU (Q4)** : l'agent renvoie un pourcentage **multi-cœurs**
  (`(cpu_delta/system_delta) * cpu_count * 100`, ex. 0–800 %). La façade
  expose `cpu_percent` **normalisé 0–100 hôte** (division par `cpu_count`,
  clamp), et conserve la valeur brute dans `cpu_percent_raw` ainsi que le
  nombre de vCPU dans `cpu_count` (transparence).
- **Mémoire (Q6)** : `mem_usage` **et** `mem_percent` sont basés sur la **même**
  grandeur, à savoir `usage - page cache` (comme `docker stats`) :
  `inactive_file` (cgroup v2) ou `total_inactive_file` (cgroup v1), clampé à
  `[0, usage]`. Le cache déduit est exposé via `mem_cache`. L'agent
  (`agent/docker_manager.py::_memory_usage_no_cache`) calcule cette valeur une
  seule fois : la surface d'intégration est donc cohérente (l'ancien `usage`
  brut incluait le cache, ce qui surestimait l'occupation).
- **Disque (Q6)** : non disponible via `docker stats` → `disk_usage`,
  `disk_limit`, `disk_percent` sont **toujours `null`**.
- **Hot set / cache chaud** : chaque appel stats (unitaire ou batch) enregistre
  les conteneurs demandés dans le **hot set Homy** (TTL 60 s, rafraîchi à
  chaque appel) géré par `app.agent_manager.stats_stream`. Un échantillon
  chaud de **moins de ~2 s** est servi directement depuis le cache (même
  normalisation, `checkedAt` = horodatage de l'échantillon) ; sinon le
  comportement live historique s'applique. Le contrat et les formes de réponse
  sont **inchangés** (un cache froid retombe toujours sur `docker stats`).

### `GET /api/integration/v1/agents/{agent}/containers/{container}/stats`

`{container}` accepte le **nom OU l'id** Docker.

```json
{
  "agent": "prod-1",
  "container": "web",
  "state": "running",
  "health": "healthy",
  "cpu_percent": 100.0,
  "cpu_percent_raw": 400.0,
  "cpu_count": 4,
  "mem_usage": 600,
  "mem_limit": 10000,
  "mem_percent": 6.0,
  "mem_cache": 400,
  "network_rx": 1234,
  "network_tx": 5678,
  "disk_usage": null,
  "disk_limit": null,
  "disk_percent": null,
  "checkedAt": "2026-09-13T14:55:52Z"
}
```

- Un conteneur **arrêté** répond `200` avec les compteurs à `0` et son
  `state`/`health` réels (pas une erreur).
- Un conteneur **inconnu** (agent joignable) répond `404 not_found`.

| Cas | Statut | `code` |
|-----|--------|--------|
| Agent inconnu | `404` | `agent_not_found` |
| Conteneur inconnu | `404` | `not_found` |
| Agent hors ligne | `503` | `agent_offline` |
| Agent injoignable | `502` | `agent_unreachable` |
| Timeout agent | `504` | `timeout` |

### `POST /api/integration/v1/containers/stats`

Corps `{"targets": [{"agent": "prod-1", "container": "web"}]}`.

Réponse **toujours `200`**, ordre préservé, erreur **par cible** :

```json
{
  "checkedAt": "2026-09-13T14:55:52Z",
  "results": [
    {"agent": "prod-1", "container": "web", "found": true,
     "state": "running", "health": "healthy", "cpu_percent": 100.0,
     "cpu_percent_raw": 400.0, "cpu_count": 4, "mem_usage": 600,
     "mem_limit": 10000, "mem_percent": 6.0, "mem_cache": 400,
     "network_rx": 1234, "network_tx": 5678,
     "disk_usage": null, "disk_limit": null, "disk_percent": null,
     "error": null},
    {"agent": "prod-1", "container": "missing", "found": false,
     "state": "unknown", "cpu_percent": 0.0, "cpu_count": 1,
     "mem_usage": 0, "mem_limit": 0, "mem_percent": 0.0, "mem_cache": 0,
     "network_rx": 0, "network_tx": 0,
     "disk_usage": null, "disk_limit": null, "disk_percent": null,
     "error": {"code": "not_found", "message": "Container 'missing' not found on agent 'prod-1'"}}
  ]
}
```

Codes d'erreur par cible : `not_found`, `agent_unreachable` (agent inconnu /
indisponible), `timeout`, `invalid_request`.

- **Groupement par agent** : une seule requête HTTP par agent, vers
  `POST /agent/containers/stats`, même si les cibles d'un agent sont
  nombreuses.
- Limites (sinon `400 invalid_request`) : `targets` non-liste, JSON invalide,
  **> 100 cibles**, corps **> 256 Kio**.
- **Coût / perf** : côté agent, `docker stats` n'a **pas** d'API batch :
  chaque conteneur coûte un `stats()` au démon
  (`agent/docker_manager.py::get_containers_stats`), exécuté dans un pool à
  **concurrence bornée** (8 workers max). Pour 100 conteneurs sur un même
  agent, compter 100 allers-retours Docker (sérialisés par groupes de 8),
  d'où le plafond de 100 cibles. Les conteneurs demandés via l'API
  d'intégration rejoignent le hot set (TTL 60 s) : les appels suivants à moins
  de ~2 s sont servis depuis le flux temps réel, sans aller-retour Docker.

### `GET /api/integration/v1/agents/{agent}/containers/{container}/stats/history`

Proxy de l'historique fusionné de l'agent (ring buffer fin ~1 s + points
SQLite downsampleés 30 s). `{container}` accepte le nom OU l'id Docker.
Fenêtres supportées : `window` ∈ `900 | 3600 | 86400` (15 min / 1 h / 24 h).

```json
{
  "container": "web",
  "window": 3600,
  "points": [
    {"ts": 1700000000000, "cpu_percent": 42.1, "mem_usage": 600,
     "mem_limit": 10000, "mem_percent": 6.0,
     "net_rx": 1234, "net_tx": 5678}
  ]
}
```

Payload transmis **tel quel** par l'agent (les points ne portent que les 7
champs ci-dessus). `cpu_percent` est ici le **brut multi-cœurs** (c'est une
série temporelle historique ; la normalisation 0–100 ne s'applique qu'aux
endpoints stats instantanés).

| Cas | Statut | `code` |
|-----|--------|--------|
| Fenêtre non supportée | `400` | `invalid_request` |
| Agent inconnu | `404` | `agent_not_found` |
| Conteneur inconnu | `404` | `not_found` |
| Agent hors ligne | `503` | `agent_offline` |
| Agent injoignable | `502` | `agent_unreachable` |
| Timeout agent | `504` | `timeout` |

### Endpoint agent support

`POST /agent/containers/stats` (auth API key) avec `{"ids": [...]}` ou
`{"names": [...]}` → `{"results": [{"id", "name", "state", "health",
"found", ...stats..., "error"}]}`, ordre préservé, `found:false` +
`error` par référence inconnue. Utilisé uniquement par la façade ; les
endpoints agent existants (`/agent/containers/{id}/stats`, etc.) sont
inchangés.

### `POST /api/integration/v1/agents/{agent}/containers/{container}/{start|stop|restart}`

Réponse `200` avec l'**état résultant** (le conteneur est relu après
l'action ; un `restart` peut brièvement donner `running` + `starting`/`none`) :

```json
{"success": true, "agent": "prod-1", "container": "web",
 "action": "stop", "state": "exited", "health": "none"}
```

| Cas | Statut | Corps |
|-----|--------|-------|
| Action appliquée | `200` | `{success:true, agent, container, action, state, health}` |
| Agent / conteneur / action inconnu | `404` | `{error, code:"not_found"}` (agent : `agent_not_found`) |
| Déjà dans l'état cible | `409` | `{error:"Container is already running\|stopped", code:"conflict", state}` |
| Agent injoignable / échec d'action | `502` | `{error, code:"agent_unreachable"\|"action_failed"}` |
| Agent hors ligne / façade non configurée | `503` | `{error, code:"agent_offline"\|"not_configured"}` |
| Délai d'action dépassé (≥ 20 s) | `504` | `{error:"Action timed out", code:"timeout"}` |

- **Idempotence** : `start` sur `running` et `stop` sur `exited`/`dead`/
  `created` renvoient `409 conflict` (pré-vérification d'état, sans mutation).
- Le timeout d'action (20 s) est ≥ au timeout de stop Docker côté agent
  (10 s) et < budget global de requête (30 s).
- Les endpoints agent `/agent/containers/{id}/start|stop|restart` conservent
  leur forme `{success}` : seule la façade orchestrateur enrichit.

---

## 5. Mapping Homy → Docky

| Homy (contrat externe) | Docky (façade implémentée) |
|------------------------|----------------------------|
| `GET /api/agents` | `GET /api/integration/v1/agents` |
| `GET /api/agents/{agent}/containers` | `GET /api/integration/v1/agents/{agent}/containers` |
| `GET /api/agents/{agent}/containers/{container}` | `GET /api/integration/v1/agents/{agent}/containers/{container}` |
| `GET /api/agents/{agent}/containers/{container}/stats` | `GET /api/integration/v1/agents/{agent}/containers/{container}/stats` |
| `GET /api/agents/{agent}/containers/{container}/stats/history` | `GET /api/integration/v1/agents/{agent}/containers/{container}/stats/history` |
| `POST /api/containers/health` (batch) | `POST /api/integration/v1/containers/health` |
| `POST /api/containers/stats` (batch) | `POST /api/integration/v1/containers/stats` |
| `POST /api/agents/{agent}/containers/{container}/start` | `POST /api/integration/v1/agents/{agent}/containers/{container}/start` |
| `POST /api/agents/{agent}/containers/{container}/stop` | `POST /api/integration/v1/agents/{agent}/containers/{container}/stop` |
| `POST /api/agents/{agent}/containers/{container}/restart` | `POST /api/integration/v1/agents/{agent}/containers/{container}/restart` |
| Header `Authorization: Bearer <clé>` | identique (clé dédiée Docky) |
| `{error, code}` | identique |

Seule la base URL change côté Homy : pointer sur
`https://<docky>/api/integration/v1`.

---

## 6. Reste à faire

Le **contrat Lot B est couvert à 100 %** : stats unitaires + batch, historique,
actions `start`/`stop`/`restart`, codes HTTP et enveloppe `{error, code}`. Les
évolutions optionnelles, non demandées à ce stade :

- **Actions `update` (image)** — non implémentée dans la façade (l'agent
  expose déjà `POST /agent/containers/{id}/update-image`, streamé).
- **Webhooks / événements Docky → Homy** — non spécifiés.
- **Stats disque** — indisponibles via `docker stats` (retour `null`, voir §4).
