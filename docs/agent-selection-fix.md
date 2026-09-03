# Agent selection/filter bug fix

## Contexte
Multi-agent : l'utilisateur peut sélectionner/désélectionner les agents via les
boutons de filtre (`_hiddenAgents`, persistés dans `localStorage`). Désélectionner
l'agent `local` alors qu'un **autre agent connecté, mais sans stack**, l'était aussi,
faisait afficher « Aucun agent affiché » (message de vue vide) — interprété par
l'utilisateur comme « aucun agent connecté ».

## Cause racine
La conclusion « aucun agent affiché / aucun agent connecté » ne reposait **que** sur
les stacks/containers. Dans le dashboard (mode grid/table), la vue était construite à
partir de `this.stacks` et `this._allContainersCache`, filtrés par `_hiddenAgents`.

Quand on désélectionne `local` :
- les stacks/containers de l'agent restant (connecté mais **sans stack**) sont vides ;
- les stacks de `local` (masqué) sont filtrées hors de la vue ;
- il ne reste plus aucun stack/container visible → `stackGroups.length === 0`
  → `_emptyViewMessage()` renvoyait « 🔇 Aucun agent affiché ».

L'agent restant, pourtant **connecté**, n'était donc pas pris en compte dans la
détermination de la vue vide.

- Fichier : `orchestrator/app/static/js/dashboard.js`
- Ligne du défaut : `_emptyViewMessage()` (retour « Aucun agent affiché »).

Point backend vérifié (non en cause) : `GET /api/agents` → `agent_manager.list_agents()`
(`app/agent_manager/client.py:124`) renvoie **tous** les agents configurés avec leur
statut réel (`ping_all()`), y compris un agent sans stack. La présence/statut d'un agent
ne dépend pas de l'existence d'une stack. Aucun correctif backend requis.

## Correction
Dans `dashboard.js`, la détermination « aucun agent » prend désormais en compte
tous les **agents sélectionnés (non cachés) et leur statut réel** :
- `_visibleAgents()` : agents présents dans `agentsList` et non masqués ;
- `_visibleConnectedAgents()` : parmi eux, ceux `online` / `connected` / `true`
  (mêmes critères que `renderAgentSelector`) ; **indépendant de la stack** ;
- `_emptyViewMessage()` : s'il existe au moins un agent sélectionné connecté, la vue
  vide affiche « ✅ N agent(s) connecté(s) sélectionné(s) » au lieu de
  « 🔇 Aucun agent affiché ».

Ainsi, désélectionner `local` laisse bien l'autre agent (connecté, sans stack) être
compté comme connecté/affiché.

## Vérification
- `node --check orchestrator/app/static/js/dashboard.js` → OK.
- Raisonnement « agent sans stack » : si `agentsList` contient par ex.
  `[{name:'local', status:'online'}, {name:'node2', status:'online'}]` et que
  `_hiddenAgents = {'local'}`, alors `_visibleAgents()` → `[node2]` et
  `_visibleConnectedAgents()` → `[node2]` → le message connecté s'affiche, même si
  `node2` n'a aucune stack/container. Inversement, si les seuls agents sélectionnés
  sont `offline`, l'ancien message « Aucun agent affiché » reste affiché.

## Fichiers modifiés
- `orchestrator/app/static/js/dashboard.js`
- `docs/agent-selection-fix.md` (ce document)
