/* ============================================================
   Docky - Frontend JavaScript (Phase 2 - Dashboard)
   ============================================================ */

const DockyApp = {
    // -------------------------------------------------------
    // State
    // -------------------------------------------------------
    stacks: [],
    _allContainersCache: [],
    _gridLayout: null,
    _gridCellSize: 170,
    _lastGridKey: null,
    _gridResizeObserver: null,
    _gridRenderTimer: null,
    _selectedStack: null,
    expandedStack: null,
    autoRefresh: true,
    refreshInterval: null,
    refreshTimer: 5000,

    _viewMode: 'grid',  // 'grid' ou 'table'

    _composeEditMode: false,

    // Multi-agent
    _hiddenAgents: new Set(),  // Set vide = tous visibles. Les agents dedans sont cachés.
    agentsList: [],              // [{name, status, ...}]
    agentsRefreshInterval: null,
    agentsRefreshTimer: 30000,
    selectedStackAgent: null,    // agent for the currently edited stack
    expandedStackAgent: null,    // agent for the currently expanded stack
    consoleContainerAgent: null, // agent for the container whose console is open

    _pendingFetches: {},  // containerId -> true/false; 'update-'+id for update checks

    // Update indicators
    _updateAvailableCount: 0,     // nombre de containers avec une mise à jour dispo
    _updateCheckToken: 0,         // jeton de rendu pour ignorer les réponses async périmées
    _updateCheckCache: {},        // clé -> dernier résultat de check d'update (source de vérité anti-flicker)
    _versionMismatches: [],       // liste des agents avec version désynchronisée
    _prevMismatchCount: 0,        // dernier nombre de mismatches (pour éviter le spam de toasts)
    _lastVersionCheck: null,      // timestamp du dernier check de versions (ms)
    _versionCheckInterval: null,  // intervalle de check des versions (1h)

    // WebSockets
    consoleWs: null,
    consoleContainerId: null,
    consoleHistory: [],

    // Events WebSocket
    _eventsWs: null,
    _eventsReconnectTimer: null,
    _refreshThrottle: false,

    // Heartbeat
    _heartbeatInterval: null,

    // Chat LLM (Phase 4)
    chatHistory: [],       // array of {role, content} sent to the API
    chatBusy: false,
    chatLLMConfigured: true,
    chatVisible: true,      // whether the chat panel is shown (persisted in localStorage)

    // Sort & Group
    _sortMode: 'name-asc',   // persisted in localStorage
    _groupMode: 'none',      // persisted in localStorage
    _statsCache: {},         // containerId -> { cpu_percent, mem_percent, mem_usage, mem_limit }

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

    async apiPost(url) {
        return this.apiFetch(url, { method: "POST" });
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
        if (!text) return "";
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    },

    formatBytes(bytes) {
        if (!bytes || bytes === 0) return "0 B";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let i = 0;
        let val = bytes;
        while (val >= 1024 && i < units.length - 1) {
            val /= 1024;
            i++;
        }
        return val.toFixed(i > 0 ? 1 : 0) + " " + units[i];
    },

    // Helper pour générer des icônes Lucide
    icon(name, className = '') {
        return `<i data-lucide="${name}" class="${className}"></i>`;
    },

    // -------------------------------------------------------
    // Multi-agent management
    // -------------------------------------------------------

    /** Build the ?agent= query string. Retourne toujours ?agent=all (filtrage côté frontend). */
    agentQueryParam() {
        return '?agent=all';
    },

    /** Build a ?agent= query string for a specific agent. */
    agentQuery(agentName) {
        if (!agentName || agentName === "all") return "";
        return "?agent=" + encodeURIComponent(agentName);
    },

    async loadVersion() {
        try {
            const resp = await fetch("/api/version", { credentials: "same-origin" });
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            const data = await resp.json();
            if (data && data.version) {
                const badge = document.getElementById("version-badge");
                if (badge) badge.textContent = "v" + data.version;
            }
        } catch (e) {
            console.error("Failed to load version:", e);
        }
    },

    async checkVersions() {
        const data = await this.apiFetch("/api/version-check");
        if (data === null) return;
        const mismatches = data.mismatches || [];
        this._versionMismatches = mismatches;
        this._lastVersionCheck = Date.now();
        const badge = document.getElementById("version-mismatch-badge");
        const prev = this._prevMismatchCount || 0;
        if (mismatches.length > 0) {
            // Toast uniquement si de nouveaux mismatches sont détectés (évite le spam)
            if (mismatches.length > prev) {
                const msg = mismatches.map(
                    m => `${m.agent}: ${m.agent_version} (orchestrateur: ${m.orchestrator_version})`
                ).join("; ");
                this.showToast("⚠️ Version mismatch: " + msg, "warning");
            }
            this._prevMismatchCount = mismatches.length;
            if (badge) {
                badge.textContent = "⚠️ " + mismatches.length + " mismatch(s)";
                badge.classList.remove("hidden");
            }
        } else {
            this._prevMismatchCount = 0;
            if (badge) badge.classList.add("hidden");
        }
    },

    _openVersionMismatchModal() {
        const modal = document.getElementById("version-mismatch-modal");
        if (!modal) return;
        const body = document.getElementById("version-mismatch-body");
        // Build list of mismatched agents with versions
        let html = '';
        for (const m of this._versionMismatches || []) {
            html += '<div class="version-mismatch-item">';
            html += '<span class="version-agent">' + this.escapeHtml(m.agent) + '</span>';
            html += '<span class="version-detail">' + this.escapeHtml(m.agent_version) + ' vs ' + this.escapeHtml(m.orchestrator_version) + '</span>';
            html += '</div>';
        }
        if (!html) {
            html = '<p class="placeholder-hint">Aucune désynchronisation détectée.</p>';
        }
        body.innerHTML = html;
        modal.classList.remove("hidden");
    },

    closeVersionMismatch() {
        const modal = document.getElementById("version-mismatch-modal");
        if (modal) modal.classList.add("hidden");
    },


    async loadAgents() {
        const data = await this.apiFetch("/api/agents");
        if (data === null) return;
        // Expecting an array or {agents: [...]}
        this.agentsList = Array.isArray(data) ? data : (data.agents || []);
        this.renderAgentSelector();
        this.updateStatsBar();
    },

    async refreshAgents() {
        await this.apiPost("/api/agents/refresh");
        await this.loadAgents();
    },

    renderAgentSelector() {
        const container = document.getElementById("agent-selector");
        if (!container) return;

        if (this.agentsList.length === 0) {
            container.innerHTML = '<span class="agent-selector-loading">Aucun agent</span>';
            return;
        }

        let html = '<span class="agent-selector-label">Filtrer:</span>';

        for (const agent of this.agentsList) {
            const name = agent.name || agent;
            const status = agent.status || "offline";
            const isOnline = status === "online" || status === "connected" || status === true;
            const dotClass = isOnline ? "online" : "offline";
            const isHidden = this._hiddenAgents.has(name);
            const activeClass = isHidden ? '' : 'active';
            const escapedName = name.replace(/'/g, "\\'");
            html += '<button class="agent-btn ' + activeClass + '" onclick="DockyApp.toggleAgentFilter(\'' + escapedName + '\')" title="' + this.escapeHtml(name) + ' — ' + this.escapeHtml(status) + '">'
                + '<span class="agent-status-dot ' + dotClass + '"></span>'
                + this.escapeHtml(name)
                + '</button>';
        }

        container.innerHTML = html;
    },

    updateStatsBar() {
        const agentsOnline = this.agentsList.filter(
            a => a.status === 'online' || a.status === 'connected'
        ).length;

        let stacks = this.stacks || [];
        let containers = this._allContainersCache || [];

        if (this._hiddenAgents.size > 0) {
            stacks = stacks.filter(s => !this._hiddenAgents.has(s.agent_name || ''));
            containers = containers.filter(c => !this._hiddenAgents.has(c.agent_name || ''));
        }

        const el = id => document.getElementById(id);
        if (el('stats-agents')) el('stats-agents').textContent = agentsOnline;
        if (el('stats-stacks')) el('stats-stacks').textContent = stacks.length;
        if (el('stats-containers')) el('stats-containers').textContent = containers.length;
        if (el('stats-running')) el('stats-running').textContent = containers.filter(c => c.status === 'running').length;
        if (el('stats-updates')) el('stats-updates').textContent = this._updateAvailableCount || 0;
    },

    toggleAgentFilter(name) {
        if (this._hiddenAgents.has(name)) {
            this._hiddenAgents.delete(name);
        } else {
            this._hiddenAgents.add(name);
        }
        localStorage.setItem('docky_hidden_agents', JSON.stringify([...this._hiddenAgents]));
        this.expandedStack = null;
        this.renderAgentSelector();
        // Ne pas fetch tout depuis l'API, juste re-rendre le grid avec le nouveau filtre
        if (this._allContainersCache && this._allContainersCache.length > 0) {
            this.renderCurrentView();
        } else {
            // Premier chargement, pas encore de données
            this.refreshStacks();
        }
        this.updateStatsBar();
        // Refresh ports if panel is open
        const portsPanel = document.getElementById("ports-panel");
        if (portsPanel && !portsPanel.classList.contains("hidden")) {
            this.loadPorts();
        }
    },

    startAgentsRefresh() {
        this.stopAgentsRefresh();
        this.agentsRefreshInterval = setInterval(() => {
            this.loadAgents();
        }, this.agentsRefreshTimer);
    },

    stopAgentsRefresh() {
        if (this.agentsRefreshInterval) {
            clearInterval(this.agentsRefreshInterval);
            this.agentsRefreshInterval = null;
        }
    },

    // -------------------------------------------------------
    // Stacks
    // -------------------------------------------------------

    async refreshStacks() {
        // Toujours fetch avec ?agent=all (filtrage côté frontend)
        const [stacksResp, containersResp] = await Promise.all([
            this.apiFetch("/api/stacks?agent=all"),
            fetch('/api/containers?agent=all', { credentials: "same-origin" })
        ]);

        if (stacksResp === null) return;
        this.stacks = stacksResp;

        // Parse containers
        let containersData = [];
        if (containersResp) {
            if (containersResp.status === 401) {
                window.location.href = "/login";
                return;
            }
            if (containersResp.status === 200) {
                try {
                    containersData = await containersResp.json();
                    if (!Array.isArray(containersData)) containersData = [];
                } catch (e) {
                    containersData = [];
                }
            }
        }
        this._allContainersCache = containersData;

        // Skip re-render if nothing changed
        const gridKey = JSON.stringify(stacksResp) + '|' + JSON.stringify(this._allContainersCache);
        if (this._lastGridKey === gridKey) return;
        this._lastGridKey = gridKey;

        this.renderCurrentView();
        this.updateStatsBar();
        this.updateStackSelector(stacksResp);
    },

    updateStackSelector(stacks) {
        const selector = document.getElementById("stack-selector");
        if (!selector) return;
        selector.innerHTML = '<option value="">-- Choisir une stack --</option>';
        for (const stack of stacks) {
            // Only managed stacks are editable; skip external and standalone
            if (stack.managed === false) continue;
            const opt = document.createElement("option");
            opt.value = stack.name + '@' + (stack.agent_name || '');
            const agentLabel = stack.agent_name ? ' (@' + stack.agent_name + ')' : '';
            opt.textContent = stack.name + agentLabel;
            selector.appendChild(opt);
        }
    },

    renderStacks() {
        const container = document.getElementById("dashboard-content");
        if (!container) return;
        this._pruneUpdateCache();
        this._updateAvailableCount = this._countCachedUpdates();
        this._updateCheckToken = (this._updateCheckToken || 0) + 1;

        if (this.stacks.length === 0) {
            container.innerHTML = `
                <div class="placeholder">
                    <p>📭 Aucune stack trouvée</p>
                    <p class="placeholder-hint">Ajoutez des stacks dans /data/stacks/</p>
                </div>`;
            return;
        }

        let html = '<div class="stacks-list">';
        this.stacks.forEach((stack) => {
            const compositeKey = stack.name + '@' + (stack.agent_name || '');
            const isExpanded = this.expandedStack === compositeKey;
            const statusBadge = this.statusBadge(stack.status);
            const containerInfo = stack.container_count > 0
                ? `${stack.running_count}/${stack.container_count} actifs`
                : "0 containers";
            const portsInfo = stack.ports && stack.ports.length > 0
                ? stack.ports.join(", ")
                : "";
            const agentBadge = stack.agent_name
                ? '<span class="stack-agent-badge">' + this.icon('terminal') + ' ' + this.escapeHtml(stack.agent_name) + '</span>'
                : "";
            // Managed / external / standalone indicator
            const isManaged = stack.managed !== false;
            const isStandalone = stack.standalone === true;
            let typeBadge = '';
            if (isStandalone) {
                typeBadge = '<span class="stack-type-badge stack-badge-standalone">standalone</span>';
            } else if (!isManaged) {
                typeBadge = '<span class="stack-type-badge stack-badge-external">externe</span>';
            } else {
                typeBadge = '<span class="stack-type-badge stack-badge-docky">' + this.escapeHtml(stack.agent_name || stack.agent || 'agent') + '</span>';
            }
            // Edit button only for managed stacks (files are editable)
            const escapedAgent = this.escapeHtml(stack.agent_name || '');
            const editBtn = isManaged
                ? '<button class="icon-btn" title="Éditer" onclick="DockyApp.selectStackFromDashboard(\'' + this.escapeHtml(stack.name) + '\', \'' + escapedAgent + '\')">' + this.icon('pen-square') + '</button>'
                : '';
            // One-click import button for external stacks (not standalone)
            const importBtn = (!isManaged && !isStandalone)
                ? '<button class="icon-btn" title="Importer dans Docky" onclick="DockyApp.importExternal(\'' + this.escapeHtml(stack.source_path || '') + '\', \'' + this.escapeHtml(stack.name) + '\', \'' + escapedAgent + '\')">' + this.icon('download') + '</button>'
                : '';
            // Stack-level start/stop/restart only for real stacks (not standalone)
            const stackActionBtns = isStandalone
                ? ''
                : '<button class="icon-btn btn-start" title="Démarrer" onclick="DockyApp.stackAction(\'' + this.escapeHtml(stack.name) + '\', \'start\', \'' + escapedAgent + '\')">' + this.icon('play') + '</button>'
                  + '<button class="icon-btn btn-stop" title="Arrêter" onclick="DockyApp.stackAction(\'' + this.escapeHtml(stack.name) + '\', \'stop\', \'' + escapedAgent + '\')">' + this.icon('square') + '</button>'
                  + '<button class="icon-btn btn-restart" title="Redémarrer" onclick="DockyApp.stackAction(\'' + this.escapeHtml(stack.name) + '\', \'restart\', \'' + escapedAgent + '\')">' + this.icon('refresh-cw') + '</button>'
                  + '<button class="icon-btn" title="Update" onclick="DockyApp.stackAction(\'' + this.escapeHtml(stack.name) + '\', \'update\', \'' + escapedAgent + '\')">' + this.icon('arrow-up') + '</button>'
                  + '<button class="icon-btn btn-logs" title="Logs" onclick="DockyApp.openStackLogs(\'' + this.escapeHtml(stack.name) + '\', \'' + escapedAgent + '\')">' + this.icon('clipboard-list') + '</button>';
            const stackUpdateKey = this._stackUpdateCacheKey(stack.name, stack.agent_name || '');
            const updateBadge = isStandalone
                ? ''
                : '<button class="update-badge ' + this._updateBadgeClass(stackUpdateKey) + '" id="stack-update-card-' + this.escapeHtml(stack.name) + '@' + escapedAgent + '" onclick="event.stopPropagation();DockyApp.stackAction(\'' + this.escapeHtml(stack.name) + '\', \'update\', \'' + escapedAgent + '\')" title="Mise à jour disponible">' + this.icon('arrow-up') + ' Update</button>';

            html += `
                <div class="stack-card ${isExpanded ? "expanded" : ""}" data-stack="${this.escapeHtml(stack.name)}" data-agent="${escapedAgent}">
                    <div class="stack-card-header" onclick="DockyApp.toggleStack('${this.escapeHtml(stack.name)}', '${escapedAgent}')">
                        <div class="stack-card-info">
                            <span class="stack-name">${this.escapeHtml(stack.name)}</span>
                            ${typeBadge}
                            ${agentBadge}
                            ${statusBadge}
                        </div>
                        <div class="stack-card-meta">
                            <span class="meta-badge">🐳 ${containerInfo}</span>
                            ${portsInfo ? `<span class="meta-badge meta-ports">${this.icon('cable')} ${this.escapeHtml(portsInfo)}</span>` : ""}
                        </div>
                        <div class="stack-card-actions" onclick="event.stopPropagation()">
                            ${editBtn}
                            ${importBtn}
                            ${stackActionBtns}
                            ${updateBadge}
                            <span class="stack-chevron">${isExpanded ? "▼" : "▶"}</span>
                        </div>
                    </div>
                    <div class="stack-containers ${isExpanded ? "" : "hidden"}" id="containers-${this.escapeHtml(stack.name)}@${escapedAgent}">
                        <div class="placeholder"><p>Chargement des containers…</p></div>
                    </div>
                </div>`;
        });
        html += "</div>";
        container.innerHTML = html;

        if (typeof lucide !== 'undefined') {
            lucide.createIcons();
        }

        // Check léger et caché des mises à jour de stack
        for (const stack of this.stacks) {
            if (stack.standalone !== true) {
                this.checkStackUpdate(stack.name, stack.agent_name || '', this._updateCheckToken);
            }
        }

        // If a stack is expanded, load its containers
        if (this.expandedStack) {
            const atIdx = this.expandedStack.lastIndexOf('@');
            const expName = atIdx > 0 ? this.expandedStack.substring(0, atIdx) : this.expandedStack;
            const expAgent = atIdx > 0 ? this.expandedStack.substring(atIdx + 1) : '';
            this.loadContainers(expName, expAgent);
        }
    },

    statusBadge(status) {
        const map = {
            running: '<span class="status-badge status-running">● running</span>',
            stopped: '<span class="status-badge status-stopped">● stopped</span>',
            partial: '<span class="status-badge status-partial">● partial</span>',
            empty: '<span class="status-badge status-empty">● empty</span>',
        };
        return map[status] || map.empty;
    },

    containerStatusBadge(status, health) {
        let cls = "status-running";
        if (status === "exited" || status === "stopped") cls = "status-stopped";
        if (status === "restarting" || status === "paused") cls = "status-partial";
        if (status === "dead" || status === "error") cls = "status-stopped";
        let label = status;
        if (health && health !== "none") {
            label += ` (${health})`;
        }
        return `<span class="status-badge ${cls}">● ${this.escapeHtml(label)}</span>`;
    },

    async toggleStack(name, agent) {
        const key = name + '@' + (agent || '');
        if (this.expandedStack === key) {
            this.expandedStack = null;
        } else {
            this.expandedStack = key;
        }
        this.renderStacks();
    },

    loadContainers(stackName, agent) {
        const target = document.getElementById("containers-" + stackName + "@" + (agent || ''));
        if (!target) return;
        // Trouver l'objet stack avec la clé composite name@agent
        const stack = this.stacks.find(s => s.name === stackName && (s.agent_name||'') === (agent||''));
        this.expandedStackAgent = agent || null;
        // Display instantly from the pre-loaded cache (no API call)
        const containers = (this._allContainersCache || []).filter(c => {
            if (stackName === 'Standalone') return !c.stack;
            return c.stack === stackName && (c.agent_name||'') === (agent||'');
        });
        this.renderContainers(target, containers, stackName, agent);
    },

    renderContainers(target, containers, stackName, agent) {
        if (!containers || !Array.isArray(containers) || containers.length === 0) {
            target.innerHTML = '<div style="color: var(--text-secondary); padding: 12px;">Aucun container ou erreur de chargement</div>';
            return;
        }

        let html = '<div class="containers-list">';
        const agt = (agent || "").replace(/'/g, "\\'");
        for (const c of containers) {
            const ports = (c.ports || [])
                .filter(p => p.host_port)
                .map(p => `${p.host_port}→${p.container}`)
                .join(", ");
            const statusBadge = this.containerStatusBadge(c.status, c.health);
            const image = this.escapeHtml(c.image);
            const name = this.escapeHtml(c.name);

            html += `
                <div class="container-card" data-id="${this.escapeHtml(c.id)}">
                    <div class="container-main">
                        <div class="container-name">
                            <span class="container-name-text">${name}</span>
                            ${statusBadge}
                        </div>
                        <div class="container-image">${this.icon('package')} ${image}</div>
                    </div>
                    <div class="container-resources" id="resources-${this.escapeHtml(c.id)}">
                        <div class="resource-line">
                            <span class="resource-label">CPU</span>
                            <div class="progress-bar"><div class="progress-fill" style="width:0%"></div></div>
                            <span class="resource-value">—</span>
                        </div>
                        <div class="resource-line">
                            <span class="resource-label">RAM</span>
                            <div class="progress-bar"><div class="progress-fill ram" style="width:0%"></div></div>
                            <span class="resource-value">—</span>
                        </div>
                    </div>
                    <div class="container-extra">
                        ${ports ? `<span class="meta-badge meta-ports">${this.icon('cable')} ${this.escapeHtml(ports)}</span>` : ""}
                        <button class="update-badge ${this._updateBadgeClass(this._containerUpdateCacheKey(c.id))}" id="update-${this.escapeHtml(c.id)}" onclick="DockyApp.containerAction('${this.escapeHtml(c.id)}', 'update-image', '${agt}')" title="Mettre à jour">${this.icon('arrow-up')} Update dispo</button>
                    </div>
                    <div class="container-actions">
                        <button class="icon-btn btn-start" title="Start" onclick="DockyApp.containerAction('${this.escapeHtml(c.id)}', 'start', '${agt}')">${this.icon('play')}</button>
                        <button class="icon-btn btn-stop" title="Stop" onclick="DockyApp.containerAction('${this.escapeHtml(c.id)}', 'stop', '${agt}')">${this.icon('square')}</button>
                        <button class="icon-btn btn-restart" title="Restart" onclick="DockyApp.containerAction('${this.escapeHtml(c.id)}', 'restart', '${agt}')">${this.icon('refresh-cw')}</button>
                        <button class="icon-btn btn-logs" title="Logs" onclick="DockyApp.openLogs('${this.escapeHtml(c.id)}', '${name}', '${agt}')">${this.icon('clipboard-list')}</button>
                        <button class="icon-btn btn-console" title="Console" onclick="DockyApp.openConsole('${this.escapeHtml(c.id)}', '${name}', '${agt}')">${this.icon('terminal')}</button>
                        <button class="icon-btn btn-update" title="Update" onclick="DockyApp.containerAction('${this.escapeHtml(c.id)}', 'update-image', '${agt}')">${this.icon('arrow-up')}</button>
                    </div>
                </div>`;
        }
        html += "</div>";
        target.innerHTML = html;

        if (typeof lucide !== 'undefined') {
            lucide.createIcons();
        }

        // Load resources for running containers; check updates for all
        // containers (the check is now lightweight and cached).
        for (const c of containers) {
            if (c.status === "running") {
                this.loadContainerStats(c.id, agent);
            }
            this.checkUpdate(c.id, agent, this._updateCheckToken);
        }
    },

    // -------------------------------------------------------
    // Grid Dashboard (Option B)
    // -------------------------------------------------------

    renderGridDashboard() {
        const container = document.getElementById("dashboard-content");
        if (!container) return;
        this._pruneUpdateCache();
        this._updateAvailableCount = this._countCachedUpdates();
        this._updateCheckToken = (this._updateCheckToken || 0) + 1;
        
        if (this.stacks.length === 0) {
            container.innerHTML = '<div class="placeholder"><p>📭 Aucune stack trouvée</p></div>';
            return;
        }
        
        const availWidth = container.clientWidth - 36;
        if (availWidth < 200) return;
        
        const gap = 8;
        const minCell = 140;
        const maxCell = 220;
        
        // Appliquer le tri et le groupement
        const sortedStacks = this._sortStacks(this.stacks);
        const allContainers = this._allContainersCache || [];
        const groups = this._groupStacks(sortedStacks);
        
        // Grouper les containers par stack et calculer maxCols
        const stackGroups = [];
        let maxStackCols = 1;
        for (const group of groups) {
            for (const stack of group.stacks) {
                let containers = allContainers.filter(c => {
                    if (stack.name === 'Standalone') return !c.stack;
                    return c.stack === stack.name && (c.agent_name||'') === (stack.agent_name||'');
                });
                if (containers.length === 0) continue;

                // Si des filtres d'agents sont actifs, ignorer les stacks dont l'agent est caché
                if (this._hiddenAgents.size > 0) {
                    const stackAgent = stack.agent_name || '';
                    if (this._hiddenAgents.has(stackAgent)) {
                        continue; // Stack ignorée si son agent est caché
                    }
                }

                // Trier les containers selon le mode de tri
                containers = this._sortContainers(containers);
                
                const n = containers.length;
                const cols = Math.max(1, Math.ceil(n / 2));
                maxStackCols = Math.max(maxStackCols, cols);
                stackGroups.push({ stack, containers, cols, n, color: this.stackColor(stack.name), groupLabel: group.label });
            }
        }
        
        if (stackGroups.length === 0) {
            container.innerHTML = '<div class="placeholder"><p>🔇 Aucun agent affiché</p><p class="placeholder-hint">Active des agents via les boutons de filtre</p></div>';
            return;
        }
        
        // Grid width = maxStackCols (garantit que chaque stack fait au max ceil(n/2) de large)
        // Mais si on peut mettre un multiple de maxStackCols pour remplir la largeur, on le fait
        const totalContainers = stackGroups.reduce((s, g) => s + g.n, 0);
        
        // Calculer combien de colonnes on peut mettre avec la taille de cellule minimale
        const maxPossibleCols = Math.floor((availWidth + gap) / (minCell + gap));
        
        // Utiliser un multiple de maxStackCols pour remplir la largeur
        let gridCols;
        if (maxPossibleCols >= maxStackCols * 2) {
            gridCols = maxStackCols * Math.floor(maxPossibleCols / maxStackCols);
        } else {
            gridCols = maxStackCols;
        }
        gridCols = Math.max(2, gridCols);
        
        // Calculer la taille de cellule pour remplir la largeur
        let cellSize = Math.floor((availWidth - (gridCols - 1) * gap) / gridCols);
        cellSize = Math.max(minCell, Math.min(maxCell, cellSize));
        
        // Recalculer gridCols avec la taille de cellule finale
        gridCols = Math.max(2, Math.floor((availWidth + gap) / (cellSize + gap)));
        // Arrondir au multiple de maxStackCols le plus proche (mais pas plus petit)
        if (gridCols >= maxStackCols) {
            gridCols = Math.floor(gridCols / maxStackCols) * maxStackCols;
            if (gridCols < maxStackCols) gridCols = maxStackCols;
        }
        
        const cellW = cellSize, cellH = cellSize;
        
        // Flow layout boustrophedon
        // Placer tous les containers à la suite, row by row
        // Ligne paire: gauche→droite, ligne impaire: droite→gauche
        const allCells = [];
        let col = 0, row = 0;
        
        let currentGroupLabel = null;
        
        for (const group of stackGroups) {
            // Ajouter un en-tête de groupe s'il y en a un
            if (group.groupLabel) {
                // Avancer à la ligne suivante si on n'est pas au début
                if (col > 0 || row > 0) {
                    col = 0;
                    row++;
                }
                // Réserver une ligne pour l'en-tête (on ne crée pas de cellule, juste un espace)
                // On stocke le label pour le rendre dans le HTML final
                allCells.push({
                    type: 'group-header',
                    label: group.groupLabel,
                    row: row
                });
                row++;
            }

            const borderColor = group.color.stroke;
            const bgColor = group.color.fill;
            const stackName = group.stack.name;
            const stackAgent = group.stack.agent_name || null;
            
            for (let i = 0; i < group.containers.length; i++) {
                // Position dans la grille
                const actualCol = (row % 2 === 0) ? col : (gridCols - 1 - col);
                allCells.push({
                    type: 'container',
                    col: actualCol,
                    row: row,
                    container: group.containers[i],
                    stackName: stackName,
                    agent: stackAgent,
                    borderColor: borderColor,
                    bgColor: bgColor
                });
                
                // Avancer le curseur
                col++;
                if (col >= gridCols) {
                    col = 0;
                    row++;
                }
            }
        }
        
        const totalRows = (col > 0) ? row + 1 : row;
        const canvasW = gridCols * (cellW + gap) - gap;
        const canvasH = totalRows * (cellH + gap) - gap;
        
        // Build HTML
        let cardsHtml = '';
        const runningContainers = [];
        const updateContainers = [];
        
        for (const cell of allCells) {
            if (cell.type === 'group-header') {
                cardsHtml += '<div class="grid-group-header" style="position:relative;width:100%;padding:8px 0 4px 0;font-size:0.8rem;font-weight:600;color:var(--text-secondary);grid-column:1/-1;">📁 ' + this.escapeHtml(cell.label) + '</div>';
                continue;
            }
            const cardX = cell.col * (cellW + gap);
            const cardY = cell.row * (cellH + gap);
            const agent = cell.agent;
            
            cardsHtml += this.renderGridContainerCard(cell.container, cardX, cardY, cellW, cellH, agent, cell.borderColor, cell.bgColor, cell.stackName);
            updateContainers.push({ id: cell.container.id, agent });
            if (cell.container.status === "running") runningContainers.push({ id: cell.container.id, agent });
        }
        
        container.innerHTML = '<div class="docky-grid-canvas" style="position:relative;width:' + canvasW + 'px;height:' + canvasH + 'px;margin:0 auto;" onclick="DockyApp.clearStackSelection()">' + cardsHtml + '</div>';
        
        for (const rc of runningContainers) {
            this.loadContainerStats(rc.id, rc.agent);
        }
        for (const uc of updateContainers) {
            this.checkUpdate(uc.id, uc.agent, this._updateCheckToken);
        }

        if (typeof lucide !== 'undefined') {
            lucide.createIcons();
        }

        // Ré-appliquer la sélection de stack après un re-render (auto-refresh)
        if (this._selectedStack) {
            const cards = document.querySelectorAll('.grid-container-card');
            cards.forEach(card => {
                const cardKey = (card.dataset.stack || '') + '@' + (card.dataset.agent || '');
                if (cardKey === this._selectedStack) {
                    card.classList.remove('grid-dimmed');
                } else {
                    card.classList.add('grid-dimmed');
                }
            });
            // Extraire le nom et l'agent depuis la clé composite
            const parts = this._selectedStack.split('@');
            const selName = parts[0];
            const selAgent = parts.slice(1).join('@') || null;
            const stack = this.stacks.find(s => s.name === selName && (s.agent_name || '') === (selAgent || ''));
            if (stack) {
                this.showStackContextPanel(stack, null);
            }
        }
    },

    // -------------------------------------------------------
    // View Mode Toggle (grid / table)
    // -------------------------------------------------------

    toggleViewMode() {
        this._viewMode = this._viewMode === 'grid' ? 'table' : 'grid';
        const btn = document.getElementById('view-toggle');
        if (btn) btn.innerHTML = this._viewMode === 'grid' ? this.icon('list') : this.icon('layout-grid');
        localStorage.setItem('docky_view_mode', this._viewMode);
        if (this._allContainersCache && this._allContainersCache.length > 0) {
            this.renderCurrentView();
        }
    },

    renderCurrentView() {
        if (this._viewMode === 'grid') {
            this.renderGridDashboard();
        } else {
            this.renderTableDashboard();
        }
    },

    // -------------------------------------------------------
    // Table Dashboard (Option C)
    // -------------------------------------------------------

    renderTableDashboard() {
        const container = document.getElementById("dashboard-content");
        if (!container) return;
        this._pruneUpdateCache();
        this._updateAvailableCount = this._countCachedUpdates();
        this._updateCheckToken = (this._updateCheckToken || 0) + 1;

        if (this.stacks.length === 0) {
            container.innerHTML = '<div class="placeholder"><p>📭 Aucune stack trouvée</p></div>';
            return;
        }

        const sortedStacks = this._sortStacks(this.stacks);
        const allContainers = this._allContainersCache || [];
        const groups = this._groupStacks(sortedStacks);

        let html = '<div class="table-dashboard">';

        for (const group of groups) {
            // Ajouter un en-tête de groupe si nécessaire
            if (group.label) {
                html += '<div class="table-group-header">📁 ' + this.escapeHtml(group.label) + '</div>';
            }

            for (const stack of group.stacks) {
                let containers = allContainers.filter(c => {
                    if (stack.name === 'Standalone') return !c.stack;
                    return c.stack === stack.name && (c.agent_name||'') === (stack.agent_name||'');
                });
                if (containers.length === 0) continue;

                // Skip if agent is hidden
                if (this._hiddenAgents.size > 0) {
                    const stackAgent = stack.agent_name || '';
                    if (this._hiddenAgents.has(stackAgent)) continue;
                }

                // Trier les containers selon le mode de tri
                containers = this._sortContainers(containers);

                const color = this.stackColor(stack.name);
                const borderColor = color.stroke;
                const bgColor = color.fill;
                const isManaged = stack.managed !== false;
                const isStandalone = stack.standalone === true;

                // Stack header
                let typeBadge = '';
                if (isStandalone) typeBadge = '<span class="stack-type-badge stack-badge-standalone">standalone</span>';
                else if (!isManaged) typeBadge = '<span class="stack-type-badge stack-badge-external">externe</span>';
                else typeBadge = '<span class="stack-type-badge stack-badge-docky">' + this.escapeHtml(stack.agent_name || stack.agent || 'agent') + '</span>';

                const escapedName = this.escapeHtml(stack.name);

                html += '<div class="table-stack-group" data-stack="' + escapedName + '" data-agent="' + this.escapeHtml(stack.agent_name || '') + '" style="border-color:' + borderColor + ';background:' + bgColor + '">';
                html += '<div class="table-stack-header">';
                html += '<span class="table-stack-name">' + escapedName + '</span>' + typeBadge;
                html += '<span class="meta-badge">🐳 ' + containers.length + '</span>';
                html += '</div>';

                // Zone scrollable horizontalement (colonnes redimensionnables)
                html += '<div class="table-stack-scroll">';

                // En-tête de colonnes avec poignées de redimensionnement
                html += '<div class="table-col-header">';
                html += '<div class="table-col-head table-col-status" title="Statut"></div>';
                html += '<div class="table-col-head table-col-name" title="Conteneur">Conteneur<div class="col-resizer" data-col="name"></div></div>';
                html += '<div class="table-col-head table-col-image" title="Image">Image<div class="col-resizer" data-col="image"></div></div>';
                html += '<div class="table-col-head table-col-resources" title="Ressources">Ressources</div>';
                html += '<div class="table-col-head table-col-ports" title="Ports">Ports<div class="col-resizer" data-col="ports"></div></div>';
                html += '<div class="table-col-head table-col-actions" title="Actions"></div>';
                html += '</div>';

                // Container rows (triés)
                for (const c of containers) {
                    const agent = stack.agent_name || '';
                    html += this.renderTableRow(c, agent, borderColor, stack.name);
                }

                html += '</div>'; // table-stack-scroll
                html += '</div>'; // table-stack-group
            }
        }

        if (html === '<div class="table-dashboard">') {
            html += '<div class="placeholder"><p>🔇 Aucun agent affiché</p></div>';
        }

        html += '</div>';
        container.innerHTML = html;

        // Restaurer et appliquer les largeurs de colonnes sauvegardées
        const tableRoot = container.querySelector('.table-dashboard');
        if (tableRoot) {
            this._applyTableColWidths(tableRoot);
        }
        this.attachTableColumnResizers();

        // Load stats for running containers; check updates for all containers
        // (the check is now lightweight and cached).
        const runningContainers = allContainers.filter(c => c.status === 'running');
        for (const rc of runningContainers) {
            const rcStack = this.stacks.find(s => s.name === (rc.stack||'') && (s.agent_name||'') === (rc.agent_name||''));
            const agent = rcStack ? (rcStack.agent_name || '') : '';
            this.loadContainerStats(rc.id, agent);
        }
        for (const c of allContainers) {
            const rcStack = this.stacks.find(s => s.name === (c.stack||'') && (s.agent_name||'') === (c.agent_name||''));
            const agent = rcStack ? (rcStack.agent_name || '') : '';
            this.checkUpdate(c.id, agent, this._updateCheckToken);
        }

        if (typeof lucide !== 'undefined') {
            lucide.createIcons();
        }

        // Ré-appliquer la sélection de stack après un re-render (auto-refresh)
        if (this._selectedStack) {
            const sections = document.querySelectorAll('.table-stack-group');
            sections.forEach(section => {
                const sectionKey = (section.dataset.stack || '') + '@' + (section.dataset.agent || '');
                if (sectionKey === this._selectedStack) {
                    section.classList.remove('grid-dimmed');
                } else {
                    section.classList.add('grid-dimmed');
                }
            });
            // Extraire le nom et l'agent depuis la clé composite
            const parts = this._selectedStack.split('@');
            const selName = parts[0];
            const selAgent = parts.slice(1).join('@') || null;
            const stack = this.stacks.find(s => s.name === selName && (s.agent_name || '') === (selAgent || ''));
            if (stack) {
                this.showStackContextPanel(stack, null);
            }
        }
    },

    renderTableRow(c, agent, borderColor, stackName) {
        if (!c) return '';

        const escapedId = this.escapeHtml(c.id);
        const name = this.escapeHtml(c.name);
        const image = this.escapeHtml(c.image);
        const statusDot = this.containerStatusDot(c.status);
        const agt = (agent || "").replace(/'/g, "\\'");
        const escapedName = this.escapeHtml(stackName);
        const ports = (c.ports || []).filter(p => p.host_port).map(p => p.host_port + '→' + p.container).join(", ");

        return '<div class="table-container-row" data-id="' + escapedId + '" data-stack="' + escapedName + '" data-agent="' + this.escapeHtml(agent || '') + '" style="border-left-color:' + borderColor + '" onclick="event.stopPropagation(); DockyApp.selectContainerInGrid(\'' + escapedId + '\', \'' + escapedName + '\', \'' + this.escapeHtml(agent || '') + '\')" ondblclick="event.stopPropagation(); DockyApp.openContainerEdit(\'' + escapedId + '\', \'' + escapedName + '\', \'' + this.escapeHtml(agent || '') + '\')">'
            + '<div class="table-row-status">' + statusDot + '</div>'
            + '<div class="table-row-name" title="' + name + '">' + name
            + '<span id="update-' + escapedId + '" class="update-badge ' + this._updateBadgeClass(this._containerUpdateCacheKey(c.id)) + '" title="Mise à jour disponible" onclick="event.stopPropagation();DockyApp.containerAction(\'' + escapedId + '\', \'update-image\', \'' + agt + '\')">' + this.icon('arrow-up') + ' Update</span>'
            + '</div>'
            + '<div class="table-row-image" title="' + image + '">' + this.icon('package') + ' ' + image + '</div>'
            + '<div class="table-row-resources">'
            + '<div class="table-resource"><span class="resource-label">CPU</span><div class="progress-bar"><div class="progress-fill" id="stats-cpu-' + escapedId + '" style="width:0%"></div></div><span class="resource-value" id="stats-cpu-val-' + escapedId + '">—</span></div>'
            + '<div class="table-resource"><span class="resource-label">RAM</span><div class="progress-bar"><div class="progress-fill ram" id="stats-ram-' + escapedId + '" style="width:0%"></div></div><span class="resource-value" id="stats-ram-val-' + escapedId + '">—</span></div>'
            + '</div>'
            + '<div class="table-row-ports" title="' + ports + '">' + (ports ? this.icon('cable') + ' ' + ports : '') + '</div>'
            + '<div class="table-row-actions" onclick="event.stopPropagation()">'
            + '<button class="grid-icon-btn btn-start" title="Start" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'start\', \'' + agt + '\')">' + this.icon('play') + '</button>'
            + '<button class="grid-icon-btn btn-stop" title="Stop" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'stop\', \'' + agt + '\')">' + this.icon('square') + '</button>'
            + '<button class="grid-icon-btn btn-restart" title="Restart" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'restart\', \'' + agt + '\')">' + this.icon('refresh-cw') + '</button>'
            + '<button class="grid-icon-btn btn-logs" title="Logs" onclick="DockyApp.openLogs(\'' + escapedId + '\', \'' + name + '\', \'' + agt + '\')">' + this.icon('clipboard-list') + '</button>'
            + '<button class="grid-icon-btn btn-console" title="Console" onclick="DockyApp.openConsole(\'' + escapedId + '\', \'' + name + '\', \'' + agt + '\')">' + this.icon('terminal') + '</button>'
            + '<button class="grid-icon-btn btn-update" title="Update" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'update-image\', \'' + agt + '\')">' + this.icon('arrow-up') + '</button>'
            + '</div></div>';
    },

    // -------------------------------------------------------
    // Colonnes redimensionnables (mode tableau)
    // -------------------------------------------------------

    _tableColWidthsKey: 'docky_table_col_widths',
    _tableColDefaults: { name: 180, image: 160, ports: 120 },
    _tableColMin: 70,

    _getTableColWidths() {
        try {
            const raw = localStorage.getItem(this._tableColWidthsKey);
            return raw ? JSON.parse(raw) : {};
        } catch (e) { return {}; }
    },

    // Applique les largeurs sauvegardées via des variables CSS sur la racine du
    // tableau (héritées par l'en-tête ET les lignes de tous les groupes).
    _applyTableColWidths(root) {
        const widths = this._getTableColWidths();
        for (const col of Object.keys(widths)) {
            if (this._tableColDefaults[col] === undefined) continue;
            const w = Math.max(this._tableColMin, widths[col]);
            root.style.setProperty('--col-' + col, w + 'px');
        }
    },

    _saveTableColWidth(col, w) {
        const widths = this._getTableColWidths();
        widths[col] = Math.max(this._tableColMin, w);
        try { localStorage.setItem(this._tableColWidthsKey, JSON.stringify(widths)); } catch (e) { /* ignore */ }
    },

    // Attache les poignées .col-resizer : mousedown → mousemove (ajustement) →
    // mouseup (relâche + persistance localStorage). Ne casse ni le tri ni le
    // scroll horizontal (les largeurs sont des variables CSS, pas des styles fixes).
    attachTableColumnResizers() {
        const root = document.querySelector('.table-dashboard');
        if (!root) return;
        const self = this;

        root.querySelectorAll('.col-resizer').forEach((resizer) => {
            if (resizer.dataset.bound === '1') return;
            resizer.dataset.bound = '1';

            resizer.addEventListener('mousedown', (e) => {
                e.preventDefault();
                e.stopPropagation();

                const col = resizer.dataset.col;
                if (!col) return;
                const startX = e.clientX;
                const current = parseInt(getComputedStyle(root).getPropertyValue('--col-' + col), 10)
                    || self._tableColDefaults[col] || 120;
                let finalW = current;

                document.body.style.cursor = 'col-resize';
                document.body.style.userSelect = 'none';
                resizer.classList.add('active');

                const onMove = (ev) => {
                    finalW = Math.max(self._tableColMin, current + (ev.clientX - startX));
                    root.style.setProperty('--col-' + col, finalW + 'px');
                };
                const onUp = () => {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                    document.body.style.cursor = '';
                    document.body.style.userSelect = '';
                    resizer.classList.remove('active');
                    self._saveTableColWidth(col, finalW);
                };

                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
            });
        });
    },

    hashString(s) { let h = 0; for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0; return Math.abs(h); },

    stackColor(name) {
        const palette = [
            { fill: 'rgba(233,69,96,0.07)', stroke: 'rgba(233,69,96,0.30)' },
            { fill: 'rgba(74,222,128,0.07)', stroke: 'rgba(74,222,128,0.30)' },
            { fill: 'rgba(96,165,250,0.07)', stroke: 'rgba(96,165,250,0.30)' },
            { fill: 'rgba(251,191,36,0.07)', stroke: 'rgba(251,191,36,0.30)' },
            { fill: 'rgba(168,85,247,0.07)', stroke: 'rgba(168,85,247,0.30)' },
            { fill: 'rgba(34,211,238,0.07)', stroke: 'rgba(34,211,238,0.30)' },
            { fill: 'rgba(249,115,22,0.07)', stroke: 'rgba(249,115,22,0.30)' },
            { fill: 'rgba(236,72,153,0.07)', stroke: 'rgba(236,72,153,0.30)' },
        ];
        return palette[this.hashString(name) % palette.length];
    },

    containerStatusDot(status) {
        let cls = 'status-running';
        if (status === 'exited' || status === 'stopped') cls = 'status-stopped';
        else if (status === 'restarting' || status === 'paused') cls = 'status-partial';
        else if (status === 'dead' || status === 'error') cls = 'status-stopped';
        return '<span class="grid-status-dot ' + cls + '" title="' + this.escapeHtml(status) + '"></span>';
    },

    renderGridContainerCard(c, left, top, width, height, agent, borderColor, bgColor, stackName) {
        if (!c) return '';
        
        const escapedId = this.escapeHtml(c.id), name = this.escapeHtml(c.name), image = this.escapeHtml(c.image);
        const statusDot = this.containerStatusDot(c.status);
        const agt = (agent || "").replace(/'/g, "\\'");
        const ports = (c.ports || []).filter(p => p.host_port).map(p => p.host_port + '→' + p.container).join(", ");
        const portsBadge = ports ? '<span class="meta-badge meta-ports grid-card-ports">' + this.icon('cable') + ' ' + this.escapeHtml(ports) + '</span>' : '';
        
        return '<div class="grid-container-card" data-id="' + escapedId + '" data-stack="' + this.escapeHtml(stackName) + '" data-agent="' + this.escapeHtml(agent || '') + '" style="left:' + left + 'px;top:' + top + 'px;width:' + width + 'px;height:' + height + 'px;z-index:3;background-color:' + bgColor + ';border-color:' + borderColor + '"' 
            + ' onclick="event.stopPropagation(); DockyApp.selectContainerInGrid(\'' + escapedId + '\', \'' + this.escapeHtml(stackName) + '\', \'' + this.escapeHtml(agent || '') + '\')"'
            + ' ondblclick="event.stopPropagation(); DockyApp.openContainerEdit(\'' + escapedId + '\', \'' + this.escapeHtml(stackName) + '\', \'' + this.escapeHtml(agent || '') + '\')">'
            + '<div class="grid-card-top"><span class="grid-card-name" title="' + name + '">' + name + '</span>' + statusDot + '</div>'
            + '<div class="grid-card-image" title="' + image + '">' + this.icon('package') + ' ' + image + '</div>'
            + '<div class="grid-card-resources" id="resources-' + escapedId + '"><div class="resource-line"><span class="resource-label">CPU</span><div class="progress-bar"><div class="progress-fill" style="width:0%"></div></div><span class="resource-value">—</span></div><div class="resource-line"><span class="resource-label">RAM</span><div class="progress-bar"><div class="progress-fill ram" style="width:0%"></div></div><span class="resource-value">—</span></div></div>'
            + '<div class="grid-card-extra">' + portsBadge + '<button class="update-badge ' + this._updateBadgeClass(this._containerUpdateCacheKey(c.id)) + '" id="update-' + escapedId + '" onclick="event.stopPropagation();DockyApp.containerAction(\'' + escapedId + '\', \'update-image\', \'' + agt + '\')" title="Mettre à jour">' + this.icon('arrow-up') + '</button></div>'
            + '<div class="grid-card-actions" onclick="event.stopPropagation()">'
            + '<button class="grid-icon-btn btn-start" title="Start" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'start\', \'' + agt + '\')">' + this.icon('play') + '</button>'
            + '<button class="grid-icon-btn btn-stop" title="Stop" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'stop\', \'' + agt + '\')">' + this.icon('square') + '</button>'
            + '<button class="grid-icon-btn btn-restart" title="Restart" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'restart\', \'' + agt + '\')">' + this.icon('refresh-cw') + '</button>'
            + '<button class="grid-icon-btn btn-logs" title="Logs" onclick="DockyApp.openLogs(\'' + escapedId + '\', \'' + name + '\', \'' + agt + '\')">' + this.icon('clipboard-list') + '</button>'
            + '<button class="grid-icon-btn btn-console" title="Console" onclick="DockyApp.openConsole(\'' + escapedId + '\', \'' + name + '\', \'' + agt + '\')">' + this.icon('terminal') + '</button>'
            + '<button class="grid-icon-btn btn-update" title="Update" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'update-image\', \'' + agt + '\')">' + this.icon('arrow-up') + '</button>'
            + '</div></div>';
    },

    selectContainerInGrid(containerId, stackName, agent) {
        const key = stackName + '@' + (agent || '');
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
        
        // Trouver la stack avec le bon agent
        const stack = this.stacks.find(s => s.name === stackName && (s.agent_name || '') === (agent || ''));
        if (stack) {
            this.showStackContextPanel(stack, containerId);
        }
    },

    showStackContextPanel(stack, selectedContainerId) {
        const panel = document.querySelector('.compose-panel .panel-body') || document.getElementById('compose-editor') || document.querySelector('.right-column .panel-body');
        if (!panel) return;
        
        const isManaged = stack.managed !== false;
        const isStandalone = stack.standalone === true;
        const escapedName = this.escapeHtml(stack.name);
        const escapedAgent = this.escapeHtml(stack.agent_name || '');
        const stackKey = stack.name + '@' + (stack.agent_name || '');

        // Anti-flicker : si l'éditeur affiche déjà CETTE stack, on ne reconstruit pas
        // le DOM du panel (sinon le contenu disparaissait/réapparaissait à chaque
        // auto-refresh ~5 s qui re-appelait showStackContextPanel depuis le rendu).
        if (panel.dataset.stackKey === stackKey && this._editorLoadedKey === stackKey) {
            this.selectedStackAgent = stack.agent_name || null;
            // On garde quand même le badge d'update de stack à jour (léger, sans re-render).
            if (!isStandalone) {
                this.checkStackUpdate(stack.name, stack.agent_name || '', this._updateCheckToken);
            }
            return;
        }

        let html = '<div class="stack-context-panel" data-stack-key="' + this.escapeHtml(stackKey) + '">';
        html += '<div class="stack-context-header">';
        html += '<h2 class="stack-context-title">' + escapedName + '</h2>';
        if (isStandalone) html += '<span class="stack-type-badge stack-badge-standalone">standalone</span>';
        else if (!isManaged) html += '<span class="stack-type-badge stack-badge-external">externe</span>';
        else html += '<span class="stack-type-badge stack-badge-docky">' + this.escapeHtml(stack.agent_name || stack.agent || 'agent') + '</span>';
        if (!isStandalone) html += '<button class="update-badge ' + this._updateBadgeClass(this._stackUpdateCacheKey(stack.name, stack.agent_name || '')) + '" id="stack-update-panel-' + escapedName + '@' + escapedAgent + '" onclick="DockyApp.stackAction(\'' + escapedName + '\', \'update\', \'' + escapedAgent + '\')" title="Mise à jour disponible">' + this.icon('arrow-up') + ' Update</button>';
        html += '</div>';
        
        // Boutons de commande du stack
        if (!isStandalone) {
            html += '<div class="stack-context-actions">';
            html += '<button class="btn btn-sm btn-success" onclick="DockyApp.stackAction(\'' + escapedName + '\', \'start\', \'' + escapedAgent + '\')">' + this.icon('play') + ' Démarrer</button>';
            html += '<button class="btn btn-sm btn-danger" onclick="DockyApp.stackAction(\'' + escapedName + '\', \'stop\', \'' + escapedAgent + '\')">' + this.icon('square') + ' Arrêter</button>';
            html += '<button class="btn btn-sm btn-warning" onclick="DockyApp.stackAction(\'' + escapedName + '\', \'restart\', \'' + escapedAgent + '\')">' + this.icon('refresh-cw') + ' Redémarrer</button>';
            html += '<button class="btn btn-sm btn-info" onclick="DockyApp.stackAction(\'' + escapedName + '\', \'update\', \'' + escapedAgent + '\')">' + this.icon('arrow-up') + ' Update</button>';
            html += '<button class="btn btn-sm" onclick="DockyApp.openStackLogs(\'' + escapedName + '\', \'' + escapedAgent + '\')">' + this.icon('clipboard-list') + ' Logs</button>';
            if (isManaged) html += '<button class="btn btn-sm" onclick="DockyApp.selectStackFromDashboard(\'' + escapedName + '\', \'' + escapedAgent + '\')">' + this.icon('pen-square') + ' Éditer</button>';
            if (!isManaged && !isStandalone) {
                if (stack.source_path) {
                    // Chemin détecté automatiquement → import direct avec preview
                    html += '<button class="btn btn-sm btn-info" onclick="DockyApp.importExternal(\'' + this.escapeHtml(stack.source_path) + '\', \'' + escapedName + '\', \'' + escapedAgent + '\')">' + this.icon('download') + ' Importer</button>';
                } else {
                    // Chemin non détecté → ouvrir le modal manuel avec le nom pré-rempli
                    html += '<button class="btn btn-sm btn-info" onclick="DockyApp.openImportModalForStack(\'' + escapedName + '\')">' + this.icon('download') + ' Importer</button>';
                }
            }
            html += '</div>';
        }
        
        // Éditeur compose (si managed) — show a loading indicator immediately
        if (isManaged) {
            html += '<div class="stack-context-compose">';
            html += '<div class="stack-context-loading" id="compose-loading">' + this.icon('loader') + ' Chargement du compose…</div>';
            html += '<div class="compose-tabs" id="compose-tabs" style="display:none"></div>';
            html += '<div class="code-editor-wrap" style="display:none">';
            html += '<div class="line-numbers" id="line-numbers"></div>';
            html += '<textarea class="code-textarea" id="code-editor" placeholder="Sélectionne un fichier..."></textarea>';
            html += '</div>';
            html += '<div class="editor-actions" style="display:none">';
            html += '<button class="btn btn-sm btn-success" onclick="DockyApp.saveCurrentFile()">' + this.icon('hard-drive') + ' Sauvegarder</button>';
            html += '<button class="btn btn-sm btn-info" onclick="DockyApp.saveAndDeploy()">' + this.icon('hard-drive') + '+' + this.icon('rocket') + ' Sauvegarder & Déployer</button>';
            html += '</div>';
            html += '</div>';
        } else {
            html += '<div class="stack-context-no-compose"><p>Stack externe — compose non accessible</p></div>';
        }
        
        html += '</div>';
        
        panel.innerHTML = html;
        
        if (typeof lucide !== 'undefined') {
            lucide.createIcons();
        }

        // Check léger et caché des mises à jour de stack
        if (!isStandalone) {
            this.checkStackUpdate(stack.name, stack.agent_name || '', this._updateCheckToken);
        }

        // Charger le compose si managed
        if (isManaged) {
            this.selectedStackAgent = stack.agent_name || null;  // DIRECTEMENT depuis l'objet stack
            this.loadEditor(stack.name, stack.agent_name);
        }
    },

    clearStackSelection() {
        // Vérifier si le compose a été modifié
        if (this.anyModified && this.anyModified()) {
            // Afficher un dialog de confirmation
            this.showUnsavedDialog(() => {
                // Sauvegarder puis désélectionner
                this._saveAndDeselect();
            }, () => {
                // Ne pas sauvegarder, désélectionner directement
                this._forceDeselect();
            }, () => {
                // Annuler, ne rien faire
            });
            return;
        }
        this._forceDeselect();
    },

    _forceDeselect() {
        this._selectedStack = null;
        this.selectedStack = null;
        this.selectedStackAgent = null;
        this._editorLoadedKey = null;
        this._composeEditMode = false;
        const body = document.getElementById('compose-body');
        if (body) body.dataset.stackKey = '';
        const cards = document.querySelectorAll('.grid-container-card');
        cards.forEach(card => card.classList.remove('grid-dimmed'));
        const sections = document.querySelectorAll('.table-stack-group');
        sections.forEach(section => section.classList.remove('grid-dimmed'));
        const selector = document.getElementById('stack-selector');
        if (selector) selector.value = '';
        this.renderEditorPlaceholder();
    },

    _saveAndDeselect() {
        // Sauvegarder d'abord, puis désélectionner
        if (typeof this.saveCurrentFile === 'function') {
            this.saveCurrentFile().then(() => {
                this._forceDeselect();
            }).catch(() => {
                this._forceDeselect();  // Forcer même si erreur
            });
        } else {
            this._forceDeselect();
        }
    },

    showUnsavedDialog(onSave, onDiscard, onCancel) {
        // Afficher un dialog modal
        const modal = document.getElementById('unsaved-dialog');
        if (!modal) return;

        // Stocker les callbacks
        this._unsavedCallbacks = { onSave, onDiscard, onCancel };
        modal.classList.remove('hidden');
    },

    _onUnsavedSave() {
        const modal = document.getElementById('unsaved-dialog');
        if (modal) modal.classList.add('hidden');
        if (this._unsavedCallbacks && this._unsavedCallbacks.onSave) {
            this._unsavedCallbacks.onSave();
        }
        this._unsavedCallbacks = null;
    },

    _onUnsavedDiscard() {
        const modal = document.getElementById('unsaved-dialog');
        if (modal) modal.classList.add('hidden');
        if (this._unsavedCallbacks && this._unsavedCallbacks.onDiscard) {
            this._unsavedCallbacks.onDiscard();
        }
        this._unsavedCallbacks = null;
    },

    _onUnsavedCancel() {
        const modal = document.getElementById('unsaved-dialog');
        if (modal) modal.classList.add('hidden');
        if (this._unsavedCallbacks && this._unsavedCallbacks.onCancel) {
            this._unsavedCallbacks.onCancel();
        }
        this._unsavedCallbacks = null;
    },

    _debouncedGridRender() {
        if (this._gridRenderTimer) clearTimeout(this._gridRenderTimer);
        this._gridRenderTimer = setTimeout(() => {
            if (this.stacks.length > 0) {
                this.renderCurrentView();
            }
        }, 200);
    },









    // -------------------------------------------------------
    // Stats / Resources
    // -------------------------------------------------------

    async loadContainerStats(containerId, agent) {
        // Skip if a request is already in progress for this container
        if (this._pendingFetches[containerId]) return;
        this._pendingFetches[containerId] = true;

        try {
            const url = '/api/containers/' + encodeURIComponent(containerId) + '/stats' + this.agentQuery(agent);
            const resp = await fetch(url, { credentials: 'same-origin' });
            if (resp.status === 401) return;
            const data = await resp.json();
            this.renderStats(containerId, data);
        } catch (e) {
            // Ignorer les erreurs (réseau, annulation…)
        } finally {
            this._pendingFetches[containerId] = false;
        }
    },

    renderStats(containerId, stats) {
        // Cache les stats pour le tri CPU/RAM
        this._statsCache[containerId] = stats;
        const cpuPct = Math.min(stats.cpu_percent, 100);
        const memPct = Math.min(stats.mem_percent, 100);

        // Grid mode: #resources-{id} container
        const target = document.getElementById("resources-" + containerId);
        if (target) {
            const cpuFill = target.querySelector(".resource-line:nth-child(1) .progress-fill");
            const cpuVal = target.querySelector(".resource-line:nth-child(1) .resource-value");
            const memFill = target.querySelector(".resource-line:nth-child(2) .progress-fill");
            const memVal = target.querySelector(".resource-line:nth-child(2) .resource-value");

            if (cpuFill) cpuFill.style.width = cpuPct + "%";
            if (cpuVal) cpuVal.textContent = stats.cpu_percent.toFixed(1) + "%";
            if (memFill) memFill.style.width = memPct + "%";
            if (memVal) memVal.textContent = this.formatBytes(stats.mem_usage) + " / " + this.formatBytes(stats.mem_limit);
        }

        // Table mode: #stats-cpu-{id} and #stats-ram-{id} elements
        const cpuFill = document.getElementById("stats-cpu-" + containerId);
        const cpuVal = document.getElementById("stats-cpu-val-" + containerId);
        const memFill = document.getElementById("stats-ram-" + containerId);
        const memVal = document.getElementById("stats-ram-val-" + containerId);

        if (cpuFill) cpuFill.style.width = cpuPct + "%";
        if (cpuVal) cpuVal.textContent = stats.cpu_percent.toFixed(1) + "%";
        if (memFill) memFill.style.width = memPct + "%";
        if (memVal) memVal.textContent = this.formatBytes(stats.mem_usage) + " / " + this.formatBytes(stats.mem_limit);
    },

    // -------------------------------------------------------
    // Activity modal (progression des commandes)
    // -------------------------------------------------------

    _openActivity(title) {
        const modal = document.getElementById("activity-modal");
        if (!modal) return;
        const titleEl = document.getElementById("activity-title");
        if (titleEl) titleEl.textContent = title || "Exécution…";
        const status = document.getElementById("activity-status");
        if (status) {
            status.textContent = "En cours…";
            status.className = "status-indicator status-running";
        }
        const output = document.getElementById("activity-output");
        if (output) output.innerHTML = '<div class="terminal-empty">Exécution…</div>';
        modal.classList.remove("hidden");
    },

    _appendActivity(text, type) {
        const output = document.getElementById("activity-output");
        if (!output) return;
        const empty = output.querySelector(".terminal-empty");
        if (empty) empty.remove();
        const line = document.createElement("div");
        line.className = "terminal-line" + (type ? " " + type : "");
        line.textContent = text;
        output.appendChild(line);
        output.scrollTop = output.scrollHeight;
    },

    _finishActivity(success, output) {
        const status = document.getElementById("activity-status");
        if (status) {
            status.textContent = success ? "Terminé" : "Échec";
            status.className = "status-indicator " + (success ? "status-running" : "status-stopped");
        }
        const outDiv = document.getElementById("activity-output");
        if (!outDiv) return;
        // En mode streaming, les lignes ont déjà été affichées en direct : on
        // les conserve et on ajoute seulement une ligne de résumé.
        const hasStreamed = outDiv.querySelector(".terminal-line") !== null;
        if (hasStreamed) {
            const summary = document.createElement("div");
            summary.className = "terminal-line terminal-summary " + (success ? "success" : "error");
            summary.textContent = success ? "✓ Terminé" : "✗ Échec";
            outDiv.appendChild(summary);
            outDiv.scrollTop = outDiv.scrollHeight;
            return;
        }
        // Fallback JSON : afficher le résultat complet.
        outDiv.innerHTML = '';
        const pre = document.createElement("pre");
        pre.className = "terminal-output-content";
        pre.textContent = output;
        outDiv.appendChild(pre);
    },

    _parseSSEBlock(block) {
        let event = null;
        let data = "";
        for (const rawLine of block.split("\n")) {
            const line = rawLine.replace(/\r$/, "");
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).trim();
        }
        if (!event) return null;
        let parsed;
        try { parsed = data ? JSON.parse(data) : {}; } catch (e) { parsed = { raw: data }; }
        return { event, data: parsed };
    },

    /**
     * Consomme un endpoint SSE (fetch + ReadableStream) et affiche les lignes
     * progressivement dans l'Activity Modal.
     *
     * Résout avec { success, output } une fois le flux terminé, ou rejette une
     * Error si le serveur a renvoyé une erreur / le flux a été coupé.
     */
    async _streamAction(url) {
        let resp;
        try {
            resp = await fetch(url, { method: "POST", credentials: "same-origin" });
        } catch (e) {
            throw new Error("Erreur réseau : " + e.message);
        }
        if (resp.status === 401) {
            window.location.href = "/login";
            return { success: false, output: "", error: "Non autorisé" };
        }
        if (!resp.ok) {
            let detail = "HTTP " + resp.status;
            try {
                const data = await resp.json();
                detail = data.detail || data.error || data.message || detail;
            } catch (e) { /* body non JSON */ }
            throw new Error(detail);
        }
        if (!resp.body) throw new Error("Réponse sans flux de données");

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let output = "";
        let success = false;
        let streamEnded = false;
        try {
            while (!streamEnded) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
                let sep;
                while ((sep = buffer.indexOf("\n\n")) !== -1) {
                    const block = buffer.slice(0, sep);
                    buffer = buffer.slice(sep + 2);
                    const parsed = this._parseSSEBlock(block);
                    if (!parsed) continue;
                    if (parsed.event === "output") {
                        const line = parsed.data.line || "";
                        if (line) {
                            output += (output ? "\n" : "") + line;
                            this._appendActivity(line, "");
                        }
                    } else if (parsed.event === "done") {
                        success = !!parsed.data.success;
                        if (parsed.data.output) output = parsed.data.output;
                        if (!success && parsed.data.error) {
                            // Priorise le message d'erreur réel de l'agent dans
                            // le résumé final, même quand du output a déjà été
                            // affiché (sinon il était silencieusement perdu).
                            // Les lignes output restent affichées ci-dessous.
                            output = parsed.data.error + (output ? "\n\n" + output : "");
                        }
                        // Le flux se termine normalement après l'événement done.
                        streamEnded = true;
                        break;
                    } else if (parsed.event === "error") {
                        throw new Error(parsed.data.error || "Erreur inconnue");
                    }
                }
            }
        } catch (e) {
            throw e;
        }
        return { success, output };
    },

    closeActivity() {
        const modal = document.getElementById("activity-modal");
        if (modal) modal.classList.add("hidden");
    },

    // -------------------------------------------------------
    // Actions
    // -------------------------------------------------------

    async containerAction(id, action, agent) {
        const labels = {start: 'Démarrer', stop: 'Arrêter', restart: 'Redémarrer', update: 'Mettre à jour', 'update-image': 'Mettre à jour'};
        this._openActivity(`${labels[action] || action} — container`);
        // update-image est streamé (pull + recreate progressifs).
        // start/stop/restart container restent JSON (rapides, basés SDK).
        if (action === 'update-image') {
            try {
                const result = await this._streamAction(`/api/containers/${encodeURIComponent(id)}/${action}` + this.agentQuery(agent));
                this._finishActivity(result.success, result.output);
                if (result.success) {
                    this.showToast(`Container ${action} OK`, "success");
                    // Après pull/recreate réussi, le digest local a changé : on
                    // invalide le résultat "update dispo" en cache (anti-flicker)
                    // puis on force un check immédiat. Le container recréé reçoit
                    // un nouvel id — il sera re-checké au re-render de
                    // refreshStacks(), et _pruneUpdateCache nettoiera l'ancien id.
                    this._invalidateContainerUpdateCache(id);
                    this.checkUpdate(id, agent);
                }
                else this.showToast(`Échec ${action} container`, "error");
            } catch(e) {
                this._finishActivity(false, e.message);
                this.showToast("Erreur: " + e.message, "error");
            }
        } else {
            try {
                const result = await this.apiPost(`/api/containers/${encodeURIComponent(id)}/${action}` + this.agentQuery(agent));
                const success = result && result.success;
                const output = result ? (result.output || result.error || JSON.stringify(result)) : "Pas de réponse";
                this._finishActivity(success, output);
                if (success) this.showToast(`Container ${action} OK`, "success");
                else this.showToast(`Échec ${action} container`, "error");
            } catch(e) {
                this._finishActivity(false, e.message);
                this.showToast("Erreur: " + e.message, "error");
            }
        }
        // Refresh immédiat
        this.refreshStacks();
    },

    async stackAction(name, action, agent) {
        const agt = agent || null;
        const labels = {start: 'Démarrer', stop: 'Arrêter', restart: 'Redémarrer', update: 'Mettre à jour', deploy: 'Déployer'};
        this._openActivity(`${labels[action] || action} — ${name}`);
        try {
            const result = await this._streamAction(`/api/stacks/${encodeURIComponent(name)}/${action}` + this.agentQuery(agt));
            this._finishActivity(result.success, result.output);
            if (result.success) this.showToast(`Stack ${action} OK`, "success");
            else this.showToast(`Échec ${action}: ${result.output || ''}`, "error");
        } catch(e) {
            this._finishActivity(false, e.message);
            this.showToast("Erreur: " + e.message, "error");
        }
        this.refreshStacks();
    },

    // -------------------------------------------------------
    // Update check
    // -------------------------------------------------------

    _containerUpdateCacheKey(containerId) { return 'c:' + containerId; },

    _stackUpdateCacheKey(stackName, agent) { return 's:' + stackName + '@' + (agent || ''); },

    // Classe CSS initiale d'un badge d'update d'après le cache : si un résultat
    // "update_available" est déjà connu, on rend le badge visible immédiatement
    // (aucun flicker au re-render, même avant la fin du prochain check async).
    _updateBadgeClass(cacheKey) {
        const cached = this._updateCheckCache[cacheKey];
        return (cached && cached.update_available === true) ? '' : 'hidden';
    },

    // Recompte le nombre de containers avec update dispo depuis le cache
    // (source de vérité : le compteur ne revient plus à 0 à chaque re-render).
    _countCachedUpdates() {
        let n = 0;
        for (const k of Object.keys(this._updateCheckCache)) {
            if (k.charAt(0) === 'c' && k.charAt(1) === ':'
                && this._updateCheckCache[k] && this._updateCheckCache[k].update_available === true) n++;
        }
        return n;
    },

    // Retire du cache les entrées de containers/stacks qui n'existent plus
    // (évite que le compteur global reste bloqué sur des valeurs obsolètes).
    _pruneUpdateCache() {
        const activeContainers = new Set((this._allContainersCache || []).map(c => this._containerUpdateCacheKey(c.id)));
        const activeStacks = new Set((this.stacks || []).map(s => this._stackUpdateCacheKey(s.name, s.agent_name || '')));
        for (const k of Object.keys(this._updateCheckCache)) {
            if (k.charAt(0) === 'c' && !activeContainers.has(k)) delete this._updateCheckCache[k];
            else if (k.charAt(0) === 's' && !activeStacks.has(k)) delete this._updateCheckCache[k];
        }
    },

    // Invalide l'entrée de cache d'update d'un container (et l'éventuel check
    // en vol pour cet id). Après un update-image réussi, le digest local a
    // changé : un résultat "update dispo" en cache est obsolète et ne doit pas
    // resservir de badge au prochain rendu. Le compteur global est recalculé
    // depuis le cache (source de vérité anti-flicker).
    _invalidateContainerUpdateCache(containerId) {
        delete this._updateCheckCache[this._containerUpdateCacheKey(containerId)];
        delete this._pendingFetches['update-' + containerId];
        const newCount = this._countCachedUpdates();
        if (newCount !== this._updateAvailableCount) {
            this._updateAvailableCount = newCount;
            this.updateStatsBar();
        }
    },

    async checkUpdate(containerId, agent, token) {
        const renderToken = (token !== undefined) ? token : this._updateCheckToken;
        // Éviter les appels concurrents pour le même container
        const key = 'update-' + containerId;
        if (this._pendingFetches[key]) return;
        this._pendingFetches[key] = true;
        const cacheKey = this._containerUpdateCacheKey(containerId);

        try {
            const url = '/api/containers/' + encodeURIComponent(containerId) + '/update-check' + this.agentQuery(agent);
            const resp = await fetch(url, { credentials: 'same-origin' });
            if (resp.status === 401) return;
            const data = await resp.json();

            // On met à jour le cache AVANT toute manipulation du DOM : c'est lui qui
            // pilote l'état initial des badges au prochain rendu (anti-flicker).
            this._updateCheckCache[cacheKey] = data || { update_available: false };
            // Le compteur global est recalculé depuis le cache : pas de retour à 0
            // pendant qu'un re-render est en cours (on ne réécrit le DOM que si la
            // valeur change réellement).
            const newCount = this._countCachedUpdates();
            if (newCount !== this._updateAvailableCount) {
                this._updateAvailableCount = newCount;
                this.updateStatsBar();
            }

            // On ne touche au DOM que si le rendu qui a déclenché ce check est toujours actif
            if (renderToken !== this._updateCheckToken) return;

            const badge = document.getElementById('update-' + containerId);
            if (data && data.update_available) {
                if (badge) {
                    badge.classList.remove('hidden');
                    // Tooltip avec versions si disponibles
                    if (data.local_tag && data.remote_tag) {
                        let tip = data.local_tag + ' → ' + data.remote_tag;
                        if (data.local_digest && data.remote_digest && data.local_digest !== data.remote_digest) {
                            tip += ' (nouveau digest)';
                        }
                        badge.title = tip;
                    } else if (data.local_digest && data.remote_digest) {
                        badge.title = data.local_digest + ' → ' + data.remote_digest;
                    }
                }
            } else if (data && data.update_available === false && badge) {
                badge.classList.add('hidden');
            }
        } catch (e) {
            // En cas d'erreur on conserve le cache existant et le badge déjà affiché
            // (on ne vide jamais l'affichage à cause d'un fetch transitoirement échoué).
        } finally {
            this._pendingFetches[key] = false;
        }
    },

    async checkStackUpdate(stackName, agent, token) {
        const renderToken = (token !== undefined) ? token : this._updateCheckToken;
        const key = 'stack-update-' + stackName + '@' + (agent || '');
        if (this._pendingFetches[key]) return;
        this._pendingFetches[key] = true;
        const cacheKey = this._stackUpdateCacheKey(stackName, agent);

        try {
            const url = '/api/stacks/' + encodeURIComponent(stackName) + '/update-check' + this.agentQuery(agent);
            const resp = await fetch(url, { credentials: 'same-origin' });
            if (resp.status === 401) return;
            const data = await resp.json();

            // Cache mis à jour en premier : les badges seront rendus dans le bon état
            // dès le prochain rendu, sans disparition/reapparition.
            this._updateCheckCache[cacheKey] = data || { update_available: false };

            if (renderToken !== this._updateCheckToken) return;

            const cardBadge = document.getElementById('stack-update-card-' + stackName + '@' + (agent || ''));
            const panelBadge = document.getElementById('stack-update-panel-' + stackName + '@' + (agent || ''));
            const badges = [cardBadge, panelBadge].filter(Boolean);

            if (data && data.update_available) {
                for (const badge of badges) badge.classList.remove('hidden');
            } else if (data && data.update_available === false) {
                for (const badge of badges) badge.classList.add('hidden');
            }
        } catch (e) {
            // Ignorer les erreurs (réseau, annulation…) : on conserve l'état affiché.
        } finally {
            this._pendingFetches[key] = false;
        }
    },

    // -------------------------------------------------------
    // Logs
    // -------------------------------------------------------

    async openLogs(containerId, name, agent) {
        // Open logs in a popup window so the user can keep it on another screen
        const url = `/popup/logs?agent=${encodeURIComponent(agent || '')}&container=${encodeURIComponent(containerId)}&name=${encodeURIComponent(name || '')}`;
        window.open(url, `logs-${containerId}`, 'width=900,height=650,scrollbars=yes,resizable=yes');
    },

    openStackLogs(stackName, agent) {
        // Open stack logs in the same popup as container logs (stack mode).
        const url = `/popup/logs?agent=${encodeURIComponent(agent || '')}&stack=${encodeURIComponent(stackName)}&name=${encodeURIComponent(stackName)}`;
        window.open(url, `logs-stack-${stackName}`, 'width=900,height=650,scrollbars=yes,resizable=yes');
    },

    // -------------------------------------------------------
    // Console (exec)
    // -------------------------------------------------------

    async openConsole(containerId, name, agent) {
        // Open console in a popup window so the user can keep it on another screen
        const url = `/popup/console?agent=${encodeURIComponent(agent || '')}&container=${encodeURIComponent(containerId)}&name=${encodeURIComponent(name || '')}`;
        window.open(url, `console-${containerId}`, 'width=900,height=650,scrollbars=yes,resizable=yes');
        // Keep legacy state for backwards compat (modal helpers remain usable)
        this.consoleContainerId = containerId;
        this.consoleContainerAgent = agent;
    },

    closeConsole() {
        if (this.consoleWs) {
            try { this.consoleWs.close(); } catch (e) {}
            this.consoleWs = null;
        }
        document.getElementById("console-modal").classList.add("hidden");
        this.consoleContainerId = null;
    },

    // -------------------------------------------------------
    // Container Edit Modal
    // -------------------------------------------------------

    async openContainerEdit(containerId, stackName, agent) {
        this._editContainerId = containerId;
        this._editContainerAgent = agent;
        this._editContainerStack = stackName;

        // Fetch spec first (without showing modal)
        try {
            const resp = await fetch(`/api/containers/${encodeURIComponent(containerId)}/edit-spec?agent=${encodeURIComponent(agent || '')}`);
            if (!resp.ok) throw new Error("Erreur " + resp.status);
            const spec = await resp.json();

            // Check if container is managed
            if (spec.managed === false) {
                this.showToast("Les containers externes ne peuvent pas être édités", "warning");
                return;
            }

            // Now show modal
            this._editSpec = spec;
            const modal = document.getElementById("container-edit-modal");
            if (!modal) return;

            document.getElementById("container-edit-title").textContent = `✏ ${this.escapeHtml(spec.name || containerId)}`;
            modal.classList.remove("hidden");
            this._renderContainerEditForm(spec);
            this._attachEditScrollSpy();
        } catch(e) {
            this.showToast("Erreur: " + e.message, "error");
        }
    },

    _attachEditScrollSpy() {
        const editBody = document.getElementById('container-edit-body');
        if (!editBody) return;
        editBody.addEventListener('scroll', () => {
            const sections = editBody.querySelectorAll('.edit-section');
            const tabs = editBody.querySelectorAll('.edit-section-tab');
            let currentSection = sections[0]?.id || '';
            sections.forEach(s => {
                const rect = s.getBoundingClientRect();
                if (rect.top <= 150) currentSection = s.id;
            });
            tabs.forEach(t => {
                t.classList.toggle('active', t.dataset.section === currentSection.replace('edit-section-', ''));
            });
        });
    },

    _renderContainerEditForm(spec) {
        const body = document.getElementById("container-edit-body");
        
        // Tabs (ancres de scroll)
        let html = '<div class="edit-section-tabs">';
        const tabs = [
            {id:'info', label: this.icon('info') + ' Infos'},
            {id:'ports', label: this.icon('cable') + ' Ports'},
            {id:'volumes', label: this.icon('hard-drive') + ' Volumes'},
            {id:'env', label: this.icon('code') + ' Env'},
            {id:'network', label: this.icon('globe') + ' Réseau'},
        ];
        tabs.forEach((t) => {
            html += `<button class="edit-section-tab ${t.id==='info'?'active':''}" data-section="${t.id}" onclick="document.getElementById('edit-section-${t.id}').scrollIntoView({behavior:'smooth'})">${t.label}</button>`;
        });
        html += '</div>';
        
        // Info section
        html += '<div class="edit-section" id="edit-section-info">';
        html += '<div class="edit-info-grid">';
        html += `<div class="edit-info-group"><label>Nom</label><input type="text" id="edit-container-name" class="form-input" value="${this.escapeHtml(spec.name)}"></div>`;
        html += `<div class="edit-info-group"><label>Image</label><input type="text" id="edit-container-image" class="form-input" value="${this.escapeHtml(spec.image)}"></div>`;
        const statusDot = spec.status === 'running' ? 'running' : (spec.status === 'exited' ? 'exited' : 'paused');
        html += `<div class="edit-info-group"><label>Statut</label><div class="edit-value"><span class="edit-status-dot ${statusDot}"></span>${this.escapeHtml(spec.status)}</div></div>`;
        html += `<div class="edit-info-group"><label>Stack</label><div class="edit-value">${this.escapeHtml(spec.stack || 'Standalone')}</div></div>`;
        html += '</div>';
        // Restart policy
        html += '<div class="form-group"><label>Politique de redémarrage</label><select id="edit-restart-policy" class="edit-select">';
        ['no','always','on-failure','unless-stopped'].forEach(p => {
            html += `<option value="${p}" ${spec.restart_policy===p?'selected':''}>${p}</option>`;
        });
        html += '</select></div>';
        html += '</div>'; // end info
        
        // Ports section
        html += '<div class="edit-section" id="edit-section-ports">';
        html += '<table class="edit-table"><thead><tr><th>Port hôte</th><th>Port container</th><th>Protocole</th><th></th></tr></thead><tbody id="edit-ports-body">';
        (spec.ports||[]).forEach(p => {
            const cp = p.container_port || '';
            const hp = p.host_port || '';
            const parts = cp.split('/');
            const portNum = parts[0] || '';
            const proto = parts[1] || 'tcp';
            html += `<tr>
                <td><input type="text" class="edit-port-host" value="${this.escapeHtml(hp)}" placeholder="8080"></td>
                <td><input type="text" class="edit-port-ctn" value="${this.escapeHtml(portNum)}" placeholder="80"></td>
                <td><select class="edit-select edit-port-proto"><option value="tcp" ${proto==='tcp'?'selected':''}>TCP</option><option value="udp" ${proto==='udp'?'selected':''}>UDP</option></select></td>
                <td><button class="btn-icon-row" onclick="this.closest('tr').remove()">${this.icon('x', 'icon-sm')}</button></td>
            </tr>`;
        });
        html += '</tbody></table><button class="edit-add-row" onclick="DockyApp._addEditRow(\'ports\')">' + this.icon('plus', 'icon-sm') + ' Ajouter un port</button>';
        html += '</div>'; // end ports
        
        // Volumes section
        html += '<div class="edit-section" id="edit-section-volumes">';
        html += '<table class="edit-table"><thead><tr><th>Chemin hôte</th><th>Chemin container</th><th>Mode</th><th></th></tr></thead><tbody id="edit-volumes-body">';
        (spec.volumes||[]).forEach(v => {
            html += `<tr>
                <td><input type="text" class="edit-vol-host" value="${this.escapeHtml(v.host_path||'')}" placeholder="/host/path"></td>
                <td><input type="text" class="edit-vol-ctn" value="${this.escapeHtml(v.container_path||'')}" placeholder="/container/path"></td>
                <td><select class="edit-select edit-vol-mode"><option value="rw" ${(v.mode||'rw')==='rw'?'selected':''}>RW</option><option value="ro" ${(v.mode||'')==='ro'?'selected':''}>RO</option></select></td>
                <td><button class="btn-icon-row" onclick="this.closest('tr').remove()">${this.icon('x', 'icon-sm')}</button></td>
            </tr>`;
        });
        html += '</tbody></table><button class="edit-add-row" onclick="DockyApp._addEditRow(\'volumes\')">' + this.icon('plus', 'icon-sm') + ' Ajouter un volume</button>';
        html += '</div>'; // end volumes
        
        // Env section
        html += '<div class="edit-section" id="edit-section-env">';
        html += '<table class="edit-table"><thead><tr><th>Variable</th><th>Valeur</th><th></th></tr></thead><tbody id="edit-env-body">';
        (spec.env||[]).forEach(e => {
            html += `<tr>
                <td><input type="text" class="edit-env-key" value="${this.escapeHtml(e.key||'')}" placeholder="KEY"></td>
                <td><input type="text" class="edit-env-val" value="${this.escapeHtml(e.value||'')}" placeholder="value"></td>
                <td><button class="btn-icon-row" onclick="this.closest('tr').remove()">${this.icon('x', 'icon-sm')}</button></td>
            </tr>`;
        });
        html += '</tbody></table><button class="edit-add-row" onclick="DockyApp._addEditRow(\'env\')">' + this.icon('plus', 'icon-sm') + ' Ajouter une variable</button>';
        html += '</div>'; // end env
        
        // Network section (read-only)
        html += '<div class="edit-section" id="edit-section-network">';
        const nets = spec.networks || [];
        if (nets.length === 0) {
            html += '<p class="placeholder-hint">Aucun réseau configuré</p>';
        } else {
            html += '<table class="edit-table"><thead><tr><th>Réseau</th><th>IP</th></tr></thead><tbody>';
            nets.forEach(n => {
                html += `<tr><td class="edit-value-readonly">${this.escapeHtml(n.name||'')}</td><td class="edit-value-readonly">${this.escapeHtml(n.ip||'')}</td></tr>`;
            });
            html += '</tbody></table>';
            html += '<p style="color:var(--text-muted);font-size:0.75rem;margin-top:8px;">' + this.icon('info') + ' La configuration réseau n\'est pas modifiable dans cette version.</p>';
        }
        html += '</div>'; // end network
        
        if (typeof lucide !== 'undefined') {
            lucide.createIcons();
        }

        body.innerHTML = html;
    },

    _addEditRow(section) {
        const tbody = document.getElementById(`edit-${section}-body`);
        if (!tbody) return;
        const rows = {
            ports: '<tr><td><input type="text" class="edit-port-host" placeholder="8080"></td><td><input type="text" class="edit-port-ctn" placeholder="80"></td><td><select class="edit-select edit-port-proto"><option value="tcp">TCP</option><option value="udp">UDP</option></select></td><td><button class="btn-icon-row" onclick="this.closest(\'tr\').remove()">' + this.icon('x', 'icon-sm') + '</button></td></tr>',
            volumes: '<tr><td><input type="text" class="edit-vol-host" placeholder="/host/path"></td><td><input type="text" class="edit-vol-ctn" placeholder="/container/path"></td><td><select class="edit-select edit-vol-mode"><option value="rw">RW</option><option value="ro">RO</option></select></td><td><button class="btn-icon-row" onclick="this.closest(\'tr\').remove()">' + this.icon('x', 'icon-sm') + '</button></td></tr>',
            env: '<tr><td><input type="text" class="edit-env-key" placeholder="KEY"></td><td><input type="text" class="edit-env-val" placeholder="value"></td><td><button class="btn-icon-row" onclick="this.closest(\'tr\').remove()">' + this.icon('x', 'icon-sm') + '</button></td></tr>',
        };
        if (rows[section]) tbody.insertAdjacentHTML('beforeend', rows[section]);
    },

    async applyContainerEdit() {
        const name = document.getElementById('edit-container-name')?.value?.trim() || this._editSpec?.name || '';
        const image = document.getElementById('edit-container-image')?.value?.trim() || this._editSpec?.image || '';

        const spec = {
            name: name,
            image: image,
            restart_policy: document.getElementById('edit-restart-policy')?.value || 'no',
            ports: [],
            volumes: [],
            env: [],
        };
        
        // Collect ports
        document.querySelectorAll('#edit-ports-body tr').forEach(tr => {
            const host = tr.querySelector('.edit-port-host')?.value?.trim();
            const ctn = tr.querySelector('.edit-port-ctn')?.value?.trim();
            const proto = tr.querySelector('.edit-port-proto')?.value || 'tcp';
            if (ctn) spec.ports.push({ host_port: host || '', container_port: `${ctn}/${proto}` });
        });
        
        // Collect volumes
        document.querySelectorAll('#edit-volumes-body tr').forEach(tr => {
            const host = tr.querySelector('.edit-vol-host')?.value?.trim();
            const ctn = tr.querySelector('.edit-vol-ctn')?.value?.trim();
            const mode = tr.querySelector('.edit-vol-mode')?.value || 'rw';
            if (host && ctn) spec.volumes.push({ host_path: host, container_path: ctn, mode });
        });
        
        // Collect env
        document.querySelectorAll('#edit-env-body tr').forEach(tr => {
            const key = tr.querySelector('.edit-env-key')?.value?.trim();
            const val = tr.querySelector('.edit-env-val')?.value?.trim();
            if (key) spec.env.push({ key, value: val || '' });
        });
        
        // Confirm if running
        if (this._editSpec && this._editSpec.status === 'running') {
            if (!confirm("Ce container est en cours d'exécution et va être recréé. Continuer ?")) return;
        }
        
        this.showToast("Application des modifications…", "info");
        
        try {
            const resp = await fetch(`/api/containers/${encodeURIComponent(this._editContainerId)}/update?agent=${encodeURIComponent(this._editContainerAgent || '')}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(spec),
            });
            const result = await resp.json();
            
            if (!result.success) {
                this.showToast("Erreur : " + (result.error || "Échec"), "error");
                return;
            }
            
            this.showToast("✓ Container mis à jour", "success");
            this.closeContainerEdit();
            await this.refreshStacks();
        } catch(e) {
            this.showToast("Erreur : " + e.message, "error");
        }
    },

    closeContainerEdit() {
        const modal = document.getElementById("container-edit-modal");
        if (modal) modal.classList.add("hidden");
        this._editSpec = null;
        this._editContainerId = null;
        this._editContainerAgent = null;
    },

    // -------------------------------------------------------
    // Ports
    // -------------------------------------------------------

    async togglePorts() {
        const panel = document.getElementById("ports-panel");
        if (panel.classList.contains("hidden")) {
            panel.classList.remove("hidden");
            await this.loadPorts();
        } else {
            panel.classList.add("hidden");
        }
    },

    async loadPorts() {
        const target = document.getElementById("ports-list");
        if (!target) return;
        target.innerHTML = '<p class="placeholder-hint">Chargement…</p>';
        const data = await this.apiFetch("/api/ports" + this.agentQueryParam());
        if (!data) return;
        if (data.length === 0) {
            target.innerHTML = '<p class="placeholder-hint">Aucun port détecté</p>';
            return;
        }
        let html = '<div class="ports-grid">';
        for (const p of data) {
            const srcClass = p.source === "docker" ? "port-docker" : "port-system";
            const agentBadge = p.agent_name
                ? `<span class="port-agent">🖥 ${this.escapeHtml(p.agent_name)}</span>`
                : "";
            html += `
                <div class="port-item ${srcClass}">
                    <span class="port-number">:${this.escapeHtml(p.port)}</span>
                    <span class="port-source">${p.source === "docker" ? "🐳" : "🖥"}</span>
                    ${p.container ? `<span class="port-container">${this.escapeHtml(p.container)}</span>` : ""}
                    ${p.stack ? `<span class="port-stack">(${this.escapeHtml(p.stack)})</span>` : ""}
                    ${agentBadge}
                </div>`;
        }
        html += "</div>";
        target.innerHTML = html;
    },

    // -------------------------------------------------------
    // Auto-refresh
    // -------------------------------------------------------

    startAutoRefresh() {
        this.stopAutoRefresh();
        this.refreshInterval = setInterval(() => {
            if (this.autoRefresh) {
                this.refreshStacks();
            }
        }, this.refreshTimer);
    },

    stopAutoRefresh() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
            this.refreshInterval = null;
        }
    },

    // -------------------------------------------------------
    // Events WebSocket + Heartbeat
    // -------------------------------------------------------

    connectEvents() {
        if (this._eventsWs) return;
        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${window.location.host}/api/events`;

        try {
            this._eventsWs = new WebSocket(url);
            this._eventsWs.onopen = () => {
                console.debug('Events WS connected');
            };
            this._eventsWs.onmessage = (event) => {
                // Debounced refresh (max 1x toutes les 2s)
                this._debouncedEventRefresh();
            };
            this._eventsWs.onclose = () => {
                this._eventsWs = null;
                // Auto-reconnect après 5s
                this._eventsReconnectTimer = setTimeout(() => this.connectEvents(), 5000);
            };
            this._eventsWs.onerror = () => {
                this._eventsWs = null;
            };
        } catch(e) {
            console.warn('Events WS error:', e);
            this._eventsReconnectTimer = setTimeout(() => this.connectEvents(), 5000);
        }
    },

    disconnectEvents() {
        if (this._eventsWs) {
            try { this._eventsWs.close(); } catch(e) {}
            this._eventsWs = null;
        }
        if (this._eventsReconnectTimer) {
            clearTimeout(this._eventsReconnectTimer);
            this._eventsReconnectTimer = null;
        }
    },

    _debouncedEventRefresh() {
        if (this._refreshThrottle) return;
        this._refreshThrottle = true;
        setTimeout(() => {
            this._refreshThrottle = false;
            if (!document.hidden) {
                this.refreshStacks();
            }
        }, 2000);
    },

    startHeartbeat() {
        this.stopHeartbeat();
        this._heartbeatInterval = setInterval(async () => {
            try {
                await fetch('/api/presence/heartbeat', {
                    method: 'POST',
                    credentials: 'same-origin'
                });
            } catch(e) {
                // Silently fail
            }
        }, 30000);
    },

    stopHeartbeat() {
        if (this._heartbeatInterval) {
            clearInterval(this._heartbeatInterval);
            this._heartbeatInterval = null;
        }
    },

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
        const batchUrl = "/api/stacks/" + encodeURIComponent(name) + "/files-with-content" + agentParam;

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
            const filesData = await this.apiFetch("/api/stacks/" + encodeURIComponent(name) + "/files" + agentParam);
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
            tabsHtml += '<button class="tab-btn' + active + mod + '" onclick="DockyApp.selectFile(' + JSON.stringify(f.name) + ')">'
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
        toolbarHtml += '<button class="btn btn-ghost btn-sm" onclick="DockyApp.openHistory()" title="Historique git">' + this.icon('clipboard-list') + '</button>';
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
    // New stack
    // -------------------------------------------------------

    DEFAULT_COMPOSE_TEMPLATE: 'version: "3.8"\n\nservices:\n  # Ajoute tes services ici\n',

    openNewStackModal() {
        const modal = document.getElementById("new-stack-modal");
        modal.classList.remove("hidden");
        document.getElementById("new-stack-name").value = "";
        document.getElementById("new-stack-compose").value = this.DEFAULT_COMPOSE_TEMPLATE;
        document.getElementById("new-stack-env").value = "";
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
        if (!name) {
            this.showToast("Le nom est requis", "error");
            return;
        }
        const agentParam = this.agentQuery(this.selectedStackAgent);
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
            this.loadEditor(name, this.selectedStackAgent, true);
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
    // Chat LLM (Phase 4)
    // -------------------------------------------------------

    async sendChatMessage() {
        if (this.chatBusy) return;
        const input = document.getElementById("chat-input");
        if (!input) return;
        const message = input.value.trim();
        if (!message) return;

        // If LLM is not configured, don't try
        if (!this.chatLLMConfigured) {
            this.renderChatMessage("system", "LLM non configuré. Va dans Settings pour configurer l'endpoint.");
            return;
        }

        // Clear welcome
        const welcome = document.getElementById("chat-welcome");
        if (welcome) welcome.remove();

        // Render user bubble
        this.renderChatMessage("user", message);
        input.value = "";

        // Build history to send (without the current message — the backend
        // appends user_message separately to avoid duplication).
        const historyToSend = [...this.chatHistory];

        // Show loading
        this.chatBusy = true;
        this.setChatInputEnabled(false);
        this.showChatLoading(true);

        try {
            const resp = await fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message, history: historyToSend }),
                credentials: "same-origin",
            });
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            const data = await resp.json();

            if (resp.status === 400 && data.detail && data.detail.toLowerCase().includes("not configured")) {
                this.chatLLMConfigured = false;
                this.setChatInputEnabled(false);
                this.renderChatMessage("system", "LLM non configuré. Va dans Settings pour configurer l'endpoint.");
                return;
            }
            if (!resp.ok) {
                const err = data.detail || ("Erreur " + resp.status);
                this.renderChatMessage("error", err);
                return;
            }

            // Tool calls indicator
            if (data.tool_calls && data.tool_calls.length > 0) {
                this.renderToolCalls(data.tool_calls);
            }

            // Use the full history returned by the backend, which includes
            // user message, assistant responses, tool_calls AND tool results.
            // This guarantees the LLM sees the complete context on the next
            // message instead of losing tool call results.
            if (data.history && Array.isArray(data.history)) {
                this.chatHistory = data.history;
            } else {
                // Fallback: construct manually as before.
                this.chatHistory.push({ role: "user", content: message });
                if (data.response) {
                    this.chatHistory.push({ role: "assistant", content: data.response });
                }
            }

            // LLM response bubble
            if (data.response) {
                this.renderChatMessage("assistant", data.response);
            }

            // Human validation requests
            if (data.needs_validation && data.needs_validation.length > 0) {
                for (const item of data.needs_validation) {
                    this.renderValidationRequest(item);
                }
            }
        } catch (e) {
            this.renderChatMessage("error", "Erreur réseau: " + e.message);
        } finally {
            this.chatBusy = false;
            this.setChatInputEnabled(true);
            this.showChatLoading(false);
        }
    },

    onChatKeydown(e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            this.sendChatMessage();
        }
    },

    renderChatMessage(role, content) {
        const container = document.getElementById("chat-messages");
        if (!container) return;

        const welcome = document.getElementById("chat-welcome");
        if (welcome) welcome.remove();

        const wrapper = document.createElement("div");
        wrapper.className = "chat-msg chat-msg-" + role;

        const bubble = document.createElement("div");
        bubble.className = "chat-bubble chat-bubble-" + role;

        if (role === "error") {
            bubble.classList.add("chat-bubble-error");
        }

        // Format content: escape HTML, then restore code blocks
        bubble.innerHTML = this.formatChatContent(content);

        wrapper.appendChild(bubble);
        container.appendChild(wrapper);
        this.scrollChatToBottom();
        return wrapper;
    },

    formatChatContent(text) {
        if (!text) return "";
        // Escape HTML first
        let html = this.escapeHtml(text);
        // Convert `inline code` to <code>
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
        // Convert multi-line code blocks ```...```
        html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
        // Basic line breaks
        html = html.replace(/\n/g, "<br>");
        // Fix: <pre> blocks shouldn't have <br>
        html = html.replace(/<pre><code>([\s\S]*?)<\/code><\/pre>/g, function(m, p1) {
            return '<pre><code>' + p1.replace(/<br>/g, '\n') + '</code></pre>';
        });
        return html;
    },

    renderToolCalls(toolCalls) {
        const container = document.getElementById("chat-messages");
        if (!container) return;
        const names = toolCalls.map(tc => tc.name || tc).join(", ");
        const div = document.createElement("div");
        div.className = "chat-toolcalls";
        div.innerHTML = '🔧 Actions effectuées: ' + this.escapeHtml(names);
        container.appendChild(div);
        this.scrollChatToBottom();
    },

    renderValidationRequest(item) {
        const container = document.getElementById("chat-messages");
        if (!container) return;

        const args = item.arguments || {};
        const toolName = item.name || "";

        // clean_agent validation
        if (toolName === "clean_agent") {
            const agentName = args.agent_name || item.agent_name || "?";
            const div = document.createElement("div");
            div.className = "chat-validation";
            div.innerHTML =
                '<div class="chat-validation-label">⚠ Le LLM veut nettoyer l\'agent:</div>' +
                '<code class="chat-validation-cmd">docker system prune -f</code>' +
                '<div class="chat-validation-container">sur l\'agent <strong>' + this.escapeHtml(agentName) + '</strong></div>' +
                '<div class="chat-validation-buttons">' +
                '<button class="btn btn-success btn-sm chat-btn-allow" onclick="DockyApp.authorizeClean(\'' +
                    this.escapeHtml(agentName) + '\', this)">Autoriser</button>' +
                '<button class="btn btn-danger btn-sm chat-btn-refuse" onclick="DockyApp.refuseExec(this)">Refuser</button>' +
                '</div>';
            container.appendChild(div);
            this.scrollChatToBottom();
            return;
        }

        // Default: exec_in_container validation
        const containerId = args.container_id || item.container_id || "?";
        const command = args.command || item.command || "?";

        const div = document.createElement("div");
        div.className = "chat-validation";
        div.innerHTML =
            '<div class="chat-validation-label">⚠ Le LLM veut exécuter:</div>' +
            '<code class="chat-validation-cmd">' + this.escapeHtml(command) + '</code>' +
            '<div class="chat-validation-container">dans le container <strong>' + this.escapeHtml(containerId) + '</strong></div>' +
            '<div class="chat-validation-buttons">' +
            '<button class="btn btn-success btn-sm chat-btn-allow" onclick="DockyApp.authorizeExec(\'' +
                this.escapeHtml(containerId) + '\', \'' + this.escapeHtml(command.replace(/'/g, "\\'")) +
                '\', this)">Autoriser</button>' +
            '<button class="btn btn-danger btn-sm chat-btn-refuse" onclick="DockyApp.refuseExec(this)">Refuser</button>' +
            '</div>';
        container.appendChild(div);
        this.scrollChatToBottom();
    },

    async authorizeExec(containerId, command, btn) {
        if (!btn) return;
        // Disable buttons
        const parent = btn.closest(".chat-validation-buttons");
        if (parent) {
            parent.querySelectorAll("button").forEach(b => b.disabled = true);
        }
        btn.textContent = "Exécution…";

        try {
            const resp = await fetch("/api/chat/validate-exec", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ container_id: containerId, command: command }),
                credentials: "same-origin",
            });
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            const data = await resp.json();
            if (resp.ok && data.success) {
                this.renderChatMessage("system", this.icon('check') + " Commande exécutée.\nSortie:\n" + (data.output || "(vide)"));
            } else {
                this.renderChatMessage("error", "Échec de l'exécution: " + (data.detail || data.output || "erreur inconnue"));
            }
        } catch (e) {
            this.renderChatMessage("error", "Erreur réseau: " + e.message);
        } finally {
            // Remove the validation box
            const box = btn.closest(".chat-validation");
            if (box) box.remove();
        }
    },

    refuseExec(btn) {
        if (!btn) return;
        const box = btn.closest(".chat-validation");
        if (box) box.remove();
        this.renderChatMessage("system", "🚫 Commande refusée par l'utilisateur.");
    },

    async authorizeClean(agentName, btn) {
        if (!btn) return;
        // Disable buttons
        const parent = btn.closest(".chat-validation-buttons");
        if (parent) {
            parent.querySelectorAll("button").forEach(b => b.disabled = true);
        }
        btn.textContent = "Exécution…";

        try {
            const resp = await fetch("/api/chat/validate-exec?agent=" + encodeURIComponent(agentName), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ type: "clean" }),
                credentials: "same-origin",
            });
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            const data = await resp.json();
            if (resp.ok && data.success) {
                this.renderChatMessage("system", this.icon('check') + " Nettoyage effectué.\nSortie:\n" + (data.output || "(vide)"));
            } else {
                this.renderChatMessage("error", "Échec du nettoyage: " + (data.detail || data.output || "erreur inconnue"));
            }
        } catch (e) {
            this.renderChatMessage("error", "Erreur réseau: " + e.message);
        } finally {
            // Remove the validation box
            const box = btn.closest(".chat-validation");
            if (box) box.remove();
        }
    },

    clearChat() {
        this.chatHistory = [];
        const container = document.getElementById("chat-messages");
        if (!container) return;
        container.innerHTML =
            '<div class="chat-welcome" id="chat-welcome">' +
            '<p>Pose une question ou demande une action sur tes containers.</p>' +
            '</div>';
    },

    // -------------------------------------------------------
    // Chat panel toggle (show/hide)
    // -------------------------------------------------------

    toggleChat() {
        this.chatVisible = !this.chatVisible;
        this.applyChatVisibility();
        // Persist preference
        try {
            localStorage.setItem('docky-chat-visible', this.chatVisible ? '1' : '0');
        } catch (e) {
            /* localStorage may be unavailable */
        }
    },

    applyChatVisibility() {
        const chatPanel = document.querySelector('.chat-panel');
        const hResizer = document.getElementById('resizer-horizontal');
        if (chatPanel) {
            chatPanel.style.display = this.chatVisible ? '' : 'none';
        }
        if (hResizer) {
            hResizer.style.display = this.chatVisible ? '' : 'none';
        }
        // Let the dashboard take the full height when the chat is hidden
        const dashboardPanel = document.querySelector('.dashboard-panel');
        if (dashboardPanel) {
            if (!this.chatVisible) {
                dashboardPanel.style.flex = '1';
                dashboardPanel.style.height = '';
            } else {
                // Restore saved height if available, otherwise reset to flex default
                const saved = localStorage.getItem('docky-dashboard-height');
                if (saved) {
                    dashboardPanel.style.height = saved + '%';
                    dashboardPanel.style.flex = 'none';
                } else {
                    dashboardPanel.style.flex = '';
                    dashboardPanel.style.height = '';
                }
            }
        }
        // Update the toggle button active state
        const btn = document.getElementById('chat-toggle');
        if (btn) {
            btn.classList.toggle('active', this.chatVisible);
        }
    },

    showChatLoading(show) {
        const loading = document.getElementById("chat-loading");
        if (!loading) return;
        if (show) loading.classList.remove("hidden");
        else loading.classList.add("hidden");
        this.scrollChatToBottom();
    },

    setChatInputEnabled(enabled) {
        const input = document.getElementById("chat-input");
        const btn = document.getElementById("chat-send-btn");
        if (input) input.disabled = !enabled;
        if (btn) btn.disabled = !enabled;
        if (enabled && input) input.focus();
    },

    scrollChatToBottom() {
        const container = document.getElementById("chat-messages");
        if (!container) return;
        // Use setTimeout to ensure DOM is updated
        requestAnimationFrame(() => {
            container.scrollTop = container.scrollHeight;
        });
    },

    // -------------------------------------------------------
    // SOUL.md editor
    // -------------------------------------------------------

    async openSoulEditor() {
        const modal = document.getElementById("soul-modal");
        if (!modal) return;
        const textarea = document.getElementById("soul-editor");
        if (textarea) {
            textarea.value = "Chargement…";
            textarea.disabled = true;
        }
        modal.classList.remove("hidden");

        const data = await this.apiFetch("/api/soul");
        if (data === null) {
            if (textarea) textarea.value = "";
            return;
        }
        if (textarea) {
            textarea.value = data.content || "";
            textarea.disabled = false;
        }
    },

    closeSoulEditor() {
        const modal = document.getElementById("soul-modal");
        if (modal) modal.classList.add("hidden");
    },

    async saveSoul() {
        const textarea = document.getElementById("soul-editor");
        if (!textarea) return;
        const content = textarea.value;
        const resp = await fetch("/api/soul", {
            method: "PUT",
            headers: { "Content-Type": "text/plain" },
            body: content,
            credentials: "same-origin",
        });
        if (resp.status === 401) {
            window.location.href = "/login";
            return;
        }
        if (resp.ok) {
            const data = await resp.json().catch(() => ({}));
            if (data.success !== false) {
                this.showToast("SOUL.md sauvegardé", "success");
                this.closeSoulEditor();
            } else {
                this.showToast("Erreur sauvegarde SOUL.md", "error");
            }
        } else {
            const data = await resp.json().catch(() => ({}));
            this.showToast("Erreur: " + (data.detail || resp.statusText), "error");
        }
    },

    // -------------------------------------------------------
    // Panel resizers (click'n'drag)
    // -------------------------------------------------------

    initResizers() {
        const self = this;

        const vResizer = document.getElementById('resizer-vertical');
        const hResizer = document.getElementById('resizer-horizontal');

        // Restaurer les tailles sauvegardées
        this.restorePanelSizes();

        if (vResizer) {
            vResizer.addEventListener('mousedown', function(e) {
                e.preventDefault();
                const layout = document.querySelector('.app-layout');
                const leftCol = document.querySelector('.left-column');
                if (!layout || !leftCol) return;

                const startX = e.clientX;
                const containerWidth = layout.getBoundingClientRect().width;
                const startWidth = leftCol.getBoundingClientRect().width;

                document.body.style.cursor = 'col-resize';
                document.body.style.userSelect = 'none';
                vResizer.classList.add('active');

                function onMouseMove(e) {
                    const dx = e.clientX - startX;
                    const newWidth = Math.max(200, Math.min(containerWidth - 200, startWidth + dx));
                    const percent = (newWidth / containerWidth) * 100;
                    leftCol.style.width = percent + '%';
                    leftCol.style.flex = 'none';
                    localStorage.setItem('docky-left-width', percent);
                }

                function onMouseUp() {
                    document.body.style.cursor = '';
                    document.body.style.userSelect = '';
                    vResizer.classList.remove('active');
                    document.removeEventListener('mousemove', onMouseMove);
                    document.removeEventListener('mouseup', onMouseUp);
                }

                document.addEventListener('mousemove', onMouseMove);
                document.addEventListener('mouseup', onMouseUp);
            });
        }

        if (hResizer) {
            hResizer.addEventListener('mousedown', function(e) {
                e.preventDefault();
                const leftCol = document.querySelector('.left-column');
                if (!leftCol) return;

                const startY = e.clientY;
                const containerHeight = leftCol.getBoundingClientRect().height;
                const dashboardPanel = document.querySelector('.dashboard-panel');
                if (!dashboardPanel) return;
                const startHeight = dashboardPanel.getBoundingClientRect().height;

                document.body.style.cursor = 'row-resize';
                document.body.style.userSelect = 'none';
                hResizer.classList.add('active');

                function onMouseMove(e) {
                    const dy = e.clientY - startY;
                    const newHeight = Math.max(150, Math.min(containerHeight - 100, startHeight + dy));
                    const percent = (newHeight / containerHeight) * 100;
                    dashboardPanel.style.height = percent + '%';
                    dashboardPanel.style.flex = 'none';
                    localStorage.setItem('docky-dashboard-height', percent);
                }

                function onMouseUp() {
                    document.body.style.cursor = '';
                    document.body.style.userSelect = '';
                    hResizer.classList.remove('active');
                    document.removeEventListener('mousemove', onMouseMove);
                    document.removeEventListener('mouseup', onMouseUp);
                }

                document.addEventListener('mousemove', onMouseMove);
                document.addEventListener('mouseup', onMouseUp);
            });
        }
    },

    restorePanelSizes() {
        const leftWidth = localStorage.getItem('docky-left-width');
        const dashHeight = localStorage.getItem('docky-dashboard-height');

        if (leftWidth) {
            const leftCol = document.querySelector('.left-column');
            if (leftCol) {
                leftCol.style.width = leftWidth + '%';
                leftCol.style.flex = 'none';
            }
        }
        if (dashHeight && this.chatVisible) {
            const dash = document.querySelector('.dashboard-panel');
            if (dash) {
                dash.style.height = dashHeight + '%';
                dash.style.flex = 'none';
            }
        }
    },

    // -------------------------------------------------------
    // Git History
    // -------------------------------------------------------

    async openHistory() {
        const name = this.selectedStack;
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
        const name = this.selectedStack;
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
        const name = this.selectedStack;
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

    // -------------------------------------------------------
    // Sort & Group
    // -------------------------------------------------------

    onSortChange() {
        const select = document.getElementById('sort-select');
        if (!select) return;
        this._sortMode = select.value;
        try {
            localStorage.setItem('docky_sort_mode', this._sortMode);
        } catch (e) { /* ignore */ }
        if (this._allContainersCache && this._allContainersCache.length > 0) {
            this.renderCurrentView();
        }
    },

    onGroupChange() {
        const select = document.getElementById('group-select');
        if (!select) return;
        this._groupMode = select.value;
        try {
            localStorage.setItem('docky_group_mode', this._groupMode);
        } catch (e) { /* ignore */ }
        if (this._allContainersCache && this._allContainersCache.length > 0) {
            this.renderCurrentView();
        }
    },

    _sortStacks(stacks) {
        const mode = this._sortMode;
        return [...stacks].sort((a, b) => {
            switch (mode) {
                case 'name-asc':
                    return a.name.localeCompare(b.name);
                case 'name-desc':
                    return b.name.localeCompare(a.name);
                case 'cpu-desc':
                case 'ram-desc':
                    // Tri par stats CPU/RAM géré au niveau des containers, pas des stacks
                    return a.name.localeCompare(b.name);
                case 'status': {
                    const order = { running: 0, partial: 1, stopped: 2, empty: 3 };
                    return (order[a.status] ?? 99) - (order[b.status] ?? 99);
                }
                default:
                    return 0;
            }
        });
    },

    _sortContainers(containers) {
        const mode = this._sortMode;
        if (mode !== 'cpu-desc' && mode !== 'ram-desc') {
            // Keep original order (already grouped by stack)
            return containers;
        }
        const key = mode === 'cpu-desc' ? 'cpu_percent' : 'mem_percent';
        return [...containers].sort((a, b) => {
            const statsA = this._statsCache[a.id] || {};
            const statsB = this._statsCache[b.id] || {};
            const valA = statsA[key] ?? 0;
            const valB = statsB[key] ?? 0;
            return valB - valA; // descending (highest first)
        });
    },

    _groupStacks(stacks) {
        const mode = this._groupMode;
        if (mode === 'none') {
            return [{ label: null, stacks }];
        }

        const groups = {};

        if (mode === 'agent') {
            for (const stack of stacks) {
                const agent = stack.agent_name || 'default';
                if (!groups[agent]) groups[agent] = [];
                groups[agent].push(stack);
            }
        }

        return Object.entries(groups).map(([label, s]) => ({ label, stacks: s }));
    },

    init() {
        // Load chat panel visibility preference (persisted in localStorage)
        try {
            this.chatVisible = localStorage.getItem('docky-chat-visible') !== '0';
        } catch (e) {
            this.chatVisible = true;
        }
        this.applyChatVisibility();

        // Restore hidden agents filter from localStorage
        try {
            const saved = localStorage.getItem('docky_hidden_agents');
            if (saved) {
                const arr = JSON.parse(saved);
                if (Array.isArray(arr)) {
                    this._hiddenAgents = new Set(arr);
                }
            }
        } catch (e) {
            this._hiddenAgents = new Set();
        }

        // Restaurer le mode d'affichage depuis localStorage
        try {
            const saved = localStorage.getItem('docky_view_mode');
            if (saved === 'grid' || saved === 'table') {
                this._viewMode = saved;
            }
        } catch (e) {
            this._viewMode = 'grid'; // défaut
        }
        const toggleBtn = document.getElementById('view-toggle');
        if (toggleBtn) toggleBtn.innerHTML = this._viewMode === 'grid' ? this.icon('list') : this.icon('layout-grid');

        // Restaurer le tri et le groupement depuis localStorage
        try {
            const sortSaved = localStorage.getItem('docky_sort_mode');
            if (sortSaved) this._sortMode = sortSaved;
        } catch (e) { /* ignore */ }
        try {
            const groupSaved = localStorage.getItem('docky_group_mode');
            // Fallback: an obsolete value (e.g. 'family') must not break rendering
            if (groupSaved === 'none' || groupSaved === 'agent') {
                this._groupMode = groupSaved;
            }
        } catch (e) { /* ignore */ }

        // Appliquer les valeurs aux selects
        const sortSelect = document.getElementById('sort-select');
        if (sortSelect) sortSelect.value = this._sortMode;
        const groupSelect = document.getElementById('group-select');
        if (groupSelect) groupSelect.value = this._groupMode;

        this.initResizers();

        // Load version number
        this.loadVersion();

        this.loadAgents();
        this.checkVersions();
        // Check périodique des versions toutes les heures (3600s)
        this._versionCheckInterval = setInterval(() => this.checkVersions(), 3600000);
        this.startAgentsRefresh();
        this.refreshStacks();
        this.updateStatsBar();

        // Event-driven: WebSocket events + heartbeat
        this.connectEvents();
        this.startHeartbeat();
        this.startAutoRefresh();
        this._debouncedEventRefresh();

        // Pause quand l'onglet est caché
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                this.stopHeartbeat();
            } else {
                this.startHeartbeat();
                this.refreshStacks();  // Refresh au retour
                // Re-check des versions si > 5 min depuis le dernier check
                if (!this._lastVersionCheck || (Date.now() - this._lastVersionCheck) > 5 * 60 * 1000) {
                    this.checkVersions();
                }
            }
        });

        // Auto-refresh checkbox
        const cb = document.getElementById("auto-refresh");
        if (cb) {
            cb.addEventListener("change", () => {
                this.autoRefresh = cb.checked;
            });
        }

        // Close modals on backdrop click
        const consoleModal = document.getElementById("console-modal");
        if (consoleModal) {
            consoleModal.addEventListener("click", (e) => {
                if (e.target === consoleModal) this.closeConsole();
            });
        }
        const newStackModal = document.getElementById("new-stack-modal");
        if (newStackModal) {
            newStackModal.addEventListener("click", (e) => {
                if (e.target === newStackModal) this.closeNewStackModal();
            });
        }
        const deleteStackModal = document.getElementById("delete-stack-modal");
        if (deleteStackModal) {
            deleteStackModal.addEventListener("click", (e) => {
                if (e.target === deleteStackModal) this.closeDeleteStackModal();
            });
        }
        const permsModal = document.getElementById("perms-modal");
        if (permsModal) {
            permsModal.addEventListener("click", (e) => {
                if (e.target === permsModal) this.closePermsModal();
            });
        }
        const soulModal = document.getElementById("soul-modal");
        if (soulModal) {
            soulModal.addEventListener("click", (e) => {
                if (e.target === soulModal) this.closeSoulEditor();
            });
        }
        const unsavedDialog = document.getElementById("unsaved-dialog");
        if (unsavedDialog) {
            unsavedDialog.addEventListener("click", (e) => {
                if (e.target === unsavedDialog) this._onUnsavedCancel();
            });
        }

        // History modal backdrop click
        const historyModal = document.getElementById("history-modal");
        if (historyModal) {
            historyModal.addEventListener("click", (e) => {
                if (e.target === historyModal) this.closeHistory();
            });
        }

        // Container edit modal backdrop click
        const editModal = document.getElementById("container-edit-modal");
        if (editModal) {
            editModal.addEventListener("click", (e) => {
                if (e.target === editModal) this.closeContainerEdit();
            });
        }

        // Activity modal backdrop click
        const activityModal = document.getElementById("activity-modal");
        if (activityModal) {
            activityModal.addEventListener("click", (e) => {
                if (e.target === activityModal) this.closeActivity();
            });
        }

        // Version mismatch modal backdrop click
        const versionMismatchModal = document.getElementById("version-mismatch-modal");
        if (versionMismatchModal) {
            versionMismatchModal.addEventListener("click", (e) => {
                if (e.target === versionMismatchModal) this.closeVersionMismatch();
            });
        }

        // Enter key shortcuts in modal inputs
        const newNameInput = document.getElementById("new-stack-name");
        if (newNameInput) {
            newNameInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") { e.preventDefault(); this.createStack(); }
            });
        }
        const permsModeInput = document.getElementById("perms-mode");
        if (permsModeInput) {
            permsModeInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") { e.preventDefault(); this.applyPermissions(); }
            });
        }

        // Chat send button
        const chatSendBtn = document.getElementById("chat-send-btn");
        if (chatSendBtn) {
            chatSendBtn.addEventListener("click", () => this.sendChatMessage());
        }

        // ESC to close modals
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") {
                this.closeConsole();
                this.closeHistory();
                this.closeNewStackModal();
                this.closeDeleteStackModal();
                this.closePermsModal();
                this.closeSoulEditor();
                this.closeContainerEdit();
                this.closeActivity();
                this.closeVersionMismatch();
                this._onUnsavedCancel();
            }
        });

        // Grid dashboard resize observer
        const dashContent = document.getElementById("dashboard-content");
        if (dashContent && window.ResizeObserver) {
            this._gridResizeObserver = new ResizeObserver(() => { this._debouncedGridRender(); });
            this._gridResizeObserver.observe(dashContent);
        }

        // Désélection par clic molette (bouton central)
        if (dashContent) {
            dashContent.addEventListener('mousedown', (e) => {
                if (e.button === 1) {  // Middle click
                    e.preventDefault();  // Empêche le scroll automatique
                    this.clearStackSelection();
                }
            });
        }
    },
};

// -------------------------------------------------------
// Boot
// -------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
    DockyApp.init();
});