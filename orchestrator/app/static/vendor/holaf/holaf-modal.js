/* ═══════════════════════════════════════════════════════════════════════════
 * Holaf UI — Brique HolafModal · version 0.2.0
 * ─────────────────────────────────────────────────────────────────────────────
 * Modale autonome (zéro dépendance runtime) : overlay, pile d'overlays
 * document-level, helpers Promise (alert / confirm / prompt / busy), focus
 * trap, scroll-lock, aria (role=dialog, aria-modal, aria-labelledby), mobile
 * 92vw. v0.2.0 — bibliothèque de thèmes : préréglages génériques (dark,
 * light, midnight, slate), thèmes customs via le registre HolafModal.themes
 * (register / get / list), thème global volatil HolafModal.setTheme().
 * Fichier DUAL : module ES (export) + global window.HolafModal — se
 * charge via <script type="module"> ou `import { HolafModal }`.
 *
 * VOLONTAIREMENT ABSENT (ce sera la brique HolafWindow) : drag / resize de
 * fenêtre, persistance position/taille (localStorage).
 *
 * Héritage design : squelette open() / controller / boutons / CSS auto-injecté
 * et focus trap issus d'aih_dialog.js (ComfyUI-AI-Helper, épuré de tout
 * drag/resize/persistance) ; pile « Échap ne ferme que le sommet » piquée à
 * Pi-Web (useOverlayStack) ; le tout via UN SEUL listener document pour tout
 * le module (pas un listener par modale ouverte).
 * ═════════════════════════════════════════════════════════════════════════ */

const HolafModal = (function () {
    "use strict";

    const VERSION = "0.2.0";

    // ─── État global du module (partagé par toutes les modales) ──────────────
    // Pile des modales ouvertes : la DERNIÈRE entrée est le « sommet », la
    // seule qui réagit à Échap (une pression ferme UNE modale, jamais la
    // cascade — pattern Pi-Web isTopOverlay). Z-index global (base 100000) et
    // compteur de scroll-lock (body.holaf-modal-open retiré seulement à 0,
    // quand la dernière modale empilée se ferme).
    const stack = [];
    let zCounter = 100000;
    let scrollLockCount = 0;
    let uidSeq = 0; // séquence pour les id aria-labelledby uniques

    const CSS_ID = "holaf-modal-style";
    const FOCUSABLE_SELECTOR =
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

    // ─── Utilitaires ─────────────────────────────────────────────────────────
    function isNode(obj) {
        return !!(obj && typeof obj === "object" && obj.nodeType === 1);
    }

    function toPx(val) {
        if (val === undefined || val === null || val === "") return undefined;
        if (typeof val === "number") return val + "px";
        return String(val);
    }

    // Coercition sûre : null/undefined → valeur de repli (jamais "null" affiché).
    function str(v, fallback) {
        return v === undefined || v === null ? (fallback || "") : String(v);
    }

    // Applique des surcharges de variables CSS (--hm-*) sur un élément.
    function applyVars(el, vars) {
        if (!vars) return;
        Object.keys(vars).forEach((k) => {
            if (k.indexOf("--") === 0) el.style.setProperty(k, String(vars[k]));
        });
    }

    // ─── Bibliothèque de thèmes ──────────────────────────────────────────────────────
    // Un thème = un objet de variables CSS --hm-* (les mêmes clés que l'option
    // `theme` de open()). Registre en mémoire + préréglages génériques enregistrés
    // au chargement (dark / light / midnight / slate). Palettes volontairement
    // NEUTRES : aucune couleur de projet en dur — la brique sert plusieurs
    // projets, qui peuvent aussi déclarer leurs thèmes via themes.register.
    const themeRegistry = Object.create(null);

    // Ne conserve que les clés commençant par « -- » (même règle que applyVars) ;
    // valeurs stringifiées.
    function filterVars(vars) {
        const out = {};
        if (!vars || typeof vars !== "object") return out;
        Object.keys(vars).forEach((k) => {
            if (k.indexOf("--") === 0) out[k] = String(vars[k]);
        });
        return out;
    }

    // Enregistre (ou REMPLACE) un thème. Retourne la copie stockée.
    function themesRegister(name, vars) {
        if (typeof name !== "string" || !name.trim()) {
            console.error("[HolafModal] themes.register : nom de thème invalide (chaîne non vide attendue).");
            return null;
        }
        themeRegistry[name] = filterVars(vars);
        return themeRegistry[name];
    }

    // Copie des variables du thème (le registre est protégé des mutations), null si inconnu.
    function themesGet(name) {
        const t = themeRegistry[name];
        return t ? Object.assign({}, t) : null;
    }

    // Noms des thèmes enregistrés (préréglages + customs).
    function themesList() {
        return Object.keys(themeRegistry);
    }

    // Résout une spécification de thème — option `theme` de open() OU argument
    // de setTheme — en objet de variables prêt pour applyVars :
    //   - string           → nom d'un thème enregistré (warn si inconnu) ;
    //   - objet --hm-*     → utilisé tel quel (comportement historique, inchangé) ;
    //   - { preset, vars } → thème enregistré + surcharges (vars gagnent sur le
    //                        preset, qui gagne sur les défauts de la brique).
    function resolveThemeVars(spec) {
        let base = null;
        let overrides = null;
        if (typeof spec === "string") {
            base = themesGet(spec);
            if (!base) {
                console.warn(
                    '[HolafModal] thème inconnu : "' + spec + '" — thèmes disponibles : ' +
                    (themesList().join(", ") || "(aucun)")
                );
                return null; // repli gracieux : défauts CSS
            }
        } else if (spec && typeof spec === "object") {
            if (typeof spec.preset === "string") {
                base = themesGet(spec.preset);
                if (!base) {
                    console.warn(
                        '[HolafModal] thème prédéfini inconnu : "' + spec.preset +
                        '" — thèmes disponibles : ' + (themesList().join(", ") || "(aucun)")
                    );
                    // pas de base : on continue avec les seules surcharges
                }
                if (spec.vars && typeof spec.vars === "object") overrides = spec.vars;
            } else {
                return spec; // objet de variables brut — comportement historique
            }
        } else {
            return null; // null / undefined / valeur exotique : aucun thème
        }
        if (!base) return overrides ? filterVars(overrides) : null;
        if (!overrides) return base;
        return Object.assign(base, overrides); // base = copie → fusion sûre
    }

    // ─── Préréglages génériques (enregistrés au chargement de la brique) ────
    // Contraste des textes ≥ 4.5:1. PAS de --hm-width dans un preset : la
    // largeur est gouvernée par size/width (un thème ne doit pas pouvoir
    // casser les classes sm/md/lg/xl).
    // dark : STRICTEMENT les valeurs par défaut du CSS injecté ci-dessous —
    // theme:"dark" ≡ aucune option theme (rétrocompatibilité à l'identique).
    themesRegister("dark", {
        "--hm-bg": "#1e1e1e",
        "--hm-bg-secondary": "#27272a",
        "--hm-bg-input": "#1a1a1a",
        "--hm-text": "#e4e4e7",
        "--hm-text-secondary": "#a1a1aa",
        "--hm-border": "#3f3f46",
        "--hm-accent": "#6366f1",
        "--hm-accent-hover": "#818cf8",
        "--hm-accent-text": "#ffffff",
        "--hm-danger": "#ef4444",
        "--hm-danger-hover": "#dc2626",
        "--hm-danger-text": "#ffffff",
        "--hm-radius": "12px",
        "--hm-overlay-bg": "rgba(0, 0, 0, 0.55)",
        "--hm-font-size": "14px",
        "--hm-shadow": "0 18px 50px rgba(0, 0, 0, 0.55)",
        "--hm-busy-bg": "rgba(30, 30, 30, 0.82)",
    });
    // light : clair zinc, ombre adoucie, overlay allégé.
    themesRegister("light", {
        "--hm-bg": "#ffffff",
        "--hm-bg-secondary": "#f4f4f5",
        "--hm-bg-input": "#fafafa",
        "--hm-text": "#18181b",
        "--hm-text-secondary": "#52525b",
        "--hm-border": "#d4d4d8",
        "--hm-accent": "#4f46e5",
        "--hm-accent-hover": "#6366f1",
        "--hm-accent-text": "#ffffff",
        "--hm-danger": "#dc2626",
        "--hm-danger-hover": "#b91c1c",
        "--hm-danger-text": "#ffffff",
        "--hm-radius": "12px",
        "--hm-overlay-bg": "rgba(24, 24, 27, 0.35)",
        "--hm-font-size": "14px",
        "--hm-shadow": "0 18px 50px rgba(24, 24, 27, 0.18)",
        "--hm-busy-bg": "rgba(255, 255, 255, 0.82)",
    });
    // midnight : bleu nuit profond « layered », accent indigo doux (texte
    // sombre sur le bouton primaire → contraste ~7:1).
    themesRegister("midnight", {
        "--hm-bg": "#10111d",
        "--hm-bg-secondary": "#181a2c",
        "--hm-bg-input": "#0c0d17",
        "--hm-text": "#e2e4f0",
        "--hm-text-secondary": "#9aa0c3",
        "--hm-border": "#272a44",
        "--hm-accent": "#818cf8",
        "--hm-accent-hover": "#a5b4fc",
        "--hm-accent-text": "#10111d",
        "--hm-danger": "#ef4444",
        "--hm-danger-hover": "#dc2626",
        "--hm-danger-text": "#ffffff",
        "--hm-radius": "12px",
        "--hm-overlay-bg": "rgba(4, 5, 12, 0.65)",
        "--hm-font-size": "14px",
        "--hm-shadow": "0 18px 50px rgba(0, 0, 0, 0.6)",
        "--hm-busy-bg": "rgba(16, 17, 29, 0.85)",
    });
    // slate : gris ardoise neutre, accent gris neutre (bouton primaire « soft »)
    // — le plus polyvalent, lisible sur fond de page de n'importe quelle teinte.
    themesRegister("slate", {
        "--hm-bg": "#1f232b",
        "--hm-bg-secondary": "#292e38",
        "--hm-bg-input": "#191d24",
        "--hm-text": "#e6e9ee",
        "--hm-text-secondary": "#9aa3b2",
        "--hm-border": "#3a4150",
        "--hm-accent": "#94a3b8",
        "--hm-accent-hover": "#b6c2d4",
        "--hm-accent-text": "#1f232b",
        "--hm-danger": "#ef4444",
        "--hm-danger-hover": "#dc2626",
        "--hm-danger-text": "#ffffff",
        "--hm-radius": "12px",
        "--hm-overlay-bg": "rgba(8, 10, 14, 0.55)",
        "--hm-font-size": "14px",
        "--hm-shadow": "0 18px 50px rgba(0, 0, 0, 0.5)",
        "--hm-busy-bg": "rgba(31, 35, 43, 0.85)",
    });

    // ─── Thème global par défaut (VOLATIL — aucune persistance) ─────────────
    // HolafModal.setTheme(...) s'applique à toutes les modales qui ne passent
    // PAS d'option `theme` à open(). Résolution : open.theme (string | objet |
    // { preset, vars }) > setTheme > défauts CSS. Chaque projet le règle une
    // fois à l'init ; rien n'est écrit en localStorage (persister le choix de
    // l'utilisateur est l'affaire du projet hôte, qui rejouera setTheme).
    let globalThemeSpec = null;

    function setTheme(spec) {
        if (spec === null || spec === undefined) {
            globalThemeSpec = null;
            return;
        }
        if (typeof spec === "string") {
            if (!themeRegistry[spec]) {
                console.warn(
                    '[HolafModal] setTheme : thème inconnu "' + spec + '" — thèmes disponibles : ' +
                    (themesList().join(", ") || "(aucun)") +
                    " (enregistrable via HolafModal.themes.register)"
                );
            }
            globalThemeSpec = spec;
            return;
        }
        if (typeof spec !== "object") {
            console.error("[HolafModal] setTheme : attendu un nom de thème, un objet --hm-* ou { preset, vars }.");
            return;
        }
        // Copie superficielle (+ vars) : les mutations externes n'affectent pas
        // le thème global ; la résolution se fait à chaque open().
        const copy = Object.assign({}, spec);
        if (spec.vars && typeof spec.vars === "object") copy.vars = Object.assign({}, spec.vars);
        globalThemeSpec = copy;
    }

    function clearTheme() {
        globalThemeSpec = null;
    }

    // ─── CSS auto-injecté (une seule fois, id holaf-modal-style) ─────────────
    // Variables posées sur la racine de CHAQUE modale (.holaf-modal-overlay /
    // .holaf-modal-root) — JAMAIS sur :root — pour que chaque instance puisse
    // être re-thémée indépendamment via l'option `theme`.
    const HOLAF_MODAL_CSS = `
.holaf-modal-overlay, .holaf-modal-root {
    --hm-bg: #1e1e1e; --hm-bg-secondary: #27272a; --hm-bg-input: #1a1a1a;
    --hm-text: #e4e4e7; --hm-text-secondary: #a1a1aa; --hm-border: #3f3f46;
    --hm-accent: #6366f1; --hm-accent-hover: #818cf8; --hm-accent-text: #ffffff;
    --hm-danger: #ef4444; --hm-danger-hover: #dc2626; --hm-danger-text: #ffffff;
    --hm-radius: 12px; --hm-overlay-bg: rgba(0, 0, 0, 0.55); --hm-font-size: 14px;
    --hm-shadow: 0 18px 50px rgba(0, 0, 0, 0.55); --hm-busy-bg: rgba(30, 30, 30, 0.82);
    --hm-width: 440px;
}
.holaf-modal-overlay { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; padding: 16px; background: var(--hm-overlay-bg); animation: holaf-modal-fade 0.16s ease-out; box-sizing: border-box; }
.holaf-modal-overlay--modeless { background: transparent; pointer-events: none; }
.holaf-modal-overlay--modeless .holaf-modal-root { pointer-events: auto; }
.holaf-modal-root { position: relative; display: flex; flex-direction: column; width: var(--hm-width); max-width: calc(100vw - 24px); max-height: 88vh; background: var(--hm-bg); color: var(--hm-text); border: 1px solid var(--hm-border); border-radius: var(--hm-radius); box-shadow: var(--hm-shadow); font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; font-size: var(--hm-font-size); line-height: 1.5; box-sizing: border-box; overflow: hidden; outline: none; animation: holaf-modal-pop 0.22s cubic-bezier(0.16, 1, 0.3, 1); }
.holaf-modal-sm { --hm-width: 340px; }
.holaf-modal-md { --hm-width: 440px; }
.holaf-modal-lg { --hm-width: 640px; }
.holaf-modal-xl { --hm-width: 860px; }
.holaf-modal-header { display: flex; align-items: center; gap: 8px; padding: 12px 16px; background: var(--hm-bg-secondary); border-bottom: 1px solid var(--hm-border); flex-shrink: 0; }
.holaf-modal-title { flex: 1; font-size: 15px; font-weight: 600; color: var(--hm-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.holaf-modal-close { background: none; border: none; cursor: pointer; color: var(--hm-text-secondary); font-size: 16px; line-height: 1; padding: 2px 6px; border-radius: 6px; flex-shrink: 0; transition: color 0.15s; touch-action: manipulation; }
.holaf-modal-close:hover { color: var(--hm-danger); }
.holaf-modal-body { flex: 1 1 auto; padding: 16px; overflow-y: auto; overflow-x: hidden; color: var(--hm-text); }
.holaf-modal-message { margin: 0; color: var(--hm-text); overflow-wrap: break-word; }
.holaf-modal-footer { display: flex; justify-content: flex-end; align-items: center; gap: 8px; padding: 12px 16px; background: var(--hm-bg-secondary); border-top: 1px solid var(--hm-border); flex-shrink: 0; }
.holaf-modal-btn { padding: 7px 16px; border-radius: 8px; border: 1px solid var(--hm-border); background: var(--hm-bg-secondary); color: var(--hm-text); font: inherit; cursor: pointer; transition: background 0.15s, border-color 0.15s; touch-action: manipulation; }
.holaf-modal-btn:hover { border-color: var(--hm-text-secondary); }
.holaf-modal-btn-primary { background: var(--hm-accent); border-color: var(--hm-accent); color: var(--hm-accent-text); }
.holaf-modal-btn-primary:hover { background: var(--hm-accent-hover); border-color: var(--hm-accent-hover); }
.holaf-modal-btn-danger { background: var(--hm-danger); border-color: var(--hm-danger); color: var(--hm-danger-text); }
.holaf-modal-btn-danger:hover { background: var(--hm-danger-hover); border-color: var(--hm-danger-hover); }
.holaf-modal-btn-cancel { background: transparent; color: var(--hm-text-secondary); }
.holaf-modal-btn-cancel:hover { color: var(--hm-text); border-color: var(--hm-text-secondary); }
.holaf-modal-input { width: 100%; margin-top: 10px; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--hm-border); background: var(--hm-bg-input); color: var(--hm-text); font: inherit; outline: none; box-sizing: border-box; }
.holaf-modal-input:focus { border-color: var(--hm-accent); }
.holaf-modal-busy { position: absolute; inset: 0; z-index: 5; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; background: var(--hm-busy-bg); padding: 16px; text-align: center; }
.holaf-modal-spinner { width: 28px; height: 28px; border: 3px solid var(--hm-border); border-top-color: var(--hm-accent); border-radius: 50%; animation: holaf-modal-spin 0.8s linear infinite; flex-shrink: 0; }
/* Scroll-lock : classe posée sur <body> tant qu'au moins une modale est ouverte. */
body.holaf-modal-open { overflow: hidden; }
/* Mobile : la modale occupe 92 % de la largeur de l'écran. */
@media (max-width: 767px) { .holaf-modal-root { width: 92vw; max-width: 92vw; } }
@keyframes holaf-modal-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes holaf-modal-pop { from { opacity: 0; transform: translateY(10px) scale(0.97); } to { opacity: 1; transform: none; } }
@keyframes holaf-modal-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .holaf-modal-overlay, .holaf-modal-root, .holaf-modal-spinner { animation: none; } }
`;

    let cssInjected = false;
    function ensureCss() {
        if (cssInjected) return;
        if (typeof document === "undefined") return;
        if (!document.getElementById(CSS_ID)) {
            const style = document.createElement("style");
            style.id = CSS_ID;
            style.textContent = HOLAF_MODAL_CSS;
            document.head.appendChild(style);
        }
        cssInjected = true;
    }

    // ─── Scroll-lock avec compteur ───────────────────────────────────────────
    // body.holaf-modal-open { overflow:hidden } n'est retiré que quand le
    // compteur retombe à 0 (plusieurs modales empilées).
    function lockScroll() {
        scrollLockCount += 1;
        if (scrollLockCount === 1 && document.body) document.body.classList.add("holaf-modal-open");
    }
    function unlockScroll() {
        scrollLockCount = Math.max(0, scrollLockCount - 1);
        if (scrollLockCount === 0 && document.body) document.body.classList.remove("holaf-modal-open");
    }

    // ─── Clavier : UN SEUL listener document pour tout le module ─────────────
    // Escape : ferme UNIQUEMENT la modale au sommet de la pile (si elle
    // l'autorise — sinon on ne fait rien, jamais de fermeture en cascade).
    // Tab : focus trap de la modale au sommet. Capture + stopPropagation : la
    // modale « avale » l'Échap pour ne pas déclencher un raccourci de la page
    // hôte en même temps.
    let keyHandlerInstalled = false;
    function installKeyHandler() {
        if (keyHandlerInstalled || typeof document === "undefined") return;
        keyHandlerInstalled = true;
        document.addEventListener(
            "keydown",
            (e) => {
                if (stack.length === 0) return;
                const top = stack[stack.length - 1]; // sommet de la pile
                if (e.key === "Escape") {
                    if (top.closeOnEscape) {
                        e.preventDefault();
                        e.stopPropagation();
                        top.close();
                    }
                    return;
                }
                if (e.key === "Tab" && top.focusTrap) trapFocus(e, top.el);
            },
            true
        );
    }

    // Focus trap (hérité d'aih_dialog) : Tab / Shift+Tab circulent dans la
    // modale ; si le focus est à l'extérieur, on le ramène à l'intérieur.
    function trapFocus(e, root) {
        const focusables = root.querySelectorAll(FOCUSABLE_SELECTOR);
        if (focusables.length === 0) {
            e.preventDefault();
            root.focus();
            return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement;
        if (!root.contains(active)) {
            e.preventDefault();
            first.focus();
        } else if (e.shiftKey && active === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && active === last) {
            e.preventDefault();
            first.focus();
        }
    }

    // ─── Noyau : HolafModal.open(options) ────────────────────────────────────
    function open(opts) {
        opts = opts || {};
        if (typeof document === "undefined") {
            throw new Error("[HolafModal] nécessite un navigateur (document indisponible).");
        }

        // ── Anti-doublon par id : déjà ouverte → bringToFront au lieu d'une
        // copie (protège des multi-clics) ; retourne le contrôleur existant.
        if (opts.id) {
            const existing = document.getElementById(opts.id);
            if (existing && existing._holafModalCtrl) {
                existing._holafModalCtrl.bringToFront();
                return existing._holafModalCtrl;
            }
        }

        ensureCss();
        installKeyHandler();

        // ── Options (valeurs par défaut) ─────────────────────────────────────
        const modal = opts.modal !== undefined ? !!opts.modal : true;
        const closeOnEscape = opts.closeOnEscape !== false;
        const closeOnOverlay = opts.closeOnOverlay !== false;
        const focusTrap = opts.focusTrap !== undefined ? !!opts.focusTrap : true;
        const scrollLock = opts.scrollLock !== undefined ? !!opts.scrollLock : true;
        const hideClose = !!opts.hideClose;

        // ── Overlay : toujours créé (backdrop si modal, transparent sinon) —
        // en mode non-modal, l'overlay laisse passer les clics (pointer-events
        // none) : boîte centrée, page encore utilisable derrière.
        const overlay = document.createElement("div");
        overlay.className = "holaf-modal-overlay" + (modal ? "" : " holaf-modal-overlay--modeless");

        // ── Racine de la modale ──────────────────────────────────────────────
        const el = document.createElement("div");
        el.className = "holaf-modal-root";
        if (opts.id) el.id = opts.id;
        el.setAttribute("role", "dialog");
        el.setAttribute("aria-modal", modal ? "true" : "false");
        el.setAttribute("tabindex", "-1"); // permet le focus programmatique (trap)

        // Taille : classe sm/md/lg/xl, ou largeur custom via --hm-width.
        if (opts.size && ["sm", "md", "lg", "xl"].indexOf(opts.size) !== -1) {
            el.classList.add("holaf-modal-" + opts.size);
        }
        const widthVar = toPx(opts.width);
        if (widthVar) el.style.setProperty("--hm-width", widthVar);

        // ── Thème de CETTE modale : résolution
        //   open.theme (string | objet --hm-* | { preset, vars })
        //     > thème global (HolafModal.setTheme)
        //     > défauts CSS de la brique (thème sombre).
        // Les variables sont posées sur l'overlay ET la racine — jamais sur
        // :root — donc chaque instance reste re-thémable indépendamment.
        // `theme: null` (ou "") : aucun thème pour cette modale, même si un
        // thème global est actif.
        let themeSpec = opts.theme;
        if (themeSpec === undefined) themeSpec = globalThemeSpec;
        const themeVars = resolveThemeVars(themeSpec);
        if (themeVars) {
            applyVars(overlay, themeVars);
            applyVars(el, themeVars);
        }

        // ── Header (titre en textContent — JAMAIS innerHTML) ─────────────────
        const header = document.createElement("div");
        header.className = "holaf-modal-header";
        const titleId = "holaf-modal-title-" + ++uidSeq;
        const titleEl = document.createElement("span");
        titleEl.className = "holaf-modal-title";
        titleEl.id = titleId;
        titleEl.textContent = str(opts.title);
        header.appendChild(titleEl);
        if (!hideClose) {
            const closeBtn = document.createElement("button");
            closeBtn.type = "button";
            closeBtn.className = "holaf-modal-close";
            closeBtn.textContent = "✕";
            closeBtn.title = "Fermer";
            closeBtn.setAttribute("aria-label", "Fermer");
            closeBtn.addEventListener("click", () => close());
            header.appendChild(closeBtn);
        }
        el.appendChild(header);
        el.setAttribute("aria-labelledby", titleId);

        // ── Body (content : string → innerHTML de confiance, ou Node) ───────
        const body = document.createElement("div");
        body.className = "holaf-modal-body";
        renderContent(body, opts.content);
        el.appendChild(body);

        // ── Footer + boutons ─────────────────────────────────────────────────
        // buttons : [{ text, value, type: "primary"|"danger"|"cancel",
        //              onClick(ctrl), close, guard }] — close:false → ne ferme
        // pas ; guard:false → passe outre le guard global (ex. Annuler).
        // onClick peut retourner false pour refuser la fermeture, ou une
        // valeur qui remplace btn.value.
        const buttons = Array.isArray(opts.buttons) ? opts.buttons : [];
        let footer = null;
        if (buttons.length > 0) {
            footer = document.createElement("div");
            footer.className = "holaf-modal-footer";
            buttons.forEach((btn) => {
                const b = document.createElement("button");
                b.type = "button";
                b.className = "holaf-modal-btn holaf-modal-btn-" + (btn.type || "primary");
                b.textContent = str(btn.text, "OK");
                if (btn.autoFocus) b.setAttribute("data-holaf-autofocus", "1");
                b.addEventListener("click", () => {
                    let result = btn.value;
                    if (typeof btn.onClick === "function") {
                        let r;
                        try {
                            r = btn.onClick(ctrl);
                        } catch (err) {
                            console.error("[HolafModal] onClick :", err);
                            return;
                        }
                        if (r === false) return; // onClick refuse la fermeture
                        if (r !== undefined && r !== null) result = r;
                    }
                    if (btn.close === false) return;
                    // Guard global sauf pour les boutons qui s'en excluent
                    // (btn.guard:false — ex. Annuler d'un confirm).
                    if (btn.guard === false) close(result);
                    else runGuardThenClose(result);
                });
                footer.appendChild(b);
            });
            el.appendChild(footer);
        }

        // ── Z-index via le compteur global (base 100000) ─────────────────────
        overlay.style.zIndex = String(++zCounter);

        // ── Insertion DOM + pile + scroll-lock ───────────────────────────────
        overlay.appendChild(el);
        document.body.appendChild(overlay);
        if (scrollLock) lockScroll();

        let closed = false;
        const entry = {
            el: el,
            closeOnEscape: closeOnEscape,
            focusTrap: focusTrap,
            close: (v) => close(v),
        };
        stack.push(entry);

        // ── Fermeture (idempotente) ──────────────────────────────────────────
        function close(value) {
            if (closed) return;
            closed = true;
            const result = value === undefined ? null : value;
            const idx = stack.indexOf(entry);
            if (idx !== -1) stack.splice(idx, 1); // sort de la pile
            if (scrollLock) unlockScroll();
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
            // Restaure le focus là où il était avant l'ouverture.
            if (prevFocus && prevFocus.isConnected) {
                try { prevFocus.focus(); } catch (e) { /* silencieux */ }
            }
            if (typeof opts.onClose === "function") {
                try { opts.onClose(result); } catch (e) { console.error("[HolafModal] onClose :", e); }
            }
            if (typeof opts._onResolve === "function") {
                try { opts._onResolve(result); } catch (e) { /* silencieux */ }
            }
        }

        // ── guard optionnel (confirm) : valide AVANT de fermer ───────────────
        // guard(value, ctrl) → false | erreur = on reste ouvert.
        function runGuardThenClose(result) {
            if (typeof opts.guard !== "function") {
                close(result);
                return;
            }
            setBusy(true);
            Promise.resolve()
                .then(() => opts.guard(result, ctrl))
                .then((r) => {
                    setBusy(false);
                    if (r === false) return; // refus → la modale reste ouverte
                    close(result);
                })
                .catch((err) => {
                    setBusy(false);
                    console.error("[HolafModal] guard :", err); // erreur → ouvert (plus sûr)
                });
        }

        // ── Overlay click (closeOnOverlay) : clic sur le FOND uniquement ─────
        if (closeOnOverlay) {
            overlay.addEventListener("click", (e) => {
                if (e.target === overlay) close();
            });
        }

        // ── Controller ───────────────────────────────────────────────────────
        let busyEl = null;
        let busyLabel = null;
        function setBusy(on, msg) {
            if (on && !busyEl) {
                busyEl = document.createElement("div");
                busyEl.className = "holaf-modal-busy";
                busyEl.setAttribute("aria-live", "polite");
                const spin = document.createElement("span");
                spin.className = "holaf-modal-spinner";
                busyLabel = document.createElement("div");
                busyLabel.className = "holaf-modal-message";
                busyEl.appendChild(spin);
                busyEl.appendChild(busyLabel);
                el.appendChild(busyEl);
            }
            if (!busyEl) return;
            if (on && msg !== undefined && msg !== null) busyLabel.textContent = String(msg);
            busyEl.style.display = on ? "flex" : "none";
        }

        function setTitle(t) {
            titleEl.textContent = str(t);
        }

        function setContent(c) {
            body.innerHTML = "";
            renderContent(body, c);
        }

        function bringToFront() {
            // Nouveau z-index (au-dessus de tout) ET remontée au sommet de la
            // pile : c'est cette modale qui recevra la prochaine touche Échap.
            overlay.style.zIndex = String(++zCounter);
            const idx = stack.indexOf(entry);
            if (idx !== -1) {
                stack.splice(idx, 1);
                stack.push(entry);
            }
        }

        const ctrl = {
            el: el,
            body: body,
            overlay: overlay,
            footer: footer,
            close: close,
            setTitle: setTitle,
            setContent: setContent,
            setBusy: setBusy,
            bringToFront: bringToFront,
        };
        el._holafModalCtrl = ctrl; // utilisé par l'anti-doublon / le cleanup

        // ── Focus initial (autoFocus sur un bouton, sinon 1er focusable) ─────
        const prevFocus = document.activeElement;
        const autoEl = el.querySelector("[data-holaf-autofocus]");
        if (autoEl) autoEl.focus();
        else {
            const focusables = el.querySelectorAll(FOCUSABLE_SELECTOR);
            if (focusables.length) focusables[0].focus();
            else el.focus();
        }

        // ── onOpen ───────────────────────────────────────────────────────────
        if (typeof opts.onOpen === "function") {
            try { opts.onOpen(ctrl); } catch (e) { console.error("[HolafModal] onOpen :", e); }
        }

        return ctrl;
    }

    // Rend le contenu dans le conteneur : string → innerHTML (contenu de
    // confiance, documenté) ; Node → rattaché tel quel (recommandé).
    function renderContent(target, content) {
        if (content === undefined || content === null) return;
        if (isNode(content)) target.appendChild(content);
        else target.innerHTML = String(content);
    }

    // Conteneur « message » des helpers (textContent → injection impossible).
    function messageNode(message) {
        const p = document.createElement("p");
        p.className = "holaf-modal-message";
        p.textContent = str(message);
        return p;
    }

    // ─── Helpers Promise ─────────────────────────────────────────────────────
    // Les helpers se ferment par leurs boutons ou Échap (pas d'un clic dans
    // le fond : closeOnOverlay=false) pour éviter les dismiss accidentels.

    // alert(title, msg, opts) → Promise (résolue à la fermeture, sans valeur utile).
    function alert(title, message, opts) {
        opts = opts || {};
        return new Promise((resolve) => {
            open({
                title: title || "Information",
                size: "sm",
                content: messageNode(message),
                buttons: [{ text: opts.okText || "OK", value: true, type: "primary", autoFocus: true }],
                theme: opts.theme,
                closeOnOverlay: false,
                _onResolve: () => resolve(undefined),
            });
        });
    }

    // confirm(title, msg, { danger, guard, confirmText, cancelText }) → Promise<boolean>
    // true si confirmé (bouton), false sinon (Annuler, ✕, Échap…).
    function confirm(title, message, opts) {
        opts = opts || {};
        return new Promise((resolve) => {
            open({
                title: title || "Confirmation",
                size: "sm",
                content: messageNode(message),
                buttons: [
                    { text: opts.cancelText || "Annuler", value: false, type: "cancel", guard: false },
                    {
                        text: opts.confirmText || "Confirmer",
                        value: true,
                        type: opts.danger ? "danger" : "primary",
                        autoFocus: true,
                    },
                ],
                guard: opts.guard,
                theme: opts.theme,
                closeOnOverlay: false,
                _onResolve: (v) => resolve(v === true),
            });
        });
    }

    // prompt(title, msg, { placeholder, initial, okText, cancelText }) → Promise<string|null>
    // La chaîne saisie ("" possible) si validé ; null si annulé (Annuler, ✕, Échap).
    function prompt(title, message, opts) {
        opts = opts || {};
        return new Promise((resolve) => {
            const input = document.createElement("input");
            input.className = "holaf-modal-input";
            input.type = "text";
            input.placeholder = opts.placeholder || "";
            input.value = opts.initial || "";
            const wrap = document.createElement("div");
            if (message) wrap.appendChild(messageNode(message));
            wrap.appendChild(input);
            const ctrl = open({
                title: title || "Saisie",
                size: "sm",
                content: wrap,
                buttons: [
                    { text: opts.cancelText || "Annuler", value: null, type: "cancel" },
                    {
                        text: opts.okText || "OK",
                        type: "primary",
                        onClick: () => input.value, // la valeur saisie remplace btn.value
                    },
                ],
                theme: opts.theme,
                closeOnOverlay: false,
                _onResolve: (v) => resolve(typeof v === "string" ? v : null),
            });
            // Entrée dans le champ = valider (comme un <form>).
            input.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    e.preventDefault();
                    ctrl.close(input.value);
                }
            });
            input.focus();
        });
    }

    // busy(msg) → { set(msg), close() } : écran d'attente non fermable
    // (ni ✕, ni Échap, ni fond). close() par code quand le travail est fini.
    function busy(message) {
        const box = document.createElement("div");
        box.style.cssText = "display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;padding:8px 4px;";
        const spin = document.createElement("span");
        spin.className = "holaf-modal-spinner";
        const label = document.createElement("div");
        label.className = "holaf-modal-message";
        label.textContent = str(message, "Chargement…");
        box.appendChild(spin);
        box.appendChild(label);
        const ctrl = open({
            title: "",
            size: "sm",
            content: box,
            hideClose: true,
            closeOnEscape: false,
            closeOnOverlay: false,
            focusTrap: false,
        });
        return {
            set(msg) {
                if (msg !== undefined && msg !== null) label.textContent = String(msg);
            },
            close() {
                ctrl.close();
            },
            el: ctrl.el,
        };
    }

    // ─── API publique ────────────────────────────────────────────────────────
    return {
        version: VERSION,
        open: open,
        alert: alert,
        confirm: confirm,
        prompt: prompt,
        busy: busy,
        // Thème global par défaut (volatil) : s'applique aux modales qui ne
        // passent pas d'option `theme` — voir README section « Thèmes ».
        setTheme: setTheme,
        clearTheme: clearTheme,
        // Registre de thèmes (préréglages + customs) :
        //   themes.register(name, vars) — enregistre/remplace (retourne la copie stockée)
        //   themes.get(name)            — copie des variables ou null
        //   themes.list()               — noms enregistrés
        themes: {
            register: themesRegister,
            get: themesGet,
            list: themesList,
        },
    };
})();

// Exposition globale (scripts classiques de la page) — pattern IIFE hérité de
// 01_aih_modal_v2.js : le module s'auto-enregistre sur window.
if (typeof window !== "undefined") {
    window.HolafModal = HolafModal;
}

// Export ESM (import { HolafModal } from "./holaf-modal.js").
export { HolafModal };