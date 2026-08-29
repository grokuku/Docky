# Update : progression de pull sur une seule ligne

## Problème
Lors d'une mise à jour de container / stack (`docker pull`, `docker compose pull`),
Docker émet une **cascade de lignes de progression** éphémères :
`Downloading …`, `Extracting`, `Waiting`, `Pulling fs layer …`, barres de
progression (avec pourcentages / tailles), etc. Ces lignes sont destinées à être
réécrites en place par le CLI docker (via `\r`, ou sous forme de lignes
distinctes quand la sortie n'est pas un TTY). Dans Docky chacune arrivait comme un
événement SSE `output` distinct et était **ajoutée** à l'Activity Modal, produisant
l'écran « cascade » encombrant.

## Cause racine
Le flux est : agent (`_run_command_stream` découpe en « lignes » via `readline`,
split sur `\n`) → Orchestrator (SSE `event: output`) → Frontend
(`dashboard.js` → `_streamAction` → `_appendActivity`, qui **append** un
`div.terminal-line` par événement).

Deux comportements docker produisent la cascade, selon le contexte (TTY ou non) :
- des `\n`-séparateurs donnant une ligne par trame de progression ;
- une trame unique contenant des `\r` (retour chariot) qui réécrit la même ligne.

Dans les deux cas, le rendu ajoutait de nouvelles lignes au lieu de réécrire la
même, d'où l'accumulation.

## Approche retenue : (b) traitement côté frontend
Nous avons retenu **l'option (b) : traitement dans le rendu d'affichage**
(`modals.js`, module `_streamAction`), et non un parsing agent.

Raisons :
- **Localisé** : le pipeline agent (compose_stream / docker_manager) est **inchangé**,
  donc les autres flux réutilisant ce pipeline (deploy, down, stop/start, logs,
  console, events) ne subissent **aucune régression**.
- **Robuste au format** : peu importe que docker émette des trames `\n` distinctes
  ou un bloc avec `\r`, la détection/consolidation frontend gère les deux.
- Le point d'émission de la cascade est exactement là où le texte est rendu
  (`_appendActivity` → `_streamAction`), réservé aux actions streamées d'update/deploy
  (pas aux websockets logs/console/events).

## Implémentation
Dans `orchestrator/app/static/js/modals.js` :

1. `_isProgressLine(raw)` — détecte une ligne de **progression éphémère** :
   - mots-clés : `Pulling fs layer`, `Downloading`, `Download complete`,
     `Extracting`, `Verifying Checksum`, `Waiting`, `Retrying`, `Already exists` ;
   - OU barre de progression pure : `[…]` + pourcentage/size.
   Les lignes *finales* utiles (`Status: Downloaded newer image …`, `Pull complete`,
   `Up-to-date`, `Digest: …`) ne matchent volontairement pas → conservées.

2. `_cleanProgressLine(raw)` — pour le cas `\r`, ne garde que la **dernière trame**
   visible de la ligne (retour chariot = réécriture en place).

3. `_appendProgressLine(raw, lastProgressEl)` — si une ligne de progression existe
   déjà, la **remplace en place** (`textContent` mis à jour) ; sinon en crée une.

4. Dans `_streamAction`, la branche `event === "output"` :
   - si `_isProgressLine` → `_appendProgressLine` (réécrit la ligne courante) ;
   - sinon → `_appendActivity` (nouvelle ligne « finale ») et remise à zéro de la
     référence de progression.

5. CSS (`orchestrator/app/static/css/style.css`) : classe `.terminal-line.progress`
   (couleur atténuée `--text-muted`, `white-space: pre-wrap`) pour distinguer la
   ligne vivante.

### Effet
- La progression (`Downloading X%`, `Extracting`, `Waiting`, défilement des
  trames) n'occupe **qu'une seule ligne** qui se met à jour à la volée.
- Les lignes finales utiles sont **préservées et ajoutées** dans l'ordre (`Pull
  complete`, `Status:`, `Digest:`, erreurs…).
- `done` final, statut de réussite/échec et cumul `output` inchangés.

## Ordre / préservation
L'ordre logique est conservé : les lignes non-progression restent appendues dans le
même ordre ; la ligne de progression s'intercale entre elles. L'événement `done`
final, le `result` et le résumé `_finishActivity` sont intacts. `output` accumule
toujours les lignes brutes (utilisé uniquement en fallback non-streamé).

## Fichiers modifiés
- `orchestrator/app/static/js/modals.js` (logique de consolidation)
- `orchestrator/app/static/css/style.css` (style `.terminal-line.progress`)

Aucun fichier Python modifié → le pipeline agent / parsing n'est pas touché.

## Généralisation à toutes les actions streamées

### Recensement des points d'affichage de sortie streamée
Toutes les actions streamées de Docky passent par **un seul point d'émission** :
`_streamAction` (`orchestrator/app/static/js/modals.js:143`), qui consomme le flux
SSE et rend chaque ligne dans l'Activity Modal. La consolidation y est donc déjà
appliquée de façon **transparente et uniforme** à toutes les actions :

| Action | Appelant | Endpoint SSE |
|---|---|---|
| `deploy` (stack) | `editor.js:550` | `/api/stacks/{name}/deploy` |
| `down` (stack) | `dashboard.js:1428` (`stackAction`) | `/api/stacks/{name}/down` |
| `start` / `stop` / `restart` (stack) | `dashboard.js:1428` (`stackAction`) | `/api/stacks/{name}/{action}` |
| `update` (stack) | `dashboard.js:1428` (`stackAction`) | `/api/stacks/{name}/update` |
| `update-image` (container) | `dashboard.js:1389` (`containerAction`) | `/api/containers/{id}/update-image` |
| `update-image` (update all) | `dashboard.js:2092` (`confirmUpdateAll`) | `/api/containers/{id}/update-image` |

Les seuls autres appels à `_appendActivity` (`dashboard.js:2090,2095,2102,2106`)
sont des lignes **informationnelles** (en-tête `[i/n] Mise à jour de …`, résumés
`✓`/`✗`) ajoutées par `confirmUpdateAll` autour des appels à `_streamAction` —
jamais des lignes de progression. Il n'existe **aucun autre consommateur SSE**
dans le frontend (pas de `EventSource`, pas d'autre `getReader`/`ReadableStream`).

### Factorisation
La logique de consolidation est **factorisée** dans `modals.js` et **réutilisée**
par `_streamAction` (aucune duplication) :
- `_isProgressLine` (`modals.js:70`) — détection des lignes éphémères ;
- `_cleanProgressLine` (`modals.js:57`) — gestion des `\r` ;
- `_appendProgressLine` (`modals.js:82`) — réécriture en place.

`_streamAction` (`modals.js:189-193`) est le **seul** endroit qui décide entre
`_appendProgressLine` (progression → une ligne) et `_appendActivity` (ligne
finale → nouvelle ligne). Comme toutes les actions passent par ce point, la
consolidation « une seule ligne » est **déjà active** pour deploy, down,
stop/start, restart, update et update-image. **Aucun changement de code n'a été
nécessaire** pour la généralisation : elle était déjà en place par construction.

## Logs : décision (ne pas appliquer)

### Comment les logs sont affichés
Les logs de container et la console sont rendus dans des **popups dédiées** via un
terminal xterm.js, et non dans l'Activity Modal :
- `orchestrator/templates/logs.html` — flux WS `/api/containers/{id}/logs/stream`,
  écriture par `_appendTerminalRaw` → `term.write(chunk)` (`logs.html:689-740`) ;
- `orchestrator/templates/console.html` — flux WS `/api/containers/{id}/exec`,
  écriture par `term.write` (`console.html:92-98`).

### Décision : **ne pas appliquer** la consolidation aux logs
Justification :
1. **Risque de faux positifs trop élevé.** Les logs sont une sortie applicative
   **arbitraire et continue** : une application peut légitimement écrire
   `Downloading …`, `Extracting …`, `Waiting …` ou une barre `[…] 50%` (téléchargement
   interne, extraction d'archive, etc.). Réécrire ces lignes en place masquerait
   de **vrais logs applicatifs** et dégraderait l'historique.
2. **Le cas `\r` est déjà géré nativement.** xterm.js est un émulateur de
   terminal : il réécrit déjà en place les trames séparées par `\r`. La
   consolidation n'apporterait rien pour ce cas dans les logs.
3. **Le cas `\n` (cascade) n'existe pas dans le flux de logs continu.** La
   cascade de lignes de progression docker ne survient que dans le contexte d'une
   action de pull/update, qui passe par `_streamAction` (déjà consolidé). Le flux
   de logs continu (`tail`/follow) ne transporte pas cette progression.
4. **Conservatisme.** La consigne est de n'appliquer une consolidation aux logs
   que si elle est « TRÈS conservatrice » et sûre. Ici le coût (masquer des logs
   légitimes) dépasse le bénéfice (quasi nul, car le terminal gère déjà `\r`).

**Conclusion :** la consolidation reste **réservée au contexte d'action**
(`_streamAction` / Activity Modal). Les flux logs/console/events restent **intacts**
(rendu terminal brut), conformément à l'approche (b) documentée plus haut.

## Tests
- Approche frontend uniquement : **aucun test pytest ajouté** (le parsing agent et
  les flux partagés restent intacts).
- `node --check` sur les JS (`modals.js`, `dashboard.js`, `editor.js`) : OK.
- Suite pytest : **417 passed** (zéro régression).
