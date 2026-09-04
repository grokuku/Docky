# Serveur MCP (Model Context Protocol)

Docky expose ses 30 outils existants à un LLM externe via un serveur **MCP**
(Model Context Protocol), construit sur **FastMCP 4.x** (le framework standard
au-dessus du SDK officiel `mcp` v2). Un client MCP (Claude Desktop, Cursor, tout
agent MCP-aware) peut ainsi piloter les containers, les stacks, la recherche
web et la mémoire persistante `soul.md` exactement comme la boucle de chat
agentique intégrée.

- Implémentation : `orchestrator/app/mcp_server.py`
- Montage HTTP : `orchestrator/app/main.py` (route `/mcp`)
- Tests : `orchestrator/tests/test_mcp_server.py`

---

## 1. Transports

### 1.1 Streamable HTTP (recommandé)

Le serveur est monté sur l'application FastAPI à la route **`/mcp`** et parle
le transport **Streamable HTTP** (mode `stateless_http`). Il est sécurisé par
une clé API Bearer (voir §3).

Exemple de connexion avec le SDK Python officiel :

```python
from mcp import ClientSession, StdioClientParameters
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client(
        "http://localhost:8000/mcp",
        headers={"Authorization": f"Bearer {API_KEY}"},
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool(
                "start_container",
                {"agent_name": "prod", "container_id": "web-1"},
            )
```

### 1.2 stdio

Pour un client local / CLI, le serveur peut être lancé sur **stdio** :

```bash
cd orchestrator
python -m app.mcp_server
```

La clé API n'est pas requise sur stdio (le transport est local et non
exposé sur le réseau).

---

## 2. Outils exposés

Les 30 outils de `app.llm.tools.TOOLS` sont ré-enregistrés tels quels. Chaque
appel MCP est délégué à `execute_tool` (résolu via la façade `app.llm.client`,
donc les mêmes chemins de code et la même gestion d'erreurs que la boucle de
chat).

### Outils sensibles (validation humaine)

`exec_in_container` et `clean_agent` sont **exposés** mais leur exécution
retourne le marqueur `__NEEDS_HUMAN_VALIDATION__` — exactement le même contrat
que la boucle de chat agentique. **Aucune commande destructive n'est jamais
exécutée automatiquement** par un LLM externe : le client MCP reçoit le
marqueur et doit déclencher une validation humaine hors-bande.

| Outil | Comportement MCP |
|-------|------------------|
| `exec_in_container` | Retourne le marqueur + agent/container/commande |
| `clean_agent` | Retourne le marqueur + `docker system prune -f` |

---

## 3. Sécurité

### 3.1 Clé API Bearer

Le transport HTTP exige un en-tête `Authorization: Bearer <clé>` sur chaque
requête. La clé est lue dans `settings.yaml` sous `security.mcp_api_key` :

```yaml
security:
  mcp_enabled: true
  mcp_api_key: <clé générée automatiquement>
```

- **Auto-génération** : si la clé est absente (déploiement existant mis à
  jour), `get_mcp_api_key()` en génère une aléatoire (`secrets.token_urlsafe`)
  et la persiste dans `settings.yaml` au premier accès.
- **Désactivation** : passer `security.mcp_enabled: false` désactive le
  contrôle Bearer (utile derrière un reverse-proxy qui gère déjà l'auth).
- **Rotation** : régénérer la clé en éditant `security.mcp_api_key` puis en
  redémarrant le serveur.

#### Clé dans l'UI

La page **Settings** (section « API MCP ») permet de gérer la clé sans éditer
`settings.yaml` à la main :

- **Affichage** : la clé est lue en clair via `GET /api/settings/mcp`
  (`{enabled, api_key}`) et affichée **masquée par défaut** ; un bouton
  « Afficher / Masquer » la révèle.
- **Copie** : un bouton « Copier » copie la clé dans le presse-papiers
  (`navigator.clipboard`).
- **Régénération** : le bouton « Régénérer la clé » appelle
  `POST /api/settings/mcp/regenerate`, qui génère une nouvelle clé
  (`secrets.token_urlsafe`), la persiste dans `settings.yaml` et la retourne.
  Une **confirmation** est demandée car cela **invalide immédiatement tous les
  clients MCP connectés** (ils doivent se reconnecter avec la nouvelle clé).

Ces deux endpoints sont protégés par l'authentification web (JWT + CSRF),
comme tous les autres endpoints `/api/settings/*`. L'état `enabled`
(`security.mcp_enabled`) est également affiché dans la section.

### 3.2 Périmètre

La clé MCP est distincte du JWT de session web et des clés d'API des agents.
Elle ne protège que la route `/mcp`. Les routes web existantes conservent leur
propre authentification (JWT + CSRF).

---

## 4. Architecture & intégration FastAPI

`app.mount("/mcp", mcp_app)` ne pilote **pas** le lifespan du sous-app
Starlette retourné par FastMCP. Le lifespan (qui démarre le session manager)
est donc entré/sorti explicitement depuis le startup/shutdown de l'application
parente :

```python
# main.py
_mcp_http_app = get_mcp_http_app()
app.mount(MCP_HTTP_PATH, _mcp_http_app, name="mcp")

@app.on_event("startup")
async def startup_event():
    global _mcp_lifespan
    ...
    _mcp_lifespan = get_mcp_lifespan(app)   # recréé à chaque startup
    await _mcp_lifespan.__aenter__()

@app.on_event("shutdown")
async def shutdown_event():
    if _mcp_lifespan is not None:
        await _mcp_lifespan.__aexit__(None, None, None)
```

> Le context manager est **recréé à chaque startup** : un objet `async with`
> ne se ré-entre pas (une seconde entrée lève `AttributeError`).

### Singletons paresseux

`build_mcp_server()` et `get_mcp_http_app()` construisent une seule fois le
serveur / le sous-app (singletons module). Le middleware d'auth résout la clé
**à la requête** (via `get_mcp_api_key()`) pour pouvoir être construit avant
que `settings.yaml` n'existe.

---

## 5. Conformité au protocole

- **Version du protocole** : le serveur négocie la version du client lors de
  `initialize` (compatible `2025-11-25` et au-delà).
- **SDK** : FastMCP 4.x, construit sur le SDK officiel `mcp` v2 (déclaré dans
  `orchestrator/requirements.txt`).
- **JSON-RPC 2.0** : tous les échanges suivent la spec MCP (initialize,
  notifications/initialized, tools/list, tools/call, etc.).

---

## 6. Tests

`orchestrator/tests/test_mcp_server.py` couvre :

- construction du serveur et enregistrement des 30 outils ;
- `tools/list` retourne les 30 outils ;
- appel d'outil via `execute_tool` avec `agent_manager` mocké ;
- marqueur de validation humaine pour `exec_in_container` / `clean_agent` ;
- génération / persistance de la clé API ;
- auth HTTP : rejet sans clé, rejet avec mauvaise clé, acceptation avec la
  bonne clé, et `tools/list` complet.

```bash
cd /projects/Docky
.venv/bin/python -m pytest orchestrator/tests/test_mcp_server.py -q
```
