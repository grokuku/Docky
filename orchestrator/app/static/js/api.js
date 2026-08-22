/* ============================================================
   Docky - Frontend JavaScript - module api
   ------------------------------------------------------------
   Extrait de app.js (refactor-app-js, v0.0.4). Aucun changement
   de comportement : code déplacé tel quel.

   Sections d'origine : Utilities

   Ce module rattache des méthodes/propriétés à l'objet global
   window.DockyApp. Il doit être chargé APRÈS app.js (la façade
   qui définit window.DockyApp et boote au DOMContentLoaded) et
   AVANT le chargement de la page (script classique synchrone).
   ============================================================ */

Object.assign(window.DockyApp, {
    // -------------------------------------------------------
    // CSRF (double-submit cookie) — voir docs/csrf-protection.md
    // -------------------------------------------------------

    /**
     * Lecture brute d'un cookie (parser simple, tolerant aux espaces).
     * Retourne null si absent ou si document est indisponible.
     */
    getCookie(name) {
        if (typeof document === "undefined" || !document.cookie) return null;
        const parts = document.cookie.split(/;\s*/);
        for (let i = 0; i < parts.length; i++) {
            const eq = parts[i].indexOf("=");
            if (eq === -1) continue;
            if (parts[i].slice(0, eq) === name) {
                const raw = parts[i].slice(eq + 1);
                try { return decodeURIComponent(raw); } catch (e) { return raw; }
            }
        }
        return null;
    },

    /** Token CSRF courant (cookie csrf_token posé par le serveur). */
    csrfToken() {
        return this.getCookie("csrf_token");
    },

    // -------------------------------------------------------
    // Utilities
    // -------------------------------------------------------

    async apiFetch(url, options = {}) {
        const method = (options.method || "GET").toUpperCase();
        const isSafeMethod = method === "GET" || method === "HEAD" || method === "OPTIONS";

        const doFetch = async () => {
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
        };

        try {
            return await doFetch();
        } catch (e) {
            // Retry unique, uniquement pour les méthodes sûres (GET/HEAD/OPTIONS),
            // et seulement sur une erreur réseau (TypeError), jamais sur AbortError
            // ni sur une réponse HTTP d'erreur (4xx/5xx).
            const isNetworkError = e instanceof TypeError && e.name !== "AbortError";
            if (isSafeMethod && isNetworkError) {
                console.warn("apiFetch: network error on " + method + ", retrying once in 500ms:", e.message);
                await new Promise(resolve => setTimeout(resolve, 500));
                try {
                    const data = await doFetch();
                    console.warn("apiFetch: retry succeeded");
                    return data;
                } catch (e2) {
                    console.error("API error (after retry):", e2);
                    this.showToast("Erreur réseau: " + e2.message, "error");
                    return null;
                }
            }
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
});

/* ============================================================
   Protection CSRF — wrapper global de window.fetch (double-submit).
   ---------------------------------------------------------------
   Installé UNE seule fois au chargement de ce module : toute
   requête mutante (POST/PUT/PATCH/DELETE…) émise via fetch() — y
   compris apiFetch/apiPost ci-dessus et les fetch() directs des
   autres modules (dashboard.js, editor.js, chat.js, modals.js,
   settings.js, events.js) — reçoit automatiquement l'en-tête
   X-CSRF-Token lu depuis le cookie csrf_token. Le serveur compare
   cookie et en-tête (app.auth.csrf). Les méthodes sûres (GET/HEAD/
   OPTIONS) ne sont jamais modifiées. En cas d'imprévu JS, la
   requête part telle quelle : le serveur répondra 403 {detail:
   "CSRF"} et l'utilisateur rechargera la page (nouveau token).
   ============================================================ */
(function installCsrfFetchWrapper() {
    if (typeof window.fetch !== "function") return;
    if (window.fetch.__dockyCsrfWrapped) return; // idempotent

    const originalFetch = window.fetch.bind(window);
    const SAFE_METHODS = { GET: true, HEAD: true, OPTIONS: true, TRACE: true };

    const wrapped = function (input, init) {
        try {
            const method = String(
                (init && init.method) ||
                (input && input.method) ||
                "GET"
            ).toUpperCase();

            if (!SAFE_METHODS[method]) {
                const token = window.DockyApp && typeof window.DockyApp.csrfToken === "function"
                    ? window.DockyApp.csrfToken()
                    : null;
                if (token) {
                    init = init ? Object.assign({}, init) : {};
                    const headers = new Headers(
                        init.headers || (input instanceof Request ? input.headers : undefined)
                    );
                    if (!headers.has("X-CSRF-Token")) {
                        headers.set("X-CSRF-Token", token);
                    }
                    init.headers = headers;
                }
            }
        } catch (e) {
            // Ne JAMAIS faire échouer la requête appelante à cause du CSRF :
            // sans en-tête le serveur répond 403 {detail:"CSRF"}, détectable.
        }
        return originalFetch(input, init);
    };
    wrapped.__dockyCsrfWrapped = true;
    window.fetch = wrapped;
})();
