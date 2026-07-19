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
    chatVisible: true,      // whether the chat panel is shown (persisted in localStorage)

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
        
        // Build stackAgentMap
        this.stackAgentMap = {};
        for (const s of data) {
            if (s.agent_name) this.stackAgentMap[s.name] = s.agent_name;
            else if (this.currentAgentFilter !== "all") this.stackAgentMap[s.name] = this.currentAgentFilter;
        }
        
        // Fetch all containers
        try {
            let containersUrl;
            if (this.currentAgentFilter === 'all') {
                containersUrl = '/api/containers?agent=all';
            } else {
                containersUrl = '/api/containers?agent=' + encodeURIComponent(this.currentAgentFilter);
            }
            const resp = await fetch(containersUrl, { credentials: "same-origin" });
            if (resp.status === 401) { window.location.href = "/login"; return; }
            const containersData = await resp.json();
            this._allContainersCache = Array.isArray(containersData) ? containersData : [];
        } catch(e) {
            this._allContainersCache = [];
        }
        
        // Skip re-render if nothing changed
        const gridKey = JSON.stringify(data) + '|' + JSON.stringify(this._allContainersCache);
        if (this._lastGridKey === gridKey) return;
        this._lastGridKey = gridKey;
        
        this.renderGridDashboard();
        this.updateStackSelector(data);
    },

    updateStackSelector(stacks) {
        const selector = document.getElementById("stack-selector");
        if (!selector) return;
        const current = selector.value;
        selector.innerHTML = '<option value="">— Sélectionner une stack —</option>';
        stacks.forEach((s) => {
            // Only managed stacks are editable; skip external and standalone
            if (s.managed === false) return;
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
            // Managed / external / standalone indicator
            const isManaged = stack.managed !== false;
            const isStandalone = stack.standalone === true;
            let typeBadge = '';
            if (isStandalone) {
                typeBadge = '<span class="stack-type-badge stack-badge-standalone">standalone</span>';
            } else if (!isManaged) {
                typeBadge = '<span class="stack-type-badge stack-badge-external">⚠ Externe</span>';
            } else {
                typeBadge = '<span class="stack-type-badge stack-badge-docky">Docky</span>';
            }
            // Edit button only for managed stacks (files are editable)
            const editBtn = isManaged
                ? '<button class="icon-btn" title="Éditer" onclick="DockyApp.selectStackFromDashboard(\'' + this.escapeHtml(stack.name) + '\')">📝</button>'
                : '';
            // One-click import button for external stacks (not standalone)
            const importBtn = (!isManaged && !isStandalone)
                ? '<button class="icon-btn" title="Importer dans Docky" onclick="DockyApp.importExternal(\'' + this.escapeHtml(stack.source_path || '') + '\', \'' + this.escapeHtml(stack.name) + '\')">📥</button>'
                : '';
            // Stack-level start/stop/restart only for real stacks (not standalone)
            const stackActionBtns = isStandalone
                ? ''
                : '<button class="icon-btn btn-start" title="Démarrer" onclick="DockyApp.stackAction(\'' + this.escapeHtml(stack.name) + '\', \'start\')">▶</button>'
                  + '<button class="icon-btn btn-stop" title="Arrêter" onclick="DockyApp.stackAction(\'' + this.escapeHtml(stack.name) + '\', \'stop\')">⏹</button>'
                  + '<button class="icon-btn btn-restart" title="Redémarrer" onclick="DockyApp.stackAction(\'' + this.escapeHtml(stack.name) + '\', \'restart\')">🔄</button>'
                  + '<button class="icon-btn" title="Update" onclick="DockyApp.stackAction(\'' + this.escapeHtml(stack.name) + '\', \'update\')">⬆</button>';

            html += `
                <div class="stack-card ${isExpanded ? "expanded" : ""}" data-stack="${this.escapeHtml(stack.name)}">
                    <div class="stack-card-header" onclick="DockyApp.toggleStack('${this.escapeHtml(stack.name)}')">
                        <div class="stack-card-info">
                            <span class="stack-name">${this.escapeHtml(stack.name)}</span>
                            ${typeBadge}
                            ${agentBadge}
                            ${statusBadge}
                        </div>
                        <div class="stack-card-meta">
                            <span class="meta-badge">🐳 ${containerInfo}</span>
                            ${portsInfo ? `<span class="meta-badge meta-ports">🔌 ${this.escapeHtml(portsInfo)}</span>` : ""}
                        </div>
                        <div class="stack-card-actions" onclick="event.stopPropagation()">
                            ${editBtn}
                            ${importBtn}
                            ${stackActionBtns}
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

    loadContainers(stackName) {
        const target = document.getElementById("containers-" + stackName);
        if (!target) return;
        const agent = this.stackAgentMap[stackName] || (this.currentAgentFilter !== "all" ? this.currentAgentFilter : null);
        this.expandedStackAgent = agent;
        // Display instantly from the pre-loaded cache (no API call)
        const containers = (this._allContainersCache || []).filter(c => {
            if (stackName === 'Standalone') return !c.stack;
            return c.stack === stackName;
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
    // Grid Dashboard (Option B)
    // -------------------------------------------------------

    renderGridDashboard() {
        const container = document.getElementById("dashboard-content");
        if (!container) return;
        
        if (this.stacks.length === 0) {
            container.innerHTML = '<div class="placeholder"><p>📭 Aucune stack trouvée</p></div>';
            return;
        }
        
        const availWidth = container.clientWidth - 36;
        if (availWidth < 200) return;
        
        const gap = 8;
        const minCell = 145;
        const maxCell = 200;
        let cols = Math.max(3, Math.min(10, Math.floor(availWidth / minCell)));
        let cellSize = Math.floor((availWidth - (cols - 1) * gap) / cols);
        cellSize = Math.max(140, Math.min(maxCell, cellSize));
        cols = Math.max(3, Math.floor((availWidth + gap) / (cellSize + gap)));
        
        const layout = this.computeGridLayout(this.stacks, this._allContainersCache || [], cols);
        this._gridLayout = layout;
        this._gridCellSize = cellSize;
        
        const cellW = cellSize, cellH = cellSize;
        const padX = 14, padY = 10, headerH = 32;
        const padTop = padY + headerH + 8;
        
        const canvasW = layout.cols * (cellW + gap) - gap + 2 * padX;
        const canvasH = layout.rows * (cellH + gap) - gap + padTop + padY;
        
        let svgPaths = '', headersHtml = '', cardsHtml = '';
        const runningContainers = [];
        
        for (const placement of layout.placements) {
            const { stack, containers, bbox } = placement;
            const color = this.stackColor(stack.name);
            const seed = this.hashString(stack.name);
            
            const bx = bbox.col * (cellW + gap);
            const by = bbox.row * (cellH + gap);
            const bw = bbox.colSpan * (cellW + gap) - gap + 2 * padX;
            const bh = bbox.rowSpan * (cellH + gap) - gap + padTop + padY;
            
            const path = this.generateOrganicPath(bx, by, bw, bh, seed);
            svgPaths += '<path d="' + path + '" fill="' + color.fill + '" stroke="' + color.stroke + '" stroke-width="1.5" opacity="0.9"/>';
            
            const hLeft = bx + padX, hTop = by + 8, hWidth = bw - 2 * padX;
            headersHtml += this.renderGridStackHeader(stack, hLeft, hTop, hWidth);
            
            const agent = this.stackAgentMap[stack.name] || (this.currentAgentFilter !== "all" ? this.currentAgentFilter : null);
            for (const cell of containers) {
                const cLeft = cell.col * (cellW + gap) + padX;
                const cTop = cell.row * (cellH + gap) + padTop;
                cardsHtml += this.renderGridContainerCard(cell.container, cLeft, cTop, cellW, cellH, agent);
                if (cell.container && cell.container.status === "running") runningContainers.push({ id: cell.container.id, agent });
            }
        }
        
        container.innerHTML = '<div class="docky-grid-canvas" style="position:relative;width:' + canvasW + 'px;height:' + canvasH + 'px;">'
            + '<svg class="docky-bubbles-svg" width="' + canvasW + '" height="' + canvasH + '" style="position:absolute;top:0;left:0;pointer-events:none;z-index:1;">' + svgPaths + '</svg>'
            + headersHtml + cardsHtml + '</div>';
        
        for (const rc of runningContainers) {
            this.loadContainerStats(rc.id, rc.agent);
            this.checkUpdate(rc.id, rc.agent);
        }
    },

    computeGridLayout(stacks, allContainers, maxCols) {
        const groups = [];
        for (const stack of stacks) {
            const containers = allContainers.filter(c => {
                if (stack.name === 'Standalone') return !c.stack;
                return c.stack === stack.name;
            });
            groups.push({ stack, containers });
        }
        groups.sort((a, b) => b.containers.length - a.containers.length);
        
        const grid = new Set();
        const placements = [];
        let maxCol = 0, maxRow = 0;
        
        for (const group of groups) {
            const n = group.containers.length;
            let w, h;
            if (n === 0) { w = 1; h = 1; }
            else {
                w = Math.ceil(Math.sqrt(n));
                h = Math.ceil(n / w);
                if (w > maxCols) { w = maxCols; h = Math.ceil(n / w); }
            }
            
            const pos = this.findGridPlacement(grid, w, h, maxCols);
            if (!pos) continue;
            
            const cells = [];
            let placed = 0;
            for (let r = pos.row; r < pos.row + h && placed < n; r++) {
                for (let c = pos.col; c < pos.col + w && placed < n; c++) {
                    cells.push({ col: c, row: r, container: group.containers[placed] });
                    placed++;
                }
            }
            if (n === 0) cells.push({ col: pos.col, row: pos.row, container: null });
            
            for (let r = pos.row; r < pos.row + h; r++) {
                for (let c = pos.col; c < pos.col + w; c++) grid.add(c + "," + r);
                grid.add((pos.col + w) + "," + r);
            }
            for (let c = pos.col; c <= pos.col + w; c++) grid.add(c + "," + (pos.row + h));
            
            placements.push({ stack: group.stack, containers: cells, bbox: { col: pos.col, row: pos.row, colSpan: w, rowSpan: h } });
            maxCol = Math.max(maxCol, pos.col + w);
            maxRow = Math.max(maxRow, pos.row + h);
        }
        return { placements, cols: maxCol, rows: maxRow };
    },

    findGridPlacement(grid, w, h, maxCols) {
        for (let row = 0; row < 500; row++) {
            for (let col = 0; col <= maxCols - w; col++) {
                if (this._rectFits(grid, col, row, w, h)) return { col, row };
            }
        }
        let maxR = 0;
        for (const key of grid) { const r = parseInt(key.split(",")[1]); if (r > maxR) maxR = r; }
        return { col: 0, row: maxR + 1 };
    },

    _rectFits(grid, col, row, w, h) {
        for (let r = row; r < row + h; r++)
            for (let c = col; c < col + w; c++)
                if (grid.has(c + "," + r)) return false;
        return true;
    },

    generateOrganicPath(x, y, w, h, seed) {
        const rnd = (i) => { const s = Math.sin(seed * 9301 + i * 49297) * 233280; return (s - Math.floor(s)) - 0.5; };
        const wobble = Math.min(w, h) * 0.02;
        const margin = Math.min(w, h) * 0.14;
        const pts = [
            { x: x + margin, y: y + rnd(0) * wobble },
            { x: x + w * 0.33, y: y + rnd(1) * wobble },
            { x: x + w * 0.67, y: y + rnd(2) * wobble },
            { x: x + w - margin, y: y + rnd(3) * wobble },
            { x: x + w + rnd(4) * wobble, y: y + h * 0.33 },
            { x: x + w + rnd(5) * wobble, y: y + h * 0.67 },
            { x: x + w - margin, y: y + h + rnd(6) * wobble },
            { x: x + w * 0.67, y: y + h + rnd(7) * wobble },
            { x: x + w * 0.33, y: y + h + rnd(8) * wobble },
            { x: x + margin, y: y + h + rnd(9) * wobble },
            { x: x + rnd(10) * wobble, y: y + h * 0.67 },
            { x: x + rnd(11) * wobble, y: y + h * 0.33 },
        ];
        let path = 'M ' + pts[0].x.toFixed(1) + ' ' + pts[0].y.toFixed(1);
        const n = pts.length;
        for (let i = 0; i < n; i++) {
            const p0 = pts[(i-1+n)%n], p1 = pts[i], p2 = pts[(i+1)%n], p3 = pts[(i+2)%n];
            const c1x = p1.x + (p2.x - p0.x) / 6, c1y = p1.y + (p2.y - p0.y) / 6;
            const c2x = p2.x - (p3.x - p1.x) / 6, c2y = p2.y - (p3.y - p1.y) / 6;
            path += ' C ' + c1x.toFixed(1) + ' ' + c1y.toFixed(1) + ', ' + c2x.toFixed(1) + ' ' + c2y.toFixed(1) + ', ' + p2.x.toFixed(1) + ' ' + p2.y.toFixed(1);
        }
        return path + ' Z';
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

    renderGridStackHeader(stack, left, top, width) {
        const isManaged = stack.managed !== false;
        const isStandalone = stack.standalone === true;
        let typeBadge = '';
        if (isStandalone) typeBadge = '<span class="stack-type-badge stack-badge-standalone">standalone</span>';
        else if (!isManaged) typeBadge = '<span class="stack-type-badge stack-badge-external">⚠ Externe</span>';
        else typeBadge = '<span class="stack-type-badge stack-badge-docky">Docky</span>';
        
        const agentBadge = (this.currentAgentFilter === "all" && stack.agent_name) ? '<span class="stack-agent-badge">🖥 ' + this.escapeHtml(stack.agent_name) + '</span>' : '';
        const statusBadge = this.statusBadge(stack.status);
        const containerInfo = stack.container_count > 0 ? stack.running_count + '/' + stack.container_count : "0";
        const countBadge = '<span class="meta-badge">🐳 ' + containerInfo + '</span>';
        const portsInfo = (stack.ports && stack.ports.length > 0) ? '<span class="meta-badge meta-ports">🔌 ' + this.escapeHtml(stack.ports.slice(0,3).join(", ")) + (stack.ports.length > 3 ? '…' : '') + '</span>' : '';
        
        const escapedName = this.escapeHtml(stack.name);
        const editBtn = isManaged ? '<button class="grid-icon-btn" title="Éditer" onclick="DockyApp.selectStackFromDashboard(\'' + escapedName + '\')">📝</button>' : '';
        const importBtn = (!isManaged && !isStandalone && stack.source_path) ? '<button class="grid-icon-btn" title="Importer" onclick="DockyApp.importExternal(\'' + this.escapeHtml(stack.source_path) + '\', \'' + escapedName + '\')">📥</button>' : '';
        const stackActionBtns = isStandalone ? '' : '<button class="grid-icon-btn btn-start" title="Démarrer" onclick="DockyApp.stackAction(\'' + escapedName + '\', \'start\')">▶</button><button class="grid-icon-btn btn-stop" title="Arrêter" onclick="DockyApp.stackAction(\'' + escapedName + '\', \'stop\')">⏹</button><button class="grid-icon-btn btn-restart" title="Redémarrer" onclick="DockyApp.stackAction(\'' + escapedName + '\', \'restart\')">🔄</button><button class="grid-icon-btn" title="Update" onclick="DockyApp.stackAction(\'' + escapedName + '\', \'update\')">⬆</button>';
        
        return '<div class="grid-stack-header" style="left:' + left + 'px;top:' + top + 'px;width:' + width + 'px;z-index:2"><div class="grid-stack-header-info"><span class="grid-stack-name">' + escapedName + '</span>' + typeBadge + agentBadge + statusBadge + countBadge + portsInfo + '</div><div class="grid-stack-header-actions">' + editBtn + importBtn + stackActionBtns + '</div></div>';
    },

    renderGridContainerCard(c, left, top, width, height, agent) {
        if (!c) return '<div class="grid-container-card grid-card-empty" style="left:' + left + 'px;top:' + top + 'px;width:' + width + 'px;height:' + height + 'px;z-index:3"></div>';
        const escapedId = this.escapeHtml(c.id), name = this.escapeHtml(c.name), image = this.escapeHtml(c.image);
        const statusDot = this.containerStatusDot(c.status);
        const agt = (agent || "").replace(/'/g, "\\'");
        const ports = (c.ports || []).filter(p => p.host_port).map(p => p.host_port + '→' + p.container).join(", ");
        const portsBadge = ports ? '<span class="meta-badge meta-ports grid-card-ports">🔌 ' + this.escapeHtml(ports) + '</span>' : '';
        
        return '<div class="grid-container-card" data-id="' + escapedId + '" style="left:' + left + 'px;top:' + top + 'px;width:' + width + 'px;height:' + height + 'px;z-index:3"><div class="grid-card-top"><span class="grid-card-name" title="' + name + '">' + name + '</span>' + statusDot + '</div><div class="grid-card-image" title="' + image + '">📦 ' + image + '</div><div class="grid-card-resources" id="resources-' + escapedId + '"><div class="resource-line"><span class="resource-label">CPU</span><div class="progress-bar"><div class="progress-fill" style="width:0%"></div></div><span class="resource-value">—</span></div><div class="resource-line"><span class="resource-label">RAM</span><div class="progress-bar"><div class="progress-fill ram" style="width:0%"></div></div><span class="resource-value">—</span></div></div><div class="grid-card-extra">' + portsBadge + '<span class="update-badge hidden" id="update-' + escapedId + '">⬆</span></div><div class="grid-card-actions"><button class="grid-icon-btn btn-start" title="Start" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'start\', \'' + agt + '\')">▶</button><button class="grid-icon-btn btn-stop" title="Stop" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'stop\', \'' + agt + '\')">⏹</button><button class="grid-icon-btn btn-restart" title="Restart" onclick="DockyApp.containerAction(\'' + escapedId + '\', \'restart\', \'' + agt + '\')">🔄</button><button class="grid-icon-btn btn-logs" title="Logs" onclick="DockyApp.openLogs(\'' + escapedId + '\', \'' + name + '\', \'' + agt + '\')">📋</button><button class="grid-icon-btn btn-console" title="Console" onclick="DockyApp.openConsole(\'' + escapedId + '\', \'' + name + '\', \'' + agt + '\')">🖥</button></div></div>';
    },

    _debouncedGridRender() {
        if (this._gridRenderTimer) clearTimeout(this._gridRenderTimer);
        this._gridRenderTimer = setTimeout(() => { if (this.stacks.length > 0) this.renderGridDashboard(); }, 200);
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
        // Open logs in a popup window so the user can keep it on another screen
        const url = `/popup/logs?agent=${encodeURIComponent(agent || '')}&container=${encodeURIComponent(containerId)}&name=${encodeURIComponent(name || '')}`;
        window.open(url, `logs-${containerId}`, 'width=900,height=650,scrollbars=yes,resizable=yes');
        // Also keep the legacy modal available via a state flag for backwards compat
        this.logsContainerId = containerId;
        this.logsContainerAgent = agent;
        this.logsStreamMode = false;
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

        // External / standalone stacks cannot be edited (files are not in /data/stacks/)
        const stackInfo = this.stacks.find((s) => s.name === name);
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
            return;
        }

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
        setTimeout(() => {
            if (src) src.focus();
        }, 50);
    },

    closeImportModal() {
        const modal = document.getElementById("import-modal");
        if (modal) modal.classList.add("hidden");
    },

    importExternal(sourcePath, stackName) {
        if (!sourcePath) {
            this.showToast('Chemin source non détecté pour cette stack', "error");
            return;
        }
        // Dry-run first to get a preview, then show a modal before the
        // actual import.
        this._importPreview = null;
        this._doImportPreview(sourcePath, stackName);
    },

    async _doImportPreview(sourcePath, stackName) {
        const agent = this.stackAgentMap[stackName] || (this.currentAgentFilter !== 'all' ? this.currentAgentFilter : null);
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

    async doImportDirect(sourcePath, stackName) {
        const agent = this.stackAgentMap[stackName] || (this.currentAgentFilter !== 'all' ? this.currentAgentFilter : null);
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
        const agent = this.currentAgentFilter !== "all" ? this.currentAgentFilter : null;

        if (!sourcePath) {
            this.showToast("Le chemin source est requis", "error");
            return;
        }
        if (!agent) {
            this.showToast("Sélectionne un agent spécifique (pas « Tous »)", "error");
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

            // Now that the exchange succeeded, add the user message to the
            // local history (it was excluded from the request payload to
            // avoid duplication with the backend).
            this.chatHistory.push({ role: "user", content: message });

            // LLM response bubble
            const responseText = data.response || "";
            if (responseText || (data.tool_calls && data.tool_calls.length > 0)) {
                this.renderChatMessage("assistant", responseText || "");

                // Include tool calls in the content saved to history so the
                // LLM sees what actions were taken in previous turns.
                let historyContent = responseText || "";
                if (data.tool_calls && data.tool_calls.length > 0) {
                    const toolSummary = data.tool_calls.map(tc =>
                        `[Action: ${tc.name}]`
                    ).join(" ");
                    historyContent = (historyContent + "\n" + toolSummary).trim();
                }
                this.chatHistory.push({ role: "assistant", content: historyContent });
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
                this.renderChatMessage("system", "✅ Nettoyage effectué.\nSortie:\n" + (data.output || "(vide)"));
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
    // Init
    // -------------------------------------------------------

    init() {
        // Load chat panel visibility preference (persisted in localStorage)
        try {
            this.chatVisible = localStorage.getItem('docky-chat-visible') !== '0';
        } catch (e) {
            this.chatVisible = true;
        }
        this.applyChatVisibility();

        this.initResizers();

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

        // Grid dashboard resize observer
        const dashContent = document.getElementById("dashboard-content");
        if (dashContent && window.ResizeObserver) {
            this._gridResizeObserver = new ResizeObserver(() => { this._debouncedGridRender(); });
            this._gridResizeObserver.observe(dashContent);
        }
    },
};

// -------------------------------------------------------
// Boot
// -------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
    DockyApp.init();
});