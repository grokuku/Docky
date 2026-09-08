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

    async apiFetch(url, options = {}) {
        // Délègue à l'adaptateur HolafFetch (voir holaf-docky-fetch.js).
        // Supprime la DUPLICATION : la page settings n'a plus sa propre
        // implémentation maison (fetch + CSRF manuel + 401 + toast) — la
        // config commune Docky est portée par l'adaptateur : auth CSRF
        // (cookie csrf_token, lu frais à chaque requête), 401 → /login,
        // timeout 30 s, JSON vérifié avant parsing, erreurs typées.
        // Contrat inchangé : données | null, toast avec le message
        // (err.message contient désormais body.detail via la brique v0.1.1).
        return window.DockyFetch.apiRequest(url, options);
    },

    async apiPost(url, body) {
        return this.apiFetch(url, {
            method: "POST",
            body: body || {},   // la brique sérialise en JSON + Content-Type
        });
    },

    async apiPut(url, body) {
        return this.apiFetch(url, {
            method: "PUT",
            body: body || {},   // la brique sérialise en JSON + Content-Type
        });
    },

    async apiDelete(url) {
        return this.apiFetch(url, { method: "DELETE" });
    },

    showToast(message, type = "info") {
        // Adaptateur fin vers la brique HolafToast (voir holaf-docky-toast.js).
        return window.DockyToast.show(message, type);
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
            // data === null : l'adaptateur a déjà toasté l'erreur HTTP — on ne
            // re-toaste pas (règle « une seule notification par erreur »). On
            // n'affiche un toast que si le serveur a répondu sans modèle.
            if (data) {
                const err = data.error ? data.error : "Aucun modèle trouvé.";
                this.showToast(err, "error");
            }
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
        // Fenêtre n°5 migrée vers HolafModal : remplace l'ancienne modale
        // statique #agent-modal. Formulaire agent en mode ADD (agent null) et
        // EDIT (agent fourni). Content dynamique (string) ; les handlers de
        // mappings (add/remove) sont des onclick inline dans le DOM de la
        // modale (fonctionnels), le pré-remplissage EDIT et le rendu des
        // mappings se font via onOpen. IDs conservés (agent-name, agent-url,
        // agent-api-key, agent-key-hint, path-mappings-list) car
        // submitAgentForm()/collectPathMappings() les lisent depuis le DOM.
        const isEdit = !!agent;
        this.editingAgentName = isEdit ? agent.name : null;

        const content = '<div class="form-group">'
            + '<label for="agent-name">Nom</label>'
            + '<input type="text" id="agent-name" placeholder="Serveur Local" autocomplete="off">'
            + '</div>'
            + '<div class="form-group">'
            + '<label for="agent-url">URL</label>'
            + '<input type="text" id="agent-url" placeholder="http://docky-agent:8080" autocomplete="off">'
            + '</div>'
            + '<div class="form-group">'
            + '<label for="agent-api-key">Clé API</label>'
            + '<input type="text" id="agent-api-key" placeholder="••••••••" autocomplete="off" class="form-input input-masked">'
            + '<p class="form-hint" id="agent-key-hint">' + (isEdit ? "Laisser vide pour ne pas changer." : "Clé API de l'agent.") + '</p>'
            + '</div>'
            + '<div class="form-group">'
            + '<label>Mappings de chemins (host → local)</label>'
            + '<div id="path-mappings-list"></div>'
            + '<button type="button" class="btn btn-ghost btn-sm" onclick="SettingsApp.addPathMapping()">+ Ajouter un mapping</button>'
            + '<p class="form-hint">Indiquez les correspondances entre les chemins de l\'hôte et ceux visibles par l\'agent (ex. /mnt/user/appdata → /mnt/user/appdata). L\'orchestrateur appliquera ces mappings lors de l\'import de stacks externes.</p>'
            + '</div>';

        HolafModal.open({
            title: isEdit ? "✏️ Éditer l'agent" : "➕ Ajouter un agent",
            content: content,
            size: "md",
            buttons: [
                { text: "Annuler", value: false, type: "cancel" },
                { text: "Enregistrer", value: true, type: "primary", onClick: () => this.submitAgentForm() },
            ],
            onOpen: () => {
                if (isEdit) {
                    document.getElementById("agent-name").value = agent.name || "";
                    document.getElementById("agent-url").value = agent.url || "";
                    document.getElementById("agent-api-key").value = "";
                    document.getElementById("agent-api-key").placeholder = agent.api_key || "••••••••";
                }
                this.renderPathMappings(isEdit ? (agent.path_mappings || []) : []);
                if (typeof lucide !== 'undefined') lucide.createIcons();
            },
            onClose: () => { this.editingAgentName = null; },
        });
    },

    closeAgentForm() {
        // La fermeture est gérée par HolafModal (boutons / Échap). Stub conservé
        // pour préserver l'API existante.
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
        const ok = await HolafModal.confirm(
            "Régénérer la clé API MCP",
            "Régénérer la clé API MCP ?\n\n" +
            "Cette action invalide immédiatement tous les clients MCP connectés. " +
            "Ils devront se reconnecter avec la nouvelle clé.",
            { danger: true, confirmText: "Régénérer", cancelText: "Annuler" }
        );
        if (!ok) return;
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
    // Registres (multi-registres)
    // -------------------------------------------------------

    /**
     * Calcule l'état du pill d'un registre configuré à partir de l'état
     * backend CONFIRMÉ (jamais de la valeur locale du formulaire).
     *
     *  - "online"  (vert)  « Activé »   : has_token et poussée confirmée sur
     *    tous les agents en ligne (ou résultat de poussée inconnu) ;
     *  - "partial" (ambre) « Partiel »  : has_token mais poussée non confirmée
     *    partout (agent hors ligne — il rattrapera à sa reconnexion — ou erreur) ;
     *  - "warning" (ambre) « Incomplet » : pas de token stocké.
     */
    registryPillState(reg) {
        if (!reg.has_token) return { cls: "status-warning", text: "Incomplet" };
        const ps = reg.push_status || {};
        const statuses = Object.values(ps);
        if (statuses.length > 0) {
            const errCount = statuses.filter((s) => s !== "ok").length;
            if (errCount > 0) return { cls: "status-partial", text: "Partiel" };
        }
        return { cls: "status-online", text: "Activé" };
    },

    renderRegistryStatus(reg) {
        const pill = this.registryPillState(reg);
        const ps = reg.push_status || {};
        const errNames = Object.keys(ps).filter((a) => ps[a] !== "ok");
        return '<span class="status-indicator ' + pill.cls + '" title="'
            + (errNames.length ? "Poussée incomplète : " + errNames.join(", ") : "")
            + '">' + pill.text + '</span>';
    },

    renderRegistries() {
        const container = document.getElementById("registries-list");
        if (!container) return;
        const configured = this.registries || [];
        const configuredUrls = new Set(configured.map((r) => r.url));
        const discovered = (this.discovered || []).filter((u) => !configuredUrls.has(u));

        let html = '';
        if (configured.length === 0 && discovered.length === 0) {
            html = '<p class="placeholder-hint">Aucun registre configuré. Cliquez sur « Ajouter un registre » ou « Scanner ».</p>';
        } else {
            if (configured.length > 0) {
                html += '<div class="registries-section-label">Configurés</div>';
                html += configured.map((r) => {
                    return '<div class="registry-row" data-url="' + this.escapeHtml(r.url) + '">'
                        + '<div class="registry-row-info">'
                        + '<span class="registry-row-url">' + this.escapeHtml(r.url) + '</span>'
                        + '<span class="registry-row-user">' + this.escapeHtml(r.username || "—") + '</span>'
                        + '</div>'
                        + this.renderRegistryStatus(r)
                        + '<div class="registry-row-actions">'
                        + '<button class="btn btn-ghost btn-sm" onclick="SettingsApp.authenticateRegistry(\'' + this.escapeHtml(r.url) + '\')">S\'authentifier</button>'
                        + '<button class="btn btn-danger btn-sm" onclick="SettingsApp.deleteRegistry(\'' + this.escapeHtml(r.url) + '\')">Déconnecter</button>'
                        + '</div>'
                        + '</div>';
                }).join("");
            }
            if (discovered.length > 0) {
                html += '<div class="registries-section-label">Découverts</div>';
                html += discovered.map((u) => {
                    return '<div class="registry-discovered" onclick="SettingsApp.showRegistryForm(\'' + this.escapeHtml(u) + '\')" title="Cliquer pour s\'authentifier">'
                        + '<span class="registry-discovered-url">' + this.escapeHtml(u) + '</span>'
                        + '<span class="registry-discovered-hint">Découvert</span>'
                        + '</div>';
                }).join("");
            }
        }
        container.innerHTML = html;
    },

    async loadRegistries() {
        const data = await this.apiFetch("/api/settings/registries");
        if (!data) return;
        this.registries = Array.isArray(data.registries) ? data.registries : [];
        this.discovered = Array.isArray(data.discovered) ? data.discovered : [];
        this.applyTailscaleState(data.tailscale);
        this.renderRegistries();
    },

    async scanRegistries() {
        this.showToast("Scan des registres en cours…", "info");
        const data = await this.apiFetch("/api/settings/registries?refresh=1");
        if (!data) return;
        this.registries = Array.isArray(data.registries) ? data.registries : [];
        this.discovered = Array.isArray(data.discovered) ? data.discovered : [];
        this.renderRegistries();
        this.showToast("Scan terminé.", "success");
    },

    showRegistryForm(url) {
        // url = registre à authentifier (pré-rempli) ou null (ajout manuel).
        const isAuth = !!url;
        const content = '<div class="form-group">'
            + '<label for="registry-url">URL du registre</label>'
            + '<input type="text" id="registry-url" placeholder="ghcr.io" autocomplete="off">'
            + '<p class="form-hint">Ex. docker.io, ghcr.io, registry.gitlab.com, localhost:5000.</p>'
            + '</div>'
            + '<div class="form-group">'
            + '<label for="registry-username">Nom d\'utilisateur</label>'
            + '<input type="text" id="registry-username" placeholder="moncompte" autocomplete="off">'
            + '</div>'
            + '<div class="form-group">'
            + '<label for="registry-token">Token / mot de passe</label>'
            + '<input type="password" id="registry-token" placeholder="••••••••" autocomplete="new-password" class="form-input input-masked">'
            + '<p class="form-hint">Laisser vide pour ne pas changer.</p>'
            + '</div>';

        HolafModal.open({
            title: isAuth ? "🔐 S'authentifier sur " + url : "➕ Ajouter un registre",
            content: content,
            size: "md",
            buttons: [
                { text: "Annuler", value: false, type: "cancel" },
                { text: "Enregistrer", value: true, type: "primary", onClick: () => this.submitRegistryForm() },
            ],
            onOpen: () => {
                document.getElementById("registry-url").value = url || "";
                if (typeof lucide !== 'undefined') lucide.createIcons();
            },
        });
    },

    authenticateRegistry(url) {
        this.showRegistryForm(url);
    },

    async submitRegistryForm() {
        const url = document.getElementById("registry-url").value.trim();
        const username = document.getElementById("registry-username").value.trim();
        const token = document.getElementById("registry-token").value;
        if (!url) {
            this.showToast("L'URL du registre est requise.", "error");
            return;
        }
        // Validation ASCII côté client (le backend renvoie sinon une 400).
        const asciiOnly = (s) => !/[^\x00-\x7F]/.test(s);
        if (!asciiOnly(url) || !asciiOnly(username) || (token && !asciiOnly(token))) {
            this.showToast("URL, nom d'utilisateur et token doivent être en ASCII.", "error");
            return;
        }
        const data = await this.apiPut("/api/settings/registries", {
            url, username, token,
        });
        if (!data) return;
        if (data.success) {
            let msg = "Registre " + url + " sauvegardé.";
            if (data.total > 0) {
                msg = "Poussé sur " + data.pushed + "/" + data.total + " agents"
                    + (data.pushed < data.total ? " — les agents hors ligne recevront la config à leur reconnexion." : ".");
            }
            const errNames = Object.keys(data.errors || {});
            if (errNames.length > 0) msg += " Erreurs : " + errNames.join(", ") + ".";
            this.showToast(msg, errNames.length > 0 ? "info" : "success");
            this.loadRegistries();
        } else {
            this.showToast(data.detail || "Erreur lors de la sauvegarde.", "error");
        }
    },

    deleteRegistry(url) {
        HolafModal.confirm(
            "Déconnecter " + url,
            "Déconnecter le registre " + url + " ?\n\n" +
            "Les agents en ligne seront déconnectés (docker logout) et le " +
            "registre retiré de la configuration.",
            { danger: true, confirmText: "Déconnecter", cancelText: "Annuler" }
        ).then((ok) => {
            if (!ok) return;
            this.confirmDeleteRegistry(url);
        });
    },

    async confirmDeleteRegistry(url) {
        const data = await this.apiDelete("/api/settings/registries/" + encodeURIComponent(url));
        if (!data) return;
        if (data.success) {
            let msg = "Registre " + url + " déconnecté.";
            if (data.total > 0) msg += " Déconnecté sur " + data.pushed + "/" + data.total + " agents.";
            this.showToast(msg, "success");
            this.loadRegistries();
        } else {
            this.showToast(data.detail || "Erreur lors de la déconnexion.", "error");
        }
    },

    // -------------------------------------------------------
    // Tailscale placeholder (persisté, AUCUN effet — à venir)
    // -------------------------------------------------------

    applyTailscaleState(ts) {
        ts = ts || {};
        const enabled = document.getElementById("tailscale-enabled");
        const host = document.getElementById("tailscale-host");
        if (enabled) enabled.checked = !!ts.enabled;
        if (host) host.value = ts.host || "";
    },

    async saveTailscale() {
        const enabled = document.getElementById("tailscale-enabled");
        const host = document.getElementById("tailscale-host");
        const data = await this.apiPut("/api/settings/registries", {
            tailscale: {
                enabled: enabled ? enabled.checked : false,
                host: host ? host.value.trim() : "",
            },
        });
        if (!data) return;
        if (data.success) {
            this.showToast("Préférence Tailscale sauvegardée (à venir).", "success");
        } else {
            this.showToast(data.detail || "Erreur lors de la sauvegarde.", "error");
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
        this.loadRegistries();
    },
};

document.addEventListener("DOMContentLoaded", () => {
    SettingsApp.init();
    // Placeholder Tailscale : persister la préférence (checkbox + adresse
    // tailnet) sans aucun effet — à venir.
    const tsEnabled = document.getElementById("tailscale-enabled");
    const tsHost = document.getElementById("tailscale-host");
    if (tsEnabled) tsEnabled.addEventListener("change", () => SettingsApp.saveTailscale());
    if (tsHost) tsHost.addEventListener("blur", () => SettingsApp.saveTailscale());
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
});