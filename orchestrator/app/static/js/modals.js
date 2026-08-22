/* ============================================================
   Docky - Frontend JavaScript - module modals
   ------------------------------------------------------------
   Extrait de app.js (refactor-app-js, v0.0.4). Aucun changement
   de comportement : code déplacé tel quel.

   Sections d'origine : Activity modal, Container Edit Modal

   Ce module rattache des méthodes/propriétés à l'objet global
   window.DockyApp. Il doit être chargé APRÈS app.js (la façade
   qui définit window.DockyApp et boote au DOMContentLoaded) et
   AVANT le chargement de la page (script classique synchrone).
   ============================================================ */

Object.assign(window.DockyApp, {
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
});
