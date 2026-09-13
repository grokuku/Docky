# Réponse de Docky au contrat d'intégration v1.0

- **Objet** : Réponse formelle de Docky au contrat d'intégration Docky ↔ Homy v1.0
- **Destinataire** : Équipe projet Homy
- **Émetteur** : Équipe Docky
- **Date** : 2026-09-13
- **Statut** : **Accepté et implémenté** (Lots A + B terminés)
- **Périmètre** : Façade REST dédiée, versionnée et server-to-server exposée par
  Docky pour la lecture de l'inventaire (agents, conteneurs), la santé, les
  statistiques et les actions `start` / `stop` / `restart`. Cette réponse décrit
  la surface **réellement disponible dans le code**, et non une intention.

---

## 1. Synthèse / verdict

Docky **accepte le contrat d'intégration v1.0** tel que proposé par Homy. La
surface complète du contrat (Lot A lecture + Lot B stats/actions) est
**implémentée et couverte par des tests automatisés**.

Deux écarts, volontaires et assumés, subsistent par rapport au document
d'origine. Ils sont **non bloquants** pour Homy et n'exigent qu'une
configuration de base URL côté Homy :

1. **Chemin préfixé.** Docky expose la façade sous `/api/integration/v1/...`
   au lieu des chemins nus `/api/...`. La surface est **dédiée et versionnée**,
   et le préfixe `/api/*` est déjà réservé à l'API navigateur (cookie de session
   JWT + CSRF). **Seule la base URL change** côté Homy ; les formes de réponse
   sont celles du contrat.
2. **Vocabulaire normalisé côté Docky.** Docky normalise les libellés Docker
   (par ex. un `state` inconnu devient `unknown`, un `health` `null` devient
   `none`, le paramètre `{container}` accepte le **nom OU l'id**). Cette
   normalisation rend le contrat déterministe et évite de propager des libellés
   Docker non stables.

Aucun autre écart fonctionnel. Les codes HTTP, l'enveloppe d'erreur
`{error, code}`, les unités et les sémantiques de batch sont conformes.

---

## 2. Décisions sur les 10 questions ouvertes (Q1 → Q10)

| # | Réponse de Docky | Justification courte | Impact Homy |
|---|------------------|----------------------|-------------|
| **Q1** | **Surface orchestrateur dédiée** (`/api/integration/v1`) + **clé Bearer dédiée**, gérée depuis l'UI Settings (afficher / copier / régénérer). | Isoler l'intégration du serveur MCP et de l'API navigateur ; clé à cycle de vie propre. | Utiliser la clé d'intégration (≠ clé MCP), header `Authorization: Bearer <clé>`. |
| **Q2** | **Normalisation** : `state` inconnu → `unknown`, `health` `null` → `none`, `{container}` accepte **le nom OU l'id** Docker. | Rendre la sortie déterministe quelle que soit la version du démon Docker. | Comparer les valeurs à l'énumération fournie (§5), ne pas supposer un libellé Docker brut. |
| **Q3** | **Réponse synchrone `200`** portant l'**état résultant** (`state` + `health`) après action. | L'appelant obtient l'état final en un seul aller-retour. | Lire `state`/`health` dans la réponse de l'action ; ne pas re-requêter systématiquement. |
| **Q4** | **`cpu_percent` normalisé 0–100 hôte**, + `cpu_percent_raw` (valeur agent multi-cœurs) + `cpu_count` (vCPU). | Un pourcentage hôte homogène est directement exploitable pour des jauges/alertes. | Utiliser `cpu_percent` (0–100) pour l'affichage ; `cpu_percent_raw`/`cpu_count` restent disponibles en transparence. |
| **Q5** | **Deux endpoints batch fournis** : santé (`POST /containers/health`) **et** stats (`POST /containers/stats`), **≤ 100 cibles**, **erreur par cible**. | Minimiser les allers-retours réseau tout en gardant un échec isolé par cible. | Grouper les cibles par lot (≤ 100) ; lire le tableau `results` dans **l'ordre des `targets`**. |
| **Q6** | `network_rx` / `network_tx` = **octets cumulés** ; `disk_usage` / `disk_limit` / `disk_percent` = **`null`** (non fournis). | Le réseau est disponible via `docker stats` ; le disque ne l'est pas. | Ne pas s'appuyer sur des stats disque ; traiter `disk_*` comme `null` (non mesuré). |
| **Q7** | `{error, code}` **partout** ; en batch, un agent injoignable → **`200` + erreur par cible** ; hors batch → **`502`**. | Un agent en panne ne doit pas casser tout un lot ; hors lot, l'échec est global. | Ne jamais interpréter un lot `200` comme « tout est OK » : inspecter `error` par cible. |
| **Q8** | Surface **versionnée `/v1`**. | Autoriser une évolution cassante future sous `/v2` sans casser les clients `/v1`. | Épingler la base URL sur `/api/integration/v1`. |
| **Q9** | `id` d'un conteneur **change à la recréation** → **stocker le `name`** comme clé stable. | Docker recrée un conteneur avec un nouvel id ; le nom est stable. | Persister le `name` (et le couple `agent`+`name`) comme identifiant logique ; ne pas se fier à l'`id` sur la durée. |
| **Q10** | **Pas de rate limiting pour l'instant** ; un `429` avec `Retry-After` pourra être ajouté ultérieurement. | Priorité à la simplicité et à la stabilité de la v1. | Ne pas dépendre d'un `429` ; prévoir de gérer un futur `429`/`Retry-After` (back-off). |

---

## 3. Surface d'intégration exposée

**Base URL** : `https://<docky>/api/integration/v1`
**Auth** : header `Authorization: Bearer <clé d'intégration>` sur **chaque**
requête. Les chemins `/api/integration/` sont exemptés du double-submit CSRF :
les requêtes doivent uniquement porter le Bearer (pas de cookie, pas de jeton
CSRF).

**Bornes communes (batchs)** :

- **≤ 100 cibles** par requête de batch. Au-delà :
  `400 {"error":"Too many targets (max 100)","code":"invalid_request"}`.
- **Corps ≤ 256 Kio**. Au-delà :
  `400 {"error":"Request body too large (max 256 KiB)","code":"invalid_request"}`.

**Schéma d'authentification** :

| Cas | Statut | Corps |
|-----|--------|-------|
| Header `Authorization` absent | `401` | `{"error":"Invalid or missing API key","code":"unauthorized"}` |
| Token Bearer invalide | `401` | `{"error":"Invalid or missing API key","code":"unauthorized"}` |
| Header présent mais scheme ≠ `Bearer` | `403` | `{"error":"Forbidden","code":"forbidden"}` |
| Clé non configurée ou façade désactivée | `503` | `{"error":"Docky is not configured","code":"not_configured"}` |

### Endpoints du contrat (7) + batchs (2)

| Méthode | Chemin | Corps | Réponse `200` | Codes d'erreur |
|---------|--------|-------|---------------|----------------|
| `GET` | `/agents` | — | `{agents:[{name,url,status,version,lastCheck}]}` | `401` `403` `503` `not_configured` `503` `no_agents` |
| `GET` | `/agents/{agent}/containers` | — | `{agent, containers:[{id,name,image,state,health,stack,service}]}` | `404` `agent_not_found` · `502` `agent_unreachable` |
| `GET` | `/agents/{agent}/containers/{container}` | — | `{agent,container,id,name,state,health,checkedAt}` | `404` `agent_not_found` · `404` `not_found` · `503` `agent_offline` · `502` `agent_unreachable` |
| `GET` | `/agents/{agent}/containers/{container}/stats` | — | `{agent,container,checkedAt,state,health,cpu_percent,cpu_percent_raw,cpu_count,mem_usage,mem_limit,mem_percent,mem_cache,network_rx,network_tx,disk_usage:null,disk_limit:null,disk_percent:null}` | `404` `agent_not_found` / `not_found` · `503` `agent_offline` · `502` `agent_unreachable` · `504` `timeout` |
| `POST` | `/containers/health` | `{targets:[{agent,container}]}` | `{checkedAt, results:[...]}` (toujours `200`) | `400` `invalid_request` |
| `POST` | `/containers/stats` | `{targets:[{agent,container}]}` | `{checkedAt, results:[...]}` (toujours `200`) | `400` `invalid_request` |
| `POST` | `/agents/{agent}/containers/{container}/start` | — | `{success,agent,container,action,state,health}` | `404` `not_found`/`agent_not_found` · `409` `conflict` · `502` `agent_unreachable`/`action_failed` · `503` `agent_offline`/`not_configured` · `504` `timeout` |
| `POST` | `/agents/{agent}/containers/{container}/stop` | — | idem `start` | idem `start` |
| `POST` | `/agents/{agent}/containers/{container}/restart` | — | idem `start` | idem `start` |

> Les trois actions sont servies par la **même** route paramétrée
> `{action} ∈ {start, stop, restart}` ; une action inconnue renvoie
> `404 {"code":"not_found"}`.

### Formes JSON exactes (telles qu'implémentées)

**`GET /agents`**

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

**`GET /agents/{agent}/containers`**

```json
{
  "agent": "prod-1",
  "containers": [
    {"id": "a1b2c3", "name": "web", "image": "nginx:latest",
     "state": "running", "health": "healthy", "stack": "MyApp", "service": "web"}
  ]
}
```

**`GET /agents/{agent}/containers/{container}`** (conteneur unitaire, avec `checkedAt`)

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

**`POST /containers/health`** (batch health ; toujours `200`, ordre préservé)

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

**`GET /agents/{agent}/containers/{container}/stats`** (stats unitaires enrichies)

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

> Un conteneur **arrêté** répond `200` avec des compteurs à `0` et son
> `state`/`health` réels (ce n'est **pas** une erreur). Un conteneur inconnu
> (agent joignable) répond `404 not_found`.

**`POST /containers/stats`** (batch stats ; toujours `200`, ordre préservé)

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
     "state": "unknown", "cpu_percent": 0.0, "cpu_percent_raw": 0.0, "cpu_count": 1,
     "mem_usage": 0, "mem_limit": 0, "mem_percent": 0.0, "mem_cache": 0,
     "network_rx": 0, "network_tx": 0,
     "disk_usage": null, "disk_limit": null, "disk_percent": null,
     "error": {"code": "not_found",
               "message": "Container 'missing' not found on agent 'prod-1'"}}
  ]
}
```

> Détail important pour Homy : en cas d'échec sur une cible, le bloc de champs
> de la cible est **présent mais remis à zéro** (mêmes clés qu'en succès) —
> un consommateur peut lire chaque champ sans test conditionnel. Codes d'erreur
> par cible : `not_found`, `agent_unreachable`, `timeout`, `invalid_request`.

**`POST /agents/{agent}/containers/{container}/{start|stop|restart}`** (état résultant)

```json
{"success": true, "agent": "prod-1", "container": "web",
 "action": "stop", "state": "exited", "health": "none"}
```

> `state`/`health` sont **relus après** l'action. Un `restart` peut donc
> brièvement rapporter `running` avec `health` `starting` ou `none`.
> Idempotence : si le conteneur est déjà dans l'état cible, la réponse est
> `409 {"error":"Container is already running|stopped","code":"conflict","state":"..."}`
> (pré-vérification, **sans mutation**).

---

## 4. Table de correspondance des chemins

Les **formes de réponse** sont celles du contrat ; **seule la base change**.

| Contrat Homy | Docky (réel) |
|--------------|--------------|
| `GET /api/agents` | `GET /api/integration/v1/agents` |
| `GET /api/agents/{agent}/containers` | `GET /api/integration/v1/agents/{agent}/containers` |
| `GET /api/agents/{agent}/containers/{container}` | `GET /api/integration/v1/agents/{agent}/containers/{container}` |
| `GET /api/agents/{agent}/containers/{container}/stats` | `GET /api/integration/v1/agents/{agent}/containers/{container}/stats` |
| `POST /api/containers/health` | `POST /api/integration/v1/containers/health` |
| `POST /api/containers/stats` | `POST /api/integration/v1/containers/stats` |
| `POST /api/agents/{agent}/containers/{container}/start` | `POST /api/integration/v1/agents/{agent}/containers/{container}/start` |
| `POST /api/agents/{agent}/containers/{container}/stop` | `POST /api/integration/v1/agents/{agent}/containers/{container}/stop` |
| `POST /api/agents/{agent}/containers/{container}/restart` | `POST /api/integration/v1/agents/{agent}/containers/{container}/restart` |
| Header `Authorization: Bearer <clé>` | identique (clé **dédiée** Docky) |
| `{error, code}` | identique |

**Raison** : surface dédiée **versionnée** (`/v1`) sous `/api/integration/`,
séparée de l'API navigateur `/api/*` (réservée à l'UI web, cookie de session
JWT + CSRF). Côté Homy, il suffit donc de **configurer la base URL** sur
`https://<docky>/api/integration/v1` et de conserver le reste du contrat tel
quel.

---

## 5. Détails de normalisation (à implémenter côté Homy en lecture)

### 5.1 `state` (conteneur)

Valeurs exposées : `running | exited | paused | restarting | created | dead | unknown`.
Tout libellé Docker non listé (`removing`, `stopping`, …), vide ou inconnu est
ramené à **`unknown`**.

| Entrée Docker | `state` exposé |
|---------------|----------------|
| `running` | `running` |
| `Exited` / `exited` | `exited` |
| `paused` | `paused` |
| `restarting` | `restarting` |
| `created` | `created` |
| `dead` | `dead` |
| `removing`, `stopping`, vide, `null`, autre | `unknown` |

### 5.2 `health`

Valeurs exposées : `healthy | unhealthy | starting | none`.
`null` (pas de healthcheck configuré) et toute valeur inconnue → **`none`**.

| Entrée Docker | `health` exposé |
|---------------|-----------------|
| `healthy` | `healthy` |
| `unhealthy` | `unhealthy` |
| `starting` | `starting` |
| `none` / `null` / vide / autre | `none` |

### 5.3 Horodatage

`checkedAt` et `lastCheck` sont au format **ISO-8601 UTC suffixé `Z`**
(ex. `2026-09-13T14:55:52Z`). `lastCheck` vaut `null` si l'agent n'a jamais
été *ping*é.

### 5.4 Unités

- **Réseau** : `network_rx` / `network_tx` en **octets cumulés** (compteurs
  du conteneur, non remis à zéro à la lecture).
- **Mémoire** : `mem_usage`, `mem_limit`, `mem_cache` en **octets**.
- **Pourcentages** : `cpu_percent`, `mem_percent` en **0–100**, arrondis à
  2 décimales.

### 5.5 Formule mémoire

`mem_usage` **et** `mem_percent` sont calculés sur la **même** grandeur, à
savoir la mémoire **hors page cache** (comme `docker stats`) :

```
mem_usage = usage_cgroup - page_cache
```

où `page_cache` est `inactive_file` (cgroup v2) ou `total_inactive_file`
(cgroup v1), borné à `[0, usage]`. Le cache déduit est exposé via
`mem_cache`. Autrement dit, côté Homy :

```
mem_usage = usage - cache        (déjà appliqué par Docky)
mem_cache = cache                (valeur du cache déduit)
```

> Il faut utiliser `mem_usage` telle que fournie (elle exclut déjà le cache) et
> **ne pas** re-soustraire `mem_cache`.

### 5.6 CPU normalisé hôte

L'agent mesure un pourcentage **multi-cœurs** (0–N×100 %, ex. 0–800 % pour 8
vCPU). Docky expose :

- `cpu_percent` : **normalisé 0–100 hôte** = `cpu_percent_raw / cpu_count`,
  borné à `[0, 100]`, arrondi à 2 décimales ;
- `cpu_percent_raw` : valeur brute de l'agent (multi-cœurs) ;
- `cpu_count` : nombre de vCPU (≥ 1).

Homy doit privilégier `cpu_percent` (0–100) pour l'affichage et les seuils.

### 5.7 Disque

`disk_usage`, `disk_limit`, `disk_percent` sont **toujours `null`** : le disque
n'est pas exposé par `docker stats`. À traiter comme « non mesuré ».

---

## 6. Codes d'erreur

Le corps d'erreur est **toujours** de la forme `{"error": "...", "code": "..."}`
(jamais l'enveloppe navigateur `{"detail": ...}`).

| Statut | `code` | Comportement attendu côté Homy |
|--------|--------|-------------------------------|
| `401` | `unauthorized` | Header absent ou token invalide → corriger/renouveler la clé ; ne pas boucler. |
| `403` | `forbidden` | Header présent mais scheme ≠ `Bearer` → corriger le format du header. |
| `404` | `not_found` | Ressource (conteneur) inconnue → la considérer comme absente. |
| `404` | `agent_not_found` | Agent inconnu → ne pas réessayer cet agent. |
| `409` | `conflict` | Action déjà dans l'état cible (corps avec `state`) → traiter comme succès idempotent. |
| `400` | `invalid_request` | Requête malformée, > 100 cibles ou corps > 256 Kio → corriger la requête. |
| `502` | `agent_unreachable` | Agent en ligne mais injoignable (erreur de transport) → réessayer plus tard. |
| `502` | `action_failed` | L'agent a refusé/échoué l'action → journaliser, réessayer ou alerter. |
| `503` | `agent_offline` | Agent connu mais hors ligne → attendre son retour en ligne. |
| `503` | `not_configured` | Clé non configurée ou façade désactivée → contacter l'administrateur Docky. |
| `503` | `no_agents` | Aucun agent configuré côté Docky → état vide, pas une erreur applicative. |
| `504` | `timeout` | Délai dépassé côté agent (stats `15 s`, action `20 s`) → réessayer, réduire le lot. |

Rappels :

- En **batch** (`/containers/health`, `/containers/stats`), les échecs par cible
  sont **dans** la réponse `200`, jamais en statut global (`agent_unreachable`
  par cible, pas de `502` global). Seule une requête **malformée** renvoie
  `400 invalid_request`.
- **Hors batch**, un agent injoignable renvoie `502` (`agent_unreachable`).

---

## 7. Comment configurer (côté Homy)

1. **Obtenir la clé.** Dans l'UI Docky : **Settings → carte « API
   d'intégration »**. La carte permet d'**afficher** (bouton *Afficher*),
   **copier** (*Copier*) et **régénérer** (*Régénérer la clé*) la clé. La clé
   est affichée en clair ; ne la partager qu'avec des intégrations de confiance.
2. **Base URL à utiliser.**
   `https://<docky>/api/integration/v1`
3. **Header d'authentification.** Sur **chaque** requête :
   `Authorization: Bearer <clé d'intégration>`
4. **Clé dédiée.** Cette clé est **indépendante de la clé MCP** : les deux ont
   un cycle de vie et une rotation **séparés**. Régénérer la clé d'intégration
   invalide immédiatement les clients externes (dont Homy), qui doivent
   se reconnecter avec la nouvelle clé.

> Endpoints d'administration de la clé (côté UI navigateur, session JWT) :
> `GET /api/settings/integration` et `POST /api/settings/integration/regenerate`.
> Ces endpoints ne font **pas** partie de la surface server-to-server Homy.

---

## 8. Comportements garantis

- **Batch partiel** : un agent en panne (hors ligne, injoignable, inconnu) ou un
  conteneur introuvable **ne casse pas** le lot ; seule sa cible porte l'erreur.
- **Ordre des `results` = ordre des `targets`** (indexation strictement
  préservée, y compris en présence d'erreurs par cible).
- **Erreur par cible** : chaque slot de `results` porte un champ
  `error` (`null` en succès) avec un `code` et un `message`.
- **Idempotence des actions** : `start` sur `running` et `stop` sur
  `exited`/`dead`/`created` renvoient `409 conflict` (état déjà atteint), sans
  mutation ; une action réellement appliquée renvoie `200`.
- **Timeouts bornés** : stats `15 s`, action `20 s` ; au-delà → `504 timeout`.
  Le timeout d'action (20 s) reste sous le budget global de requête (30 s).
- **Pas de secret dans les réponses ni dans les logs** : la façade ne renvoie
  jamais la clé, et ne journalise pas de secret.
- **Pas de fetch d'URL arbitraire** : Docky n'appelle que les agents qu'il
  connaît (base unique configurée + clé), jamais une URL fournie par l'appelant.

---

## 9. Non couvert / optionnel (honnête)

- **Action `update` d'image** : non exposée par la façade. L'agent Docky
  implémente déjà `POST /agent/containers/{id}/update-image` (en flux SSE),
  mais elle n'est **pas** reprise côté intégration à ce stade.
- **Webhooks / événements Docky → Homy** : non spécifiés et non implémentés.
  L'intégration est, pour l'instant, **tirée** par Homy (pull).
- **Stats disque** : indisponibles via `docker stats` → champs `disk_*`
  toujours `null`.
- **Rate limiting** : non implémenté pour l'instant. Un `429` avec en-tête
  `Retry-After` pourra être ajouté ultérieurement ; Homy devrait prévoir de le
  gérer (back-off) sans en dépendre aujourd'hui.

---

## 10. Tests & garanties de stabilité

- **Tests automatisés** : la façade est couverte par
  `orchestrator/tests/test_integration_api.py`, soit **60 fonctions de test
  (77 cas collectés, dont 3 paramétrées)** couvrant : authentification
  (`401`/`403`/`503`), exemption CSRF, normalisation (`state`, `health`,
  `cpu_percent`), les endpoints Lot A (agents, conteneurs, conteneur unitaire,
  batch health), le Lot B (stats unitaires, batch stats, actions) et les
  endpoints Settings de la clé d'intégration.
- **Smoke HTTP** : la suite pilote l'application FastAPI **réelle** via un
  client HTTP in-process (routes, dépendance d'auth Bearer, exemption CSRF
  réelles), ce qui constitue un smoke automatisé de bout en bout. Un smoke
  manuel en conditions réelles peut être rejoué avec :

  ```bash
  curl -H "Authorization: Bearer <clé>" https://<docky>/api/integration/v1/agents
  curl -X POST -H "Authorization: Bearer <clé>" -H "Content-Type: application/json" \
    -d '{"targets":[{"agent":"prod-1","container":"web"}]}' \
    https://<docky>/api/integration/v1/containers/health
  ```
- **Compatibilité ascendante** : engagement sur `/v1`. Toute évolution
  **cassante** de la surface irait sous un nouveau préfixe **`/v2`** ; la
  surface `/v1` reste stable pour les clients existants.

---

*Document établi à partir de l'implémentation réelle (Lots A + B) — toute
divergence constatée en production doit nous être signalée afin de mettre à
jour ce document et le contrat.*
