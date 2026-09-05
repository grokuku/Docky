# Protection des secrets `.env` vis-à-vis des LLM

## Problème

Les fichiers `.env` des stacks contiennent des secrets (clés API, mots de
passe, tokens…). Les LLM — le chat Docky **et** le serveur MCP — ne doivent
**jamais** avoir accès au **contenu** de ces fichiers. Ils peuvent en revanche
voir la **présence** du fichier (listing).

## Où le blocage est appliqué

Le blocage est centralisé dans `orchestrator/app/llm/tools.py`, dans
`execute_tool`. C'est le point de passage unique des deux LLM :

- le chat Docky appelle `execute_tool` via la boucle agentique
  (`app.llm.client.run_chat`) ;
- le serveur MCP (`app/mcp_server.py`) importe `execute_tool` depuis
  `app.llm.tools` et délègue chaque appel d'outil à cette fonction.

Bloquer ici couvre donc automatiquement les deux LLM.

## Outils concernés

| Outil | Comportement |
|-------|--------------|
| `read_stack_file` | Sur `.env` → renvoie le placeholder `[Contenu masqué : fichier .env (secrets)]` au lieu du contenu. L'agent n'est même pas interrogé. |
| `get_stack_files_with_content` | Outil **défensif** (non exposé dans `TOOLS` aujourd'hui, utilisé par l'UI/éditeur). S'il était appelé via `execute_tool`, le champ `content` du fichier `.env` est remplacé par le placeholder ; les autres fichiers restent visibles. |
| `modify_stack_file` | Écrit le nouveau contenu via `save_stack_file` et ne renvoie **jamais** le contenu courant au LLM. Le LLM peut écrire un nouveau `.env` sans jamais lire l'ancien. |
| `get_stack_files` | **Inchangé** : listing uniquement, la présence du `.env` reste visible, aucun contenu n'est renvoyé. |

## Détails d'implémentation

- `ENV_FILE_PLACEHOLDER = "[Contenu masqué : fichier .env (secrets)]"`
  (`orchestrator/app/llm/tools.py`).
- `_is_env_file(filename)` : retourne `True` si le nom est exactement `.env`,
  **insensible à la casse** (`.env`, `.ENV`, `.Env`…).
- `read_stack_file` : si `_is_env_file(filename)`, renvoie directement le
  placeholder **sans** appeler `agent_manager.get_stack_file` (aucune fuite,
  même côté agent).

## Ce qui n'est PAS bloqué (volontairement)

Le blocage est appliqué **uniquement au niveau des outils LLM**. Les fonctions
générales de lecture de fichiers de l'agent (`agent_manager.get_stack_file`,
`get_stack_files_with_content`, `save_stack_file`) restent intactes : l'UI /
l'éditeur continue de voir et d'éditer le `.env` normalement.

## Tests

Ajoutés dans `orchestrator/tests/test_llm_tools.py` :

- `read_stack_file` sur `.env` → contenu masqué, agent non interrogé ;
- masquage insensible à la casse (`.ENV`, `.Env`, `.env`) ;
- `read_stack_file` sur un fichier normal → contenu visible ;
- `get_stack_files_with_content` → `.env` masqué, autres fichiers visibles ;
- `get_stack_files` → `.env` listé (présence), sans contenu ;
- `modify_stack_file` sur `.env` → aucun contenu courant ne fuit.

Résultat : `498 passed` (492 existants + 6 nouveaux).
