/* ============================================================
   Docky — Adaptateur fin Toast → HolafToast (brique holaf-toast)
   ------------------------------------------------------------
   Remplaçant de l'ancien toast maison (#toast + .toast CSS).

   Stratégie : adaptateur fin. Les call sites appellent toujours
   `showToast(message, type)` (signature inchangée) ; seule
   l'IMPLÉMENTATION est remplacée par un wrapper vers la brique
   `window.HolafToast` (chargée via <script type="module">).

   Mapping des types Docky → types brique (1:1, la brique
   supporte les 4) :
     info    → info
     success → success
     error   → error
     warning → warning

   Comportement conservé par rapport à l'ancien toast :
     - durée d'affichage 3000 ms (auto-dismiss) ;
     - position bas (bottom-right, la brique n'a pas de
       bottom-center — coin le plus proche de l'ancien centrage bas).

   Thème Docky via le REGISTRE de la brique (v0.2.0) : on
   enregistre le thème "docky" (variables --ht-*) puis on le pose
   comme thème GLOBAL (HolafToast.setTheme("docky")) pour que
   TOUS les toasts Docky héritent de la palette sans répéter
   l'option `theme` à chaque show(). Plus aucun override CSS
   !important dans style.css.

   Ce module est un script CLASSIQUE (non module) : il ne fait
   que définir `window.DockyToast` et lit `window.HolafToast` au
   moment de l'appel (jamais au chargement), donc l'ordre de
   chargement avec la brique (module, différé) n'a pas d'importance.
   Fail-safe silencieux : si la brique n'est pas chargée, il se
   replie sur un console.warn sans casser les call sites.

   NB — la brique est chargée en <script type="module"> (différé) :
   elle peut ne pas être disponible au chargement de CE script
   (classique, exécuté avant). L'enregistrement du thème est donc
   IDEMPOTENT et re-tenté à chaque show() : dès que la brique est
   prête, le thème "docky" est enregistré et posé en global.
   ============================================================ */

/* global HolafToast */

(function () {
    "use strict";

    // Types Docky → types brique (1:1).
    var TYPE_MAP = { info: "info", success: "success", error: "error", warning: "warning" };

    // Durée d'affichage conservée de l'ancien toast maison (3000 ms).
    var DURATION = 3000;
    // Position : bottom-right (coin le plus proche de l'ancien centrage bas).
    var POSITION = "bottom-right";

    // Palette Docky — variables --ht-* (mêmes clés que les presets de la
    // brique : dark / light / midnight / slate). Surfaces #1d1d36, bordures
    // #262643, texte #f0f0f8, rayon 12px, accents Docky.
    var DOCKY_THEME = {
        "--ht-bg": "#1d1d36",
        "--ht-fg": "#f0f0f8",
        "--ht-border": "#262643",
        "--ht-accent-info": "#60a5fa",
        "--ht-accent-success": "#34d399",
        "--ht-accent-warning": "#fbbf24",
        "--ht-accent-error": "#f87171",
        "--ht-radius": "12px",
        "--ht-shadow": "0 6px 24px rgba(13, 13, 27, 0.7)",
    };

    // Enregistre le thème "docky" dans le registre de la brique et le pose
    // comme thème GLOBAL (s'applique à tous les toasts sans option `theme`).
    // IDEMPOTENT : re-tenté à chaque show() pour couvrir le cas où la brique
    // (module différé) n'est pas encore chargée au chargement de ce script.
    // Fail-safe silencieux : si la brique (ou son API thèmes) n'est pas
    // chargée, on se retire sans erreur ni console noise.
    function ensureTheme() {
        if (
            typeof HolafToast !== "undefined" &&
            HolafToast.themes &&
            typeof HolafToast.themes.register === "function" &&
            typeof HolafToast.setTheme === "function"
        ) {
            HolafToast.themes.register("docky", DOCKY_THEME);
            HolafToast.setTheme("docky");
        }
    }

    // show(message, type, opts?) — opts { position, duration } optionnels.
    // Défauts : bottom-right / 3000 ms (comportement historique conservé).
    function show(message, type, opts) {
        ensureTheme();
        var t = TYPE_MAP[type] || "info";
        var text = String(message === null || message === undefined ? "" : message);
        opts = opts || {};
        var options = {
            duration: opts.duration === undefined ? DURATION : Math.max(0, Number(opts.duration) || 0),
            position: opts.position || POSITION,
        };
        if (window.HolafToast && typeof window.HolafToast[t] === "function") {
            window.HolafToast[t](text, options);
        } else {
            // Repli fail-safe : la brique n'est pas encore chargée.
            console.warn("[DockyToast] HolafToast indisponible, message ignoré:", text);
        }
    }

    // Tente l'enregistrement du thème dès le chargement (si la brique est
    // déjà là) ; sinon il sera re-tenté au premier show().
    ensureTheme();

    window.DockyToast = { show: show };
})();
