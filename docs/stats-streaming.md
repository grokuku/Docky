# Streaming de stats temps réel — Lots 1, 2 & 3 (agent + orchestrateur + frontend)

Ce document décrit l'implémentation du streaming de stats conteneurs :

- **Lot 1 (agent)** : `agent/stats_stream.py` + routes `/agent/stats/*` ;
- **Lot 2 (orchestrateur)** : `orchestrator/app/agent_manager/stats_stream.py`,
  la WS navigateur `WS /api/stats/stream`, le proxy d'historique et le hot set
  de la façade d'intégration.

Le **Lot 3 (frontend)** consomme le protocole WS décrit en §8.4.

## 1. Vue d'ensemble

`docker stats` n'a **pas** d'API batch ni stream-side par conteneur : chaque
container coûte un appel HTTP persistant `container.stats(stream=True)` au
démon Docker. Le module centralise ce coût et n'observe **que** les containers
explicitement surveillés (watch set).

```
orchestrateur ──POST /agent/stats/watch──►  watch set (TTL 30 s)
                                                  │
                            ┌─────────────────────┼─────────────────────┐
                            ▼                     ▼                     ▼
                    streamer(web)         streamer(db)          streamer(api)
                    stats(stream=True)    stats(stream=True)    stats(stream=True)
                            │  ~1 s               │                     │
                            ▼                     ▼                     ▼
                     ring buffer 900 pts   ring buffer           ring buffer
                            │
              ┌─────────────┼───────────────────────┐
              ▼             ▼                        ▼
        downsampler     broadcast WS          get_history()
        (1 pt/30 s)     (abonnés /stats/stream) (ring + sqlite)
              ▼
        sqlite samples (rétention 24 h)
```

### Watch set

- `POST /agent/stats/watch` ajoute des refs (`id` **ou** `name`) et rafraîchit
  leur TTL. L'appel est **idempotent** : une ref déjà surveillée conserve son
  streamer et voit seulement son `last_seen` remis à jour (résolution vers le
  `short_id` canonique, donc `web` et `abcdef123456` désignent la même entrée).
- Un sweeper léger (thread daemon, réveil toutes les ~5 s, pas de boucle
  aggressive) expire les entrées non rafraîchies depuis **30 s** et arrête leur
  streamer. Un orchestrateur qui se déconnecte sans `unwatch` ne laisse donc
  pas de souscriptions Docker tourner indéfiniment.
- `POST /agent/stats/unwatch` retire explicitement des entrées (arrêt immédiat
  du streamer).
- **Aucune reprise au boot** : le watch set n'est pas persisté. Après un
  redémarrage de l'agent, rien n'est streamé tant qu'un orchestrateur ne
  rappelle pas `/stats/watch`.

### Streamer (une souscription persistante par container)

`StatsStreamer` lance un thread daemon par container surveillé :

1. `client.containers.get(id)` puis `container.stats(stream=True, decode=True)` ;
2. à chaque échantillon (~1 s) : construction du payload trimé, mise à jour du
   ring buffer, alimentation du downsampler, broadcast aux abonnés WS ;
3. si le stream se termine (container arrêté) ou échoue, reconnexion bornée
   (`STREAM_RETRY_SECONDS = 2 s`) tant que l'entrée est encore surveillée ;
4. à la sortie du watch set (unwatch ou expiration TTL) : le générateur Docker
   est fermé et le thread rejoint.

### Ring buffer

`deque(maxlen=900)` par container ≈ **15 min** à 1 échantillon/s (plancher
imposé par Docker — voir §6). C'est la partie fine (1 s) de l'historique.

### Persistance downsamplée

- Un point **toutes les 30 s** (`DOCKY_STATS_DOWNSAMPLE_SECONDS`) est écrit
  dans `sqlite3` (`<data_dir>/stats_history.db`) par un **thread writer
  dédié** alimenté par une file : les streamers ne bloquent jamais sur l'I/O.
- La clé primaire `(container_id, ts)` + la barrière d'intervalle garantissent
  l'absence de doublon.
- Purge opportuniste (toutes les 5 min et au premier réveil du writer) des
  points plus vieux que la rétention (`DOCKY_STATS_RETENTION_HOURS`, défaut
  **24 h**).

### Broadcast WebSocket

`WS /agent/stats/stream` (auth `verify_api_key_ws`) enregistre un abonné
(`asyncio.Queue`, taille 200). Le broadcast se fait depuis les threads
streamers via `loop.call_soon_threadsafe(...)` ; si l'abonné ne suit pas, le
point le plus ancien est jeté (jamais de blocage du streamer). Se déconnecter
retire **uniquement** l'abonné : le watch set continue de tourner.

## 2. Payload d'un échantillon (trimé)

```json
{
  "ts": 1700000000000,
  "id": "a1b2c3d4e5f6",
  "name": "web",
  "state": "running",
  "cpu_percent": 42.1,
  "cpu_count": 4,
  "mem_usage": 12345678,
  "mem_limit": 1073741824,
  "mem_percent": 1.15,
  "mem_cache": 4096,
  "net_rx": 1234,
  "net_tx": 5678
}
```

- `cpu_percent` : **brut multi-cœurs** (`(cpu_delta/system_delta) * cpu_count
  * 100`, ex. 0–800 %), identique à
  `agent.docker_manager.get_container_stats`. C'est à la façade d'intégration
  (Lot 2) de normaliser en 0–100 hôte (`/ cpu_count`).
- `mem_usage` / `mem_percent` : basés sur `usage − page cache`, comme
  `docker stats` ; `mem_cache` expose le cache déduit.
- `net_rx` / `net_tx` : somme agrégée des interfaces (pas de détail par
  interface).
- Pas de tableaux per-CPU / per-interface, pas de stack/service : payload
  volontairement compact pour un flux 1 Hz.

Le calcul réutilise `docker_manager._stats_from_raw()` (extrait de
`_container_stats_payload`) : la formule est partagée entre le one-shot et le
flux, aucune divergence possible.

## 3. Protocole WebSocket `/agent/stats/stream`

Auth : header `Authorization: Bearer <clé>` (préféré) ou query `?api_key=<clé>`.

Messages serveur → client (JSON) :

1. **Snapshot initial** (immédiatement après connexion) :

   ```json
   {"type": "snapshot", "samples": [ {échantillon}, ... ]}
   ```

   Une entrée par container actuellement surveillé ayant déjà produit un
   échantillon (permet d'afficher tout de suite sans attendre le prochain tick).

2. **Échantillons live** :

   ```json
   {"type": "sample", "sample": {échantillon}}
   ```

Aucun message n'est attendu du client (flux unidirectionnel). Le client doit
ignorer les types inconnus (extensibilité).

## 4. API REST (agent)

Auth API key (`Authorization: Bearer ...`) sur tous les endpoints ci-dessous.

| Méthode | Chemin | Corps / Query | Réponse |
|---------|--------|---------------|---------|
| `POST` | `/agent/stats/watch` | `{"containers": [id\|name, ...]}` (`ids` accepté) | `{"success": true, "watched": [short_id, ...]}` |
| `POST` | `/agent/stats/unwatch` | `{"containers": [id\|name, ...]}` | `{"success": true, "unwatched": [...], "watched": [...]}` |
| `GET` | `/agent/stats/watch` | — | `{"watched": [short_id, ...]}` |
| `GET` | `/agent/containers/{id}/stats/history` | `?window=900\|3600\|86400` | `{"container": "...", "window": 900, "points": [...]}` |

- `watch` renvoie les `short_id` canoniques effectivement surveillés (les refs
  inconnues sont ignorées silencieusement).
- `history` : `400` si `window` non supportée, `404` si le container est
  inconnu. La série est **fusionnée** : ring buffer mémoire (fin, ~1 s) +
  points persistés downsampleés (30 s) sur la fenêtre. En cas de collision de
  timestamp, la valeur mémoire (plus fraîche) l'emporte.
- Les points d'historique ne contiennent que
  `{ts, cpu_percent, mem_usage, mem_limit, mem_percent, net_rx, net_tx}`.
- Les endpoints existants `/agent/containers/{id}/stats` et
  `/agent/containers/stats` (batch) restent **inchangés**.

## 5. Schéma SQLite

Fichier : `<data_dir>/stats_history.db` (volume persistant de l'agent).

```sql
CREATE TABLE samples (
    container_id TEXT    NOT NULL,
    ts           INTEGER NOT NULL,   -- epoch ms
    cpu_percent  REAL    NOT NULL,
    mem_usage    INTEGER NOT NULL,
    mem_limit    INTEGER NOT NULL,
    mem_percent  REAL    NOT NULL,
    net_rx       INTEGER NOT NULL,
    net_tx       INTEGER NOT NULL,
    PRIMARY KEY (container_id, ts)
);
CREATE INDEX idx_samples_container_ts ON samples (container_id, ts);
```

Rétention : les lignes `ts < now − DOCKY_STATS_RETENTION_HOURS` sont purgées.

## 6. Variables d'environnement

| Variable | Défaut | Rôle |
|----------|--------|------|
| `DOCKY_STATS_RETENTION_HOURS` | `24` | Rétention de la persistance downsampleée (heures). |
| `DOCKY_STATS_DOWNSAMPLE_SECONDS` | `30` | Intervalle entre deux points persistés. |
| `DOCKY_DATA_DIR` | `/data` | Emplacement du fichier `stats_history.db`. |

Les valeurs invalides ou non positives sont ignorées avec un `warning` et les
défauts sont appliqués.

## 7. Limites et coût

- **Plancher 1 s** : le démon Docker produit ses stats à ~1 Hz ; impossible de
  descendre plus bas.
- **Coût** : ~14 ms CPU / container / s mesuré pour une souscription
  persistante (lecture + calcul du payload), soit ~1,4 % d'un cœur pour 100
  containers surveillés. Le streaming ne doit donc être activé que sur les
  containers réellement affichés/observés.
- Le nombre de streamers est proportionnel au watch set ; le TTL de 30 s borne
  les fuites en cas de déconnexion brutale de l'orchestrateur.
- Le ring buffer est volatile : à l'arrêt du process, seul l'historique
  downsampleé (30 s) est conservé.

## 8. Orchestrateur (Lot 2)

### 8.1 Module `app.agent_manager.stats_stream`

Le module expose `AgentStatsStreamManager` (singleton `get_manager()` /
`set_manager()`), qui centralise :

- **une WS par agent** vers `/agent/stats/stream` (ouverte à la demande dès
  qu'un conteneur de cet agent entre dans le hot set, reconnectée avec backoff
  exponentiel `1 s → 30 s`, fermée quand plus rien n'est surveillé) ;
- le **hot set unifié** = conteneurs demandés par l'UI (tuiles visibles)
  ∪ conteneurs demandés par Homy ;
- le **dispatch** des échantillons vers le cache chaud et les clients
  frontend.

Tout tourne sur la boucle d'événements de l'application (pas de thread) ;
les seules coutures injectables sont le provider type `AgentManager`
(métadonnées + auth + HTTP) et `websockets.connect` (tests).

### 8.2 Hot set : refcount par source + TTL Homy

Chaque entrée du hot set porte un **refcount par source** :

- `ui` — abonnements navigateur ; incrémenté par `subscribe(agent, container,
  "ui")`, décrémenté par `unsubscribe(...)`. Pas de TTL : seul le
  désabonnement du dernier client libère l'entrée.
- `homy` — présence marquée par la façade d'intégration ; `touch` /
  `mark_hot_set` **ne s'incrémente pas** (idempotent) et rafraîchit un
  **TTL de 60 s**. Un sweeper (toutes les 5 s) retire les sources expirées.

Un conteneur reste donc surveillé tant qu'au moins une source est active ; le
runner de l'agent est arrêté uniquement quand le hot set de l'agent devient
vide. Un balayage Homy soutenu ne peut pas fuiter de watch (pas
 d'incrément).

### 8.3 Ré-armement du TTL agent (~20 s)

Tant qu'un agent a des conteneurs surveillés et la WS connectée, une boucle
`_rewatch_loop` renvoie `POST /agent/stats/watch` pour **tous** les refs
toutes les **20 s** (< TTL agent de 30 s). Quand une ref disparaît du hot set
(unsubscribe ou expiration Homy), un `POST /agent/stats/unwatch` explicite est
envoyé pour les refs retirées. À la (re)connexion, le watch set complet est
renvoyé immédiatement (le watch set agent n'est pas persistant au boot).

### 8.4 WS frontend `WS /api/stats/stream`

Auth JWT cookie (`_check_auth_ws`) ; le CSRF ne s'applique pas aux scopes
WebSocket. Le client n'est **jamais** exposé directement à l'agent :
l'orchestrateur agrège tous les agents dans un seul hub et filtre par cible.

Protocole client → serveur :

```json
{"type": "subscribe",   "targets": [{"agent": "prod-1", "container": "web"}]}
{"type": "unsubscribe", "targets": [{"agent": "prod-1", "container": "web"}]}
```

Protocole serveur → client (mêmes messages que l'agent) :

1. `{"type": "snapshot", "samples": [ {échantillon}, ... ]}` — envoyé juste
   après un `subscribe`, avec les derniers échantillons connus des cibles
   nouvellement ajoutées ;
2. `{"type": "sample", "sample": {échantillon}}` — à chaque tick live ;
3. `{"type": "error", "message": ...}` en cas de message malformé.

Les types inconnus sont ignorés. Les clients abonnés reçoivent **uniquement**
les échantillons de leurs cibles (filtrage côté orchestre). Un client lent ne
bloque jamais le flux : sa file est bornée (200) et l'échantillon le plus
ancien est jeté. À la déconnexion, toutes ses cibles UI sont libérées
(source `ui`).

`cpu_percent` est relayé **brut multi-cœurs** + `cpu_count` ; c'est le
frontend qui choisit la représentation (contrairement à la façade
d'intégration qui normalise en 0–100 hôte).

### 8.5 Historique

- **UI** : `GET /api/containers/{id}/stats/history?agent=&window=900|3600|86400`
  (JWT, comme les autres routes conteneurs) — proxy de l'agent ; `400` fenêtre
  non supportée, `404` conteneur inconnu, `502` agent injoignable.
- **Intégration** : `GET /api/integration/v1/agents/{agent}/containers/
  {container}/stats/history?window=...` (Bearer, enveloppe `{error, code}`).
  Le payload est transmis tel quel (`{container, window, points}`).

### 8.6 Robustesse

- types de messages WS inconnus ignorés ; JSON malformé toléré ;
- reconnexion agent avec **backoff exponentiel** et log ; un agent injoignable
  n'affecte pas les autres ;
- file par client bornée + drop du plus ancien (jamais de blocage) ;
- le hot cache de la façade d'intégration est **best-effort** : toute erreur du
  gestionnaire de stream retombe sur le snapshot live (jamais de 500) ;
- arrêt propre (`stats_stream.stop()`) branché sur le shutdown de l'application.

### 8.7 Cache chaud de la façade d'intégration

À chaque appel stats (unitaire ou batch), la façade enregistre les conteneurs
validés dans le hot set Homy (TTL 60 s). Si un échantillon chaud de **moins de
~2 s** existe pour la cible, la réponse est servie depuis le cache avec
`checkedAt` = horodatage de l'échantillon ; sinon le snapshot live `docker
stats` historique est utilisé. **Le contrat et les formes de réponse sont
inchangés**. Le batch mélange librement cibles chaudes (instantanées) et
cibles froides (un seul aller-retour par agent pour les froides).

## 9. Ce que doit faire le Lot 3 (frontend)

- Se connecter à `WS /api/stats/stream` (cookie JWT) et envoyer un `subscribe`
  par tuile visible (agent + conteneur) ; `unsubscribe` au démontage de la
  tuile.
- Afficher le `snapshot` initial dès réception, puis les `sample` live.
- Normaliser l'affichage CPU : `cpu_percent` brut + `cpu_count` sont fournis
  (ex. `cpu_percent / cpu_count` pour un pourcentage hôte 0–100).
- Gérer plusieurs fenêtres (15 min / 1 h / 24 h) via
  `GET /api/containers/{id}/stats/history`.
- Se déconnecter proprement (fermeture WS) pour laisser l'orchestrateur
  libérer les cibles UI ; tolérer les messages de type inconnu et la perte
  d'échantillons (drop du plus ancien sur client lent).

## 10. Tests

**Lot 1 — agent** : `agent/tests/test_stats_stream.py` couvre — payload trimé,
downsampling (1 point/30 s, sans doublon), persistance SQLite
(écriture/lecture par fenêtre/purge), watch/unwatch (idempotence, TTL,
rafraîchissement, redémarrage de streamer), ring buffer (taille, ordre),
streamer (démarrage/arrêt, thread fermé), broadcast (snapshot + live +
désabonnement propre), `get_history` fusionné, et les routes (401 / 400 / 404,
WS snapshot + live).

**Lot 2 — orchestrateur** :

- `orchestrator/tests/test_stats_stream.py` — refcount UI/Homy, TTL Homy 60 s
  et rafraîchissement, une WS par agent (ouverture/fermeture), re-watch
  périodique ~20 s, reconnexion avec backoff exponentiel, ingestion des
  messages (snapshot/sample/type inconnu/JSON invalide), dispatch filté aux
  abonnés frontend, snapshot initial, libération des cibles UI au
  désabonnement, cache chaud frais vs périmé (`get_fresh_sample`).
- `orchestrator/tests/test_stats_routes.py` — proxy d'historique UI
  (401/200/400/404/502), protocole WS frontend (auth 4401, subscribe →
  snapshot, sample live, unsubscribe, message malformé, cibles invalides),
  historique d'intégration (200/400/404/502/503 + enveloppe `{error, code}`),
  et hot set de la façade (unitaire + batch, frais vs live, dégradation
  propre si le cache échoue).

**Lot 3 — frontend** :

- `orchestrator/tests/test_stats_frontend.py` — le dashboard référence
  `/static/js/stats.js` (servi 200, `text/javascript`), contient le sélecteur
  d'intervalle (`#stats-interval-select`) et l'indicateur (`#stats-live-
  indicator`), et redirige vers `/login` sans cookie JWT. Le protocole WS
  lui-même reste couvert par `test_stats_routes.py`.
- Validation navigateur headless (chromium, CDP) : 7 scénarios — abonnement
  des tuiles visibles, échantillon injecté → jauge CPU normalisée, sortie de
  viewport → `unsubscribe`, sparkline SVG initialisée depuis l'historique,
  clic tuile → modale graphique à courbes, sélecteur d'intervalle persisté
  après reload, onglet caché → abonnements libérés.

## 11. Lot 3 (frontend) — client de streaming

### 11.1 Module `static/js/stats.js` (`window.DockyStats`)

Fichier autonome (script classique, sans bundler ni dépendance), chargé en
**dernier** dans `dashboard.html`. Il consomme les APIs des modules existants
(`DockyFetch`, `DockyApp`, `HolafModal`) mais ne modifie aucune brique.

API publique :

| Méthode | Rôle |
|---------|------|
| `subscribe(agent, container)` / `unsubscribe(...)` | abonnement programmatique (refcount local) |
| `onSample(cb)` | callback `(evt)` sur chaque échantillon rendu ; retourne la désinscription |
| `getLast(agent, container)` | dernier échantillon reçu |
| `setInterval(ms)` | `1000` / `2000` / `5000` / `0` (pause) — persisté en `localStorage` |
| `pause()` / `resume()` | bascule l'intervalle (via `setInterval(0)`) |
| `isConnected()` / `isPaused()` | état du flux |
| `openGraph(tileEl)` | ouvre la vue graphique (utilisé par le clic sur la tuile) |
| `reconcile()` | ré-apparie les tuiles visibles et les cibles abonnées |
| `getStatus()` | instantané d'état (diagnostic / tests) |

**Protocole côté client** : le module ouvre `WS /api/stats/stream` (même
origine, cookie JWT), puis envoie `subscribe` / `unsubscribe` par **lots
debouncés (50 ms)** afin d'éviter un message par tuile. Les cibles sont
dédupliquées par refcount : un même container visible dans plusieurs rendus
n'est souscrit qu'une fois. À l'ouverture, l'ensemble désiré complet est
renvoyé d'un coup (rattrapage après reconnexion). Les `snapshot` sont
ingérés comme les `sample` ; les types inconnus et le JSON malformé sont
ignorés.

**Reconnexion** : backoff exponentiel `1 s → 30 s` (plafonné). Après
`FAIL_THRESHOLD = 3` échecs consécutifs, l'indicateur bascule en mode
« polling » (le module continue néanmoins de retenter : la reprise est
automatique).

### 11.2 Abonnement par tuile visible

Les trois vues (liste, grille, tableau) portent `data-agent`,
`data-container` et `data-status` sur chaque tuile. Un `IntersectionObserver`
(`rootMargin: 100px`) souscrit à l'entrée et désabonne à la sortie ; la
visibilité est aussi évaluée **synchronement** (`getBoundingClientRect`) à
chaque `reconcile()` pour éviter un désabonnement/réabonnement à chaque
re-rendu 5 s. `reconcile()` est appelé après chaque rendu (liste/grille/
tableau) **et** par un `MutationObserver` sur `#dashboard-content` : le diff
entre tuiles visibles et cibles abonnées est appliqué par lots. Les
containers arrêtés/morts ne sont pas souscrits (`data-status`).

**Pause** :
- onglet caché (`visibilitychange`) → `unsubscribe` de tout, envoi immédiat
  puis fermeture de la WS ; au retour, reconnexion et réabonnement des tuiles
  visibles ;
- intervalle « Pause » → plus aucun abonnement mais la WS reste ouverte pour
  une reprise instantanée ; les jauges conservent la dernière valeur (ou
  « — » si aucune donnée).

### 11.3 Jauges, réseau et sparklines

Le **CPU** affiché est normalisé **0-100 hôte** :
`cpu_percent / cpu_count`, borné. La valeur brute (multi-cœurs) reste
accessible en infobulle (`title`), et `cpu_count` y figure. La **RAM**
(affiche + barre) provient de `mem_usage`/`mem_limit`/`mem_percent`. Le
**réseau** rx/tx est affiché en débit (delta des compteurs cumulés / dt),
formaté en B/s. Ces valeurs live **remplacent** celles du polling
(`loadContainerStats`) : `renderStats` n'écrit plus le DOM quand un
échantillon live frais existe (`DockyStats.shouldSuppressPolling`), ce qui
supprime le clignotement. Le polling reste actif et redevient la source
d'affichage dès que la WS est indisponible.

Chaque tuile reçoit (injecté par le module) un petit bloc `.docky-live` :
débits réseau + **sparklines SVG CPU/RAM** (≈ 40 points, fenêtre 15 min),
initialisées via `GET /api/containers/{id}/stats/history?window=900` au
premier affichage puis alimentées par les échantillons live. Rendu SVG fait
main, gradient léger, couleurs `#e94560` (CPU) et `#38bdf8` (RAM).

### 11.4 Vue graphique (clic tuile)

Un clic sur la zone stats d'une tuile (délégué en phase de capture, avant la
sélection de stack) ouvre une **HolafModal** (thème docky automatique) :
courbes **CPU %** et **RAM %**, sélecteur de fenêtre **15 min / 1 h / 24 h**,
valeurs **min / moyenne / max**, grille, axes horaires et infobulle au
survol. Les données viennent de `GET /api/containers/{id}/stats/history`
(1 s sur 15 min, 30 s sur 1 h/24 h) puis sont étendues par les échantillons
live tant que la modale est ouverte.

### 11.5 Intervalle réglable

Sélecteur discret dans l'en-tête du panneau Dashboard, à côté de
l'auto-refresh existant : **1 s / 2 s / 5 s / Pause**. La préférence est
persistée sous `localStorage['docky-stats-interval']`. Il **ne** remplace
pas l'auto-refresh (qui régit le rafraîchissement de la liste
containers/stacks — c'est un réglage indépendant) : les deux contrôles
coexistent sans interférence. L'intervalle borne la fréquence de **rendu**
(le flux agent reste à ~1 Hz) ; « Pause » coupe les abonnements et gèle
l'affichage.

### 11.6 Repli (fallback) et indicateur

L'ancien polling (`loadContainerStats` unitaire + batch agent) est conservé
comme repli. Un indicateur discret (`.stats-live-dot`) reflète l'état :
vert = WebSocket actif, orange = repli polling, gris = pause. Aucune
dépendance ajoutée : les graphiques/sparklines sont en SVG vanilla.

> Option notée pour plus tard : extraire une brique « chart » dans
> holaf-lib. À ne pas créer dans ce lot.
