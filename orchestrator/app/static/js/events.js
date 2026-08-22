/* ============================================================
   Docky - Frontend JavaScript - module events
   ------------------------------------------------------------
   Extrait de app.js (refactor-app-js, v0.0.4). Aucun changement
   de comportement : code déplacé tel quel.

   Sections d'origine : Events WebSocket + Heartbeat

   Ce module rattache des méthodes/propriétés à l'objet global
   window.DockyApp. Il doit être chargé APRÈS app.js (la façade
   qui définit window.DockyApp et boote au DOMContentLoaded) et
   AVANT le chargement de la page (script classique synchrone).
   ============================================================ */

Object.assign(window.DockyApp, {
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
});
