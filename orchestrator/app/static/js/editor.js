/* ============================================================
   Docky - Frontend JavaScript - module editor
   ------------------------------------------------------------
   Extrait de app.js (refactor-app-js, v0.0.4). Aucun changement
   de comportement : code déplacé tel quel.

   Sections d'origine : Compose editor (Phase 3), .env + toggle fichiers, New stack, Import stack, Delete stack, Permissions, Git History

   Ce module rattache des méthodes/propriétés à l'objet global
   window.DockyApp. Il doit être chargé APRÈS app.js (la façade
   qui définit window.DockyApp et boote au DOMContentLoaded) et
   AVANT le chargement de la page (script classique synchrone).
   ============================================================ */

Object.assign(window.DockyApp, {
    // -------------------------------------------------------
    // Compose editor (Phase 3)
    // -------------------------------------------------------

    selectedStack: null,
    stackFiles: [],
    currentFile: null,
    fileContents: {},      // filename -> current editor content
    savedContents: {},     // filename -> last saved content (server)
    editorLoading: false,
    _editorLoadedKey: null,   // clé (name@agent) de la stack actuellement chargée dans l'éditeur
    _editorLoadToken: 0,      // token anti-race : incrémenté à chaque loadEditor, le dernier clic gagne
    _showAllStackFiles: false, // toggle « afficher tous les fichiers » (par défaut : liste propre compose + .env)
    deployTargetStack: null,
    deleteTargetStack: null,
    permsTargetFile: null,

    onStackSelect() {
        const selector = document.getElementById("stack-selector");
        if (!selector) return;
        const value = selector.value;
        if (!value) {
            this.clearStackSelection();
            return;
        }
        // value = "stackName@agentName"
        const atIdx = value.lastIndexOf('@');
        const name = atIdx > 0 ? value.substring(0, atIdx) : value;
        const agent = atIdx > 0 ? value.substring(atIdx + 1) : null;

        this.selectStackFromDashboard(name, agent);
        // Quand la stack est choisie depuis le dropdown, on ramène le dashboard
        // (liste / grille / table) sur l'élément de cette stack s'il n'était pas
        // visible dans la zone scrollée.
        this._scrollToStackInDashboard(name, agent);
    },

    // Fait défiler la vue dashboard jusqu'au groupe de la stack sélectionnée.
    // Ne scroll QUE si un élément correspondant existe réellement dans le DOM
    // (une vue liste/grille/table affichée) : si l'éditeur seul est affiché ou
    // qu'aucune vue n'a rendu cette stack (ex. grille sans container), on ne
    // force aucun défilement.
    _scrollToStackInDashboard(name, agent) {
        const targetKey = name + '@' + (agent || '');
        const stackEls = document.querySelectorAll('.stack-card, .table-stack-group, .grid-container-card');
        for (const el of stackEls) {
            const elKey = (el.dataset.stack || '') + '@' + (el.dataset.agent || '');
            if (elKey === targetKey) {
                try {
                    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                } catch (e) {
                    // scrollIntoView({behavior:'smooth'}) peut ne pas être
                    // supporté sur tous les navigateurs : repli sans options.
                    el.scrollIntoView();
                }
                return;
            }
        }
    },

    selectStackFromDashboard(name, agent) {
        const key = name + '@' + (agent || '');
        this._selectedStack = key;

        // Assombrir les containers qui ne sont pas dans ce stack (mode grille)
        const cards = document.querySelectorAll('.grid-container-card');
        cards.forEach(card => {
            const cardStack = card.dataset.stack;
            const cardAgent = card.dataset.agent;
            const cardKey = cardStack + '@' + (cardAgent || '');
            if (cardKey === key) {
                card.classList.remove('grid-dimmed');
            } else {
                card.classList.add('grid-dimmed');
            }
        });

        // Assombrir les sections entières qui ne sont pas dans ce stack (mode tableau)
        const sections = document.querySelectorAll('.table-stack-group');
        sections.forEach(section => {
            const sectionKey = (section.dataset.stack || '') + '@' + (section.dataset.agent || '');
            if (sectionKey === key) {
                section.classList.remove('grid-dimmed');
            } else {
                section.classList.add('grid-dimmed');
            }
        });

        // Mettre à jour le sélecteur
        const selector = document.getElementById("stack-selector");
        if (selector) selector.value = key;

        // Afficher le panel contextuel
        const stack = this.stacks.find(s => s.name === name && (s.agent_name || '') === (agent || ''));
        if (stack) {
            this.showStackContextPanel(stack, null);
        }

        // Charger l'éditeur
        this.selectedStackAgent = agent || null;
        this.loadEditor(name, agent);
    },

    async loadEditor(name, agent, force) {
        const key = name + '@' + (agent || '');

        // Token anti-race : chaque appel invalide les fetchs encore en vol d'un
        // clic précédent. Si un nouveau clic a eu lieu pendant un await, on
        // abandonne la mise à jour (pas d'écriture de contenu ni de
        // _editorLoadedKey) — le dernier clic gagne.
        const _loadToken = ++this._editorLoadToken;
        const isStale = () => _loadToken !== this._editorLoadToken;

        // Anti-flicker : cette stack est déjà chargée dans l'éditeur, on ne re-fetche
        // pas et on ne reconstruit pas le DOM. Le contenu de l'éditeur ne doit jamais
        // être remplacé par un rechargement périodique tant qu'il est affiché.
        // (force=true pour les rechargements volontaires : création/restauration.)
        if (!force && this._editorLoadedKey === key) {
            this.selectedStack = key;
            this.selectedStackAgent = agent || null;
            this.renderEditor();
            return;
        }

        // Changement de stack pendant l'édition : on quitte le mode édition pour
        // recharger proprement (sinon renderEditor afficherait l'ancien contenu).
        this._composeEditMode = false;

        this.selectedStack = key;
        this.selectedStackAgent = agent || null;

        const setEditorKey = () => {
            this._editorLoadedKey = key;
            const body = document.getElementById('compose-body');
            if (body) body.dataset.stackKey = key;
        };

        // External / standalone stacks cannot be edited (files are not in /data/stacks/)
        const stackInfo = this.stacks.find((s) => s.name === name && (s.agent_name||'') === (agent||''));
        if (stackInfo && (stackInfo.managed === false || stackInfo.standalone === true)) {
            this.stackFiles = [];
            this.currentFile = null;
            this.fileContents = {};
            this.savedContents = {};
            const label = stackInfo.standalone === true
                ? "Containers standalone (hors Docker Compose)."
                : "Stack externe - non gérée par Docky.";
            this.renderEditorPlaceholder(
                label + " Les fichiers ne sont pas accessibles. " +
                "Vous pouvez démarrer/arrêter/redémarrer cette stack depuis le dashboard."
            );
            setEditorKey();
            return;
        }

        this.editorLoading = true;
        this.currentFile = null;
        this.fileContents = {};
        this.savedContents = {};
        this.renderEditorLoading();

        // --- Batch route: tries to fetch all files with content in one call ---
        const agentParam = this.agentQuery(agent);
        // Liste blanche (compose + .env) par défaut ; le toggle « afficher tous
        // les fichiers » demande la liste complète au serveur.
        const includeQS = this._showAllStackFiles
            ? (agentParam ? "&include_hidden=true" : "?include_hidden=true")
            : "";
        const batchUrl = "/api/stacks/" + encodeURIComponent(name) + "/files-with-content" + agentParam + includeQS;

        let batchOk = false;
        try {
            const batchResp = await fetch(batchUrl, { credentials: "same-origin" });
            if (isStale()) return;
            if (batchResp.status === 401) {
                window.location.href = "/login";
                return;
            }
            if (batchResp.ok) {
                const batchData = await batchResp.json();
                if (isStale()) return;
                if (batchData && batchData.files && batchData.files.length > 0) {
                    // Build stackFiles and fileContents from batch data
                    this.stackFiles = batchData.files.map(f => ({
                        name: f.filename,
                        size: f.size || 0,
                        is_dir: false
                    }));
                    for (const f of batchData.files) {
                        this._setEditorFileContent(f.filename, f.content || "");
                    }
                    batchOk = true;
                }
            }
        } catch (e) {
            console.warn("Batch load failed, falling back to sequential:", e);
        }

        // --- Fallback: legacy sequential load ---
        if (!batchOk) {
            const filesData = await this.apiFetch("/api/stacks/" + encodeURIComponent(name) + "/files" + agentParam + includeQS);
            if (isStale()) return;
            if (!filesData || !filesData.files) {
                this.renderEditorPlaceholder("Impossible de charger les fichiers de la stack.");
                setEditorKey();
                return;
            }
            this.stackFiles = filesData.files;
            if (this.stackFiles.length === 0) {
                this.renderEditorPlaceholder("Aucun fichier dans cette stack.");
                setEditorKey();
                return;
            }
            // Load all file contents sequentially (legacy path)
            for (const f of this.stackFiles) {
                const resp = await fetch("/api/stacks/" + encodeURIComponent(name) + "/files/" + encodeURIComponent(f.name) + agentParam, { credentials: "same-origin" });
                if (isStale()) return;
                if (resp.ok) {
                    const text = await resp.text();
                    if (isStale()) return;
                    this._setEditorFileContent(f.name, text);
                } else {
                    // Fetch en échec : on ne remplace jamais l'affichage par un
                    // contenu vide — on conserve ce qu'on avait déjà.
                    this._setEditorFileContent(f.name, this.fileContents[f.name] || "");
                }
            }
        }

        if (isStale()) return;
        this.editorLoading = false;
        setEditorKey();
        // Select first file (prefer docker-compose.yml)
        let first = this.stackFiles[0].name;
        for (const f of this.stackFiles) {
            if (f.name === "docker-compose.yml" || f.name === "docker-compose.yaml" || f.name === "compose.yml" || f.name === "compose.yaml") {
                first = f.name;
                break;
            }
        }
        this.selectFile(first);
    },

    toggleComposeEdit() {
        this._composeEditMode = !this._composeEditMode;
        this.renderEditor();
    },

    selectFile(filename) {
        this.currentFile = filename;
        this.renderEditor();
    },

    renderEditorPlaceholder(message) {
        const body = document.getElementById("compose-body");
        if (!body) return;
        const msg = message || "Sélectionnez une stack pour éditer ses fichiers.";
        body.innerHTML = '<div class="placeholder"><p>' + this.escapeHtml(msg) + '</p><p class="placeholder-hint">Cliquez sur une stack du dashboard ou choisissez-la dans la liste.</p></div>';
    },

    // Ne remplace le contenu d'un fichier QUE si le contenu reçu diffère du
    // contenu actuel. Ne jamais écraser avec un contenu vide/indisponible.
    _setEditorFileContent(filename, content) {
        if (content === null || content === undefined) return;
        if (this.fileContents[filename] === content) return;
        this.fileContents[filename] = content;
        this.savedContents[filename] = content;
    },

    renderEditorLoading() {
        const body = document.getElementById("compose-body");
        if (!body) return;
        body.innerHTML = '<div class="placeholder"><p>' + this.icon('loader') + ' Chargement des fichiers…</p></div>';
    },

    isModified(filename) {
        return this.fileContents[filename] !== this.savedContents[filename];
    },

    anyModified() {
        for (const f of Object.keys(this.fileContents)) {
            if (this.isModified(f)) return true;
        }
        return false;
    },

    renderEditor() {
        const body = document.getElementById("compose-body");
        if (!body || !this.selectedStack) return;

        // Préserver curseur + scroll si l'éditeur (textarea) est déjà affiché,
        // pour ne pas faire sauter le curseur quand on re-rend le même contenu.
        const oldEditor = document.getElementById("code-editor");
        let cursorPos = -1;
        let editorScrollTop = 0;
        if (oldEditor && this._composeEditMode) {
            cursorPos = oldEditor.selectionStart;
            editorScrollTop = oldEditor.scrollTop;
        }

        // Tabs
        let tabsHtml = '<div class="compose-tabs">';
        for (const f of this.stackFiles) {
            const active = f.name === this.currentFile ? " active" : "";
            const mod = this.isModified(f.name) ? " modified" : "";
            // Le nom de fichier est encodé en URL puis décodé dans selectFile :
            // les guillemets / apostrophes du nom ne peuvent pas casser l'attribut
            // onclick (JSON.stringify produisait des " qui terminaient l'attribut
            // et rendait TOUS les onglets — dont .env — impossibles à cliquer).
            const tabFileArg = encodeURIComponent(f.name).replace(/'/g, '%27');
            tabsHtml += '<button class="tab-btn' + active + mod + '" onclick="DockyApp.selectFile(decodeURIComponent(\'' + tabFileArg + '\'))">'
                + this.escapeHtml(f.name)
                + '<span class="tab-modified-dot">●</span></button>';
        }
        tabsHtml += '</div>';

        // Toolbar
        const mod = this.isModified(this.currentFile);
        const anyMod = this.anyModified();
        const _parts = this.selectedStack.split('@');
        const _stackName = _parts[0];
        const _stackAgent = this.selectedStackAgent || '';
        const _escapedName = this.escapeHtml(_stackName);
        const _escapedAgent = this.escapeHtml(_stackAgent);
        let toolbarHtml = '<div class="compose-toolbar">';
        toolbarHtml += '<button class="btn btn-success btn-sm" onclick="DockyApp.saveCurrentFile()"' + (mod ? '' : ' disabled') + '>' + this.icon('hard-drive') + ' Sauvegarder</button>';
        toolbarHtml += '<button class="btn btn-info btn-sm" onclick="DockyApp.saveAndDeploy()"' + (anyMod ? '' : ' disabled') + '>' + this.icon('rocket') + ' Sauvegarder & Déployer</button>';
        // Créer un .env vide s'il n'existe pas encore dans la stack.
        if (!this.stackFiles.some(f => f.name === ".env")) {
            toolbarHtml += '<button class="btn btn-ghost btn-sm" onclick="DockyApp.createEnvFile()" title="Créer un fichier .env vide">' + this.icon('file-plus') + ' Créer .env</button>';
        }
        toolbarHtml += '<button class="btn btn-ghost btn-sm" onclick="DockyApp.openHistory()" title="Historique">' + this.icon('clipboard-list') + '</button>';
        toolbarHtml += '<div class="spacer"></div>';
        toolbarHtml += '<button class="btn btn-ghost btn-sm" onclick="DockyApp.stackAction(\'' + _escapedName + '\', \'start\', \'' + _escapedAgent + '\')" title="Démarrer">' + this.icon('play') + '</button>';
        toolbarHtml += '<button class="btn btn-ghost btn-sm" onclick="DockyApp.stackAction(\'' + _escapedName + '\', \'stop\', \'' + _escapedAgent + '\')" title="Arrêter">' + this.icon('square') + '</button>';
        toolbarHtml += '<button class="btn btn-ghost btn-sm" onclick="DockyApp.stackAction(\'' + _escapedName + '\', \'restart\', \'' + _escapedAgent + '\')" title="Redémarrer">' + this.icon('refresh-cw') + '</button>';
        toolbarHtml += '<button class="btn btn-ghost btn-sm" onclick="DockyApp.stackAction(\'' + _escapedName + '\', \'update\', \'' + _escapedAgent + '\')" title="Tout mettre à jour">' + this.icon('arrow-up') + ' Tout update</button>';
        toolbarHtml += '<button class="btn btn-ghost btn-sm" onclick="DockyApp.openStackLogs(\'' + _escapedName + '\', \'' + _escapedAgent + '\')" title="Logs">' + this.icon('clipboard-list') + ' Logs</button>';
        toolbarHtml += '<div class="spacer"></div>';
        // Bouton bascule lecture seule / édition
        if (this._composeEditMode) {
            toolbarHtml += '<button class="btn btn-ghost btn-sm" onclick="DockyApp.toggleComposeEdit()" title="Aperçu lecture seule">' + this.icon('eye') + ' Aperçu</button>';
        } else {
            toolbarHtml += '<button class="btn btn-primary btn-sm" onclick="DockyApp.toggleComposeEdit()" title="Passer en mode édition">' + this.icon('pen-square') + ' Modifier</button>';
        }
        toolbarHtml += '<button class="btn btn-sm" onclick="DockyApp.openPermsModal()" title="Permissions du fichier">' + this.icon('lock') + '</button>';
        toolbarHtml += '<button class="btn btn-danger btn-sm" onclick="DockyApp.openDeleteStackModal(\''+ this.escapeHtml(this.selectedStack) +'\')" title="Supprimer la stack">' + this.icon('trash-2') + '</button>';
        toolbarHtml += '<button class="btn btn-ghost btn-sm' + (this._showAllStackFiles ? ' active' : '') + '" onclick="DockyApp.toggleShowAllStackFiles()" title="Afficher/masquer les fichiers non éditables (compose + .env uniquement par défaut)">'
            + this.icon('eye') + ' ' + (this._showAllStackFiles ? 'Fichiers éditables' : 'Afficher tous') + '</button>';
        toolbarHtml += '</div>';

        // Editor area
        const content = this.fileContents[this.currentFile] || "";
        let editorHtml = '<div class="code-editor-wrap">';
        if (this._composeEditMode) {
            // Mode édition : textarea modifiable
            editorHtml += '<div class="line-numbers" id="line-numbers"></div>';
            editorHtml += '<textarea class="code-textarea" id="code-editor" spellcheck="false"'
                + ' oninput="DockyApp.onEditorInput()"'
                + ' onscroll="DockyApp.syncLineScroll()"'
                + ' onkeydown="DockyApp.onEditorKeydown(event)"'
                + '>' + this.escapeHtml(content) + '</textarea>';
        } else {
            // Mode lecture seule : bloc pre
            editorHtml += '<pre class="compose-readonly" id="code-editor-readonly">' + this.escapeHtml(content) + '</pre>';
        }
        editorHtml += '</div>';

        // Status bar
        let statusHtml = '<div class="compose-status">';
        statusHtml += '<span class="status-dot' + (mod ? ' modified' : '') + '"></span>';
        statusHtml += '<span>' + (mod ? 'Modifié (non sauvegardé)' : 'Aucune modification') + '</span>';
        statusHtml += '<span style="margin-left:auto;">' + (this.selectedStackAgent ? '🖥 ' + this.escapeHtml(this.selectedStackAgent) + ' · ' : '') + this.escapeHtml(this.currentFile || '') + ' · ' + content.split("\n").length + ' lignes</span>';
        statusHtml += '</div>';

        body.innerHTML = tabsHtml + toolbarHtml + editorHtml + statusHtml;
        if (this._composeEditMode) {
            this.updateLineNumbers();
        } else {
            this.updateReadonlyLineNumbers();
        }

        if (typeof lucide !== 'undefined') {
            lucide.createIcons();
        }

        // Restaurer curseur + scroll après le re-render (contenu identique)
        if (this._composeEditMode && cursorPos >= 0) {
            const editor = document.getElementById("code-editor");
            if (editor) {
                editor.selectionStart = editor.selectionEnd = Math.min(cursorPos, editor.value.length);
                editor.scrollTop = editorScrollTop;
            }
        }
    },

    updateLineNumbers() {
        const editor = document.getElementById("code-editor");
        const ln = document.getElementById("line-numbers");
        if (!editor || !ln) return;
        const lines = editor.value.split("\n").length;
        let html = "";
        for (let i = 1; i <= lines; i++) {
            html += i + "\n";
        }
        ln.textContent = html;
    },

    updateReadonlyLineNumbers() {
        const pre = document.getElementById("code-editor-readonly");
        const ln = document.getElementById("line-numbers");
        if (!pre || !ln) return;
        const lines = pre.textContent.split("\n").length;
        let html = "";
        for (let i = 1; i <= lines; i++) {
            html += i + "\n";
        }
        ln.textContent = html;
    },

    syncLineScroll() {
        const editor = document.getElementById("code-editor");
        const ln = document.getElementById("line-numbers");
        if (!editor || !ln) return;
        ln.scrollTop = editor.scrollTop;
    },

    onEditorInput() {
        const editor = document.getElementById("code-editor");
        if (!editor || !this.currentFile) return;
        this.fileContents[this.currentFile] = editor.value;
        this.updateLineNumbers();
        // Update modified indicators without full re-render
        this.updateModifiedIndicators();
    },

    updateModifiedIndicators() {
        // Update tab dots
        document.querySelectorAll(".compose-tabs .tab-btn").forEach((btn) => {
            // extract filename from text content (without the dot)
            const text = btn.childNodes[0] ? btn.childNodes[0].nodeValue.trim() : "";
            if (this.isModified(text)) {
                btn.classList.add("modified");
            } else {
                btn.classList.remove("modified");
            }
        });
        // Update save button disabled state
        const saveBtn = document.querySelector(".compose-toolbar .btn-success");
        if (saveBtn) saveBtn.disabled = !this.isModified(this.currentFile);
        const deployBtn = document.querySelector(".compose-toolbar .btn-info");
        if (deployBtn) deployBtn.disabled = !this.anyModified();
        // Status bar
        const statusDot = document.querySelector(".compose-status .status-dot");
        const statusText = document.querySelector(".compose-status span:nth-child(2)");
        if (statusDot && statusText) {
            const mod = this.isModified(this.currentFile);
            statusDot.className = "status-dot" + (mod ? " modified" : "");
            statusText.textContent = mod ? "Modifié (non sauvegardé)" : "Aucune modification";
        }
    },

    onEditorKeydown(e) {
        if (e.key === "Tab") {
            e.preventDefault();
            const editor = e.target;
            const start = editor.selectionStart;
            const end = editor.selectionEnd;
            // Insert 2 spaces (YAML-friendly)
            editor.value = editor.value.substring(0, start) + "  " + editor.value.substring(end);
            editor.selectionStart = editor.selectionEnd = start + 2;
            this.onEditorInput();
        } else if (e.key === "s" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            this.saveCurrentFile();
        }
    },

    async saveCurrentFile() {
        if (!this.selectedStack || !this.currentFile) return;
        // Extraire le nom de stack depuis la clé composite (name@agent)
        const atIdx = this.selectedStack.indexOf('@');
        const stackName = atIdx > 0 ? this.selectedStack.substring(0, atIdx) : this.selectedStack;
        const content = this.fileContents[this.currentFile];
        const agentParam = this.agentQuery(this.selectedStackAgent);
        const resp = await fetch("/api/stacks/" + encodeURIComponent(stackName) + "/files/" + encodeURIComponent(this.currentFile) + agentParam, {
            method: "PUT",
            headers: { "Content-Type": "text/plain" },
            body: content,
            credentials: "same-origin",
        });
        if (resp.status === 401) { window.location.href = "/login"; return; }
        if (resp.ok) {
            this.savedContents[this.currentFile] = content;
            this.updateModifiedIndicators();
            this.showToast("Fichier sauvegardé : " + this.currentFile, "success");
        } else {
            const data = await resp.json().catch(() => ({}));
            this.showToast("Erreur sauvegarde : " + (data.detail || resp.statusText), "error");
        }
    },

    async saveAndDeploy() {
        if (!this.selectedStack) return;
        // Extraire le nom de stack depuis la clé composite (name@agent)
        const atIdx = this.selectedStack.indexOf('@');
        const stackName = atIdx > 0 ? this.selectedStack.substring(0, atIdx) : this.selectedStack;
        // Save all modified files
        const agent = this.selectedStackAgent;
        const agentParam = this.agentQuery(agent);
        this.showToast("Sauvegarde et déploiement…", "info");
        let allOk = true;
        for (const fname of Object.keys(this.fileContents)) {
            if (this.isModified(fname)) {
                const resp = await fetch("/api/stacks/" + encodeURIComponent(stackName) + "/files/" + encodeURIComponent(fname) + agentParam, {
                    method: "PUT",
                    headers: { "Content-Type": "text/plain" },
                    body: this.fileContents[fname],
                    credentials: "same-origin",
                });
                if (!resp.ok) allOk = false;
                else this.savedContents[fname] = this.fileContents[fname];
            }
        }
        if (!allOk) {
            this.showToast("Erreur lors de la sauvegarde", "error");
            return;
        }
        // Deploy (streamé)
        this._openActivity(`Déployer — ${stackName}`);
        try {
            const result = await this._streamAction("/api/stacks/" + encodeURIComponent(stackName) + "/deploy" + agentParam);
            this._finishActivity(result.success, result.output);
            if (result.success) this.showToast("Déploiement réussi ✓", "success");
            else this.showToast("Déploiement échoué : " + (result.output || ""), "error");
        } catch(e) {
            this._finishActivity(false, e.message);
            this.showToast("Déploiement échoué : " + e.message, "error");
        }
        this.updateModifiedIndicators();
        this.refreshStacks();
    },

    // -------------------------------------------------------
    // .env (création) + toggle « afficher tous les fichiers »
    // -------------------------------------------------------

    async createEnvFile() {
        if (!this.selectedStack) return;
        // Extraire le nom de stack depuis la clé composite (name@agent)
        const atIdx = this.selectedStack.indexOf('@');
        const stackName = atIdx > 0 ? this.selectedStack.substring(0, atIdx) : this.selectedStack;
        const agentParam = this.agentQuery(this.selectedStackAgent);
        // Un .env vide via le mécanisme standard save_stack_file (PUT /files/.env)
        const resp = await fetch("/api/stacks/" + encodeURIComponent(stackName) + "/files/.env" + agentParam, {
            method: "PUT",
            headers: { "Content-Type": "text/plain" },
            body: "",
            credentials: "same-origin",
        });
        if (resp.status === 401) { window.location.href = "/login"; return; }
        if (resp.ok) {
            this.showToast("Fichier .env créé", "success");
            // Ajoute le fichier à la liste et ouvre l'édition, sans re-fetch global
            // (pour ne pas perdre les modifications non sauvegardées des autres onglets).
            if (!this.stackFiles.some(f => f.name === ".env")) {
                this.stackFiles.push({ name: ".env", size: 0, is_dir: false });
            }
            this._setEditorFileContent(".env", "");
            this.currentFile = ".env";
            if (!this._composeEditMode) this.toggleComposeEdit();
            this.renderEditor();
        } else {
            const data = await resp.json().catch(() => ({}));
            this.showToast("Erreur création .env : " + (data.detail || data.error || resp.statusText), "error");
        }
    },

    async toggleShowAllStackFiles() {
        if (!this.selectedStack) return;
        this._showAllStackFiles = !this._showAllStackFiles;
        const atIdx = this.selectedStack.indexOf('@');
        const stackName = atIdx > 0 ? this.selectedStack.substring(0, atIdx) : this.selectedStack;
        const agentParam = this.agentQuery(this.selectedStackAgent);
        const qs = this._showAllStackFiles ? "include_hidden=true" : "include_hidden=false";
        const sep = agentParam ? "&" : "?";
        const url = "/api/stacks/" + encodeURIComponent(stackName) + "/files" + agentParam + sep + qs;

        const filesData = await this.apiFetch(url);
        if (!filesData || !filesData.files) {
            // Annuler la bascule en cas d'échec
            this._showAllStackFiles = !this._showAllStackFiles;
            this.showToast("Impossible de recharger la liste des fichiers", "error");
            return;
        }
        this.stackFiles = filesData.files;

        // Charger le contenu des fichiers nouvellement visibles (les fichiers déjà
        // chargés — y compris les modifications non sauvegardées — sont conservés).
        const loadedFiles = [];
        for (const f of this.stackFiles) {
            if (this.fileContents[f.name] !== undefined) {
                // Déjà chargé (éventuellement modifié) : conservé tel quel.
                loadedFiles.push(f);
                continue;
            }
            const resp = await fetch("/api/stacks/" + encodeURIComponent(stackName) + "/files/" + encodeURIComponent(f.name) + agentParam, { credentials: "same-origin" });
            if (resp.ok) {
                const text = await resp.text();
                this._setEditorFileContent(f.name, text);
                loadedFiles.push(f);
            } else {
                // Fichier illisible (binaire, permission…) : on le SAUTE. On ne
                // crée jamais de buffer vide éditable qui risquerait d'écraser le
                // fichier avec un contenu vide à la sauvegarde.
                console.warn("Fichier illisible, exclu de l'éditeur :", f.name);
            }
        }
        this.stackFiles = loadedFiles;

        // Si le fichier courant est masqué (retour à la liste éditables), basculer
        // sur le premier fichier de la liste.
        if (!this.stackFiles.some(f => f.name === this.currentFile)) {
            this.selectFile(this.stackFiles.length ? this.stackFiles[0].name : null);
        } else {
            this.renderEditor();
        }
    },

    // -------------------------------------------------------
    // New stack
    // -------------------------------------------------------

    DEFAULT_COMPOSE_TEMPLATE: 'version: "3.8"\n\nservices:\n  # Ajoute tes services ici\n',

    openNewStackModal() {
        const modal = document.getElementById("new-stack-modal");
        modal.classList.remove("hidden");
        document.getElementById("new-stack-name").value = "";
        document.getElementById("new-stack-compose").value = this.DEFAULT_COMPOSE_TEMPLATE;
        document.getElementById("new-stack-env").value = "";

        // Peupler le sélecteur d'agent cible
        const agentSelect = document.getElementById("new-stack-agent");
        if (agentSelect) {
            agentSelect.innerHTML = '<option value="">-- Choisir un agent --</option>';
            for (const agent of this.agentsList) {
                const aName = agent.name || agent;
                const opt = document.createElement("option");
                opt.value = aName;
                opt.textContent = aName + (agent.status === "online" ? " 🟢" : " 🔴");
                agentSelect.appendChild(opt);
            }
            // Valeur par défaut : l'agent de la stack en cours d'édition, sinon le premier agent
            let defaultAgent = this.selectedStackAgent;
            if (!defaultAgent || !this.agentsList.some(a => (a.name || a) === defaultAgent)) {
                defaultAgent = this.agentsList.length ? (this.agentsList[0].name || this.agentsList[0]) : "";
            }
            agentSelect.value = defaultAgent;
        }

        setTimeout(() => document.getElementById("new-stack-name").focus(), 50);
    },

    closeNewStackModal() {
        document.getElementById("new-stack-modal").classList.add("hidden");
    },

    // -------------------------------------------------------
    // Import stack
    // -------------------------------------------------------

    openImportModal() {
        const modal = document.getElementById("import-modal");
        if (modal) modal.classList.remove("hidden");
        const src = document.getElementById("import-source-path");
        const name = document.getElementById("import-stack-name");
        if (src) src.value = "";
        if (name) name.value = "";

        // Peupler le sélecteur d'agent
        const agentSelect = document.getElementById("import-agent");
        if (agentSelect) {
            agentSelect.innerHTML = '<option value="">-- Choisir un agent --</option>';
            for (const agent of this.agentsList) {
                const aName = agent.name || agent;
                const opt = document.createElement("option");
                opt.value = aName;
                opt.textContent = aName + (agent.status === "online" ? " 🟢" : " 🔴");
                // Pas de sélection par défaut en mode multi-sélection
                agentSelect.appendChild(opt);
            }
        }

        setTimeout(() => {
            if (src) src.focus();
        }, 50);
    },

    openImportModalForStack(stackName) {
        this.openImportModal();
        const nameField = document.getElementById("import-stack-name");
        if (nameField) nameField.value = stackName;
    },

    closeImportModal() {
        const modal = document.getElementById("import-modal");
        if (modal) modal.classList.add("hidden");
    },

    importExternal(sourcePath, stackName, agent) {
        if (!sourcePath) {
            this.showToast('Chemin source non détecté pour cette stack', "error");
            return;
        }
        // Dry-run first to get a preview, then show a modal before the
        // actual import.
        this._importPreview = null;
        this._doImportPreview(sourcePath, stackName, agent);
    },

    async _doImportPreview(sourcePath, stackName, agent) {
        if (!agent) {
            this.showToast('Agent non trouvé pour cette stack', "error");
            return;
        }

        this.showToast('Génération de la preview...', "info");

        try {
            const resp = await fetch('/api/stacks/import?agent=' + encodeURIComponent(agent), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source_path: sourcePath, stack_name: stackName, dry_run: true }),
                credentials: 'same-origin',
            });
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            const data = await resp.json().catch(() => ({}));

            if (resp.ok && data.success) {
                this.showImportPreview(sourcePath, stackName, agent, data);
            } else {
                this.showToast(data.detail || data.error || "Erreur lors de la preview", "error");
            }
        } catch (e) {
            this.showToast('Erreur: ' + e.message, "error");
        }
    },

    showImportPreview(sourcePath, stackName, agent, previewData) {
        // Stocker les infos pour la confirmation
        this._importPreview = { sourcePath, stackName, agent };

        const modal = document.getElementById('import-preview-modal');
        const contentEl = document.getElementById('import-preview-content');
        const conversionsEl = document.getElementById('import-preview-conversions');
        const warningsEl = document.getElementById('import-preview-warnings');

        // Afficher le compose converti
        if (contentEl) contentEl.textContent = previewData.preview || previewData.converted_compose || '';

        // Afficher les conversions
        if (conversionsEl) {
            if (previewData.conversions && previewData.conversions.length > 0) {
                conversionsEl.innerHTML = '<div style="color: var(--text-secondary); margin-bottom: 8px;">Chemins convertis (' + previewData.conversions.length + '):</div>' +
                    previewData.conversions.map(c => '<div style="color: #4fc3f7; font-family: monospace; font-size: 12px; padding: 2px 0;">' + this.escapeHtml(c) + '</div>').join('');
                conversionsEl.style.display = 'block';
            } else {
                conversionsEl.innerHTML = '<div style="color: var(--text-secondary);">Aucune conversion nécessaire (chemins déjà absolus)</div>';
                conversionsEl.style.display = 'block';
            }
        }

        // Afficher les warnings
        if (warningsEl) {
            if (previewData.warnings && previewData.warnings.length > 0) {
                warningsEl.innerHTML = '<div style="color: #ff9800; margin-bottom: 8px;">⚠️ Avertissements:</div>' +
                    previewData.warnings.map(w => '<div style="color: #ff9800; font-size: 12px; padding: 2px 0;">' + this.escapeHtml(w) + '</div>').join('');
                warningsEl.style.display = 'block';
            } else {
                warningsEl.style.display = 'none';
            }
        }

        if (modal) modal.classList.remove('hidden');
    },

    closeImportPreview() {
        const modal = document.getElementById('import-preview-modal');
        if (modal) modal.classList.add('hidden');
    },

    async confirmImport() {
        if (!this._importPreview) return;
        const { sourcePath, stackName, agent } = this._importPreview;

        this.closeImportPreview();
        this.showToast('Import en cours...', "info");

        try {
            const resp = await fetch('/api/stacks/import?agent=' + encodeURIComponent(agent), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source_path: sourcePath, stack_name: stackName, dry_run: false }),
                credentials: 'same-origin',
            });
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            const data = await resp.json().catch(() => ({}));

            if (resp.ok && data.success) {
                let msg = 'Stack « ' + (data.name || stackName) + ' » importée avec succès';
                if (data.conversions && data.conversions.length > 0) {
                    msg += ' (' + data.conversions.length + ' chemin(s) converti(s))';
                }
                if (data.warnings && data.warnings.length > 0) {
                    msg += '\n⚠ ' + data.warnings.join(', ');
                }
                this.showToast(msg, "success");
                this._importPreview = null;
                await this.refreshStacks();
            } else {
                this.showToast(data.detail || data.error || "Erreur lors de l'import", "error");
            }
        } catch (e) {
            this.showToast('Erreur: ' + e.message, "error");
        }
    },

    async doImportDirect(sourcePath, stackName, agent) {
        if (!agent) {
            this.showToast('Agent non trouvé pour cette stack', "error");
            return;
        }

        this.showToast('Import en cours...', "info");

        try {
            const resp = await fetch('/api/stacks/import?agent=' + encodeURIComponent(agent), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source_path: sourcePath, stack_name: stackName }),
                credentials: 'same-origin',
            });
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            const data = await resp.json().catch(() => ({}));

            if (resp.ok && data.success) {
                let msg = 'Stack « ' + (data.name || stackName) + ' » importée avec succès';
                if (data.conversions && data.conversions.length > 0) {
                    msg += ' (' + data.conversions.length + ' chemin(s) converti(s))';
                }
                if (data.warnings && data.warnings.length > 0) {
                    msg += '\n⚠ ' + data.warnings.join(', ');
                }
                this.showToast(msg, "success");
                await this.refreshStacks();
            } else {
                this.showToast(data.detail || data.error || "Erreur lors de l'import", "error");
            }
        } catch (e) {
            this.showToast('Erreur: ' + e.message, "error");
        }
    },

    async doImport() {
        const sourcePath = (document.getElementById("import-source-path").value || "").trim();
        const stackName = (document.getElementById("import-stack-name").value || "").trim() || null;
        const agentSelect = document.getElementById("import-agent");
        const agent = agentSelect ? agentSelect.value : null;

        if (!sourcePath) {
            this.showToast("Le chemin source est requis", "error");
            return;
        }
        if (!agent) {
            this.showToast("Sélectionne un agent cible", "error");
            return;
        }

        try {
            const resp = await fetch(
                "/api/stacks/import?agent=" + encodeURIComponent(agent),
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ source_path: sourcePath, stack_name: stackName }),
                    credentials: "same-origin",
                }
            );
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            const data = await resp.json().catch(() => ({}));

            if (resp.ok && data.success) {
                let msg = 'Stack « ' + (data.name || stackName || sourcePath) + ' » importée avec succès';
                if (data.conversions && data.conversions.length > 0) {
                    msg += '\n\nChemins convertis (' + data.conversions.length + '):\n' + data.conversions.slice(0, 5).join('\n');
                    if (data.conversions.length > 5) msg += '\n... et ' + (data.conversions.length - 5) + ' autres';
                }
                if (data.warnings && data.warnings.length > 0) {
                    msg += '\n\n⚠️ Avertissements:\n' + data.warnings.join('\n');
                }
                this.showToast(msg, "success");
                this.closeImportModal();
                await this.refreshStacks();
            } else {
                this.showToast(data.detail || data.error || "Erreur lors de l'import", "error");
            }
        } catch (e) {
            this.showToast("Erreur: " + e.message, "error");
        }
    },

    async createStack() {
        const name = document.getElementById("new-stack-name").value.trim();
        const compose = document.getElementById("new-stack-compose").value;
        const env = document.getElementById("new-stack-env").value;
        const agentSelect = document.getElementById("new-stack-agent");
        const agent = agentSelect ? agentSelect.value : null;
        if (!name) {
            this.showToast("Le nom est requis", "error");
            return;
        }
        if (!agent) {
            this.showToast("Sélectionne un agent cible", "error");
            return;
        }
        const agentParam = this.agentQuery(agent);
        const resp = await fetch("/api/stacks" + agentParam, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, compose, env }),
            credentials: "same-origin",
        });
        if (resp.status === 401) { window.location.href = "/login"; return; }
        if (resp.ok) {
            this.closeNewStackModal();
            this.showToast("Stack créée : " + name, "success");
            await this.refreshStacks();
            this.loadEditor(name, agent, true);
        } else {
            const data = await resp.json().catch(() => ({}));
            this.showToast("Erreur création : " + (data.detail || resp.statusText), "error");
        }
    },

    // -------------------------------------------------------
    // Delete stack
    // -------------------------------------------------------

    openDeleteStackModal(name) {
        this.deleteTargetStack = name;
        document.getElementById("delete-stack-name").textContent = name;
        document.getElementById("delete-stack-modal").classList.remove("hidden");
    },

    closeDeleteStackModal() {
        document.getElementById("delete-stack-modal").classList.add("hidden");
        this.deleteTargetStack = null;
    },

    async confirmDeleteStack() {
        const raw = this.deleteTargetStack;
        if (!raw) return;
        // Extraire le nom et l'agent depuis la clé composite (name@agent)
        let stackName = raw;
        let agent = null;
        const atIdx = raw.indexOf('@');
        if (atIdx > 0) {
            stackName = raw.substring(0, atIdx);
            agent = raw.substring(atIdx + 1);
        }
        const agentParam = this.agentQuery(agent);
        const resp = await fetch("/api/stacks/" + encodeURIComponent(stackName) + agentParam, {
            method: "DELETE",
            credentials: "same-origin",
        });
        if (resp.status === 401) { window.location.href = "/login"; return; }
        if (resp.ok) {
            this.closeDeleteStackModal();
            this.showToast("Stack supprimée : " + stackName, "success");
            if (this.selectedStack === raw) {
                this.selectedStack = null;
                this.selectedStackAgent = null;
                this.renderEditorPlaceholder();
            }
            const selector = document.getElementById("stack-selector");
            if (selector) selector.value = "";
            await this.refreshStacks();
        } else {
            const data = await resp.json().catch(() => ({}));
            this.showToast("Erreur suppression : " + (data.detail || resp.statusText), "error");
        }
    },

    // -------------------------------------------------------
    // Permissions
    // -------------------------------------------------------

    openPermsModal() {
        if (!this.selectedStack || !this.currentFile) {
            this.showToast("Sélectionnez un fichier", "error");
            return;
        }
        this.permsTargetFile = this.currentFile;
        document.getElementById("perms-filename").textContent = this.currentFile;
        document.getElementById("perms-mode").value = "644";
        document.getElementById("perms-modal").classList.remove("hidden");
        setTimeout(() => document.getElementById("perms-mode").focus(), 50);
    },

    closePermsModal() {
        document.getElementById("perms-modal").classList.add("hidden");
        this.permsTargetFile = null;
    },

    async applyPermissions() {
        const mode = document.getElementById("perms-mode").value.trim();
        if (!mode || !/^[0-7]{3,4}$/.test(mode)) {
            this.showToast("Mode invalide (ex: 644)", "error");
            return;
        }
        // Extraire le nom de stack depuis la clé composite (name@agent)
        const atIdx = this.selectedStack.indexOf('@');
        const stackName = atIdx > 0 ? this.selectedStack.substring(0, atIdx) : this.selectedStack;
        const agentParam = this.agentQuery(this.selectedStackAgent);
        const resp = await fetch("/api/stacks/" + encodeURIComponent(stackName) + "/files/" + encodeURIComponent(this.permsTargetFile) + "/permissions" + agentParam, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode }),
            credentials: "same-origin",
        });
        if (resp.status === 401) { window.location.href = "/login"; return; }
        if (resp.ok) {
            this.closePermsModal();
            this.showToast("Permissions appliquées : " + mode, "success");
        } else {
            const data = await resp.json().catch(() => ({}));
            this.showToast("Erreur : " + (data.detail || resp.statusText), "error");
        }
    },

    // -------------------------------------------------------
    // Git History
    // -------------------------------------------------------

    async openHistory() {
        // selectedStack est la clé composite « name@agent » : il faut en extraire
        // le nom de stack seul, sinon l'endpoint d'historique reçoit un nom
        // inexistant et renvoie toujours une liste vide.
        const atIdx = this.selectedStack ? this.selectedStack.lastIndexOf('@') : -1;
        const name = atIdx > 0 ? this.selectedStack.substring(0, atIdx) : this.selectedStack;
        const agent = this.selectedStackAgent;
        if (!name || !agent) {
            this.showToast("Sélectionne d'abord une stack", "warning");
            return;
        }

        const modal = document.getElementById("history-modal");
        if (!modal) return;
        modal.classList.remove("hidden");

        document.getElementById("history-title").textContent = `📋 Historique — ${name}`;
        document.getElementById("history-body").innerHTML = '<p class="placeholder-hint">Chargement…</p>';

        try {
            const resp = await fetch(`/api/stacks/${encodeURIComponent(name)}/history?agent=${encodeURIComponent(agent)}`);
            const data = await resp.json();
            const history = data.history || [];

            if (history.length === 0) {
                document.getElementById("history-body").innerHTML = '<p class="placeholder-hint">Aucun historique disponible</p>';
                return;
            }

            let html = '<div class="history-list" id="history-list">';
            for (const h of history) {
                const date = new Date(h.date).toLocaleString('fr-FR');
                html += `<div class="history-item" data-hash="${h.hash}" onclick="DockyApp._selectHistory('${h.hash}')">
                    <span class="history-date">${this.escapeHtml(date)}</span>
                    <span class="history-msg">${this.escapeHtml(h.message)}</span>
                    <span class="history-actions">
                        <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();DockyApp._previewHistory('${h.hash}')">📄</button>
                        <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();DockyApp._restoreHistory('${h.hash}')">↩</button>
                    </span>
                </div>`;
            }
            html += '</div>';
            html += '<div id="history-preview" class="history-preview" style="display:none;"></div>';

            document.getElementById("history-body").innerHTML = html;

            if (typeof lucide !== 'undefined') {
                lucide.createIcons();
            }

            // Auto-select first
            const first = document.querySelector('.history-item');
            if (first) this._selectHistory(first.dataset.hash);
        } catch(e) {
            document.getElementById("history-body").innerHTML = `<p class="placeholder-hint">Erreur: ${this.escapeHtml(e.message)}</p>`;
        }
    },

    closeHistory() {
        const modal = document.getElementById("history-modal");
        if (modal) modal.classList.add("hidden");
    },

    async _selectHistory(hash) {
        document.querySelectorAll('.history-item').forEach(el => {
            el.classList.toggle('selected', el.dataset.hash === hash);
        });
        await this._previewHistory(hash);
    },

    async _previewHistory(hash) {
        // selectedStack est la clé composite « name@agent » → extraire le nom seul.
        const atIdx = this.selectedStack ? this.selectedStack.lastIndexOf('@') : -1;
        const name = atIdx > 0 ? this.selectedStack.substring(0, atIdx) : this.selectedStack;
        const agent = this.selectedStackAgent;
        if (!name || !agent || !hash) return;

        const previewDiv = document.getElementById('history-preview');
        if (!previewDiv) return;

        previewDiv.innerHTML = '<div class="history-preview-header">⏳ Chargement…</div>';
        previewDiv.style.display = 'block';

        try {
            const resp = await fetch(`/api/stacks/${encodeURIComponent(name)}/history/${hash}?agent=${encodeURIComponent(agent)}`);
            const data = await resp.json();
            const content = data.content || '(fichier non disponible)';

            previewDiv.innerHTML = `
                <div class="history-preview-header">📄 ${this.escapeHtml(data.message || '')} — ${this.escapeHtml(data.date || '')}</div>
                <div class="history-preview-code">${this.escapeHtml(content)}</div>
            `;
            if (typeof lucide !== 'undefined') {
                lucide.createIcons();
            }
        } catch(e) {
            previewDiv.innerHTML = `<div class="history-preview-header">Erreur</div><div class="history-preview-code">${this.escapeHtml(e.message)}</div>`;
        }
    },

    async _restoreHistory(hash) {
        // selectedStack est la clé composite « name@agent » → extraire le nom seul.
        const atIdx = this.selectedStack ? this.selectedStack.lastIndexOf('@') : -1;
        const name = atIdx > 0 ? this.selectedStack.substring(0, atIdx) : this.selectedStack;
        const agent = this.selectedStackAgent;
        if (!name || !agent || !hash) return;

        if (!confirm(`Restaurer la stack ${name} vers la version ${hash.slice(0, 8)} ? Le compose actuel sera écrasé.`)) return;

        this.showToast("Restauration en cours…", "info");
        try {
            const resp = await fetch(`/api/stacks/${encodeURIComponent(name)}/history/restore/${hash}?agent=${encodeURIComponent(agent)}`, { method: 'POST' });
            const result = await resp.json();
            if (result.success) {
                this.showToast("✓ Stack restaurée", "success");
                this.closeHistory();
                this.loadEditor(name, agent, true);
            } else {
                this.showToast("Erreur: " + (result.error || "Échec"), "error");
            }
        } catch(e) {
            this.showToast("Erreur: " + e.message, "error");
        }
    },
});
