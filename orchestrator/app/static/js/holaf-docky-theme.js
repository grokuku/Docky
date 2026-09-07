/* ═══════════════════════════════════════════════════════════════════════════
 * Docky — Thème HolafModal « docky » (Proposition 4)
 * ─────────────────────────────────────────────────────────────────────────────
 * Module autonome, chargé APRÈS la brique /static/vendor/holaf/holaf-modal.js
 * (dans settings.html) :
 *   1. enregistre le thème "docky" dans le registre HolafModal.themes ;
 *   2. le pose comme thème GLOBAL (HolafModal.setTheme("docky")) pour que
 *      TOUTES les modales Docky héritent de la palette sans répéter l'option
 *      `theme` à chaque HolafModal.open() (ex. la modale « Supprimer l'agent »
 *      dans settings.js).
 *
 * Fail-safe silencieux : si la brique HolafModal n'est pas chargée (ou pas
 * encore initialisée), ce module se retire sans erreur ni console noise.
 *
 * Palette « Proposition 4 » validée pour Docky (fond #151527, accent #e94560,
 * rayon 16px, overlay rgba(13,13,27,.7)).
 * ═════════════════════════════════════════════════════════════════════════ */

/* global HolafModal */

/* eslint-disable no-var */
(function () {
    "use strict";

    // Palette « Proposition 4 » — variables --hm-* (mêmes clés que les presets
    // de la brique : dark / light / midnight / slate).
    var DOCKY_THEME = {
        "--hm-bg": "#151527",
        "--hm-bg-secondary": "#1d1d36",
        "--hm-bg-input": "#1d1d36",
        "--hm-text": "#f0f0f8",
        "--hm-text-secondary": "#b4b4cf",
        "--hm-border": "#262643",
        "--hm-accent": "#e94560",
        "--hm-accent-hover": "#ff5e7a",
        "--hm-accent-text": "#ffffff",
        "--hm-danger": "#ef4444",
        "--hm-danger-hover": "#dc2626",
        "--hm-danger-text": "#ffffff",
        "--hm-radius": "16px",
        "--hm-overlay-bg": "rgba(13, 13, 27, 0.7)",
        "--hm-shadow": "0 18px 50px rgba(5, 5, 16, 0.5)",
        "--hm-font-size": "14px",
    };

    // Fail-safe silencieux : la brique (et son API thèmes) doit être chargée.
    if (
        typeof HolafModal === "undefined" ||
        !HolafModal.themes ||
        typeof HolafModal.themes.register !== "function" ||
        typeof HolafModal.setTheme !== "function"
    ) {
        return;
    }

    HolafModal.themes.register("docky", DOCKY_THEME);
    // Thème par défaut de toutes les modales Docky : les appels open() sans
    // option `theme` (comme « Supprimer l'agent ») l'héritent automatiquement.
    HolafModal.setTheme("docky");
})();
