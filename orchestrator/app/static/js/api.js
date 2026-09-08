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
    // Utilities
    // -------------------------------------------------------

    async apiFetch(url, options = {}) {
        // Délègue à l'adaptateur HolafFetch (voir holaf-docky-fetch.js) :
        // config commune Docky (auth CSRF cookie csrf_token, timeout 30 s,
        // 401 → /login, retry réseau ×1 500 ms sur méthodes sûres). Contrat
        // public inchangé : données | null, toast avec le message d'erreur
        // (err.message contient désormais body.detail via la brique v0.1.1).
        return window.DockyFetch.apiRequest(url, options);
    },

    async apiPost(url) {
        return this.apiFetch(url, { method: "POST" });
    },

    showToast(message, type = "info") {
        // Adaptateur fin vers la brique HolafToast (voir holaf-docky-toast.js).
        return window.DockyToast.show(message, type);
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
