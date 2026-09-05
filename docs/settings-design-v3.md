# Settings — Design v3 (2e passe d'amélioration)

Amélioration du design v2 (cards teintées par section). On garde l'ADN v2
(teintes sectionnelles, grille 6 colonnes, chips 34px, halo hero) et on affine
5 axes : contraste, en-têtes de cartes, surfaces imbriquées, états, micro-détails.

## 1. Tokens teintes (inchangés, à exposer en CSS vars par section)

| Section | base | --soft 12% | --ring 28% | --glow 16% |
|---|---|---|---|---|
| LLM | `#e94560` | `rgba(233,69,96,.12)` | `rgba(233,69,96,.28)` | `rgba(233,69,96,.16)` |
| Agents | `#a78bfa` | `rgba(167,139,250,.12)` | `rgba(167,139,250,.28)` | `rgba(167,139,250,.16)` |
| Historique | `#2dd4bf` | `rgba(45,212,191,.12)` | `rgba(45,212,191,.28)` | `rgba(45,212,191,.16)` |
| Sécurité | `#34d399` | `rgba(52,211,153,.12)` | `rgba(52,211,153,.28)` | `rgba(52,211,153,.16)` |
| MCP | `#38bdf8` | `rgba(56,189,248,.12)` | `rgba(56,189,248,.28)` | `rgba(56,189,248,.16)` |

## 2. Contraste / lisibilité

- **Labels** : `color #c0c0d8` → `#d6d6ea`, `font-weight 500` → `600`,
  `letter-spacing 0.01em`, `margin-bottom 6px` → `8px`.
- **Texte input** : `#eaeaea` → `#f2f2f7` ; **placeholder** `#8888a0` → `#6b6b85`
  (hiérarchie : placeholder plus discret que la valeur).
- **form-hint** : `#8888a0` → `#9aa0b8`, `line-height 1.5`, `margin-top 4px` → `6px`.
- **Titre de carte** : `1rem/600` → `0.9375rem/700`, `letter-spacing 0.02em`,
  `color #f2f2f7`.
- **Sous-titre de carte** (nouveau) : `0.75rem/400`, `color #9aa0b8`, `margin-top 2px`.

## 3. En-têtes de cartes (richesse)

- **Chip icône** : `34x34` → `38x38`, `radius 10px`, fond
  `linear-gradient(135deg, var(--soft), transparent 70%)`, bordure interne
  `1px solid var(--ring)`, ombre `0 0 0 1px rgba(0,0,0,.25), 0 4px 12px var(--glow)`.
  Icône `18px`, `color var(--base)`.
- **Barre d'accent supérieure** : `3px` dégradé vers transparent (conservé) +
  **ligne de reflet** `1px` `rgba(255,255,255,.06)` en haut de carte (effet verre).
- **En-tête 2 lignes** : titre + sous-titre descriptif par section
  (LLM « Modèle, endpoint & clés », Agents « Connexions & mappings de chemins »,
  Historique « Rétention des versions », Sécurité « Mot de passe »,
  MCP « Clé Bearer pour clients externes »).
- **Watermark** : icône lucide de la section en filigrane, `64px`, `opacity .08`,
  `color var(--base)`, positionnée en haut-droite de l'en-tête (pointer-events none).

## 4. Surfaces imbriquées

- **Agent-row** : `bg #1a1a2e` → `#1b1b33` (plus clair que la carte `#151527`),
  `border #2a2a4a`, `radius 12px`, **barre latérale gauche** `3px` `var(--base)`
  (dégradé vertical vers transparent), `padding 12px 14px` → `14px 16px`.
- **Avatar initiales** : `34x34`, `radius 10px`, fond `var(--soft)`, texte
  `var(--base)` `0.8125rem/700`, bordure `1px solid var(--ring)`.
- **Inputs** : `bg #1d1d36` (conservé) + **reflet haut** `inset 0 1px 0 rgba(255,255,255,.04)`.
- **Callout warning LLM** : `bg rgba(255,193,7,.1)` → `rgba(255,193,7,.08)`,
  `radius 6px` → `10px`, barre gauche `4px #ffc107` (conservée) + **chip ⚠**
  `24x24` `radius 8px` `bg rgba(255,193,7,.15)` aligné à gauche du texte.

## 5. Cohérence des états (hover / focus / active)

- **Focus inputs/selects** : `border-color var(--accent)` → `border-color var(--base)`
  + `box-shadow 0 0 0 3px var(--ring)` (ring teinté 3px, partout, y compris
  password, model-select, modal-textarea).
- **btn-primary** : hover `background var(--base)` + `box-shadow 0 0 0 3px var(--ring), 0 4px 18px var(--glow)`.
- **btn-ghost** : hover `border-color var(--base)` + `color var(--base)`.
- **btn-danger** : `bg var(--danger)` → `bg rgba(239,68,68,.12)`, `color #fca5a5`,
  `border 1px solid rgba(239,68,68,.35)` ; hover `bg rgba(239,68,68,.2)`.
- **Tous boutons** : `active { transform: translateY(1px) scale(.99) }`,
  `focus-visible { box-shadow 0 0 0 3px var(--ring) }`.
- **Cartes** : hover `translateY(-2px)` + `border-color var(--ring)` +
  ombre `0 10px 30px rgba(0,0,0,.35)` → `0 14px 40px rgba(0,0,0,.45)`.
- **Pills statut** : dot `7px` + halo `3px` `var(--soft)` (conservé) ; état
  `running` garde le pulse `docky-pulse`.

## 6. Micro-détails premium

- **Hero page** : titre `1.5rem/700` (conservé) + sous-titre `0.875rem/400`
  `#9aa0b8` ; halo radial `rgba(167,139,250,.10)` (conservé) + **2e halo**
  `rgba(233,69,96,.06)` en haut-droite (profondeur).
- **Scrollbar custom** : `10px` track transparent, thumb `#2a2a4a` `radius 999px`,
  hover `var(--base)` ; `scrollbar-width: thin`.
- **Rythme** : grille `gap 20px` → `22px` ; `settings-body padding 22px 24px` →
  `24px 26px` ; boutons `radius 8px` → `10px`, `padding 8px 16px` → `9px 18px`.
- **Transitions** : `0.2s ease` partout (conservé), ajout `transform` sur
  cartes/boutons dans la même transition.

## Règles de non-régression
- Aucun `id` modifié → `settings.js` intact.
- Les classes `.panel`, `.btn-*`, `.form-input`, `.status-indicator` restent
  partagées avec le dashboard : **scoper** les changements sous `.settings-panel`
  ou via les nouvelles classes `.settings-*` pour ne pas casser le dashboard.
- Vérifier `node --check settings.js` + `pytest -q` (492 tests) après implémentation.
