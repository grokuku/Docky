# Refactor `orchestrator/app/llm/client.py` → sous-modules cohésifs

Objectif : découper `orchestrator/app/llm/client.py` (1 601 lignes) en modules
cohésifs sous `orchestrator/app/llm/`, SANS changement de comportement, en
gardant la suite de tests verte après chaque étape.

**Règle critique** (même principe que le refactor `agent/docker_manager.py`
réussi) : les tests monkeypatchent des symboles DANS le namespace
`app.llm.client` :
- `app.llm.client.agent_manager` (conftest `mock_agent_manager`,
  `test_llm_client.build_system_prompt_*`) ;
- `app.llm.client.LLMClient` / `build_system_prompt` / `execute_tool`
  (`test_llm_tools.run_chat_*`) ;
- `app.llm.client.firecrawl_search` / `firecrawl_scrape` / `firecrawl_map`
  (`test_llm_tools.web_*`).

Stratégie choisie = **(a) façade ré-export** : chaque symbole déplacé est
ré-exporté dans le namespace `app.llm.client`, et le code interne des
sous-modules qui appelle un symbole potentiellement monkeypatché le résout via
le namespace de la façade au moment de l'appel (helper `_client()`, résolution
tardive) afin que les patches des tests continuent de prendre effet et qu'aucun
cycle d'import ne soit créé. Aucun fichier de test existant n'est modifié.

## Rappels / dépendances vérifiées au départ
- `app.routes.api` importe depuis `app.llm.client` : `LLMClient`, `run_chat`,
  `read_soul`, `update_soul`, `execute_tool`, `build_system_prompt`, `TOOLS`,
  `HUMAN_VALIDATION_MARKER`.
- `app.routes.dashboard` n'importe RIEN depuis `app.llm.client`.
- Les tests importent en plus : `parse_compose_metadata`,
  `_format_container_ports`, `LLMClient`, `execute_tool`, `TOOLS`, `run_chat`.
- `agent_manager` est le singleton importé de `app.agent_manager.client`
  (instancié à l'import) — il doit rester LE MÊME objet (import direct, pas de
  nouvelle instance).
- `pyproject.toml` : `pythonpath = ["orchestrator", "."]` (ne pas revenir en
  arrière).

## État initial (baseline)
- `python -m pytest -q` → **287 passed** (10.12s)

## Étapes

---

## Étape 1 — Constantes + fonctions pures (TERMINÉE)

### Fichiers créés
- `orchestrator/app/llm/constants.py` — toutes les constantes partagées :
  `HUMAN_VALIDATION_MARKER`, `MAX_TOOL_ROUNDS`, `_DEFAULT_WEB_ENDPOINT`,
  `_TOOLS_DOCKER_AGENT_PARAM`.
- `orchestrator/app/llm/prompt.py` — `parse_compose_metadata` et
  `_format_container_ports` (fonctions pures). `build_system_prompt` y sera
  déplacé à l'étape 2.

### Fichiers modifiés
- `orchestrator/app/llm/client.py` : suppression de `import re` (devenu
  inutile), suppression des définitions locales de `parse_compose_metadata` et
  `_format_container_ports`, ajout de l'import/ré-export depuis
  `app.llm.prompt`.

### Tests
- `python -m pytest -q` → **287 passed**

---

## Étape 2 — Soul + system prompt (TERMINÉE)

### Fichiers créés
- `orchestrator/app/llm/soul.py` — `_soul_path`, `read_soul`, `update_soul`
  (helpers soul.md ; utilisés par prompt et tools).

### Fichiers modifiés
- `orchestrator/app/llm/prompt.py` : ajout de `build_system_prompt` (fonction
  complète). Elle résout `agent_manager` via `_client().agent_manager` au moment
  de l'appel (monkeypatchable par les tests), et importe `read_soul` depuis
  `app.llm.soul`. `parse_compose_metadata` / `_format_container_ports` restent
  dans le même module (appelés directement, non monkeypatchés).
- `orchestrator/app/llm/client.py` : suppression des helpers soul (section
  « Soul.md management ») et de la fonction `build_system_prompt` (203 lignes) ;
  ré-exports ajoutés depuis `app.llm.prompt` (`build_system_prompt`) et
  `app.llm.soul` (`_soul_path`, `read_soul`, `update_soul`) ; `from pathlib
  import Path` et `from app.config import get_data_dir` retirés (devenus
  inutiles : seul le bloc `read_compose_reference` les ré-importe localement).

### Tests
- `python -m pytest -q` → **287 passed**

---

## Étape 3 — Web (TERMINÉE)

### Fichiers créés
- `orchestrator/app/llm/web.py` — `_DEFAULT_WEB_ENDPOINT` (importé depuis
  `app.llm.constants`), `_get_web_endpoint`, `_firecrawl_headers`,
  `firecrawl_search`, `firecrawl_scrape`, `firecrawl_map`. Dépend de
  `app.config.load_settings` et `httpx` → aucun cycle.

### Fichiers modifiés
- `orchestrator/app/llm/client.py` : suppression du bloc « WebClaw / Firecrawl »
  (140 lignes) ; ré-exports depuis `app.llm.web` + `_DEFAULT_WEB_ENDPOINT`
  depuis `app.llm.constants`. À ce stade `execute_tool` est toujours dans
  client.py et résout `firecrawl_*` dans son propre namespace (les imports).

### Tests
- `python -m pytest -q` → **287 passed**

---

## Étape 4 — Tools (TERMINÉE)

### Fichiers créés
- `orchestrator/app/llm/tools.py` — `TOOLS` (liste des 30 outils),
  `_format_stack_result`, `execute_tool`. Généré par copie exacte des blocs de
  `client.py` (script python de génération, bornes vérifiées) puis adaptations :
  - `_TOOLS_DOCKER_AGENT_PARAM` et `HUMAN_VALIDATION_MARKER` importés depuis
    `app.llm.constants` (plus de définition locale) ;
  - `read_soul` / `update_soul` importés depuis `app.llm.soul` ;
  - `execute_tool` résout `agent_manager` et `firecrawl_*` via
    `client = _client()` au moment de l'appel (monkeypatchables).

### Fichiers modifiés
- `orchestrator/app/llm/client.py` : suppression des sections
  « Tool definitions » + « Tool executor » (874 lignes) et de la définition
  locale `MAX_TOOL_ROUNDS` ; ré-exports depuis `app.llm.tools` (`TOOLS`,
  `_format_stack_result`, `execute_tool`) et import complet des constantes
  (`HUMAN_VALIDATION_MARKER`, `MAX_TOOL_ROUNDS`, `_DEFAULT_WEB_ENDPOINT`,
  `_TOOLS_DOCKER_AGENT_PARAM`). `run_chat` reste dans client.py et résout
  `TOOLS` / `execute_tool` / `build_system_prompt` / `LLMClient` /
  `HUMAN_VALIDATION_MARKER` / `MAX_TOOL_ROUNDS` dans son propre namespace
  (les ré-exports) — les monkeypatchs `app.llm.client.<symbole>` des tests
  run_chat continuent de s'appliquer.

### Tests
- `python -m pytest -q` → **287 passed**

---

## Étape 5 — Nettoyage final de la façade + smoke test (TERMINÉE)

### Fichiers modifiés
- `orchestrator/app/llm/client.py` : docstring mis à jour (décrit la structure
  façade/sous-modules). Les imports sont tous soit utilisés par `LLMClient` /
  `run_chat`, soit des ré-exports volontaires (namespace identique).

### Fichier créé
- `orchestrator/tests/test_llm_modules_import.py` (NOUVEAU, 5 tests smoke) :
  importe tous les sous-modules, vérifie que la façade ré-exporte chaque
  symbole attendu, que `agent_manager` est le même singleton
  (`app.llm.client.agent_manager is app.agent_manager.client.agent_manager`),
  que `_client()` résout la façade sans cycle, que les constantes sont les
  mêmes objets partagés, et que `len(TOOLS) == 30`.

### Tests
- `python -m pytest -q` → **292 passed** (287 historiques + 5 smoke)

---

## État final / rapport

### Fichiers créés
| Fichier | Contenu |
|---|---|
| `orchestrator/app/llm/constants.py` | HUMAN_VALIDATION_MARKER, MAX_TOOL_ROUNDS, _DEFAULT_WEB_ENDPOINT, _TOOLS_DOCKER_AGENT_PARAM |
| `orchestrator/app/llm/prompt.py` | build_system_prompt, parse_compose_metadata, _format_container_ports + `_client()` |
| `orchestrator/app/llm/soul.py` | _soul_path, read_soul, update_soul |
| `orchestrator/app/llm/tools.py` | TOOLS (30 outils), _format_stack_result, execute_tool + `_client()` |
| `orchestrator/app/llm/web.py` | _get_web_endpoint, _firecrawl_headers, firecrawl_search, firecrawl_scrape, firecrawl_map |
| `orchestrator/tests/test_llm_modules_import.py` | 5 tests smoke (imports + ré-export + singleton + pas de cycle) |
| `docs/refactor-llm-client.md` | cette trace |

### Fichiers modifiés
- `orchestrator/app/llm/client.py` : **1 601 → 355 lignes** (façade +
  `LLMClient` + `run_chat` + ré-exports). Imports retirés : `re`, `Path`,
  `get_data_dir` (devenus inutiles dans la façade).
- `orchestrator/app/llm/__init__.py` : inchangé.

### Symboles déplacés par module
- **constants** : HUMAN_VALIDATION_MARKER, MAX_TOOL_ROUNDS,
  _DEFAULT_WEB_ENDPOINT, _TOOLS_DOCKER_AGENT_PARAM.
- **prompt** : build_system_prompt, parse_compose_metadata,
  _format_container_ports.
- **soul** : _soul_path, read_soul, update_soul.
- **tools** : TOOLS, _format_stack_result, execute_tool.
- **web** : _get_web_endpoint, _firecrawl_headers, firecrawl_search,
  firecrawl_scrape, firecrawl_map.

### Tests modifiés
Aucun test existant modifié (stratégie a — façade ré-export + résolution
interne via le namespace `app.llm.client`). Un nouveau fichier de smoke test
ajouté.

### Résultats pytest (étape par étape)
| Étape | suite complète |
|---|---|
| Baseline | 287 ✓ |
| 1 constants + fonctions pures | 287 ✓ |
| 2 soul + build_system_prompt | 287 ✓ |
| 3 web | 287 ✓ |
| 4 tools | 287 ✓ |
| 5 nettoyage + smoke | **292 ✓** (287 + 5) |

### Ce qui RESTE dans client.py (documenté, volontaire)
`LLMClient` (chat / chat_stream / is_configured / _headers) et `run_chat` :
le boucle agentic dépend des symboles monkeypatchés par les tests run_chat
(`LLMClient`, `build_system_prompt`, `execute_tool`) résolus dans le namespace
façade — les garder dans la façade garantit que ces patches continuent de
s'appliquer sans indirection.

### Points de blocage / incidents
1. **Script de génération tools.py** : bornes de slices excluant le délimiteur
   de fin (`]`) → `TOOLS` non fermé → `SyntaxError`. Corrigé en rendant les
   slices inclusifs et en ajoutant des assertions de bornes (`tools_block[-1]
   == "]"`). Détecté immédiatement par `py_compile`.
2. **Premier dump de contrôle** : une assertion comparait une chaîne à un
   élément de liste (bug du script, pas du code) — corrigé, extraction valide.
3. Aucun cycle d'import : vérifié en important les sous-modules dans plusieurs
   ordres (`app.llm.tools`, `app.llm.prompt`, `app.llm.web`, `app.llm.soul`,
   `app.llm.constants`, puis `app.llm.client` en dernier et en premier).

### Vérifications finales
- 21 symboles d'origine ré-exportés dans `app.llm.client` (smoke test) ✓
- `agent_manager` singleton unique (`app.llm.client.agent_manager is
  app.agent_manager.client.agent_manager`) ✓
- `_client()` résout la façade sans cycle ✓
- `python -m pytest -q` → **292 passed** ✓

La présente trace (`docs/refactor-llm-client.md`) contient l'historique
complet : chaque étape, fichiers créés/modifiés, symboles déplacés, tests et
résultats pytest, points de blocage.
