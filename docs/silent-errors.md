# Silent-error handlers → explicit logging (v0.0.4)

Objectif : remplacer les gestionnaires d'erreurs silencieux
(`except Exception: return []/{} / None / False / pass`) par une journalisation
explicite, **sans modifier les valeurs retournées ni le flux**, afin de
préserver les 402 tests (qui vérifient précisément que ces fonctions retournent
un fallback vide en cas d'exception).

Ajout de logs uniquement : la valeur de retour et le flux sont strictement
conservés. Niveau `WARNING` (lecture / récupération, impact modéré) ou `ERROR`
(mutation / écriture, impact fort), avec contexte (agent, container, stack,
fichier) mais **jamais** de secret (URL avec query string, clé API, token).

## Recensement / tri

Patterns cherchés : `except Exception`, `except:`, `except <Type>: return []/{} / None / False / pass`.

### Catégorie (a) — journalisés (masquent une panne réelle : réseau, Docker, fichier, parsing)

**orchestrator/app/agent_manager/client.py** (façade agent, appels HTTP réseau)
- `get_container` → `return None`
- `get_container_stats` → `return {}`
- `get_container_logs` → `return []`
- `exec_container` → `return {"success": False, "error": str(e)}`
- `stop_container` / `restart_container` → `return False`
- `check_update` → `return {"update_available": False, ...}` (exemple cité)
- `get_container_edit_spec` → `return None`
- `get_stack_files` → `return []`
- `get_stack_file` → `return None`
- `get_stack_files_with_content` → `return {"files": []}`
- `get_stack_history` → `return []`
- `get_stack_version` → `return None`
- `check_stack_update`, `restore_stack_version`, `update_git_history_settings`,
  `clean_agent` → retournent un dict d'erreur (désormais loggé)

Déjà journalisés avant cette passe (inchangés) : `get_containers`, `get_stacks`,
`get_ports`.

**orchestrator/app/agent_manager/cache.py**
- `_load_cache` → échec lecture/JSON du cache (reset) : `WARNING`
- `_save_cache` → échec écriture cache : `ERROR` (`logger.exception`)

**orchestrator/app/llm/prompt.py**
- lecture métadonnées compose (`get_stack_file`) dans `build_system_prompt` →
  `WARNING` (masquait une panne agent pendant la construction du prompt)

**orchestrator/app/llm/web.py** (Firecrawl/WebClaw, appels réseau externes)
- `firecrawl_search` / `firecrawl_scrape` / `firecrawl_map` → `WARNING` sur
  `HTTPStatusError` et `RequestError` (le message d'erreur était déjà retourné
  au LLM, mais aucune trace log)

**orchestrator/app/routes/settings.py**
- health-check agent → `agent_versions[name] = "unreachable"` : `WARNING`

**agent/docker_manager.py** (Docker daemon)
- `list_containers` → `return []` (DockerException)
- `_get_container_full_spec` → `return None`
- `_external_compose_info` → `return None, None` (DockerException)
- `_compose_project_services` → `return None` (échec parsing YAML)
- git commit de suppression de stack → `WARNING`

**agent/docker/update_check.py**
- `_local_repo_digests` → `return []` (lecture `image.attrs`)

**agent/docker/ports.py**
- scan Docker des ports → `WARNING` (repli sur le scan système)

**agent/routes.py**
- lecture contenu de fichier (`get_stack_files_with_content`) → `WARNING`

### Catégorie (b) — laissés silencieux (volontaire, chemin de contrôle normal / exception bénigne)

- `except ValueError` / `except json.JSONDecodeError` : validation / parsing
  bénin d'arguments (ex. `import_stack`, `chat.tool_args`, SSE chunks, ports hex).
- `except asyncio.CancelledError: break/pass` / `WebSocketDisconnect: pass` :
  arrêt normal de boucles WS.
- `except asyncio.QueueFull` / `QueueEmpty` : drop d'événement de back-pressure.
- `proc.kill()` / `proc.wait()` / `sock.close()` / `websocket.close()` / `gen.close()`
  en `finally` : nettoyage best-effort, rien d'utile à journaliser.
- `routes/*.py` `request.json()` → `JSONResponse(400, "Invalid JSON")` : réponse
  HTTP correcte au client (pas un masquage), laisser en l'état.
- `_load_agents` fallback `docker.from_env()` quand le socket unix échoue :
  repli volontaire (déjà loggé via `tls_verify` / pas de vrai masquage).
- `save_stack_file` `except Exception: pass` pour lire le payload JSON d'erreur :
  bénin, le message d'erreur est déjà propagé par le flux principal.
- `events._sanity_loop` `except Exception: pass` : redondant, car
  `_incremental_refresh` journalise déjà ses propres erreurs.
- `git_history.py` `return []` quand pas de dépôt git / pas de commits : état
  normal « pas d'historique ».
- `_local_repo_digests_for_image` `return []` (image introuvable) : cas normal
  « image absente ».

## Exemples de logs ajoutés (les plus représentatifs)

```python
# client.py — check_update (exemple cité dans la tâche)
except Exception as exc:
    logger.warning("check_update failed for agent '%s', container '%s': %s",
                   agent_name, container_id, exc)
    return {"update_available": False, "error": "Agent unreachable"}

# client.py — get_container (retour None)
except Exception as exc:
    logger.warning("get_container failed for agent '%s', container '%s': %s",
                   agent_name, container_id, exc)
    return None

# cache.py — _save_cache (échec écriture disque)
except Exception:
    logger.exception("Failed to persist cache to %s", self._cache_path)

# agent/docker_manager.py — list_containers (Docker daemon down)
except DockerException as exc:
    logger.warning("list_containers failed: %s", exc)
    return []

# llm/web.py — firecrawl_search (panne réseau Firecrawl)
except httpx.RequestError as exc:
    logger.warning("Firecrawl/WebClaw search request error: %s", exc)
    return f"[error] Firecrawl/WebClaw search request error: {exc}"
```

## Modules couverts

- orchestrator/app/agent_manager/client.py, cache.py
- orchestrator/app/llm/prompt.py, web.py
- orchestrator/app/routes/settings.py
- agent/docker_manager.py
- agent/docker/update_check.py, ports.py
- agent/routes.py

## Tests ajoutés

- `orchestrator/tests/test_agent_manager.py` : `test_check_update_failure_logs_warning`,
  `test_get_container_failure_logs_warning` (caplog) — 2 tests.
- `agent/tests/test_silent_errors.py` (nouveau) : `test_list_containers_docker_down_logs_warning`,
  `test_get_container_full_spec_docker_down_logs_warning` — 2 tests.

Aucun test existant modifié.

## Résultat pytest

`timeout 300 .venv/bin/python -m pytest -q` → **406 passed** (402 initiaux + 4
nouveaux), 3 warnings, ~40 s. Aucun test cassé : les valeurs retournées et le
flux sont identiques, seule la journalisation a été ajoutée.
