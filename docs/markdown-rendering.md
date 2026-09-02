# Rendu Markdown dans le chat

## Objectif

La sortie du LLM affichée dans le panneau de chat de Docky est désormais
interprétée en **Markdown** (titres, gras, italique, listes, blocs de code,
code inline, liens, citations…) au lieu d'être affichée en markdown brut.

Aucun rendu Markdown n'avait été introduit avant cette PR : `chat.js` ne
faisait qu'échapper le HTML puis remplacer les backticks par des `<code>`
minimalistes et transformer les sauts de ligne en `<br>`. Le contenu du LLM
s'affichait donc avec la syntaxe markdown brute visible (`**texte**`, `# titre`,
`` `code` ``, …).

## Où le chat rend les messages

- `orchestrator/app/static/js/chat.js`
  - `renderChatMessage(role, content)` insère chaque message dans le DOM du
    chat. La ligne clé : `bubble.innerHTML = this.markdownToHtml(content);`
    (remplace l'ancien `this.formatChatContent(content)`).
  - La méthode s'applique aux rôles `user`, `assistant` **et** `system` /
    `error` : le rendu est cohérent pour tous les types de message.

> Détail : le frontend chat n'utilise **pas** actuellement de flux streaming
> (le WebSocket `/chat/stream` existe côté backend mais n'est pas consommé par
> le frontend ; le chat appelle le POST non-streaming `/api/chat`). La sortie
> du LLM est donc rendue **une seule fois**, à la fin de la requête. Le
> renderer a néanmoins été conçu *stateless/pur* : il peut être rappelé à
> chaque chunk incrémental si un streaming est ajouté plus tard, sans perte de
> curseur ni de scroll.

## Renderer ajouté

Toutes les fonctions vivent dans `orchestrator/app/static/js/chat.js` (aucune
dépendance ni CDN externe, pas de bundler — JS vanilla).

| Fonction | Rôle |
|---|---|
| `markdownToHtml(md)` | Point d'entrée. Normalise les fins de ligne, protège les blocs de code, parse la structure en blocs (titres, HR, citations, listes, paragraphes) puis rend chaque contenu en inline sûr. |
| `_markdownInline(text)` | Échappe le contenu (anti-XSS) puis appelle `_markdownInlineEscaped`. |
| `_markdownInlineEscaped(text)` | Applique les styles inline sur du texte **déjà échappé** : code inline, liens, gras, italique, barré. |
| `_safeMarkdownLink(url)` | Whitelist de schémas pour les `href`. |

### Syntaxe Markdown couverte

- **Titres** : `#` … `######` → `<h1>`…`<h6>`.
- **Gras** : `**texte**` → `<strong>`.
- **Italique** : `*texte*` et `_texte_` → `<em>`.
- **Barré** : `~~texte~~` → `<del>`.
- **Code inline** : `` `code` `` → `<code>`.
- **Blocs de code** : `` ```lang … ``` `` → `<pre><code class="language-lang">`
  (sans coloration, simple bloc monospace ; le langage est optionnel).
- **Listes à puces** : `-`, `*`, `+` → `<ul>/<li>`.
- **Listes ordonnées** : `1.` → `<ol>/<li>`.
- **Listes de tâches** : `- [ ]` / `- [x]` → `<ul class="task-list">` avec
  case à cocher désactivée (`disabled`).
- **Liens** : `[texte](url)` → `<a href="…" target="_blank"
  rel="noopener noreferrer">`.
- **Citations** : `> texte` (lignes consécutives) → `<blockquote>`.
- **Lignes horizontales** : `---`, `***`, `___` → `<hr>`.
- **Paragraphes et sauts de ligne** : lignes consécutives regroupées en
  `<p>`, jointes par `<br>` ; une ligne blanche crée un nouveau paragraphe.
- Tout élément **inconnu / HTML brut** reste échappé et affiché tel quel.

CSS des éléments markdown dans le chat : ajouté dans
`orchestrator/app/static/css/style.css` (section « Markdown rendering inside
chat bubbles »).

## Sécurité XSS

Le contenu du LLM est **arbitraire** : il faut donc ne **jamais** injecter de
HTML brut. Mesures appliquées :

1. **Échappement systématique** — chaque contenu passé au rendu inline passe
   par `escapeHtml` (`&`, `<`, `>`, `"`, `'` → entités HTML) via
   `_markdownInline`. Un `<script>` dans la sortie du LLM devient
   `&lt;script&gt;…` et est affiché comme texte, jamais exécuté.
2. **Blocs de code échappés** — le contenu des blocs ``` est de nouveau
   échappé au rendu (`esc(f.code)`), ce qui le préserve en texte brut même
   s'il contient des balises.
3. **Whitelist de tags** — seuls des tags contrôlés sont générés :
   `h1–h6`, `strong`, `em`, `del`, `code`, `pre`, `p`, `ul`, `ol`, `li`,
   `blockquote`, `hr`, `a`, `input`. Pas d'`iframe`, pas de `script`, pas
   d'attribut `on*`.
4. **Liens sanitizés** — `_safeMarkdownLink` neutralise tout URL avec un
   schéma exécutable (`javascript:`, `vbscript:`, `data:`, `file:`) en le
   remplaçant par `#`. Les URL non-dangereux (http, https, mailto, ftp, tel,
   ancre, relatif) sont conservés. Chaque lien ouvert depuis le chat porte
   `target="_blank" rel="noopener noreferrer"`.
5. **Échappement de la langue de code** — l'attribut `class="language-…"` est
   construit à partir de la langue **échappée** (pas d'injection d'attribut).

Le renderer n'utilise jamais `innerHTML` sur du contenu non échappé : toutes
les valeurs interpolées sont soit échappées, soit générées par le renderer.

## Streaming

Comme indiqué plus haut, le frontend chat ne streame pas encore. Lorsqu'un
flux streaming sera branché, il suffira de rappeler `markdownToHtml` sur
l'accumulateur de texte à chaque chunk et de re-setter `bubble.innerHTML`.
Le renderer étant pur/stateless (aucun état global), le re-rendu est
idempotent ; il ne faut alors re-scroller qu'une seule fois par message pour
ne pas interrompre la lecture.

## Copie

Le chat Docky ne dispose pour l'instant **d'aucun** bouton « copie ». Si un
bouton était ajouté, il doit copier le **texte brut** (pas le HTML interprété),
par exemple en lisant `data.response` / le texte source plutôt que
`element.innerHTML`. Aucun changement de comportement de copie n'a été
introduit ici.

## Fichiers modifiés

- `orchestrator/app/static/js/chat.js` — remplacement de `formatChatContent`
  par le renderer markdown (`markdownToHtml`, `_markdownInline`,
  `_markdownInlineEscaped`, `_safeMarkdownLink`) et son intégration dans
  `renderChatMessage`.
- `orchestrator/app/static/css/style.css` — styles des balises markdown
  générées dans les bulles de chat.
- `docs/markdown-rendering.md` — ce document.

## Validation

- `node --check orchestrator/app/static/js/chat.js` → OK.
- Harness de tests standalone (renderer + XSS) → 17/17 OK.
- `python -m pytest -q` → 420 passed, 0 failed (aucune régression backend).
