# Améliorations des menus contextuels (clics droits)

Trois améliorations liées aux clics droits dans le frontend de Docky.

## Feature 1 — Le menu contextuel se ferme au clic extérieur SANS cliquer sur ce qui est derrière

**Problème** : le menu contextuel container se fermait au clic ailleurs, mais le
clic pouvait aussi déclencher l'élément situé derrière (bouton, lien, `onclick`…).

**Correction** : dans `_attachContextMenuListeners()` (`orchestrator/app/static/js/dashboard.js`),
le clic extérieur est désormais intercepté en **phase de capture** sur `document` :

- un écouteur `mousedown` (bouton gauche uniquement, `e.button === 0`) bloque
  l'événement (`preventDefault()` + `stopPropagation()`) pour empêcher les
  interactions basées sur `mousedown` (drag, resizers…) de démarrer derrière le menu ;
- un écouteur `click` bloque l'événement (`preventDefault()` + `stopPropagation()`)
  pour que l'action de l'élément derrière (bouton, lien, `onclick`…) ne se déclenche
  pas, puis ferme le menu.

Le menu ne se ferme **pas** sur le `mousedown` (sinon le `click` suivant ne serait
plus considéré comme « extérieur » et atteindrait l'élément derrière). Les clics
**dans** le menu (`menu.contains(e.target)`) ne sont jamais bloqués, et un clic
droit (bouton 2) reste libre pour ouvrir un nouveau menu ailleurs.

## Feature 2 — Clic droit sur la zone des ports → correspondances host:container

La zone d'affichage des ports d'un container (badge `meta-ports` dans la vue liste
`renderContainers`, colonne `table-row-ports` dans la vue tableau `renderTableRow`)
accepte désormais un clic droit qui ouvre une petite popup listant proprement les
correspondances `port_hôte → port_container`.

- `openPortsContextMenu(event, containerId)` (`dashboard.js`) construit la popup à
  partir des ports du container (`c.ports`), formatés en lignes lisibles.
- Fermeture au clic extérieur (même mécanique de capture que la Feature 1), à Échap
  et au scroll via `_attachPortsContextMenuListeners()`.
- Élément HTML : `#ports-context-menu` (`templates/dashboard.html`), styles dans
  `style.css` (`.ctx-menu-title`, `.ctx-menu-port-row`, …).

## Feature 3 — Dans l'éditeur de container, clic droit sur un port hôte → ports libres

Dans l'onglet **Ports** de la modale d'édition, un clic droit sur un champ
`.edit-port-host` ouvre un menu proposant des ports hôtes libres pour le port
container de la ligne.

**Algorithme** (`openHostPortContextMenu` dans `modals.js`) :

1. Lit le port container de la ligne (`.edit-port-ctn`). S'il est invalide, un
   toast d'avertissement s'affiche.
2. Récupère les ports utilisés via `GET /api/ports?agent=all` (agrégation
   `agent_manager.get_all_ports` / `get_used_ports`). Si l'agent est injoignable
   (`data === null` ou exception), un message d'erreur clair s'affiche.
3. Construit les propositions (~10) :
   - le **prochain port libre** : on part du port container et on incrémente tant
     que le port est utilisé (ex. container 9000, si 9000–9003 pris → 9004) ;
   - le port container lui-même s'il est libre ;
   - les **variantes avec chiffres devant** : `port + 10000`, `+20000`, `+30000`, …
     (19000, 29000, 39000, …) tant qu'elles sont libres.
   - déduplication puis troncature à 10 options.
4. Cliquer sur une proposition remplace le port hôte dans le champ ciblé
   (`_setHostPort`).

Fermeture au clic extérieur, à Échap et au scroll via
`_attachHostPortContextMenuListeners()`. Élément HTML : `#host-port-context-menu`
(`templates/dashboard.html`).

## Fichiers modifiés

- `orchestrator/app/static/js/dashboard.js` — Feature 1, Feature 2
- `orchestrator/app/static/js/modals.js` — Feature 3
- `orchestrator/app/static/js/app.js` — enregistrement des écouteurs de fermeture
- `orchestrator/templates/dashboard.html` — éléments de menu `#ports-context-menu`
  et `#host-port-context-menu`
- `orchestrator/app/static/css/style.css` — styles des menus ports

## Vérifications

- `node --check` sur les trois fichiers JS modifiés : OK.
- `pytest -q` : 498 passed.
