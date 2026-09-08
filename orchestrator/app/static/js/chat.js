/* ============================================================
   Docky - Frontend JavaScript - module chat
   ------------------------------------------------------------
   Extrait de app.js (refactor-app-js, v0.0.4). Aucun changement
   de comportement : code déplacé tel quel.

   Sections d'origine : Chat LLM (Phase 4), Chat panel toggle, SOUL.md editor

   Ce module rattache des méthodes/propriétés à l'objet global
   window.DockyApp. Il doit être chargé APRÈS app.js (la façade
   qui définit window.DockyApp et boote au DOMContentLoaded) et
   AVANT le chargement de la page (script classique synchrone).
   ============================================================ */

Object.assign(window.DockyApp, {
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
            // Migré vers DockyFetch (adaptateur HolafFetch) : auth CSRF + 401 →
            // /login gérés par l'adaptateur. Le flux LLM est long (120-180 s) :
            // timeout étendu à 180 s pour ne pas couper la réponse du serveur.
            const data = await window.DockyFetch.request("/api/chat", {
                method: "POST",
                body: { message, history: historyToSend },
                timeout: 180000,
            });

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
            // « LLM non configuré » : le serveur répond 400 {detail: "LLM is not
            // configured…"} — conservé tel quel (message précis existant).
            if (e.status === 400 && e.data && e.data.detail && e.data.detail.toLowerCase().includes("not configured")) {
                this.chatLLMConfigured = false;
                this.setChatInputEnabled(false);
                this.renderChatMessage("system", "LLM non configuré. Va dans Settings pour configurer l'endpoint.");
                return;
            }
            const err = e.status === 0
                ? "Erreur réseau: " + e.message
                : (e.message || ("Erreur " + e.status));
            this.renderChatMessage("error", err);
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

        // Render markdown → safe HTML (XSS-safe, see markdownToHtml)
        bubble.innerHTML = this.markdownToHtml(content);

        wrapper.appendChild(bubble);
        container.appendChild(wrapper);
        this.scrollChatToBottom();
        return wrapper;
    },

    // -------------------------------------------------------
    // Markdown rendering — renderer maison, auto-contenu, XSS-safe.
    // -------------------------------------------------------
    // Stratégie de sécurité :
    //   . La structure en blocs (titres, listes, citations, code…) est détectée
    //     sur le texte brut, mais CHAQUE contenu est ensuite échappé
    //     (<, >, &, ", ') AVANT d'y appliquer les styles markdown inline.
    //     Aucun HTML arbitraire ne peut donc être injecté (XSS).
    //   . On ne génère que des tags/attributs strictement contrôlés
    //     (aucun iframe, aucun attribut on*).
    //   . Les liens sont sanitizés (whitelist de schémas) + target=_blank et
    //     rel=noopener noreferrer.
    //   . Le renderer est pur/stateless : on peut le rappeler à chaque chunk
    //     incrémental d'un flux streaming re-rendu sans perte d'état ni scroll.
    markdownToHtml(md) {
        if (!md) return "";
        const esc = (s) => this.escapeHtml(s);

        // Normalisation des fins de ligne, puis protection des blocs de code
        // ```...``` sur le texte BRUT (leur contenu n'est donc jamais
        // interprété comme du markdown). Chaque bloc devient un placeholder
        // sur une ligne isolée.
        let text = md.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
        const fences = [];
        text = text.replace(/```([^\n`]*)\n?([\s\S]*?)```/g, (m, lang, code) => {
            fences.push({ lang: (lang || "").trim(), code: code.replace(/\n$/, "") });
            return "\n\u0000F" + (fences.length - 1) + "\u0000\n";
        });

        const isBlank   = /^\s*$/;
        const isHr      = /^\s*([-*_])\s*\1\s*\1(?:\s*\1)*\s*$/;
        const isHeading = /^(#{1,6})\s+(.*)$/;
        const isQuote   = /^>\s?(.*)$/;
        const isFence   = /^\u0000F(\d+)\u0000$/;
        const isUl      = /^\s*[-*+]\s+(.*)$/;
        const isOl      = /^\s*\d+[.)]\s+(.*)$/;
        const isTask    = /^\s*[-*+]\s+\[([ xX])\]\s+(.*)$/;

        const lines = text.split("\n");
        const out = [];
        const inline = (t) => this._markdownInline(t);

        const renderFence = (idx) => {
            const f = fences[idx];
            const langAttr = f.lang ? ' class="language-' + esc(f.lang) + '"' : "";
            // Le contenu du bloc est échappé ici → toujours affiché en texte
            // brut, jamais exécuté (= aucun XSS, aucune coloration).
            return '<pre><code' + langAttr + '>' + esc(f.code) + '</code></pre>';
        };

        let i = 0;
        while (i < lines.length) {
            const line = lines[i];
            if (isBlank.test(line)) { i++; continue; }

            // Bloc de code (placeholder protégé)
            const fp = line.match(isFence);
            if (fp) { out.push(renderFence(+fp[1])); i++; continue; }

            // Ligne horizontale
            if (isHr.test(line)) { out.push('<hr>'); i++; continue; }

            // Titre (h1..h6)
            const h = line.match(isHeading);
            if (h) {
                const lvl = h[1].length;
                out.push('<h' + lvl + '>' + inline(h[2]) + '</h' + lvl + '>');
                i++;
                continue;
            }

            // Citation (lignes ">" consécutives)
            if (isQuote.test(line)) {
                const q = [];
                while (i < lines.length && isQuote.test(lines[i])) {
                    q.push(lines[i].match(isQuote)[1]);
                    i++;
                }
                // Chaque ligne est rendue inline séparément, puis reliée par un
                // vrai <br> (que l'échappement inline ne dégrade pas).
                out.push('<blockquote>' + q.map(x => inline(x)).join("<br>") + '</blockquote>');
                continue;
            }

            // Liste de tâches
            if (isTask.test(line)) {
                const items = [];
                while (i < lines.length && isTask.test(lines[i])) {
                    const m = lines[i].match(isTask);
                    const checked = (m[1] === "x" || m[1] === "X");
                    items.push('<li class="task-item"><input type="checkbox"' +
                        (checked ? " checked" : "") + ' disabled> ' + inline(m[2]) + '</li>');
                    i++;
                }
                out.push('<ul class="task-list">' + items.join("") + '</ul>');
                continue;
            }

            // Liste à puces
            if (isUl.test(line)) {
                const items = [];
                while (i < lines.length && isUl.test(lines[i])) {
                    items.push('<li>' + inline(lines[i].match(isUl)[1]) + '</li>');
                    i++;
                }
                out.push('<ul>' + items.join("") + '</ul>');
                continue;
            }

            // Liste ordonnée
            if (isOl.test(line)) {
                const items = [];
                while (i < lines.length && isOl.test(lines[i])) {
                    items.push('<li>' + inline(lines[i].match(isOl)[1]) + '</li>');
                    i++;
                }
                out.push('<ol>' + items.join("") + '</ol>');
                continue;
            }

            // Paragraphe (sauts de ligne conservés)
            const para = [];
            while (i < lines.length &&
                   !isBlank.test(lines[i]) &&
                   !isHr.test(lines[i]) &&
                   !isHeading.test(lines[i]) &&
                   !isQuote.test(lines[i]) &&
                   !isUl.test(lines[i]) &&
                   !isOl.test(lines[i]) &&
                   !isTask.test(lines[i]) &&
                   !isFence.test(lines[i])) {
                para.push(lines[i]);
                i++;
            }
            if (para.length) {
                // Le <br> de liaison est ajouté APRÈS le rendu inline de chaque
                // ligne pour ne pas être échappé en texte littéral.
                out.push('<p>' + para.map(x => inline(x)).join("<br>") + '</p>');
            }
        }

        return out.join("\n");
    },

    // Point d'entrée inline : on échappe TOUT le HTML (anti-XSS) puis on
    // applique les styles markdown. Le texte renvoyé ne peut contenir que des
    // tags contrôlés.
    _markdownInline(text) {
        if (!text) return "";
        return this._markdownInlineEscaped(this.escapeHtml(text));
    },

    // Rendu inline sur du texte déjà échappé : code `...`, liens [t](u),
    // **gras**, *italique*, _italique_, ~~barré~~.
    _markdownInlineEscaped(text) {
        const codes = [];
        // Protéger le code inline d'abord (ses contenus ne sont pas reformatés)
        let r = text.replace(/`([^`]+)`/g, (m, c) => {
            codes.push(c);
            return "\u0000C" + (codes.length - 1) + "\u0000";
        });

        // Liens [texte](url) — href sanitizé, target=_blank + rel noopener
        r = r.replace(/\[([^\]]+)\]\(([^\s)]+)\)/g, (m, label, url) => {
            const href = this._safeMarkdownLink(url);
            return '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' +
                this._markdownInlineEscaped(label) + '</a>';
        });

        // **gras** (avant italique)
        r = r.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        // *italique*
        r = r.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, (m, p1, p2) => p1 + '<em>' + p2 + '</em>');
        // _italique_
        r = r.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, (m, p1, p2) => p1 + '<em>' + p2 + '</em>');
        // ~~barré~~
        r = r.replace(/~~([^~]+)~~/g, '<del>$1</del>');

        // Restaurer le code inline
        r = r.replace(/\u0000C(\d+)\u0000/g, (m, i) => '<code>' + codes[+i] + '</code>');
        return r;
    },

    // Whitelist des schémas de liens : tout schéma exécutable (javascript:,
    // vbscript:, data:, file:) est neutralisé (→ "#"). Le reste (http, https,
    // mailto, ftp, tel, ancre, relatif) est conservé tel quel.
    _safeMarkdownLink(url) {
        const u = (url || "").trim();
        if (/^(javascript|vbscript|data|file):/i.test(u)) return "#";
        return u;
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
            const data = await window.DockyFetch.request("/api/chat/validate-exec", {
                method: "POST",
                body: { container_id: containerId, command: command },
            });
            if (data.success) {
                this.renderChatMessage("system", this.icon('check') + " Commande exécutée.\nSortie:\n" + (data.output || "(vide)"));
            } else {
                this.renderChatMessage("error", "Échec de l'exécution: " + (data.detail || data.output || "erreur inconnue"));
            }
        } catch (e) {
            if (e.status === 0) {
                this.renderChatMessage("error", "Erreur réseau: " + e.message);
            } else {
                this.renderChatMessage("error", "Échec de l'exécution: " + (e.message || (e.data && e.data.detail) || "erreur inconnue"));
            }
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
            const data = await window.DockyFetch.request("/api/chat/validate-exec?agent=" + encodeURIComponent(agentName), {
                method: "POST",
                body: { type: "clean" },
            });
            if (data.success) {
                this.renderChatMessage("system", this.icon('check') + " Nettoyage effectué.\nSortie:\n" + (data.output || "(vide)"));
            } else {
                this.renderChatMessage("error", "Échec du nettoyage: " + (data.detail || data.output || "erreur inconnue"));
            }
        } catch (e) {
            if (e.status === 0) {
                this.renderChatMessage("error", "Erreur réseau: " + e.message);
            } else {
                this.renderChatMessage("error", "Échec du nettoyage: " + (e.message || (e.data && e.data.detail) || "erreur inconnue"));
            }
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
        // Fenêtre n°4 migrée vers HolafModal : remplace l'ancienne modale
        // statique #soul-modal (textarea SOUL.md + sauvegarde). Taille
        // confortable (lg + largeur 720px) car l'existant était large
        // (modal-soul 720px). ID #soul-editor conservé (saveSoul() le lit).
        const content = '<p class="form-hint" style="margin-bottom:10px;">Instructions de personnalité/contexte du LLM. Ce contenu est injecté dans le system prompt.</p>'
            + '<textarea id="soul-editor" class="modal-textarea" rows="18" spellcheck="false"></textarea>';

        HolafModal.open({
            title: "📄 SOUL.md",
            content: content,
            size: "lg",
            width: 720,
            buttons: [
                { text: "Annuler", value: false, type: "cancel" },
                { text: "Sauvegarder", value: true, type: "primary", onClick: () => this.saveSoul() },
            ],
            onOpen: () => {
                const textarea = document.getElementById("soul-editor");
                if (textarea) {
                    textarea.value = "Chargement…";
                    textarea.disabled = true;
                }
            },
        });

        const data = await this.apiFetch("/api/soul");
        if (data === null) {
            const textarea = document.getElementById("soul-editor");
            if (textarea) textarea.value = "";
            return;
        }
        const textarea = document.getElementById("soul-editor");
        if (textarea) {
            textarea.value = data.content || "";
            textarea.disabled = false;
        }
    },

    closeSoulEditor() {
        // La fermeture est gérée par HolafModal (boutons / Échap). Stub conservé
        // pour préserver l'API existante (Échap global dans app.js).
    },

    async saveSoul() {
        const textarea = document.getElementById("soul-editor");
        if (!textarea) return;
        const content = textarea.value;
        try {
            // PUT corps brut (text/plain) : la brique ne sérialise pas une
            // string en JSON (isRawBody) — on garde le Content-Type text/plain.
            const data = await window.DockyFetch.request("/api/soul", {
                method: "PUT",
                headers: { "Content-Type": "text/plain" },
                body: content,
            });
            if (data.success !== false) {
                this.showToast("SOUL.md sauvegardé", "success");
                this.closeSoulEditor();
            } else {
                this.showToast("Erreur sauvegarde SOUL.md", "error");
            }
        } catch (e) {
            this.showToast("Erreur: " + (e.message || (e.data && e.data.detail) || "erreur"), "error");
        }
    },
});
