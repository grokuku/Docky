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

    // -------------------------------------------------------
    // Utilities
    // -------------------------------------------------------

    async apiFetch(url, options = {}) {
        try {
            const resp = await fetch(url, {
                ...options,
                headers: { ...(options.headers || {}) },
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
        document.getElementById("firecrawl-api-key").value = "";
        document.getElementById("firecrawl-api-key").placeholder = data.firecrawl_key || "••••••••";
    },

    async saveLLMConfig() {
        const body = {
            endpoint: document.getElementById("llm-endpoint").value.trim(),
            api_key: document.getElementById("llm-api-key").value,
            model: document.getElementById("llm-model").value.trim(),
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
            title.textContent = "✏ Éditer l'agent";
            nameInput.value = agent.name || "";
            urlInput.value = agent.url || "";
            keyInput.value = "";
            keyInput.placeholder = agent.api_key || "••••••••";
            if (keyHint) keyHint.textContent = "Laisser vide pour ne pas changer.";
        } else {
            this.editingAgentName = null;
            title.textContent = "➕ Ajouter un agent";
            nameInput.value = "";
            urlInput.value = "";
            keyInput.value = "";
            keyInput.placeholder = "••••••••";
            if (keyHint) keyHint.textContent = "Clé API de l'agent.";
        }
        modal.classList.remove("hidden");
    },

    closeAgentForm() {
        const modal = document.getElementById("agent-modal");
        if (modal) modal.classList.add("hidden");
        this.editingAgentName = null;
    },

    async submitAgentForm() {
        const name = document.getElementById("agent-name").value.trim();
        const url = document.getElementById("agent-url").value.trim();
        const apiKey = document.getElementById("agent-api-key").value;
        if (!name || !url) {
            this.showToast("Le nom et l'URL sont requis.", "error");
            return;
        }
        if (this.editingAgentName) {
            const data = await this.apiPut(
                "/api/settings/agents/" + encodeURIComponent(this.editingAgentName),
                { name, url, api_key: apiKey }
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
            const data = await this.apiPost("/api/settings/agents", { name, url, api_key: apiKey });
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
        const el = document.getElementById("delete-agent-name");
        if (el) el.textContent = name;
        document.getElementById("delete-agent-modal").classList.remove("hidden");
    },

    closeDeleteAgent() {
        const modal = document.getElementById("delete-agent-modal");
        if (modal) modal.classList.add("hidden");
        this.pendingDeleteAgent = null;
    },

    async confirmDeleteAgent() {
        if (!this.pendingDeleteAgent) return;
        const name = this.pendingDeleteAgent;
        this.closeDeleteAgent();
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
    // Init
    // -------------------------------------------------------

    init() {
        this.loadLLMConfig();
        this.loadAgents();
    },
};

document.addEventListener("DOMContentLoaded", () => SettingsApp.init());