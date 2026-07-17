/* ============================================================
   Docky - Frontend JavaScript (Phase 2 - Dashboard)
   ============================================================ */

const DockyApp = {
    // -------------------------------------------------------
    // State
    // -------------------------------------------------------
    stacks: [],
    expandedStack: null,
    autoRefresh: true,
    refreshInterval: null,
    refreshTimer: 5000,

    // Multi-agent
    currentAgentFilter: "all",   // "all" or agent name
    agentsList: [],              // [{name, status, ...}]
    agentsRefreshInterval: null,
    agentsRefreshTimer: 30000,
    stackAgentMap: {},           // stackName -> agentName
    selectedStackAgent: null,    // agent for the currently edited stack
    expandedStackAgent: null,    // agent for the currently expanded stack
    logsContainerAgent: null,    // agent for the container whose logs are open
    consoleContainerAgent: null, // agent for the container whose console is open

    // WebSockets
    logsWs: null,
    logsStreamMode: false,
    logsContainerId: null,
    consoleWs: null,
    consoleContainerId: null,
    consoleHistory: [],

    // Chat LLM (Phase 4)
    chatHistory: [],       // array of {role, content} sent to the API
    chatBusy: false,
    chatLLMConfigured: true,

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

    // -------------------------------------------------------
    // Multi-agent management
    // -------------------------------------------------------

    /** Build the ?agent= query string for the current filter. */
    agentQueryParam() {
        return "?agent=" + encodeURIComponent(this.currentAgentFilter);
    },

    /** Build a ?agent= query string for a specific agent. */
    agentQuery(agentName) {
        if (!agentName || agentName === "all") return "";
        return "?agent=" + encodeURIComponent(agentName);
    },

    async loadAgents() {
        const data = await this.apiFetch("/api/agents");
        if (data === null) return;
        // Expecting an array or {agents: [...]}
        this.agentsList = Array.isArray(data) ? data : (data.agents || []);
        this.renderAgentSelector();
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

        let html = '';
        // "Tous" button
        const allActive = this.currentAgentFilter === "all" ? " active" : "";
        html += '<button class="agent-btn' + allActive + '" onclick="DockyApp.setAgentFilter(\'all\')" title="Tous les agents">'
            + '🌍 Tous'
            + '</button>';

        for (const agent of this.agentsList) {
            const name = agent.name || agent;
            const status = agent.status || "offline";
            const isOnline = status === "online" || status === "connected" || status === true;
            const dotClass = isOnline ? "online" : "offline";
            const active = this.currentAgentFilter === name ? " active" : "";
            html += '<button class="agent-btn' + active + '" onclick="DockyApp.setAgentFilter(' + JSON.stringify(name) + ')" title="' + this.escapeHtml(name) + ' — ' + this.escapeHtml(status) + '">'
                + '<span class="agent-status-dot ' + dotClass + '"></span>'
                + this.escapeHtml(name)
                + '</button>';
        }

        container.innerHTML = html;
    },

    setAgentFilter(agentName) {
        if (this.currentAgentFilter === agentName) return;
        this.currentAgentFilter = agentName;
        this.expandedStack = null;
        this.renderAgentSelector();
        this.refreshStacks();
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
        const data = await this.apiFetch("/api/stacks" + this.agentQueryParam());
        if (data === null) return;
        this.stacks = data;
        // Build stackName -> agentName map
        this.stackAgentMap = {};
        for (const s of data) {
            if (s.agent_name) {
                this.stackAgentMap[s.name] = s.agent_name;
            } else if (this.currentAgentFilter !== "all") {
                this.stackAgentMap[s.name] = this.currentAgentFilter;
            }
        }
        this.renderStacks();
        // Update compose selector
        this.updateStackSelector(data);
    },

    updateStackSelector(stacks) {
        const selector = document.getElementById("stack-selector");
        if (!selector) return;
        const current = selector.value;
        selector.innerHTML = '<option value="">— Sélectionner une stack —</option>';
        stacks.forEach((s) => {
            const opt = document.createElement("option");
            opt.value = s.name;
            opt.textContent = s.name;
            selector.appendChild(opt);
        });
        if (current) selector.value = current;
    },

    renderStacks() {
        const container = document.getElementById("dashboard-content");
        if (!container) return;

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
            const isExpanded = this.expandedStack === stack.name;
            const statusBadge = this.statusBadge(stack.status);
            const containerInfo = stack.container_count > 0
                ? `${stack.running_count}/${stack.container_count} actifs`
                : "0 containers";
            const portsInfo = stack.ports && stack.ports.length > 0
                ? stack.ports.join(", ")
                : "";
            const agentBadge = (this.currentAgentFilter === "all" && stack.agent_name)
                ? '<span class="stack-agent-badge">🖥 ' + this.escapeHtml(stack.agent_name) + '</span>'
                : "";

            html += `
                <div class="stack-card ${isExpanded ? "expanded" : ""}" data-stack="${this.escapeHtml(stack.name)}">
                    <div class="stack-card-header" onclick="DockyApp.toggleStack('${this.escapeHtml(stack.name)}')">
                        <div class="stack-card-info">
                            <span class="stack-name">${this.escapeHtml(stack.name)}</span>
                            ${agentBadge}
                            ${statusBadge}
                        </div>
                        <div class="stack-card-meta">
                            <span class="meta-badge">🐳 ${containerInfo}</span>
                            ${portsInfo ? `<span class="meta-badge meta-ports">🔌 ${this.escapeHtml(portsInfo)}</span>` : ""}
                        </div>
                        <div class="stack-card-actions" onclick="event.stopPropagation()">
                            <button class="icon-btn" title="Éditer" onclick="DockyApp.selectStackFromDashboard('${this.escapeHtml(stack.name)}')">📝</button>
                            <button class="icon-btn btn-start" title="Démarrer" onclick="DockyApp.stackAction('${this.escapeHtml(stack.name)}', 'start')">▶</button>
                            <button class="icon-btn btn-stop" title="Arrêter" onclick="DockyApp.stackAction('${this.escapeHtml(stack.name)}', 'stop')">⏹</button>
                            <button class="icon-btn btn-restart" title="Redémarrer" onclick="DockyApp.stackAction('${this.escapeHtml(stack.name)}', 'restart')">🔄</button>
                            <span class="stack-chevron">${isExpanded ? "▼" : "▶"}</span>
                        </div>
                    </div>
                    <div class="stack-containers ${isExpanded ? "" : "hidden"}" id="containers-${this.escapeHtml(stack.name)}">
                        <div class="placeholder"><p>Chargement des containers…</p></div>
                    </div>
                </div>`;
        });
        html += "</div>";
        container.innerHTML = html;

        // If a stack is expanded, load its containers
        if (this.expandedStack) {
            this.loadContainers(this.expandedStack);
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

    async toggleStack(name) {
        if (this.expandedStack === name) {
            this.expandedStack = null;
        } else {
            this.expandedStack = name;
        }
        this.renderStacks();
    },

    async loadContainers(stackName) {
        const target = document.getElementById("containers-" + stackName);
        if (!target) return;
        const agent = this.stackAgentMap[stackName] || (this.currentAgentFilter !== "all" ? this.currentAgentFilter : null);
        this.expandedStackAgent = agent;
        const data = await this.apiFetch("/api/stacks/" + encodeURIComponent(stackName) + "/containers" + this.agentQuery(agent));
        if (data === null) return;
        this.renderContainers(target, data, stackName, agent);
    },

    renderContainers(target, containers, stackName, agent) {
        if (!containers || containers.length === 0) {
            target.innerHTML = '<div class="placeholder"><p>Aucun container pour cette stack</p></div>';
            return;
        }

        let html = '<div class="containers-list">';
        const agt = agent ? encodeURIComponent(agent) : "";
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
                        <div class="container-image">📦 ${image}</div>
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
                        ${ports ? `<span class="meta-badge meta-ports">🔌 ${this.escapeHtml(ports)}</span>` : ""}
                        <span class="update-badge hidden" id="update-${this.escapeHtml(c.id)}">⬆ Update dispo</span>
                    </div>
                    <div class="container-actions">
                        <button class="icon-btn btn-start" title="Start" onclick="DockyApp.containerAction('${this.escapeHtml(c.id)}', 'start', '${agt}')">▶</button>
                        <button class="icon-btn btn-stop" title="Stop" onclick="DockyApp.containerAction('${this.escapeHtml(c.id)}', 'stop', '${agt}')">⏹</button>
                        <button class="icon-btn btn-restart" title="Restart" onclick="DockyApp.containerAction('${this.escapeHtml(c.id)}', 'restart', '${agt}')">🔄</button>
                        <button class="icon-btn btn-logs" title="Logs" onclick="DockyApp.openLogs('${this.escapeHtml(c.id)}', '${name}', '${agt}')">📋</button>
                        <button class="icon-btn btn-console" title="Console" onclick="DockyApp.openConsole('${this.escapeHtml(c.id)}', '${name}', '${agt}')">🖥</button>
                    </div>
                </div>`;
        }
        html += "</div>";
        target.innerHTML = html;

        // Load resources for running containers
        for (const c of containers) {
            if (c.status === "running") {
                this.loadContainerStats(c.id, agent);
                this.checkUpdate(c.id, agent);
            }
        }
    },

    // -------------------------------------------------------
    // Stats / Resources
    // -------------------------------------------------------

    async loadContainerStats(containerId, agent) {
        const data = await this.apiFetch("/api/containers/" + containerId + "/stats" + this.agentQuery(agent));
        if (!data) return;
        this.renderStats(containerId, data);
    },

    renderStats(containerId, stats) {
        const target = document.getElementById("resources-" + containerId);
        if (!target) return;
        const cpuPct = Math.min(stats.cpu_percent, 100);
        const memPct = Math.min(stats.mem_percent, 100);

        const cpuFill = target.querySelector(".resource-line:nth-child(1) .progress-fill");
        const cpuVal = target.querySelector(".resource-line:nth-child(1) .resource-value");
        const memFill = target.querySelector(".resource-line:nth-child(2) .progress-fill");
        const memVal = target.querySelector(".resource-line:nth-child(2) .resource-value");

        if (cpuFill) cpuFill.style.width = cpuPct + "%";
        if (cpuVal) cpuVal.textContent = stats.cpu_percent.toFixed(1) + "%";
        if (memFill) memFill.style.width = memPct + "%";
        if (memVal) memVal.textContent = this.formatBytes(stats.mem_usage) + " / " + this.formatBytes(stats.mem_limit);
    },

    // -------------------------------------------------------
    // Actions
    // -------------------------------------------------------

    async containerAction(id, action, agent) {
        this.showToast(`${action} container…`, "info");
        const result = await this.apiPost(`/api/containers/${id}/${action}` + this.agentQuery(agent));
        if (result && result.success) {
            this.showToast(`Container ${action} OK`, "success");
        } else {
            this.showToast(`Échec ${action} container`, "error");
        }
        // Refresh after a short delay
        setTimeout(() => this.refreshStacks(), 1000);
    },

    async stackAction(name, action) {
        this.showToast(`${action} stack "${name}"…`, "info");
        const agent = this.stackAgentMap[name] || (this.currentAgentFilter !== "all" ? this.currentAgentFilter : null);
        const result = await this.apiPost(`/api/stacks/${encodeURIComponent(name)}/${action}` + this.agentQuery(agent));
        if (result && result.success) {
            this.showToast(`Stack ${action} OK`, "success");
        } else {
            const err = result && result.error ? result.error : "";
            this.showToast(`Échec ${action} stack: ${err}`, "error");
        }
        setTimeout(() => this.refreshStacks(), 2000);
    },

    // -------------------------------------------------------
    // Update check
    // -------------------------------------------------------

    async checkUpdate(containerId, agent) {
        const data = await this.apiFetch("/api/containers/" + containerId + "/update-check" + this.agentQuery(agent));
        if (!data) return;
        if (data.update_available) {
            const badge = document.getElementById("update-" + containerId);
            if (badge) badge.classList.remove("hidden");
        }
    },

    // -------------------------------------------------------
    // Logs
    // -------------------------------------------------------

    async openLogs(containerId, name, agent) {
        this.logsContainerId = containerId;
        this.logsContainerAgent = agent;
        this.logsStreamMode = false;
        const title = document.getElementById("logs-title");
        if (title) title.textContent = `📋 Logs - ${name}`;
        const modal = document.getElementById("logs-modal");
        modal.classList.remove("hidden");
        const output = document.getElementById("logs-output");
        output.innerHTML = '<div class="terminal-line">Chargement…</div>';

        // Fetch static logs
        const agentParam = agent ? "\u0026agent=" + encodeURIComponent(agent) : "";
        const data = await this.apiFetch(`/api/containers/${containerId}/logs?tail=200${agentParam}`);
        if (data && data.lines) {
            this.renderLogs(data.lines);
        }
        // Reset stream toggle
        const toggle = document.getElementById("logs-stream-toggle");
        if (toggle) toggle.checked = false;
    },

    renderLogs(lines) {
        const output = document.getElementById("logs-output");
        if (!output) return;
        if (!lines || lines.length === 0) {
            output.innerHTML = '<div class="terminal-line terminal-empty">— Aucun log —</div>';
            return;
        }
        let html = "";
        for (const line of lines) {
            html += `<div class="terminal-line">${this.escapeHtml(line)}</div>`;
        }
        output.innerHTML = html;
        output.scrollTop = output.scrollHeight;
    },

    toggleLogsStream() {
        const toggle = document.getElementById("logs-stream-toggle");
        this.logsStreamMode = toggle ? toggle.checked : false;
        if (this.logsStreamMode) {
            this.startLogsStream();
        } else {
            this.stopLogsStream();
        }
    },

    startLogsStream() {
        this.stopLogsStream();
        const containerId = this.logsContainerId;
        if (!containerId) return;
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${proto}//${window.location.host}/api/containers/${containerId}/logs/stream`;
        try {
            this.logsWs = new WebSocket(wsUrl);
            this.logsWs.onmessage = (event) => {
                const output = document.getElementById("logs-output");
                if (!output) return;
                const lineDiv = document.createElement("div");
                lineDiv.className = "terminal-line";
                lineDiv.textContent = event.data;
                output.appendChild(lineDiv);
                // Keep last 1000 lines
                while (output.children.length > 1000) {
                    output.removeChild(output.firstChild);
                }
                output.scrollTop = output.scrollHeight;
            };
            this.logsWs.onerror = () => {
                this.showToast("Erreur stream logs", "error");
            };
            this.logsWs.onclose = () => {
                this.logsWs = null;
            };
        } catch (e) {
            this.showToast("WebSocket logs: " + e.message, "error");
        }
    },

    stopLogsStream() {
        if (this.logsWs) {
            try { this.logsWs.close(); } catch (e) {}
            this.logsWs = null;
        }
    },

    closeLogs() {
        this.stopLogsStream();
        document.getElementById("logs-modal").classList.add("hidden");
        this.logsContainerId = null;
    },

    // -------------------------------------------------------
    // Console (exec)
    // -------------------------------------------------------

    async openConsole(containerId, name, agent) {
        this.consoleContainerId = containerId;
        this.consoleContainerAgent = agent;
        const title = document.getElementById("console-title");
        if (title) title.textContent = `🖥 Console - ${name}`;
        const modal = document.getElementById("console-modal");
        modal.classList.remove("hidden");
        const output = document.getElementById("console-output");
        output.innerHTML = '<div class="terminal-line terminal-empty">Connexion au container…</div>';

        // Connect WebSocket
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${proto}//${window.location.host}/api/containers/${containerId}/exec`;
        try {
            this.consoleWs = new WebSocket(wsUrl);
            this.consoleWs.onopen = () => {
                output.innerHTML = '<div class="terminal-line terminal-info">— Connecté. Tapez vos commandes. —</div>';
                const input = document.getElementById("console-input");
                if (input) input.focus();
            };
            this.consoleWs.onmessage = (event) => {
                const lineDiv = document.createElement("div");
                lineDiv.className = "terminal-line";
                lineDiv.textContent = event.data;
                output.appendChild(lineDiv);
                output.scrollTop = output.scrollHeight;
            };
            this.consoleWs.onerror = () => {
                this.showToast("Erreur console WebSocket", "error");
            };
        } catch (e) {
            this.showToast("Console: " + e.message, "error");
        }

        // Setup input handler
        const input = document.getElementById("console-input");
        if (input) {
            input.onkeydown = (e) => {
                if (e.key === "Enter") {
                    const cmd = input.value;
                    if (!cmd.trim()) return;
                    // Display the command
                    const cmdDiv = document.createElement("div");
                    cmdDiv.className = "terminal-line terminal-cmd";
                    cmdDiv.textContent = "$ " + cmd;
                    output.appendChild(cmdDiv);
                    // Send to WS
                    if (this.consoleWs && this.consoleWs.readyState === WebSocket.OPEN) {
                        this.consoleWs.send(cmd);
                    }
                    // History
                    this.consoleHistory.push(cmd);
                    input.value = "";
                    output.scrollTop = output.scrollHeight;
                }
            };
        }
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
            const agentBadge = (this.currentAgentFilter === "all" && p.agent_name)
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
    // Compose editor (Phase 3)
    // -------------------------------------------------------

    selectedStack: null,
    stackFiles: [],
    currentFile: null,
    fileContents: {},      // filename -> current editor content
    savedContents: {},     // filename -> last saved content (server)
    editorLoading: false,
    deployTargetStack: null,
    deleteTargetStack: null,
    permsTargetFile: null,

    onStackSelect(name) {
        if (!name) {
            this.selectedStack = null;
            this.renderEditorPlaceholder();
            return;
        }
        this.loadEditor(name);
    },

    selectStackFromDashboard(name) {
        // Called when clicking a stack card in the dashboard
        const selector = document.getElementById("stack-selector");
        if (selector) selector.value = name;
        this.loadEditor(name);
    },

    async loadEditor(name) {
        this.selectedStack = name;
        this.selectedStackAgent = this.stackAgentMap[name] || (this.currentAgentFilter !== "all" ? this.currentAgentFilter : null);
        const agent = this.selectedStackAgent;
        this.editorLoading = true;
        this.currentFile = null;
        this.fileContents = {};
        this.savedContents = {};
        this.renderEditorLoading();

        const filesData = await this.apiFetch("/api/stacks/" + encodeURIComponent(name) + "/files" + this.agentQuery(agent));
        if (!filesData || !filesData.files) {
            this.renderEditorPlaceholder("Impossible de charger les fichiers de la stack.");
            return;
        }
        this.stackFiles = filesData.files;
        if (this.stackFiles.length === 0) {
            this.renderEditorPlaceholder("Aucun fichier dans cette stack.");
            return;
        }
        // Load all file contents
        const agentParam = this.agentQuery(agent);
        for (const f of this.stackFiles) {
            const resp = await fetch("/api/stacks/" + encodeURIComponent(name) + "/files/" + encodeURIComponent(f.name) + agentParam, { credentials: "same-origin" });
            if (resp.ok) {
                const text = await resp.text();
                this.fileContents[f.name] = text;
                this.savedContents[f.name] = text;
            } else {
                this.fileContents[f.name] = "";
                this.savedContents[f.name] = "";
            }
        }
        this.editorLoading = false;
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

    renderEditorLoading() {
        const body = document.getElementById("compose-body");
        if (!body) return;
        body.innerHTML = '<div class="placeholder"><p>⏳ Chargement des fichiers…</p></div>';
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
        let toolbarHtml = '<div class="compose-toolbar">';
        toolbarHtml += '<button class="btn btn-success btn-sm" onclick="DockyApp.saveCurrentFile()"' + (mod ? '' : ' disabled') + '>💾 Sauvegarder</button>';
        toolbarHtml += '<button class="btn btn-info btn-sm" onclick="DockyApp.saveAndDeploy()"' + (anyMod ? '' : ' disabled') + '>🚀 Sauvegarder & Déployer</button>';
        toolbarHtml += '<div class="spacer"></div>';
        toolbarHtml += '<button class="btn btn-sm" onclick="DockyApp.openPermsModal()" title="Permissions du fichier">🔒</button>';
        toolbarHtml += '<button class="btn btn-danger btn-sm" onclick="DockyApp.openDeleteStackModal(\''+ this.escapeHtml(this.selectedStack) +'\')" title="Supprimer la stack">🗑</button>';
        toolbarHtml += '</div>';

        // Editor area
        const content = this.fileContents[this.currentFile] || "";
        let editorHtml = '<div class="code-editor-wrap">';
        editorHtml += '<div class="line-numbers" id="line-numbers"></div>';
        editorHtml += '<textarea class="code-textarea" id="code-editor" spellcheck="false"'
            + ' oninput="DockyApp.onEditorInput()"'
            + ' onscroll="DockyApp.syncLineScroll()"'
            + ' onkeydown="DockyApp.onEditorKeydown(event)"'
            + '>' + this.escapeHtml(content) + '</textarea>';
        editorHtml += '</div>';

        // Status bar
        let statusHtml = '<div class="compose-status">';
        statusHtml += '<span class="status-dot' + (mod ? ' modified' : '') + '"></span>';
        statusHtml += '<span>' + (mod ? 'Modifié (non sauvegardé)' : 'Aucune modification') + '</span>';
        statusHtml += '<span style="margin-left:auto;">' + (this.selectedStackAgent ? '🖥 ' + this.escapeHtml(this.selectedStackAgent) + ' · ' : '') + this.escapeHtml(this.currentFile || '') + ' · ' + content.split("\n").length + ' lignes</span>';
        statusHtml += '</div>';

        body.innerHTML = tabsHtml + toolbarHtml + editorHtml + statusHtml;
        this.updateLineNumbers();
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
        const content = this.fileContents[this.currentFile];
        const agentParam = this.agentQuery(this.selectedStackAgent);
        const resp = await fetch("/api/stacks/" + encodeURIComponent(this.selectedStack) + "/files/" + encodeURIComponent(this.currentFile) + agentParam, {
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
        // Save all modified files
        const stack = this.selectedStack;
        const agent = this.selectedStackAgent;
        const agentParam = this.agentQuery(agent);
        this.showToast("Sauvegarde et déploiement…", "info");
        let allOk = true;
        for (const fname of Object.keys(this.fileContents)) {
            if (this.isModified(fname)) {
                const resp = await fetch("/api/stacks/" + encodeURIComponent(stack) + "/files/" + encodeURIComponent(fname) + agentParam, {
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
        // Deploy
        const result = await this.apiPost("/api/stacks/" + encodeURIComponent(stack) + "/deploy" + agentParam);
        if (result && result.success) {
            this.showToast("Déploiement réussi ✅", "success");
        } else {
            const err = result && result.error ? result.error : "";
            this.showToast("Déploiement échoué : " + err, "error");
        }
        this.updateModifiedIndicators();
        setTimeout(() => this.refreshStacks(), 2500);
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

    async createStack() {
        const name = document.getElementById("new-stack-name").value.trim();
        const compose = document.getElementById("new-stack-compose").value;
        const env = document.getElementById("new-stack-env").value;
        if (!name) {
            this.showToast("Le nom est requis", "error");
            return;
        }
        const agentParam = this.agentQuery(this.currentAgentFilter !== "all" ? this.currentAgentFilter : this.selectedStackAgent);
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
            this.loadEditor(name);
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
        const name = this.deleteTargetStack;
        if (!name) return;
        const agent = this.stackAgentMap[name] || (this.currentAgentFilter !== "all" ? this.currentAgentFilter : null);
        const agentParam = this.agentQuery(agent);
        const resp = await fetch("/api/stacks/" + encodeURIComponent(name) + agentParam, {
            method: "DELETE",
            credentials: "same-origin",
        });
        if (resp.status === 401) { window.location.href = "/login"; return; }
        if (resp.ok) {
            this.closeDeleteStackModal();
            this.showToast("Stack supprimée : " + name, "success");
            if (this.selectedStack === name) {
                this.selectedStack = null;
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
        const agentParam = this.agentQuery(this.selectedStackAgent);
        const resp = await fetch("/api/stacks/" + encodeURIComponent(this.selectedStack) + "/files/" + encodeURIComponent(this.permsTargetFile) + "/permissions" + agentParam, {
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

        // Store in history
        this.chatHistory.push({ role: "user", content: message });

        // Show loading
        this.chatBusy = true;
        this.setChatInputEnabled(false);
        this.showChatLoading(true);

        try {
            const resp = await fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message, history: this.chatHistory }),
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

            // LLM response bubble
            const responseText = data.response || "";
            if (responseText) {
                this.renderChatMessage("assistant", responseText);
                this.chatHistory.push({ role: "assistant", content: responseText });
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
                this.renderChatMessage("system", "✅ Commande exécutée.\nSortie:\n" + (data.output || "(vide)"));
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

    clearChat() {
        this.chatHistory = [];
        const container = document.getElementById("chat-messages");
        if (!container) return;
        container.innerHTML =
            '<div class="chat-welcome" id="chat-welcome">' +
            '<p>Pose une question ou demande une action sur tes containers.</p>' +
            '</div>';
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
    // Init
    // -------------------------------------------------------

    init() {
        this.loadAgents();
        this.startAgentsRefresh();
        this.refreshStacks();
        this.startAutoRefresh();

        // Auto-refresh checkbox
        const cb = document.getElementById("auto-refresh");
        if (cb) {
            cb.addEventListener("change", () => {
                this.autoRefresh = cb.checked;
            });
        }

        // Logs stream toggle
        const streamToggle = document.getElementById("logs-stream-toggle");
        if (streamToggle) {
            streamToggle.addEventListener("change", () => this.toggleLogsStream());
        }

        // Close modals on backdrop click
        const logsModal = document.getElementById("logs-modal");
        if (logsModal) {
            logsModal.addEventListener("click", (e) => {
                if (e.target === logsModal) this.closeLogs();
            });
        }
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
                this.closeLogs();
                this.closeConsole();
                this.closeNewStackModal();
                this.closeDeleteStackModal();
                this.closePermsModal();
                this.closeSoulEditor();
            }
        });
    },
};

// -------------------------------------------------------
// Boot
// -------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
    DockyApp.init();
});