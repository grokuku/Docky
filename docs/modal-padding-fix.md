# Correctif global : marges/padding des fenêtres modales

## Cause identifiée

Dans `orchestrator/app/static/css/style.css`, la classe `.modal-body` était
définie avec :

```css
.modal-body {
    flex: 1;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    padding: 0;      /* ← aucun padding par défaut */
    min-height: 0;
}
```

Le padding n'était appliqué que via la classe optionnelle `.modal-body-pad`
(`padding: 18px`). Or deux modales utilisaient `.modal-body` **sans** la classe
`modal-body-pad` :

- **Console** (`dashboard.html:157`) — correcte car ses éléments internes
  (`terminal-output`, `console-input-area`) portent leur propre padding.
- **Dialogue « Modifications non sauvegardées »** (`dashboard.html:336`) —
  contenu (`<p>`) collé aux bords, sans aucun padding.

Résultat : le contenu des modales sans `modal-body-pad` était collé aux bords,
et `overflow: hidden` pouvait couper le contenu qui débordait.

## Correctif CSS appliqué

Fichier : `orchestrator/app/static/css/style.css`

1. **`.modal-body`** reçoit désormais un padding par défaut cohérent
   (`18px`) et `overflow: auto` (au lieu de `hidden`) pour que le contenu ne
   soit plus collé aux bords ni coupé :

   ```css
   .modal-body {
       flex: 1;
       overflow: auto;
       display: flex;
       flex-direction: column;
       padding: 18px;
       min-height: 0;
   }
   ```

2. **Cas particulier console** : ajout d'un override pour éviter un
   double-padding (les éléments internes de la console portent déjà leur
   propre padding) :

   ```css
   .modal-console .modal-body {
       padding: 0;
   }
   ```

`.modal-body-pad` (padding `18px`, overflow `auto`) reste inchangé et continue
de s'appliquer (spécificité supérieure) — aucune régression sur les modales qui
l'utilisent déjà.

## Couverture de toutes les modales

| Modale | Classe body | Résultat |
|--------|-------------|----------|
| Console | `.modal-body` | padding `0` (override) — inchangé |
| Nouvelle stack | `.modal-body-pad` | padding 18px |
| Import stack | `.modal-body-pad` | padding 18px |
| Preview import | `.modal-body-pad` | padding 18px |
| Supprimer stack | `.modal-body-pad` | padding 18px |
| Down stack | `.modal-body-pad` | padding 18px |
| Permissions | `.modal-body-pad` | padding 18px |
| SOUL.md | `.modal-body-pad` | padding 18px |
| **Non sauvegardées** | `.modal-body` | **padding 18px (corrigé)** |
| Historique | `.modal-body-pad` | padding 18px |
| Éditer container | `.modal-body-pad` | padding 18px |
| Activité | `.modal-body-pad` | padding 18px |
| Version mismatch | `.modal-body-pad` | padding 18px |
| Update all | `.modal-body-pad` | padding 18px |
| WebUI | `.modal-body-pad` | padding 18px |
| Agent (form) | `.modal-body-pad` | padding 18px |
| Supprimer agent | `.modal-body-pad` | padding 18px |

Toutes les modales (listes, formulaires, confirmations) sont couvertes.

## Validation

- `node --check` : aucun fichier JS modifié (correctif purement CSS).
- `timeout 300 python -m pytest -q` → **492 passed** (zéro régression).
- Smoke TestClient :
  - `GET /dashboard` → **200**
  - `GET /settings` → **200**
