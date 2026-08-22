# Chiffrement de la communication Orchestrateur ↔ Agent

> Traçabilité : sécurisation de la liaison `orchestrator/app/agent_manager/*`
> et `app/routes/containers.py` ↔ `agent/` (TLS par défaut, clé API hors URL
> pour les WebSockets, documentation du déploiement). Baseline pytest au
> démarrage : **392 passed**. Après les changements : **402 passed**
> (392 existants + 10 nouveaux, zéro régression).

## 1. Objectif et périmètre

Docky est composé de deux services qui échangent sur le réseau :

- **Orchestrateur** (FastAPI, `orchestrator/`) : interface web, LLM, pilotage
  global.
- **Agent(s)** (`agent/`) : accès Docker local, un agent par hôte.

L'orchestrateur communique avec chaque agent via :

- **REST / SSE** (httpx) : `{agent.url}/agent/*` avec en-tête
  `Authorization: Bearer <api_key>`.
- **WebSocket** : `/agent/events` (rafraîchissement événementiel),
  `/agent/containers/{id}/logs/stream` et `/agent/containers/{id}/exec`
  (proxy de console).

Ce document décrit comment **chiffrer** cette liaison et pourquoi la clé API
ne doit **jamais** transiter dans l'URL.

> Le chiffrement *de bout en bout* (mTLS au niveau applicatif, chiffrement des
> données stockées) reste un sujet de **déploiement / proxy** : ce document
> couvre ce qui est implémenté dans le code (TLS vérifié par défaut, CA
> personnalisée, clé en en-tête) et les meilleures pratiques d'infrastructure.

## 2. État implémenté dans le code

### 2.1 TLS vérifié par défaut (httpx REST/SSE)

Aucun `verify=False` n'existe dans le code. Par défaut **toute** connexion
httpx vers un agent vérifie le certificat TLS (`verify=True`), ce qui permet
déjà les URL `https://` en l'état.

Configurable **par agent** dans `data/settings.yaml` :

```yaml
agents:
  - name: "Serveur Local"
    url: "https://agent:8080"      # ou https://localhost:8080
    api_key: "change-this-to-your-agent-key"
    tls_verify: true                # défaut SÛR, ne JAMAIS mettre à false
    ca_cert: ""                     # CA personnalisée (chemin fichier PEM)
```

- `tls_verify` : défaut `true`. Si explicitement `false`, un `WARNING`
  explicite est journalisé au chargement et la vérification est désactivée
  (uniquement pour un réseau de confiance).
- `ca_cert` : chemin vers une CA personnalisée (certificat auto-signé /
  interne) utilisée comme bundle de confiance à la place du store système.

Ces options sont appliquées aux requêtes httpx (`_request`,
`_stream_request`, `ping_agent`) via `AgentManager._agent_tls_options`, et aux
connexions WebSocket via `AgentManager._agent_ws_ssl` /
`_agent_ws_connect_kwargs`.

### 2.2 Clé API hors URL pour les WebSockets

La clé API était auparavant passée en **query string** (`?api_key=...`) sur
les WebSockets orchestrateur → agent. Une URL peut être journalisée par un
proxy / reverse proxy / un accès logs, fuyant la clé.

Désormais l'orchestrateur envoie la clé via l'en-tête `Authorization: Bearer`
(pour `/agent/events`, `/containers/{id}/logs/stream` et
`/containers/{id}/exec`). Le changement est centralisé dans
`AgentManager._agent_ws_connect_kwargs` :

- `additional_headers={"Authorization": f"Bearer {api_key}"}`
- `ssl` = contexte TLS (CA personnalisée / opt-out explicite) quand nécessaire.

Côté agent, `verify_api_key_ws` accepte **déjà** l'en-tête `Authorization:
Bearer` et conserve le query param `api_key` comme **fallback de
compatibilité** pour les agents déjà déployés et les clients navigateur.

## 3. Options de chiffrement du déploiement

### 3.1 Reverse proxy TLS (recommandé, le plus simple)

Placez un reverse proxy (Traefik, Caddy, nginx, HAProxy) **devant l'agent** qui
termine le TLS et redirige vers le port interne de l'agent (ex. `:8080`).

- L'orchestrateur pointe vers `https://agent.domaine:8443` (ou le port du
  proxy).
- Le certificat peut être public (Let's Encrypt) ou interne (CA privée).
- Pour un certificat interne/auto-signé, renseignez `ca_cert` sur
  l'orchestrateur pour ne pas désactiver la vérification.

Exemple de service dans le compose de l'agent (Traefik) :

```yaml
services:
  docky-agent:
    ...
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.agent.rule=Host(`agent.domaine`)"
      - "traefik.http.routers.agent.entrypoints=websecure"
      - "traefik.http.routers.agent.tls.certresolver=letsencrypt"
      - "traefik.http.services.agent.loadbalancer.server.port=8080"
```

### 3.2 Réseau privé / VPN (WireGuard, Tailscale, overlay)

Ne pas exposer l'agent sur l'internet public du tout :

- **Réseau privé** : orchestrateur et agents sur le même réseau Docker overlay
  / VLAN privé ; l'orchestrateur pointe vers `http://agent:8080` (ou un nom
  d'hôte interne).
- **WireGuard / Tailscale / OpenVPN** : chaque hôte rejoint un tunnel privé
  (ex. `100.x.y.z`) ; l'orchestrateur pointe vers `http://<tailscale-ip>:8080`.
- Le trafic est chiffré par le tunnel ; la vérification TLS applicative peut
  rester active si le service interne expose du HTTPS.

Meilleure pratique combinée : **tunnel privé + TLS** pour la défense en
profondeur.

### 3.3 mTLS (mutual TLS) — avancé

Pour un chiffrement et une authentification mutuelle de bout en bout entre
l'orchestrateur et l'agent :

- Le reverse proxy / le service expose HTTPS exigeant un certificat client.
- L'orchestrateur fournit son certificat client (mTLS) et vérifie la CA de
  l'agent.
- httpx supporte `cert=(certfile, keyfile)` ; cela peut être ajouté à
  `_agent_tls_options` (champ `client_cert` / `client_key` à ajouter à la
  config agent) en phase 2.

### 3.4 Recommandations synthétiques

| Situation | Recommandation |
|---|---|
| Même machine / réseau Docker privé | `http://agent:8080` sur réseau overlay, pas de port exposé |
| Hôtes distants | Reverse proxy TLS **ou** WireGuard/Tailscale, `https://` |
| Certificat interne/auto-signé | `ca_cert: /path/ca.pem` (ne PAS mettre `tls_verify: false`) |
| Internet public | **À éviter** ; si nécessaire, reverse proxy TLS + VPN, clé API secrète |

## 4. Clé API en secret

- Générez une clé forte : `openssl rand -hex 32`.
- Ne **jamais** la committer dans `settings.yaml` / `.env.example`.
- Passez-la par variable d'environnement (`DOCKY_AGENT_API_KEY`) et injectez-la
  dans le `settings.yaml` de l'orchestrateur au runtime.
- Révocation : changez la clé sur l'agent et mettez à jour la config
  orchestrateur ; les connexions actives sont relancées (auto-reconnect).

## 5. Configuration de l'API agents (via l'interface)

Les endpoints `/api/settings/agents` (POST/PUT) persistent désormais
`tls_verify` (défaut `true`) et `ca_cert` (chaîne vide par défaut) pour chaque
agent, de sorte que les réglages TLS transitent par l'interface aussi bien que
par édition manuelle de `settings.yaml`.

## 6. Tests

Ajoutés dans `orchestrator/tests/test_agent_manager.py` (10 nouveaux) :

- défaut `tls_verify` = `true` au chargement ;
- `tls_verify: false` → `WARNING` journalisé + `verify=False` ;
- `ca_cert` chargé et passé à httpx (`verify=<chemin>`) ;
- `_agent_ws_connect_kwargs` envoie `Authorization: Bearer` sans `ssl` (ws) ;
- contexte `ssl.SSLContext` créé pour `ca_cert` et pour `tls_verify: false` ;
- `_connect_agent_events` connecte en WS avec la clé en **en-tête** et
  **sans** `api_key` dans l'URL.

Résultat : `timeout 300 .venv/bin/python -m pytest -q` → **402 passed** (392
existants + 10 nouveaux, 0 échec).

## 7. Écarts restants / hors scope

- **mTLS applicatif** : le champ `client_cert`/`client_key` n'est pas encore
  exposé dans la config agent (phase ultérieure).
- **Chiffrement de bout en bout des données au repos / en transit** relève du
  déploiement (proxy, tunnel, volumes chiffrés), pas du code applicatif.
- Le query param `api_key` reste accepté côté agent comme **fallback** pour
  compatibilité ; il doit être retiré de toute URL par l'orchestrateur (fait)
  et supprimé des logs des proxies.
