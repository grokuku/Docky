/* ============================================================
   Docky - Settings page (LLM config + agents management)
   ============================================================ */

const SettingsApp = {
    // -------------------------------------------------------
    // State
    // -------------------------------------------------------
    agents: [],
    editingAgentName: null,   // null = add mode, string = edit mode
    pendingDeleteAgent: null,
    mcpKeyVisible: false,     // whether the MCP key is shown in clear
    dockerhubHasToken: false, // whether a token is already stored server-side

    // -------------------------------------------------------
    // Utilities
    // -------------------------------------------------------

    /** Lecture brute d'un cookie (parser simple, tolerant aux espaces). */
    getCookie(name) {
        if (typeof document === "undefined" || !document.cookie) return null;
        const parts = document.cookie.split(/;\s*/);
        for (let i = 0; i < parts.length; i++) {
            const eq = parts[i].indexOf("=");
            if (eq === -1) continue;
            if (parts[i].slice(0, eq) === name) {
                const raw = parts[i].slice(eq + 1);
                try { return decodeURIComponent(raw); } catch (e) { return raw; }
            }
        }
        return null;
    },

    async apiFetch(url, options = {}) {
        const method = (options.method || "GET").toUpperCase();
        const isSafeMethod = method === "GET" || method === "HEAD" || method === "OPTIONS";
        const headers = { ...(options.headers || {}) };
        // Double-submit cookie : toute requête mutante doit porter le
        // X-CSRF-Token lu depuis le cookie csrf_token (voir docs/csrf-protection.md).
        // La page settings ne charge pas api.js (wrapper global window.fetch),
        // donc on ajoute l'en-tête ici, de façon autonome.
        if (!isSafeMethod && !headers["X-CSRF-Token"]) {
            const token = this.getCookie("csrf_token");
            if (token) headers["X-CSRF-Token"] = token;
        }
        try {
            const resp = await fetch(url, {
                ...options,
                headers,
                credentials: "same-origin",
            });
            if (resp.status === 401) {
                window.location.href = "/login";
                return null;
            }
            return await resp.json();
        } catch (e) {
            console.error("API error:", e);
            this.showToast("Erreur réseau: " + e.message, "error");
            return null;
        }
    },

    async apiPost(url, body) {
        return this.apiFetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        });
    },

    async apiPut(url, body) {
        return this.apiFetch(url, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        });
    },

    async apiDelete(url) {
        return this.apiFetch(url, { method: "DELETE" });
    },

    showToast(message, type = "info") {
        const toast = document.getElementById("toast");
        if (!toast) return;
        toast.textContent = message;
        toast.className = "toast " + type;
        toast.classList.remove("hidden");
        setTimeout(() => toast.classList.add("hidden"), 3000);
    },

    escapeHtml(text) {
        if (text === null || text === undefined) return "";
        const div = document.createElement("div");
        div.textContent = String(text);
        return div.innerHTML;
    },

    setLLMStatus(state, text) {
        const el = document.getElementById("llm-status");
        if (!el) return;
        el.className = "status-indicator status-" + state;
        el.textContent = text;
    },

    // -------------------------------------------------------
    // LLM configuration
    // -------------------------------------------------------

    async loadLLMConfig() {
        const data = await this.apiFetch("/api/settings/llm");
        if (!data) return;
        document.getElementById("llm-endpoint").value = data.endpoint || "";
        document.getElementById("llm-api-key").value = "";
        document.getElementById("llm-api-key").placeholder = data.api_key || "••••••••";
        const modelSelect = document.getElementById("llm-model");
        const currentModel = data.model || "";
        if (modelSelect) {
            modelSelect.innerHTML = '';
            if (currentModel) {
                const opt = document.createElement("option");
                opt.value = currentModel;
                opt.textContent = currentModel + " (non scanné)";
                modelSelect.appendChild(opt);
                modelSelect.value = currentModel;
            } else {
                const opt = document.createElement("option");
                opt.value = "";
                opt.textContent = "-- Configurer l'endpoint puis scanner --";
                modelSelect.appendChild(opt);
            }
        }
        document.getElementById("firecrawl-endpoint").value = data.firecrawl_endpoint || "";
        document.getElementById("firecrawl-api-key").value = "";
        document.getElementById("firecrawl-api-key").placeholder = data.firecrawl_key || "••••••••";
    },

    async saveLLMConfig() {
        const body = {
            endpoint: document.getElementById("llm-endpoint").value.trim(),
            api_key: document.getElementById("llm-api-key").value,
            model: document.getElementById("llm-model").value.trim(),
            firecrawl_endpoint: document.getElementById("firecrawl-endpoint").value.trim(),
            firecrawl_key: document.getElementById("firecrawl-api-key").value,
        };
        const data = await this.apiPut("/api/settings/llm", body);
        if (!data) return;
        if (data.success) {
            this.showToast("Configuration LLM sauvegardée.", "success");
            this.loadLLMConfig();
        } else {
            this.showToast(data.detail || "Erreur lors de la sauvegarde.", "error");
        }
    },

    async testLLM() {
        this.setLLMStatus("unknown", "Test en cours…");
        const btn = document.getElementById("llm-test-btn");
        if (btn) btn.disabled = true;
        const data = await this.apiPost("/api/settings/llm/test");
        if (btn) btn.disabled = false;
        if (!data) {
            this.setLLMStatus("offline", "Erreur réseau");
            return;
        }
        if (data.success) {
            this.setLLMStatus("online", "Connecté ✓");
            this.showToast("Connexion LLM réussie.", "success");
        } else {
            this.setLLMStatus("offline", "Échec ✗");
            this.showToast(data.detail || "Connexion LLM échouée.", "error");
        }
    },

    async scanModels() {
        const endpoint = document.getElementById("llm-endpoint").value.trim();
        const apiKey = document.getElementById("llm-api-key").value.trim();

        if (!endpoint) {
            this.showToast("Veuillez configurer l'endpoint d'abord.", "error");
            return;
        }

        const modelSelect = document.getElementById("llm-model");
        const previousValue = modelSelect ? modelSelect.value : "";
        if (modelSelect) {
            modelSelect.innerHTML = '<option value="">Scan en cours…</option>';
            modelSelect.disabled = true;
        }
        const btn = document.getElementById("scan-models-btn");
        if (btn) btn.disabled = true;

        const data = await this.apiPost("/api/settings/llm/models", { endpoint, api_key: apiKey });

        if (btn) btn.disabled = false;
        if (!modelSelect) return;

        if (data && data.success && data.models && data.models.length > 0) {
            modelSelect.innerHTML = '<option value="">-- Choisir un modèle --</option>';
            data.models.forEach((m) => {
                const opt = document.createElement("option");
                opt.value = m;
                opt.textContent = m;
                if (m === previousValue) opt.selected = true;
                modelSelect.appendChild(opt);
            });
            modelSelect.disabled = false;
            this.showToast(data.models.length + " modèle(s) disponible(s).", "success");
        } else {
            modelSelect.innerHTML = '<option value="">Aucun modèle trouvé</option>';
            modelSelect.disabled = false;
            const err = (data && data.error) ? data.error : "Aucun modèle trouvé.";
            this.showToast(err, "error");
        }
    },

    // -------------------------------------------------------
    // Agents management
    // -------------------------------------------------------

    async loadAgents() {
        const data = await this.apiFetch("/api/settings/agents");
        if (!data) return;
        this.agents = Array.isArray(data) ? data : [];
        this.renderAgents();
    },

    renderAgents() {
        const container = document.getElementById("agents-list");
        if (!container) return;
        if (this.agents.length === 0) {
            container.innerHTML = '<p class="placeholder-hint">Aucun agent configuré. Cliquez sur « Ajouter un agent ».</p>';
            return;
        }
        container.innerHTML = this.agents.map((a) => {
            const statusClass = a.status === "online" ? "status-online"
                : a.status === "offline" ? "status-offline"
                : "status-unknown";
            const statusText = a.status === "online" ? "En ligne"
                : a.status === "offline" ? "Hors ligne"
                : "Inconnu";
            return `
                <div class="agent-row" data-name="${this.escapeHtml(a.name)}">
                    <div class="agent-row-info">
                        <span class="agent-row-name">${this.escapeHtml(a.name)}</span>
                        <span class="agent-row-url">${this.escapeHtml(a.url)}</span>
                    </div>
                    <span class="status-indicator ${statusClass}">${statusText}</span>
                    <div class="agent-row-actions">
                        <button class="btn btn-ghost btn-sm" onclick="SettingsApp.testAgent('${this.escapeHtml(a.name)}')">Tester</button>
                        <button class="btn btn-ghost btn-sm" onclick="SettingsApp.showAgentForm(${JSON.stringify(a).replace(/"/g, '&quot;')})">Éditer</button>
                        <button class="btn btn-danger btn-sm" onclick="SettingsApp.deleteAgent('${this.escapeHtml(a.name)}')">Supprimer</button>
                    </div>
                </div>`;
        }).join("");
    },

    async testAgent(name) {
        this.showToast("Test de l'agent " + name + "…", "info");
        const data = await this.apiPost("/api/settings/agents/" + encodeURIComponent(name) + "/test");
        if (!data) return;
        if (data.success) {
            this.showToast("Agent " + name + " en ligne ✓", "success");
        } else {
            this.showToast("Agent " + name + " hors ligne ✗", "error");
        }
        this.loadAgents();
    },

    showAgentForm(agent) {
        const modal = document.getElementById("agent-modal");
        const title = document.getElementById("agent-modal-title");
        const nameInput = document.getElementById("agent-name");
        const urlInput = document.getElementById("agent-url");
        const keyInput = document.getElementById("agent-api-key");
        const keyHint = document.getElementById("agent-key-hint");

        if (agent) {
            this.editingAgentName = agent.name;
            title.innerHTML = '<i data-lucide="pen-square"></i> Éditer l\'agent';
            nameInput.value = agent.name || "";
            urlInput.value = agent.url || "";
            keyInput.value = "";
            keyInput.placeholder = agent.api_key || "••••••••";
            if (keyHint) keyHint.textContent = "Laisser vide pour ne pas changer.";
            this.renderPathMappings(agent.path_mappings || []);
        } else {
            this.editingAgentName = null;
            title.innerHTML = '<i data-lucide="plus"></i> Ajouter un agent';
            nameInput.value = "";
            urlInput.value = "";
            keyInput.value = "";
            keyInput.placeholder = "••••••••";
            if (keyHint) keyHint.textContent = "Clé API de l'agent.";
            this.renderPathMappings([]);
        }
        modal.classList.remove("hidden");
        if (typeof lucide !== 'undefined') lucide.createIcons();
    },

    closeAgentForm() {
        const modal = document.getElementById("agent-modal");
        if (modal) modal.classList.add("hidden");
        this.editingAgentName = null;
    },

    // -------------------------------------------------------
    // Path mappings (host → local)
    // -------------------------------------------------------

    renderPathMappings(mappings) {
        const list = document.getElementById('path-mappings-list');
        if (!list) return;
        mappings = mappings || [];
        let html = '';
        mappings.forEach((m) => {
            html += '<div class="path-mapping-row">' +
                '<input type="text" class="form-input path-mapping-host" placeholder="/chemin/hote" value="' + this.escapeHtml(m.host || '') + '">' +
                '<span>→</span>' +
                '<input type="text" class="form-input path-mapping-local" placeholder="/chemin/local" value="' + this.escapeHtml(m.local || '') + '">' +
                '<button type="button" onclick="SettingsApp.removePathMapping(this)"><i data-lucide="x"></i></button>' +
                '</div>';
        });
        list.innerHTML = html;
    },

    addPathMapping() {
        const list = document.getElementById('path-mappings-list');
        if (!list) return;
        const div = document.createElement('div');
        div.className = 'path-mapping-row';
        div.innerHTML = '<input type="text" class="form-input path-mapping-host" placeholder="/chemin/hote">' +
            '<span>→</span>' +
            '<input type="text" class="form-input path-mapping-local" placeholder="/chemin/local">' +
            '<button type="button" onclick="SettingsApp.removePathMapping(this)"><i data-lucide="x"></i></button>';
        list.appendChild(div);
    },

    removePathMapping(btn) {
        btn.parentElement.remove();
    },

    collectPathMappings() {
        const list = document.getElementById('path-mappings-list');
        if (!list) return [];
        const rows = list.querySelectorAll('.path-mapping-row');
        const mappings = [];
        rows.forEach(row => {
            const host = row.querySelector('.path-mapping-host').value.trim();
            const local = row.querySelector('.path-mapping-local').value.trim();
            if (host && local) {
                mappings.push({ host, local });
            }
        });
        return mappings;
    },

    async submitAgentForm() {
        const name = document.getElementById("agent-name").value.trim();
        const url = document.getElementById("agent-url").value.trim();
        const apiKey = document.getElementById("agent-api-key").value;
        const pathMappings = this.collectPathMappings();
        if (!name || !url) {
            this.showToast("Le nom et l'URL sont requis.", "error");
            return;
        }
        if (this.editingAgentName) {
            const data = await this.apiPut(
                "/api/settings/agents/" + encodeURIComponent(this.editingAgentName),
                { name, url, api_key: apiKey, path_mappings: pathMappings }
            );
            if (!data) return;
            if (data.success) {
                this.showToast("Agent mis à jour.", "success");
                this.closeAgentForm();
                this.loadAgents();
            } else {
                this.showToast(data.detail || "Erreur lors de la mise à jour.", "error");
            }
        } else {
            const data = await this.apiPost("/api/settings/agents", { name, url, api_key: apiKey, path_mappings: pathMappings });
            if (!data) return;
            if (data.success) {
                this.showToast("Agent ajouté.", "success");
                this.closeAgentForm();
                this.loadAgents();
            } else {
                this.showToast(data.detail || "Erreur lors de l'ajout.", "error");
            }
        }
    },

    deleteAgent(name) {
        this.pendingDeleteAgent = name;
        // Pilote HolafModal : remplace l'ancienne modale statique #delete-agent-modal
        // par une modale HolafModal.open() (même contenu/comportement).
        HolafModal.open({
            title: "🗑 Supprimer l'agent",
            content: "<p>Supprimer l'agent <strong>" + this.escapeHtml(name) + "</strong> ?</p>"
                + '<p class="form-hint danger-text">⚠ Cette action est irréversible.</p>',
            size: "sm",
            buttons: [
                { text: "Annuler", value: false, type: "cancel" },
                { text: "Confirmer", value: true, type: "danger", onClick: () => this.confirmDeleteAgent() },
            ],
            onClose: () => { this.pendingDeleteAgent = null; },
        });
    },

    closeDeleteAgent() {
        // La fermeture est gérée par HolafModal (boutons / Échap). On ne fait
        // que purger l'état cible pour préserver l'API existante.
        this.pendingDeleteAgent = null;
    },

    async confirmDeleteAgent() {
        if (!this.pendingDeleteAgent) return;
        const name = this.pendingDeleteAgent;
        this.pendingDeleteAgent = null;
        const data = await this.apiDelete("/api/settings/agents/" + encodeURIComponent(name));
        if (!data) return;
        if (data.success) {
            this.showToast("Agent supprimé.", "success");
            this.loadAgents();
        } else {
            this.showToast(data.detail || "Erreur lors de la suppression.", "error");
        }
    },

    // -------------------------------------------------------
    // Password change
    // -------------------------------------------------------

    async changePassword() {
        const current = document.getElementById("current-password").value;
        const newPwd = document.getElementById("new-password").value;
        const confirm = document.getElementById("confirm-password").value;

        if (!current || !newPwd || !confirm) {
            this.showToast("Tous les champs sont requis.", "error");
            return;
        }

        if (newPwd !== confirm) {
            this.showToast("Les mots de passe ne correspondent pas.", "error");
            return;
        }

        if (newPwd.length < 6) {
            this.showToast("Le mot de passe doit faire au moins 6 caractères.", "error");
            return;
        }

        const data = await this.apiPut("/api/settings/password", {
            current_password: current,
            new_password: newPwd,
        });
        if (!data) return;

        if (data.success) {
            this.showToast("Mot de passe changé avec succès.", "success");
            document.getElementById("current-password").value = "";
            document.getElementById("new-password").value = "";
            document.getElementById("confirm-password").value = "";
        } else {
            this.showToast(data.detail || data.error || "Erreur lors du changement de mot de passe.", "error");
        }
    },

    // -------------------------------------------------------
    // Git history retention
    // -------------------------------------------------------

    async loadGitHistorySettings() {
        const data = await this.apiFetch("/api/settings/git-history");
        if (!data) return;
        const input = document.getElementById("git-history-retention");
        if (input) {
            input.value = data.max_versions || 50;
        }
    },

    async saveGitHistorySettings() {
        const input = document.getElementById("git-history-retention");
        if (!input) return;
        const maxVersions = parseInt(input.value, 10);
        if (isNaN(maxVersions) || maxVersions < 5 || maxVersions > 500) {
            this.showToast("Veuillez entrer un nombre entre 5 et 500.", "error");
            return;
        }
        const data = await this.apiPut("/api/settings/git-history", {
            max_versions: maxVersions
        });
        if (!data) return;
        if (data.success) {
            this.showToast("Configuration de l'historique sauvegardée.", "success");
        } else {
            this.showToast(data.detail || "Erreur lors de la sauvegarde.", "error");
        }
    },

    // -------------------------------------------------------
    // API MCP
    // -------------------------------------------------------

    async loadMcpSettings() {
        const data = await this.apiFetch("/api/settings/mcp");
        if (!data) return;
        const input = document.getElementById("mcp-api-key");
        if (input) {
            input.value = data.api_key || "";
            input.type = this.mcpKeyVisible ? "text" : "password";
        }
        const status = document.getElementById("mcp-status");
        if (status) {
            status.className = "status-indicator " + (data.enabled ? "status-online" : "status-offline");
            status.textContent = data.enabled ? "Activé" : "Désactivé";
        }
        const btn = document.getElementById("mcp-toggle-btn");
        if (btn) btn.textContent = this.mcpKeyVisible ? "Masquer" : "Afficher";
    },

    toggleMcpKey() {
        this.mcpKeyVisible = !this.mcpKeyVisible;
        const input = document.getElementById("mcp-api-key");
        if (input) input.type = this.mcpKeyVisible ? "text" : "password";
        const btn = document.getElementById("mcp-toggle-btn");
        if (btn) btn.textContent = this.mcpKeyVisible ? "Masquer" : "Afficher";
    },

    async copyMcpKey() {
        const input = document.getElementById("mcp-api-key");
        if (!input || !input.value) {
            this.showToast("Aucune clé à copier.", "error");
            return;
        }
        try {
            await navigator.clipboard.writeText(input.value);
            this.showToast("Clé API MCP copiée.", "success");
        } catch (e) {
            this.showToast("Impossible de copier: " + e.message, "error");
        }
    },

    async regenerateMcpKey() {
        if (!window.confirm(
            "Régénérer la clé API MCP ?\n\n" +
            "Cette action invalide immédiatement tous les clients MCP connectés. " +
            "Ils devront se reconnecter avec la nouvelle clé."
        )) {
            return;
        }
        const data = await this.apiPost("/api/settings/mcp/regenerate");
        if (!data) return;
        if (data.success) {
            this.mcpKeyVisible = true;
            const input = document.getElementById("mcp-api-key");
            if (input) {
                input.value = data.api_key || "";
                input.type = "text";
            }
            const btn = document.getElementById("mcp-toggle-btn");
            if (btn) btn.textContent = "Masquer";
            this.showToast("Clé API MCP régénérée.", "success");
        } else {
            this.showToast(data.detail || "Erreur lors de la régénération.", "error");
        }
    },

    // -------------------------------------------------------
    // Docker Hub
    // -------------------------------------------------------

    /**
     * Calcule l'état du pill de statut Docker Hub à partir de l'état backend
     * CONFIRMÉ (jamais de la valeur locale du formulaire).
     *
     * Logique à 3 états (+1 raffinement quand le résultat de poussée est
     * connu, c.-à-d. en réponse à un PUT/clear) :
     *  - "online"     (vert)  « Activé »   : enabled && has_token — config
     *    complète ; poussée confirmée sur tous les agents en ligne, ou
     *    résultat de poussée inconnu (chargement initial) ;
     *  - "partial"    (ambre) « Partiel »  : enabled && has_token, mais la
     *    poussée n'a pas pu être confirmée partout (agent hors ligne — il
     *    rattrapera à sa reconnexion — ou erreur) ;
     *  - "incomplete" (ambre) « Incomplet » : enabled mais has_token=false —
     *    la config ne peut pas fonctionner sans token ;
     *  - "offline"    (gris)  « Désactivé » : enabled=false.
     */
    dockerhubPillState(enabled, hasToken, push) {
        if (!enabled) return { cls: "status-offline", text: "Désactivé" };
        if (!hasToken) return { cls: "status-warning", text: "Incomplet" };
        if (push && typeof push.total === "number" && push.total > 0) {
            const errCount = push.errors ? Object.keys(push.errors).length : 0;
            if ((typeof push.pushed === "number" && push.pushed < push.total) || errCount > 0) {
                return { cls: "status-partial", text: "Partiel" };
            }
        }
        return { cls: "status-online", text: "Activé" };
    },

    /**
     * Met à jour le pill de statut de la carte Docker Hub. Appelé au
     * chargement (GET) et immédiatement après chaque sauvegarde/désactivation
     * — toujours à partir de l'état confirmé par le backend (payload du GET,
     * ou réponse du PUT/clear qui porte désormais l'état persisté), jamais
     * d'une valeur locale du formulaire.
     */
    renderDockerhubStatus(enabled, hasToken, push) {
        const status = document.getElementById("dockerhub-status");
        if (!status) return;
        const pill = this.dockerhubPillState(enabled, hasToken, push);
        status.className = "status-indicator " + pill.cls;
        status.textContent = pill.text;
        // Détail de la poussée en tooltip quand le résultat par agent est
        // connu et que la config est active (pour un clear le toast le dit).
        if (enabled && push && typeof push.total === "number" && push.total > 0) {
            const errNames = Object.keys(push.errors || {});
            status.title = "Poussé sur " + (push.pushed || 0) + "/" + push.total + " agent(s)" +
                (errNames.length > 0 ? " — " + errNames.join(", ") : "");
        } else {
            status.title = "";
        }
    },

    /**
     * Applique au formulaire + au pill un état backend confirmé : payload du
     * GET (chargement initial) ou réponse du PUT/clear (état persisté +,
     * pour ces derniers, les résultats de poussée par agent). Source unique
     * de vérité = le serveur ; aucune valeur locale n'est réinjectée.
     */
    applyDockerhubState(data, push) {
        this.dockerhubHasToken = !!data.has_token;
        const enabledInput = document.getElementById("dockerhub-enabled");
        if (enabledInput) enabledInput.checked = !!data.enabled;
        const username = document.getElementById("dockerhub-username");
        if (username) username.value = data.username || "";
        const token = document.getElementById("dockerhub-token");
        if (token) {
            token.value = "";
            token.placeholder = data.has_token ? "•••••••• (configuré)" : "••••••••";
        }
        this.renderDockerhubStatus(!!data.enabled, !!data.has_token, push);
    },

    async loadDockerhubSettings() {
        const data = await this.apiFetch("/api/settings/dockerhub");
        if (!data) return;
        // Au chargement, le résultat de poussée n'est pas connu du GET : le
        // pill reflète la config (Activé / Incomplet / Désactivé). Les agents
        // hors ligne rattrapent la config à leur reconnexion.
        this.applyDockerhubState(data);
    },

    async saveDockerhubSettings() {
        const enabledEl = document.getElementById("dockerhub-enabled");
        const usernameEl = document.getElementById("dockerhub-username");
        const tokenEl = document.getElementById("dockerhub-token");
        if (!enabledEl || !usernameEl || !tokenEl) return;

        const enabled = enabledEl.checked;
        const username = usernameEl.value.trim();
        const token = tokenEl.value;

        if (enabled && !username) {
            this.showToast("Veuillez saisir le nom d'utilisateur Docker Hub.", "error");
            return;
        }
        if (enabled && !token && !this.dockerhubHasToken) {
            this.showToast("Veuillez saisir un access token Docker Hub.", "error");
            return;
        }
        // Validation ASCII côté client (le backend renvoie sinon une 400).
        const asciiOnly = (s) => !/[^\x00-\x7F]/.test(s);
        if (!asciiOnly(username)) {
            this.showToast("Le nom d'utilisateur ne doit contenir que des caractères ASCII.", "error");
            return;
        }
        if (token && !asciiOnly(token)) {
            this.showToast("Le token ne doit contenir que des caractères ASCII.", "error");
            return;
        }

        const data = await this.apiPut("/api/settings/dockerhub", {
            enabled,
            username,
            token,
        });
        if (!data) return;
        if (data.success) {
            let msg;
            if (data.total > 0) {
                msg = "Poussé sur " + data.pushed + "/" + data.total + " agents" +
                    (data.pushed < data.total ? " — les agents hors ligne recevront la config à leur reconnexion." : ".");
            } else {
                msg = "Configuration Docker Hub sauvegardée (aucun agent en ligne).";
            }
            const errNames = Object.keys(data.errors || {});
            if (errNames.length > 0) {
                msg += " Erreurs : " + errNames.join(", ") + ".";
            }
            this.showToast(msg, errNames.length > 0 ? "info" : "success");
            // Le PUT renvoie l'état persisté (enabled/has_token confirmés) et
            // les résultats de poussée par agent : le pill et le formulaire
            // sont mis à jour immédiatement depuis CETTE réponse confirmée —
            // pas de valeur locale réinjectée, pas de GET de rattrapage qui
            // pourrait écraser le pill (notamment l'état « Partiel »).
            this.applyDockerhubState(data, {
                pushed: data.pushed,
                total: data.total,
                errors: data.errors,
            });
        } else {
            this.showToast(data.detail || "Erreur lors de la sauvegarde.", "error");
        }
    },

    async clearDockerhub() {
        if (!window.confirm(
            "Désactiver l'authentification Docker Hub ?\n\n" +
            "Les agents en ligne seront déconnectés (docker logout) et les " +
            "identifiants supprimés de la configuration."
        )) {
            return;
        }
        const data = await this.apiPost("/api/settings/dockerhub/clear");
        if (!data) return;
        if (data.success) {
            let msg = "Docker Hub désactivé.";
            if (data.total > 0) {
                msg += " Déconnecté sur " + data.pushed + "/" + data.total + " agents.";
            }
            this.showToast(msg, "success");
            // La réponse du clear porte l'état persisté confirmé
            // (enabled=false, credentials effacés) : l'UI s'aligne dessus.
            this.applyDockerhubState(data, {
                pushed: data.pushed,
                total: data.total,
                errors: data.errors,
            });
        } else {
            this.showToast(data.detail || "Erreur lors de la désactivation.", "error");
        }
    },

    // -------------------------------------------------------
    // Init
    // -------------------------------------------------------

    init() {
        this.loadLLMConfig();
        this.loadAgents();
        this.loadGitHistorySettings();
        this.loadMcpSettings();
        this.loadDockerhubSettings();
    },
};

document.addEventListener("DOMContentLoaded", () => {
    SettingsApp.init();
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
});