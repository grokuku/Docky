/* ============================================================
   Docky - Frontend JavaScript - FAÇADE (point d'entrée)
   ------------------------------------------------------------
   Refactor app-js (v0.0.4) : ce fichier définit l'objet global
   window.DockyApp (propriétés d'état + init()) et boote au
   DOMContentLoaded.

   Les méthodes métier sont rattachées par les modules chargés
   APRÈS ce fichier et AVANT le chargement de la page (scripts
   classiques synchrones, voir dashboard.html) :
     - api.js, events.js, dashboard.js, editor.js, chat.js, modals.js
   Chaque module : Object.assign(window.DockyApp, { ... }).

   Toutes les méthodes appelées par les templates (onclick=
   "DockyApp.<methode>(...)") et par le HTML généré dynamiquement
   restent sur le même objet -> comportement inchangé.
   ============================================================ */

window.DockyApp = {
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
    _searchQuery: '',        // recherche partielle par nom de container
    _searchDebounceTimer: null,
    _statsCache: {},         // containerId -> { cpu_percent, mem_percent, mem_usage, mem_limit }

    // -------------------------------------------------------
    // init() - bootstrap (extrait de app.js, refactor-app-js)
    // -------------------------------------------------------
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

        // Restaurer la recherche par nom de container
        try {
            const searchSaved = localStorage.getItem('docky_container_search');
            if (searchSaved) this._searchQuery = searchSaved;
        } catch (e) { /* ignore */ }

        // Appliquer les valeurs aux selects / input
        const sortSelect = document.getElementById('sort-select');
        if (sortSelect) sortSelect.value = this._sortMode;
        const groupSelect = document.getElementById('group-select');
        if (groupSelect) groupSelect.value = this._groupMode;
        const searchInput = document.getElementById('container-search');
        if (searchInput) searchInput.value = this._searchQuery;

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
