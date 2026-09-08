/* ============================================================
   Docky — Adaptateur fin Fetch → HolafFetch (brique holaf-fetch)
   ------------------------------------------------------------
   Remplaçant des anciennes implémentations maison apiFetch
   (api.js ET settings.js — la duplication est supprimée).

   Stratégie : adaptateur fin. La brique HolafFetch (copie pinnée
   vendor/holaf/holaf-fetch.js) apporte le cœur blindé : JSON
   vérifié avant parsing, erreurs typées HolafFetchError
   { status, data, body }, timeout AbortController, messages
   automatiques (body.error, sinon body.detail — convention
   FastAPI). L'adaptateur n'ajoute AUCUNE logique HTTP : il
   injecte la CONFIG COMMUNE Docky à chaque requête :

     - auth CSRF (double-submit cookie) : auth { type: "csrf",
       cookieName: "csrf_token" } → en-tête X-CSRF-Token lu frais
       à chaque requête (voir docs/csrf-protection.md) ;
     - on.status 401 → redirection /login (session expirée) ;
       surchargeable via opts.noRedirect401 (true = pas de redirection,
       ex. heartbeat de présence qui doit rester silencieux) ;
     - timeout 30 000 ms par défaut, surchargeable via
       opts.timeout (0 = aucun).

   Deux niveaux d'API :

     DockyFetch.request(url, opts)
       Adaptateur fin : applique la config commune puis délègue à
       HolafFetch.request. En cas d'échec il LÈVE la
       HolafFetchError (err.status / err.data / err.message
       accessibles). Réservé aux appelants qui réagissent au
       statut — ex. le polling du dashboard (err.status === 404).

     DockyFetch.apiRequest(url, opts)
       Contrat Docky historique (utilisé par apiFetch/apiPost) :
       ne lève JAMAIS — retourne les données ou null ; affiche un
       toast avec le message d'erreur (err.message, sinon
       err.data.detail) ; 401 → null silencieux (la redirection
       est déjà déclenchée par le hook on.status).

   Retry réseau — au niveau ADAPTATEUR (pas via l'option retry de
   la brique, qui reprendrait aussi les 5xx) : un seul essai
   supplémentaire après 500 ms, UNIQUEMENT sur erreur réseau
   (statut 0, message « erreur réseau ») et UNIQUEMENT pour les
   méthodes sûres (GET/HEAD/OPTIONS). Jamais sur les mutations,
   jamais sur les 4xx/5xx, jamais sur les timeouts (un serveur
   lent resterait lent au 2e essai) ni sur une annulation
   explicite (opts.signal déjà aborted).

   Ce module est un script CLASSIQUE (non module) : il lit
   window.HolafFetch au moment de l'appel (jamais au chargement),
   donc l'ordre de chargement avec la brique (module, différé)
   n'a pas d'importance. Fail-safe : si la brique n'est pas
   chargée au moment de l'appel, request() lève une erreur claire
   et apiRequest() se replie sur un toast + null, sans casser les
   call sites.

   NB — le wrapper CSRF global de window.fetch installé par api.js a été
   RETIRÉ (adoption HolafFetch finalisée) : tous les fetch directs des
   modules (dashboard.js, editor.js, chat.js, modals.js, events.js,
   logs.html) passent désormais par CET adaptateur, qui signe les requêtes
   mutantes avec l'auth CSRF (double-submit cookie). Sur la page settings
   (pas d'api.js), c'est aussi l'auth CSRF de CET adaptateur qui signe.
   ============================================================ */

/* global HolafFetch */

(function () {
    "use strict";

    // ─── Constantes ──────────────────────────────────────────────────────────
    // Timeout par défaut conservé de la brique (30 s) ; surchargeable par
    // appel via opts.timeout (0 = aucun).
    var DEFAULT_TIMEOUT = 30000;
    // Retry réseau adaptateur : ×1, après 500 ms (comportement historique
    // de l'ancien apiFetch maison).
    var RETRY_DELAY_MS = 500;
    var SAFE_METHODS = { GET: true, HEAD: true, OPTIONS: true };

    function isSafeMethod(method) {
        return !!SAFE_METHODS[String(method || "GET").toUpperCase()];
    }

    function sleep(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    // ─── Config commune ──────────────────────────────────────────────────────
    // Fusionne (sans muter l'objet de l'appelant) : auth CSRF, timeout par
    // défaut, hook on.status 401 → /login (compose avec un éventuel hook
    // de l'appelant, qui reste appelé pour TOUS les statuts).
    function buildConfig(opts) {
        opts = opts || {};
        var userOnStatus = (opts.on && typeof opts.on.status === "function")
            ? opts.on.status
            : null;
        // noRedirect401 : certains flux (ex. heartbeat de présence) doivent
        // rester silencieux sur 401 — pas de redirection /login.
        var noRedirect401 = !!opts.noRedirect401;
        var cfg = Object.assign({}, opts);
        cfg.auth = opts.auth || { type: "csrf", cookieName: "csrf_token" };
        if (opts.timeout === undefined) cfg.timeout = DEFAULT_TIMEOUT;
        cfg.on = {
            status: function (code) {
                if (code === 401 && !noRedirect401) {
                    // Session expirée : même contrat que l'ancien apiFetch.
                    window.location.href = "/login";
                }
                // Le hook éventuel de l'appelant reste appelé (composition).
                if (userOnStatus) userOnStatus(code);
            },
        };
        return cfg;
    }

    // ─── Message d'erreur lisible ────────────────────────────────────────────
    // HolafFetchError.message contient déjà body.error / body.detail (v0.1.1,
    // convention FastAPI) ; err.data.detail sert de repli (ex. corps non
    // parsé), puis un générique réseau.
    function messageFromError(err) {
        if (err && typeof err.message === "string" && err.message.trim() !== "") {
            return err.message;
        }
        if (err && err.data && typeof err.data.detail === "string" && err.data.detail.trim() !== "") {
            return err.data.detail;
        }
        return "erreur réseau";
    }

    function isHolafError(err) {
        return typeof HolafFetch !== "undefined"
            && err instanceof HolafFetch.HolafFetchError;
    }

    // Erreur réseau "pure" (fetch rejeté) — PAS un timeout (message
    // « timeout »), PAS une erreur HTTP typée (4xx/5xx).
    function isNetworkError(err) {
        return isHolafError(err) && err.status === 0 && err.message === "erreur réseau";
    }

    // ─── Adaptateur fin : request (lève HolafFetchError) ─────────────────────
    async function request(url, opts) {
        opts = opts || {};
        if (typeof HolafFetch === "undefined") {
            // Brique pas encore chargée (module différé) : erreur claire.
            var err = new Error("DockyFetch: HolafFetch indisponible");
            err.status = 0;
            throw err;
        }
        var cfg = buildConfig(opts);
        try {
            return await HolafFetch.request(url, cfg);
        } catch (err) {
            // Retry réseau ×1, 500 ms — méthodes sûres uniquement, jamais
            // sur une annulation explicite (signal déjà aborted).
            var aborted = !!(opts.signal && opts.signal.aborted);
            if (!aborted && isSafeMethod(opts.method) && isNetworkError(err)) {
                console.warn("DockyFetch: network error on "
                    + String(opts.method || "GET").toUpperCase()
                    + " " + url + ", retrying once in " + RETRY_DELAY_MS + "ms");
                await sleep(RETRY_DELAY_MS);
                var data = await HolafFetch.request(url, cfg);
                console.warn("DockyFetch: retry succeeded");
                return data;
            }
            throw err;
        }
    }

    // ─── Contrat Docky : apiRequest (data | null, jamais throw) ──────────────
    // Utilisé par apiFetch/apiPost (api.js) et par l'apiFetch de settings.js.
    //
    // Règle canonique « une seule notification par erreur » : l'adaptateur
    // affiche le toast d'erreur PAR DÉFAUT. Deux options permettent à un
    // appelant de reprendre la main sans créer de double-toast :
    //   opts.silent        (bool)  — supprime le toast de l'adaptateur ;
    //                                l'appelant affiche son propre message
    //                                (plus précis / contextuel) sur data === null.
    //   opts.errorMessage  (string) — remplace le message générique de
    //                                l'adaptateur par un message précis.
    // Les deux sont mutuellement exclusifs (errorMessage prime sur silent).
    async function apiRequest(url, opts) {
        try {
            return await request(url, opts);
        } catch (err) {
            // 401 : la redirection /login est déjà déclenchée par le hook
            // on.status — retour silencieux (comportement historique).
            if (isHolafError(err) && err.status === 401) return null;
            console.error("API error:", err);
            // Message : opts.errorMessage (précis) sinon le message dérivé de
            // l'erreur (err.message contient body.detail via la brique v0.1.1 ;
            // repli err.data.detail).
            var message = (opts && opts.errorMessage)
                ? String(opts.errorMessage)
                : messageFromError(err);
            var silent = !!(opts && opts.silent);
            if (!silent) {
                // Toast avec le message. Fail-safe : si la brique toast n'est
                // pas chargée, console seulement.
                if (window.DockyToast && typeof window.DockyToast.show === "function") {
                    window.DockyToast.show(message, "error");
                } else {
                    console.error("DockyFetch: toast indisponible —", message);
                }
            }
            return null;
        }
    }

    window.DockyFetch = {
        version: "0.1.0",
        DEFAULT_TIMEOUT: DEFAULT_TIMEOUT,
        RETRY_DELAY_MS: RETRY_DELAY_MS,
        request: request,
        apiRequest: apiRequest,
    };
})();