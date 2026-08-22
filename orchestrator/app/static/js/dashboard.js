/* ============================================================
   Docky - Frontend JavaScript - module dashboard
   ------------------------------------------------------------
   Extrait de app.js (refactor-app-js, v0.0.4). Aucun changement
   de comportement : code déplacé tel quel.

   Sections d'origine : Multi-agent management, Stacks, Grid Dashboard (Option B), View Mode Toggle, Table Dashboard (Option C), Colonnes redimensionnables, Stats / Resources, Actions, Update check, Logs, Console (exec), Ports, Auto-refresh, Panel resizers, Sort & Group

   Ce module rattache des méthodes/propriétés à l'objet global
   window.DockyApp. Il doit être chargé APRÈS app.js (la façade
   qui définit window.DockyApp et boote au DOMContentLoaded) et
   AVANT le chargement de la page (script classique synchrone).
   ============================================================ */

Object.assign(window.DockyApp, {
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
        // Le compteur de containers reflète aussi la recherche par nom
        containers = this._filterContainers(containers);

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
        if (!containers || !Array.isArray(containers)) {
            target.innerHTML = '<div style="color: var(--text-secondary); padding: 12px;">Aucun container ou erreur de chargement</div>';
            return;
        }

        // Filtre de recherche par nom (appliqué à la vue liste aussi)
        containers = this._filterContainers(containers);
        if (containers.length === 0) {
            target.innerHTML = '<div style="color: var(--text-secondary); padding: 12px;">'
                + (this._searchQuery ? 'Aucun container ne correspond à la recherche' : 'Aucun container ou erreur de chargement')
                + '</div>';
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

                // Filtre de recherche par nom (avant le tri)
                containers = this._filterContainers(containers);
                if (containers.length === 0) continue;

                // Trier les containers selon le mode de tri
                containers = this._sortContainers(containers);
                
                const n = containers.length;
                const cols = Math.max(1, Math.ceil(n / 2));
                maxStackCols = Math.max(maxStackCols, cols);
                stackGroups.push({ stack, containers, cols, n, color: this.stackColor(stack.name), groupLabel: group.label });
            }
        }
        
        if (stackGroups.length === 0) {
            container.innerHTML = this._emptyViewMessage();
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
        
        const cellW = cellSize;
        // La hauteur de rangée doit laisser assez de place au contenu de la carte
        // (nom, image, 2 lignes de ressources, badge ports + update, 6 boutons
        // d'action qui peuvent passer sur 2 lignes sur les petites cartes). On
        // garde un minimum raisonnable : les cartes utilisent min-height (hauteur
        // auto) et ne se chevauchent plus sur les lignes suivantes.
        const cellH = Math.max(cellSize, 172);
        
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

                // Filtre de recherche par nom (avant le tri)
                containers = this._filterContainers(containers);
                if (containers.length === 0) continue;

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
            html += this._emptyViewMessage();
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

    _tableColWidthsKey: 'docky_table_col_widths_pct',   // largeurs en % du conteneur
    _legacyTableColWidthsKey: 'docky_table_col_widths', // anciennes valeurs en px (migration)
    _tableColDefaults: { name: 25, image: 20, ports: 16 },  // en % du conteneur
    _tableColMinPx: 70,

    // Calcule la largeur de la zone de contenu de l'en-tête (référence des %).
    // Les largeurs en % des colonnes sont relatives à cette valeur.
    _tableContainerWidth(headerEl) {
        if (!headerEl) return 0;
        const style = getComputedStyle(headerEl);
        return headerEl.clientWidth
            - (parseFloat(style.paddingLeft) || 0)
            - (parseFloat(style.paddingRight) || 0);
    },

    _getTableColWidths() {
        try {
            const raw = localStorage.getItem(this._tableColWidthsKey);
            return raw ? JSON.parse(raw) : {};
        } catch (e) { return {}; }
    },

    // Migre proprement les anciennes largeurs en px (clé historique) vers des
    // % (nouvelle clé) en utilisant la largeur réelle du conteneur au premier
    // rendu. La clé historique est ensuite supprimée.
    _migrateTableColWidths(containerWidth) {
        if (!containerWidth || containerWidth <= 0) return;
        try {
            const legacyRaw = localStorage.getItem(this._legacyTableColWidthsKey);
            if (!legacyRaw) return;
            const legacy = JSON.parse(legacyRaw);
            const migrated = {};
            let changed = false;
            for (const col of Object.keys(legacy)) {
                if (this._tableColDefaults[col] === undefined) continue;
                const px = parseFloat(legacy[col]);
                if (isNaN(px) || px <= 0) continue;
                migrated[col] = Math.round((px / containerWidth) * 1000) / 10;
                changed = true;
            }
            if (changed) {
                localStorage.setItem(this._tableColWidthsKey, JSON.stringify(migrated));
                localStorage.removeItem(this._legacyTableColWidthsKey);
            }
        } catch (e) { /* ignore */ }
    },

    // Applique les largeurs sauvegardées via des variables CSS sur la racine du
    // tableau (héritées par l'en-tête ET les lignes de tous les groupes). Les
    // valeurs sont en % : elles suivent naturellement le redimensionnement de la
    // fenêtre/du panneau. La colonne « ressources » (flex:1) absorbe le reste.
    _applyTableColWidths(root) {
        const containerWidth = this._tableContainerWidth(root.querySelector('.table-col-header'));
        this._migrateTableColWidths(containerWidth);

        const widths = this._getTableColWidths();
        const minPct = containerWidth > 0 ? (this._tableColMinPx / containerWidth) * 100 : 0;
        for (const col of Object.keys(widths)) {
            if (this._tableColDefaults[col] === undefined) continue;
            let pct = parseFloat(widths[col]);
            if (isNaN(pct) || pct <= 0) continue;
            // Les valeurs sauvegardées sont toujours des % du conteneur : on
            // clamps à 100 % à l'application. La migration des anciennes valeurs
            // px (clé legacy) est gérée par _migrateTableColWidths — toute valeur
            // > 100 reste un % légitime et ne doit jamais être réinterprétée.
            if (minPct > 0) pct = Math.max(minPct, pct);
            root.style.setProperty('--col-' + col, Math.min(100, pct) + '%');
        }
    },

    _saveTableColWidth(col, wPct) {
        const widths = this._getTableColWidths();
        widths[col] = wPct;
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

                // Largeur de référence (contenu de l'en-tête) au moment du
                // mousedown : les largeurs en % sont relatives à cette valeur.
                const containerWidth = self._tableContainerWidth(resizer.closest('.table-col-header'));
                if (containerWidth <= 0) return;
                const minPct = (self._tableColMinPx / containerWidth) * 100;

                const startX = e.clientX;
                const currentPct = parseFloat(getComputedStyle(root).getPropertyValue('--col-' + col))
                    || self._tableColDefaults[col] || 0;
                let finalPct = currentPct;

                document.body.style.cursor = 'col-resize';
                document.body.style.userSelect = 'none';
                resizer.classList.add('active');

                const onMove = (ev) => {
                    // Convertit le déplacement (px) en % du conteneur.
                    const currentPx = (currentPct / 100) * containerWidth;
                    const newPx = Math.max(self._tableColMinPx, currentPx + (ev.clientX - startX));
                    finalPct = (newPx / containerWidth) * 100;
                    root.style.setProperty('--col-' + col, finalPct + '%');
                };
                const onUp = () => {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                    document.body.style.cursor = '';
                    document.body.style.userSelect = '';
                    resizer.classList.remove('active');
                    // Clamp à 100 % avant persistance : évite de sauvegarder une
                    // valeur > 100 après un drag large ou un relâchement hors
                    // fenêtre (qui serait réinterprétée comme des px au rendu).
                    finalPct = Math.min(100, finalPct);
                    self._saveTableColWidth(col, finalPct);
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
        const portsBadge = ports ? '<span class="meta-badge meta-ports grid-card-ports" title="' + this.escapeHtml(ports) + '">' + this.icon('cable') + ' ' + this.escapeHtml(ports) + '</span>' : '';

        return '<div class="grid-container-card" data-id="' + escapedId + '" data-stack="' + this.escapeHtml(stackName) + '" data-agent="' + this.escapeHtml(agent || '') + '" style="left:' + left + 'px;top:' + top + 'px;width:' + width + 'px;min-height:' + height + 'px;z-index:3;background-color:' + bgColor + ';border-color:' + borderColor + '"'
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
            if (resp.status === 404) {
                // Défense supplémentaire, pas la cause principale : le backend
                // répond 404 quand le container n'existe plus (par ex. après un
                // update-image réussi qui a recréé le container avec un NOUVEL
                // id). En pratique le badge fantôme est déjà purgé via le chemin
                // 200-with-false + _pruneUpdateCache au re-render ; on ne fait ici
                // que retirer l'entrée du cache et resynchroniser le compteur pour
                // rester cohérent si un vrai 404 arrivait.
                delete this._updateCheckCache[cacheKey];
                const newCount = this._countCachedUpdates();
                if (newCount !== this._updateAvailableCount) {
                    this._updateAvailableCount = newCount;
                    this.updateStatsBar();
                }
                return;
            }
            let data = null;
            try {
                data = await resp.json();
            } catch (e) {
                data = null;
            }
            if (!data || typeof data !== 'object') {
                // Réponse inattendue (HTML d'erreur, etc.) : ne pas planter ni
                // écrire une entrée vide qui masquerait/afflicherait un badge.
                if (renderToken === this._updateCheckToken) {
                    const badge = document.getElementById('update-' + containerId);
                    if (badge) badge.classList.add('hidden');
                }
                return;
            }

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

    // Recherche partielle (substring) insensible à la casse sur le nom.
    // Réagit à l'input (pas seulement au submit) mais débounce le rendu pour
    // éviter de re-fetcher les stats/updates à chaque frappe.
    onSearchInput(value) {
        this._searchQuery = (value || '').trim();
        try {
            localStorage.setItem('docky_container_search', this._searchQuery);
        } catch (e) { /* ignore */ }
        if (this._searchDebounceTimer) clearTimeout(this._searchDebounceTimer);
        this._searchDebounceTimer = setTimeout(() => {
            if (this._allContainersCache && this._allContainersCache.length > 0) {
                this.renderCurrentView();
            }
            this.updateStatsBar();
        }, 150);
    },

    _filterContainers(containers) {
        const q = (this._searchQuery || '').toLowerCase();
        if (!q) return containers;
        return containers.filter(c => (c.name || '').toLowerCase().includes(q));
    },

    // Message vide selon que la recherche est active ou non.
    _emptyViewMessage() {
        return this._searchQuery
            ? '<div class="placeholder"><p>🔍 Aucun container ne correspond à la recherche</p></div>'
            : '<div class="placeholder"><p>🔇 Aucun agent affiché</p><p class="placeholder-hint">Active des agents via les boutons de filtre</p></div>';
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
        // Tri par défaut = ordre alphabétique par nom (insensible à la casse,
        // localeCompare sur le nom complet) AU SEIN de chaque groupe de stack.
        // Appliqué APRÈS le filtrage recherche mais AVANT le rendu des lignes
        // (appelé par renderTableDashboard ET renderGridDashboard).
        // Un tri explicite (select sort-select) reste prioritaire : name-desc,
        // status, cpu-desc, ram-desc.
        if (mode === 'name-asc') {
            return [...containers].sort((a, b) =>
                (a.name || '').localeCompare(b.name || '', undefined, { sensitivity: 'base' }));
        }
        if (mode === 'name-desc') {
            return [...containers].sort((a, b) =>
                (b.name || '').localeCompare(a.name || '', undefined, { sensitivity: 'base' }));
        }
        if (mode === 'status') {
            // Cohérent avec containerStatusBadge : running → partial → stopped.
            const rank = (s) => {
                const st = (s || '').toLowerCase();
                if (st === 'running') return 0;
                if (st === 'restarting' || st === 'paused') return 1;
                if (st === 'exited' || st === 'stopped' || st === 'dead' || st === 'error' || st === 'created') return 2;
                return 3;
            };
            return [...containers].sort((a, b) => rank(a.status) - rank(b.status));
        }
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
});
