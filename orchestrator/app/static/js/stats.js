/* ============================================================
   Docky - Frontend JavaScript - module stats (LOT 3)
   ------------------------------------------------------------
   Streaming de stats temps réel côté navigateur.

   Consomme la WebSocket ``/api/stats/stream`` (cookie JWT, même
   origine) exposée par l'orchestrateur (LOT 2) :

     client -> serveur : {"type":"subscribe",   "targets":[{"agent":…,"container":…}]}
                        {"type":"unsubscribe", "targets":[…]}
     serveur -> client : {"type":"snapshot","samples":[…]}
                        {"type":"sample","sample":{…}}
     types inconnus ignorés.

   Contrat public (window.DockyStats) :

     DockyStats.subscribe(agent, container)
     DockyStats.unsubscribe(agent, container)
     DockyStats.onSample(cb)        -> fonction de désinscription
     DockyStats.getLast(agent, container)
     DockyStats.setInterval(ms)     -> 1000 | 2000 | 5000 | 0 (pause)
     DockyStats.pause() / DockyStats.resume()
     DockyStats.isConnected()
     DockyStats.openGraph(tileEl)   -> modale graphique (clic tuile)
     DockyStats.reconcile()         -> ré-appariement tuiles visibles / cibles
     DockyStats.getStatus()         -> état interne (diagnostic / tests)

   Responsabilités :
     - connexion WS + reconnexion avec backoff 1 s -> 30 s ;
     - abonnements par tuile visible (IntersectionObserver + refcount) ;
     - pause onglet caché (visibilitychange) et intervalle « Pause » ;
     - jauges live (CPU normalisé 0-100 hôte, RAM, réseau) + sparklines SVG ;
     - vue graphique (modale HolafModal) 15 min / 1 h / 24 h ;
     - repli sur le polling existant (loadContainerStats) si la WS échoue,
       avec indicateur discret.

   Aucune dépendance externe : sparklines et graphiques sont dessinés en
   SVG fait main (projet vanilla, pas de bundler).

   Ce module est un script CLASSIQUE (non module). Il doit être chargé
   APRÈS dashboard.js et AVANT DOMContentLoaded.
   ============================================================ */

/* global HolafModal */

(function () {
    "use strict";

    // ------------------------------------------------------------------
    // Constantes
    // ------------------------------------------------------------------
    var DEFAULT_INTERVAL = 2000;
    var ALLOWED_INTERVALS = [1000, 2000, 5000];
    var STORAGE_KEY = "docky-stats-interval";
    var HISTORY_WINDOW = 900;          // sparklines : 15 min
    var SPARK_POINTS = 40;             // ~40 points par sparkline
    var HISTORY_KEEP_MS = 15 * 60 * 1000;
    var BACKOFF_MIN = 1000;
    var BACKOFF_MAX = 30000;
    var FAIL_THRESHOLD = 3;            // -> bascule indicateur « polling »
    var BATCH_DELAY = 50;              // debounce d'envoi subscribe/unsubscribe
    var SEP = "\u0000";                // séparateur clé composite
    var CPU_COLOR = "#e94560";
    var RAM_COLOR = "#38bdf8";

    // ------------------------------------------------------------------
    // État
    // ------------------------------------------------------------------
    var inited = false;

    var ws = null;
    var connected = false;
    var backoff = BACKOFF_MIN;
    var failCount = 0;
    var fallback = false;
    var reconnectTimer = null;

    var intervalMs = DEFAULT_INTERVAL;
    var lastNonZeroInterval = DEFAULT_INTERVAL;
    var hidden = false;

    var targets = new Map();     // key -> rec (dernier échantillon + historique)
    var explicit = new Map();    // key -> refcount (API subscribe/unsubscribe)
    var visible = new Map();     // key -> refcount (tuiles visibles)
    var subscribed = new Set();  // cibles effectivement souscrites côté serveur

    var pendingAdd = new Set();
    var pendingRemove = new Set();
    var batchTimer = null;

    var handlers = [];           // callbacks onSample
    var observed = new Map();    // element -> { key, agent, container, status, visible }
    var observer = null;
    var mutationObs = null;
    var mutationTimer = null;

    var gradientSeq = 0;
    var graphCtrl = null;        // contrôleur HolafModal de la vue graphique
    var reconcileCount = 0;      // diagnostic : détecte une boucle de reconcile

    // ------------------------------------------------------------------
    // Petits helpers
    // ------------------------------------------------------------------

    function keyOf(agent, container) {
        return String(agent || "") + SEP + String(container || "");
    }

    function splitKey(key) {
        var idx = key.indexOf(SEP);
        if (idx < 0) return { agent: "", container: key };
        return { agent: key.slice(0, idx), container: key.slice(idx + 1) };
    }

    function clampPct(v) {
        var n = Number(v);
        if (!isFinite(n)) return 0;
        return Math.max(0, Math.min(100, n));
    }

    function formatBytes(bytes) {
        if (window.DockyApp && typeof window.DockyApp.formatBytes === "function") {
            return window.DockyApp.formatBytes(bytes);
        }
        if (!bytes || bytes <= 0) return "0 B";
        var units = ["B", "KB", "MB", "GB", "TB"];
        var i = 0, val = bytes;
        while (val >= 1024 && i < units.length - 1) { val /= 1024; i++; }
        return val.toFixed(i > 0 ? 1 : 0) + " " + units[i];
    }

    function formatRate(bytesPerSec) {
        if (bytesPerSec === null || bytesPerSec === undefined) return "—";
        return formatBytes(Math.round(bytesPerSec)) + "/s";
    }

    function pad2(n) { return (n < 10 ? "0" : "") + n; }

    function formatClock(ts) {
        var d = new Date(ts);
        return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    }

    function formatDateTime(ts) {
        var d = new Date(ts);
        return pad2(d.getDate()) + "/" + pad2(d.getMonth() + 1) + " " +
            pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
    }

    function windowLabel(win) {
        if (win === 3600) return "1 h";
        if (win === 86400) return "24 h";
        return "15 min";
    }

    function pageHidden() {
        return typeof document !== "undefined" && document.hidden === true;
    }

    function isActive() {
        return !hidden && intervalMs > 0;
    }

    function historyUrl(container, agent, win) {
        return "/api/containers/" + encodeURIComponent(container) +
            "/stats/history?agent=" + encodeURIComponent(agent || "") +
            "&window=" + encodeURIComponent(String(win));
    }

    function requestJson(url, opts) {
        if (window.DockyFetch && typeof window.DockyFetch.request === "function") {
            return window.DockyFetch.request(url, opts || { timeout: 10000 });
        }
        return fetch(url, opts || {}).then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
        });
    }

    // ------------------------------------------------------------------
    // Cibles / historique
    // ------------------------------------------------------------------

    function targetRec(agent, container) {
        var key = keyOf(agent, container);
        var rec = targets.get(key);
        if (!rec) {
            rec = {
                key: key,
                agent: String(agent || ""),
                container: String(container || ""),
                last: null,
                lastRenderWall: 0,
                history: [],
                historyRequested: false,
                net: { rx: null, tx: null },
            };
            targets.set(key, rec);
        }
        return rec;
    }

    function cpuCountFor(rec) {
        if (rec.last && Number(rec.last.cpu_count) > 0) return Number(rec.last.cpu_count);
        var cache = window.DockyApp && window.DockyApp._statsCache;
        if (cache && cache[rec.container] && Number(cache[rec.container].cpu_count) > 0) {
            return Number(cache[rec.container].cpu_count);
        }
        return 1;
    }

    function toPoint(raw, cpuCount) {
        return {
            ts: Number(raw.ts) || Date.now(),
            cpu: clampPct((Number(raw.cpu_percent) || 0) / cpuCount),
            cpuRaw: Number(raw.cpu_percent) || 0,
            ram: clampPct(raw.mem_percent),
        };
    }

    function trimHistory(rec) {
        if (rec.history.length <= 1) return;
        var lastTs = rec.history[rec.history.length - 1].ts;
        var minTs = lastTs - HISTORY_KEEP_MS;
        var start = 0;
        while (start < rec.history.length - 1 && rec.history[start].ts < minTs) start++;
        if (start > 0) rec.history.splice(0, start);
    }

    function ensureHistory(rec) {
        if (rec.historyRequested) return;
        rec.historyRequested = true;
        var win = HISTORY_WINDOW;
        requestJson(historyUrl(rec.container, rec.agent, win))
            .then(function (data) {
                if (!data || !Array.isArray(data.points)) return;
                var cpuCount = cpuCountFor(rec);
                var points = data.points
                    .filter(function (p) { return p && p.ts != null; })
                    .map(function (p) { return toPoint(p, cpuCount); });
                // Fusion avec les points live déjà reçus (le plus récent gagne).
                var byTs = new Map();
                points.forEach(function (p) { byTs.set(p.ts, p); });
                rec.history.forEach(function (p) { byTs.set(p.ts, p); });
                rec.history = Array.from(byTs.values()).sort(function (a, b) {
                    return a.ts - b.ts;
                });
                renderSpark(rec);
            })
            .catch(function () { /* historique best-effort */ });
    }

    // ------------------------------------------------------------------
    // Abonnements (diff visible / explicite -> batch WS)
    // ------------------------------------------------------------------

    function addCount(map, key) {
        map.set(key, (map.get(key) || 0) + 1);
        return map.get(key);
    }

    function decCount(map, key) {
        var n = (map.get(key) || 0) - 1;
        if (n <= 0) map.delete(key);
        else map.set(key, n);
    }

    function desiredKeys() {
        if (!isActive()) return [];
        var out = new Set();
        explicit.forEach(function (n, k) { if (n > 0) out.add(k); });
        visible.forEach(function (n, k) { if (n > 0) out.add(k); });
        return Array.from(out);
    }

    function applySubscriptions() {
        var desired = new Set(desiredKeys());
        desired.forEach(function (k) {
            if (!subscribed.has(k)) {
                subscribed.add(k);
                pendingAdd.add(k);
                pendingRemove.delete(k);
                var parts = splitKey(k);
                ensureHistory(targetRec(parts.agent, parts.container));
            }
        });
        Array.from(subscribed).forEach(function (k) {
            if (!desired.has(k)) {
                subscribed.delete(k);
                pendingRemove.add(k);
                pendingAdd.delete(k);
            }
        });
        scheduleFlush();
    }

    function scheduleFlush() {
        if (batchTimer) return;
        batchTimer = window.setTimeout(flushBatch, BATCH_DELAY);
    }

    function targetObjFor(key) {
        var p = splitKey(key);
        return { agent: p.agent, container: p.container };
    }

    function flushBatch() {
        batchTimer = null;
        if (!connected) return; // le rattrapage se fait à l'ouverture (full send)
        var adds = Array.from(pendingAdd);
        var removes = Array.from(pendingRemove);
        pendingAdd.clear();
        pendingRemove.clear();
        if (adds.length) send("subscribe", adds.map(targetObjFor));
        if (removes.length) send("unsubscribe", removes.map(targetObjFor));
    }

    function send(type, targetList) {
        if (!ws || ws.readyState !== 1) return;
        try {
            ws.send(JSON.stringify({ type: type, targets: targetList }));
        } catch (e) { /* ignore */ }
    }

    function sendFullSubscriptions() {
        pendingAdd.clear();
        pendingRemove.clear();
        var desired = desiredKeys();
        if (!desired.length) return;
        // Découpage en lots raisonnables (le serveur tolère de grandes listes).
        var CHUNK = 100;
        for (var i = 0; i < desired.length; i += CHUNK) {
            send("subscribe", desired.slice(i, i + CHUNK).map(targetObjFor));
        }
    }

    // ------------------------------------------------------------------
    // WebSocket : connexion / reconnexion / réception
    // ------------------------------------------------------------------

    function wsUrl() {
        var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        return proto + "//" + window.location.host + "/api/stats/stream";
    }

    function connect() {
        if (hidden) return;
        if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
        var socket;
        try {
            socket = new WebSocket(wsUrl());
        } catch (e) {
            scheduleReconnect();
            return;
        }
        ws = socket;

        socket.onopen = function () {
            if (ws !== socket) return;
            connected = true;
            fallback = false;
            failCount = 0;
            backoff = BACKOFF_MIN;
            // Marque les cibles désirées comme souscrites AVANT l'envoi groupé
            // pour ne pas envoyer un second subscribe via le batch.
            subscribed.clear();
            pendingAdd.clear();
            pendingRemove.clear();
            if (batchTimer) { window.clearTimeout(batchTimer); batchTimer = null; }
            desiredKeys().forEach(function (k) {
                subscribed.add(k);
                var p = splitKey(k);
                ensureHistory(targetRec(p.agent, p.container));
            });
            sendFullSubscriptions();
            updateIndicator();
        };
        socket.onmessage = function (ev) {
            if (ws !== socket) return;
            onMessage(ev);
        };
        socket.onerror = function () { /* onclose enchaîne */ };
        socket.onclose = function () {
            if (ws !== socket) return;
            ws = null;
            onClose();
        };
    }

    function scheduleReconnect() {
        if (reconnectTimer || hidden) return;
        var delay = Math.max(BACKOFF_MIN, Math.min(BACKOFF_MAX, backoff));
        backoff = Math.min(BACKOFF_MAX, backoff * 2);
        reconnectTimer = window.setTimeout(function () {
            reconnectTimer = null;
            connect();
        }, delay);
    }

    function onClose() {
        connected = false;
        subscribed.clear();
        pendingAdd.clear();
        pendingRemove.clear();
        if (batchTimer) { window.clearTimeout(batchTimer); batchTimer = null; }
        failCount++;
        if (failCount >= FAIL_THRESHOLD) fallback = true;
        updateIndicator();
        scheduleReconnect();
    }

    function onMessage(ev) {
        var data;
        try { data = JSON.parse(ev.data); } catch (e) { return; }
        if (!data || typeof data !== "object") return;
        if (data.type === "snapshot" && Array.isArray(data.samples)) {
            data.samples.forEach(handleSample);
        } else if (data.type === "sample" && data.sample) {
            handleSample(data.sample);
        }
        // Types inconnus ignorés (extensibilité).
    }

    function closeWs() {
        if (reconnectTimer) { window.clearTimeout(reconnectTimer); reconnectTimer = null; }
        if (ws) {
            // Le garde ``ws !== socket`` dans onclose empêche toute reconnexion
            // pour ce socket remplacé (fermeture volontaire).
            var socket = ws;
            ws = null;
            try { socket.close(); } catch (e) { /* ignore */ }
        }
        connected = false;
        subscribed.clear();
        pendingAdd.clear();
        pendingRemove.clear();
        if (batchTimer) { window.clearTimeout(batchTimer); batchTimer = null; }
        updateIndicator();
    }

    // ------------------------------------------------------------------
    // Ingestion d'un échantillon
    // ------------------------------------------------------------------

    function handleSample(sample) {
        if (!sample || sample.id === undefined) return;
        var sid = String(sample.id);
        var sname = sample.name != null ? String(sample.name) : "";
        var recs = [];
        subscribed.forEach(function (k) {
            var p = splitKey(k);
            if (p.container === sid || (sname && p.container === sname)) {
                recs.push(targetRec(p.agent, p.container));
            }
        });
        if (!recs.length) return;
        recs.forEach(function (rec) { ingest(rec, sample); });
    }

    function ingest(rec, sample) {
        var prev = rec.last;
        var cpuCount = Number(sample.cpu_count) > 0 ? Number(sample.cpu_count) : cpuCountFor(rec);
        var pt = toPoint(sample, cpuCount);
        rec.last = sample;
        rec.history.push(pt);
        trimHistory(rec);

        // Débit réseau : les compteurs de l'échantillon sont cumulatifs.
        if (prev && prev.ts != null && sample.ts != null && sample.ts > prev.ts) {
            var dt = (sample.ts - prev.ts) / 1000;
            if (dt > 0) {
                rec.net.rx = Math.max(0, (Number(sample.net_rx) || 0) - (Number(prev.net_rx) || 0)) / dt;
                rec.net.tx = Math.max(0, (Number(sample.net_tx) || 0) - (Number(prev.net_tx) || 0)) / dt;
            }
        }

        // Throttle de rendu selon l'intervalle choisi (le 1er passe toujours).
        var now = Date.now();
        var due = rec.lastRenderWall === 0 || (intervalMs > 0 && now - rec.lastRenderWall >= intervalMs);
        if (!due) return;
        rec.lastRenderWall = now;

        applyLive(rec, sample, pt, cpuCount);
        renderSpark(rec);
        notify(rec, sample, pt);
    }

    function applyLive(rec, sample, pt, cpuCount) {
        var cache = window.DockyApp && window.DockyApp._statsCache;
        if (cache) {
            cache[rec.container] = {
                cpu_percent: Number(sample.cpu_percent) || 0,
                cpu_count: cpuCount,
                mem_usage: Number(sample.mem_usage) || 0,
                mem_limit: Number(sample.mem_limit) || 0,
                mem_percent: Number(sample.mem_percent) || 0,
                _ts: Date.now(),
                _live: true,
            };
        }
        if (window.DockyApp && typeof window.DockyApp.applyStatsDom === "function") {
            window.DockyApp.applyStatsDom(rec.container, {
                cpuNorm: pt.cpu,
                cpuRaw: pt.cpuRaw,
                cpuCount: cpuCount,
                memUsage: sample.mem_usage,
                memLimit: sample.mem_limit,
                memPercent: pt.ram,
            });
        }
        updateNetDisplay(rec);
        updateGaugeTitle(rec, sample, cpuCount);
    }

    function updateGaugeTitle(rec, sample, cpuCount) {
        var tiles = tileElements(rec.key);
        var title = "CPU " + (Number(sample.cpu_percent) || 0).toFixed(1) +
            "% brut · " + cpuCount + " cœur" + (cpuCount > 1 ? "s" : "") +
            " · RAM " + clampPct(sample.mem_percent).toFixed(1) + "%" +
            " · rx " + formatRate(rec.net.rx) + " tx " + formatRate(rec.net.tx);
        tiles.forEach(function (el) {
            var res = resourcesOf(el);
            if (res) res.title = title;
        });
    }

    function notify(rec, sample, pt) {
        if (!handlers.length) return;
        var evt = { agent: rec.agent, container: rec.container, sample: sample, point: pt };
        handlers.slice().forEach(function (cb) {
            try { cb(evt); } catch (e) { console.error("[DockyStats] onSample", e); }
        });
    }

    // ------------------------------------------------------------------
    // Tuiles : ressources, réseau, sparklines
    // ------------------------------------------------------------------

    var RES_SELECTOR = ".grid-card-resources, .container-resources, .table-row-resources";

    function resourcesOf(tileEl) {
        if (!tileEl || !tileEl.querySelector) return null;
        return tileEl.querySelector(RES_SELECTOR);
    }

    function tileElements(key) {
        var out = [];
        observed.forEach(function (er, el) {
            if (er.key === key && el.isConnected) out.push(el);
        });
        return out;
    }

    function ensureLiveExtras(tileEl) {
        if (!tileEl || tileEl._dockyLive) return tileEl && tileEl._dockyLive;
        var res = resourcesOf(tileEl);
        if (!res) return null;
        var box = document.createElement("div");
        box.className = "docky-live";
        box.onclick = function (e) { e.stopPropagation(); };

        var net = document.createElement("div");
        net.className = "docky-net";
        var rx = document.createElement("span");
        rx.className = "docky-net-item docky-net-rx";
        rx.textContent = "↓ —";
        var tx = document.createElement("span");
        tx.className = "docky-net-item docky-net-tx";
        tx.textContent = "↑ —";
        net.appendChild(rx);
        net.appendChild(tx);

        var sparkCpu = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        sparkCpu.setAttribute("class", "docky-spark docky-spark-cpu");
        sparkCpu.setAttribute("viewBox", "0 0 100 20");
        sparkCpu.setAttribute("preserveAspectRatio", "none");
        sparkCpu.setAttribute("aria-hidden", "true");

        var sparkRam = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        sparkRam.setAttribute("class", "docky-spark docky-spark-ram");
        sparkRam.setAttribute("viewBox", "0 0 100 20");
        sparkRam.setAttribute("preserveAspectRatio", "none");
        sparkRam.setAttribute("aria-hidden", "true");

        box.appendChild(net);
        box.appendChild(sparkCpu);
        box.appendChild(sparkRam);
        res.appendChild(box);
        tileEl._dockyLive = { box: box, rx: rx, tx: tx, cpu: sparkCpu, ram: sparkRam };
        return tileEl._dockyLive;
    }

    function updateNetDisplay(rec) {
        tileElements(rec.key).forEach(function (el) {
            var live = ensureLiveExtras(el);
            if (!live) return;
            live.rx.textContent = "↓ " + formatRate(rec.net.rx);
            live.tx.textContent = "↑ " + formatRate(rec.net.tx);
        });
    }

    function sparkPaths(values) {
        var n = values.length;
        if (n < 2) return { line: "", area: "" };
        var w = 100, h = 20;
        var stepX = w / (n - 1);
        var pts = values.map(function (v, i) {
            var y = h - 1 - (clampPct(v) / 100) * (h - 2);
            return [i * stepX, y];
        });
        var line = pts.map(function (p) { return p[0].toFixed(2) + "," + p[1].toFixed(2); }).join(" ");
        var area = "0," + h + " " + line + " " + w + "," + h;
        return { line: line, area: area };
    }

    function paintSpark(svg, values, color) {
        if (!svg) return;
        var tail = values.slice(-SPARK_POINTS);
        var paths = sparkPaths(tail);
        if (!paths.line) {
            svg.innerHTML = "";
            return;
        }
        var gid = "docky-spark-grad-" + (++gradientSeq);
        svg.innerHTML =
            '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
            '<stop offset="0%" stop-color="' + color + '" stop-opacity="0.35"/>' +
            '<stop offset="100%" stop-color="' + color + '" stop-opacity="0"/>' +
            "</linearGradient></defs>" +
            '<polygon points="' + paths.area + '" fill="url(#' + gid + ')"/>' +
            '<polyline points="' + paths.line + '" fill="none" stroke="' + color +
            '" stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>';
    }

    function renderSpark(rec) {
        if (!rec.history.length) return;
        var cpuValues = rec.history.map(function (p) { return p.cpu; });
        var ramValues = rec.history.map(function (p) { return p.ram; });
        tileElements(rec.key).forEach(function (el) {
            var live = ensureLiveExtras(el);
            if (!live) return;
            paintSpark(live.cpu, cpuValues, CPU_COLOR);
            paintSpark(live.ram, ramValues, RAM_COLOR);
        });
    }

    // ------------------------------------------------------------------
    // Abonnement par tuile visible (IntersectionObserver + reconcile)
    // ------------------------------------------------------------------

    function statusWatchable(status) {
        var s = String(status || "running").toLowerCase();
        return s !== "exited" && s !== "stopped" && s !== "dead" && s !== "created";
    }

    function inViewport(el) {
        var m = 100;
        var r = el.getBoundingClientRect();
        var vh = window.innerHeight || document.documentElement.clientHeight;
        var vw = window.innerWidth || document.documentElement.clientWidth;
        if (!(r.bottom > -m && r.top < vh + m && r.right > -m && r.left < vw + m)) return false;
        // Le conteneur de tuiles (#dashboard-content = .panel-body) défile en
        // interne : intersecter aussi sa boîte visible, comme le fait
        // l'IntersectionObserver (root viewport, clipping par l'ancêtre).
        var root = document.getElementById("dashboard-content");
        if (root && root !== el && root.contains(el)) {
            var rr = root.getBoundingClientRect();
            if (!(r.bottom > rr.top - m && r.top < rr.bottom + m &&
                  r.right > rr.left - m && r.left < rr.right + m)) return false;
        }
        return true;
    }

    function ensureObserver() {
        if (observer || typeof IntersectionObserver === "undefined") return;
        observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (en) {
                var er = observed.get(en.target);
                if (!er) return;
                if (en.isIntersecting && !er.visible) {
                    if (!statusWatchable(er.status)) return;
                    er.visible = true;
                    addCount(visible, er.key);
                    ensureHistory(targetRec(er.agent, er.container));
                } else if (!en.isIntersecting && er.visible) {
                    er.visible = false;
                    decCount(visible, er.key);
                }
            });
            applySubscriptions();
        }, { root: null, rootMargin: "100px", threshold: 0 });
    }

    function reconcile() {
        var root = document.getElementById("dashboard-content");
        if (!root) return;
        reconcileCount++;
        ensureObserver();
        var tiles = Array.prototype.slice.call(root.querySelectorAll("[data-container]"));
        var current = new Set(tiles);

        // Retirer les tuiles disparues (re-rendu) ou détachées du DOM.
        Array.from(observed.keys()).forEach(function (el) {
            if (!el.isConnected || !current.has(el)) {
                var er = observed.get(el);
                if (observer) observer.unobserve(el);
                observed.delete(el);
                if (er && er.visible) { er.visible = false; decCount(visible, er.key); }
            }
        });

        // Observer les nouvelles tuiles et calculer leur visibilité d'emblée
        // (évite un désabonnement/réabonnement à chaque re-rendu 5 s).
        tiles.forEach(function (el) {
            if (observed.has(el)) return;
            var container = el.dataset.container;
            if (!container) return;
            var agent = el.dataset.agent || "";
            var key = keyOf(agent, container);
            var er = {
                key: key,
                agent: agent,
                container: container,
                status: el.dataset.status || "running",
                visible: false,
            };
            observed.set(el, er);
            if (observer) observer.observe(el);
            if (statusWatchable(er.status) && inViewport(el)) {
                er.visible = true;
                addCount(visible, key);
                ensureHistory(targetRec(agent, container));
            }
        });

        applySubscriptions();
        // Réaffiche les sparklines (les nouveaux éléments SVG sont vides).
        observed.forEach(function (er) {
            var rec = targets.get(er.key);
            if (rec && rec.history.length) renderSpark(rec);
        });
    }

    function bindVisibilityObserver() {
        if (typeof document === "undefined") return;
        document.addEventListener("visibilitychange", function () {
            hidden = pageHidden();
            if (hidden) {
                // Onglet caché : libère tout (unsubscribe explicite) puis coupe
                // le flux. La fermeture libère aussi côté serveur.
                applySubscriptions();
                flushBatch();
                closeWs();
            } else {
                connect();
                reconcile();
            }
            updateIndicator();
        });
    }

    function bindMutationObserver() {
        var root = document.getElementById("dashboard-content");
        if (!root || typeof MutationObserver === "undefined") return;
        mutationObs = new MutationObserver(function () {
            if (mutationTimer) window.clearTimeout(mutationTimer);
            mutationTimer = window.setTimeout(function () {
                mutationTimer = null;
                reconcile();
            }, 100);
        });
        // ``subtree: false`` : seul le remplacement direct des enfants de
        // #dashboard-content (chaque rendu fait ``innerHTML = …``) déclenche une
        // réconciliation. On ignore ainsi les mutations internes générées par
        // DockyStats lui-même (ajout de .docky-live, repaint SVG) qui, sinon,
        // boucleraient observer → reconcile → repaint → observer.
        mutationObs.observe(root, { childList: true, subtree: false });
    }

    function bindGraphClick() {
        // Capture : intercepte le clic sur la zone stats AVANT le handler de
        // sélection de la tuile (onclick inline) — ouvre la vue graphique.
        document.addEventListener("click", function (e) {
            var zone = e.target && e.target.closest ? e.target.closest(RES_SELECTOR) : null;
            if (!zone) return;
            var tile = zone.closest("[data-container]");
            if (!tile) return;
            e.preventDefault();
            e.stopPropagation();
            openGraph(tile);
        }, true);
    }

    // ------------------------------------------------------------------
    // Vue graphique (HolafModal + SVG fait main)
    // ------------------------------------------------------------------

    function svgEl(name, attrs) {
        var el = document.createElementNS("http://www.w3.org/2000/svg", name);
        Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
        return el;
    }

    function buildGraphContent(rec) {
        var wrap = document.createElement("div");
        wrap.className = "docky-graph";

        var head = document.createElement("div");
        head.className = "docky-graph-head";

        var legend = document.createElement("div");
        legend.className = "docky-graph-legend";
        var cpuLegend = document.createElement("span");
        cpuLegend.className = "docky-graph-legend-item docky-graph-legend-cpu";
        var ramLegend = document.createElement("span");
        ramLegend.className = "docky-graph-legend-item docky-graph-legend-ram";
        legend.appendChild(cpuLegend);
        legend.appendChild(ramLegend);

        var select = document.createElement("select");
        select.className = "docky-graph-window";
        [900, 3600, 86400].forEach(function (w) {
            var opt = document.createElement("option");
            opt.value = String(w);
            opt.textContent = windowLabel(w);
            select.appendChild(opt);
        });
        select.value = String(HISTORY_WINDOW);

        head.appendChild(legend);
        head.appendChild(select);

        var plot = document.createElement("div");
        plot.className = "docky-graph-plot";
        var svg = svgEl("svg", {
            class: "docky-graph-svg",
            viewBox: "0 0 640 240",
            preserveAspectRatio: "none",
        });
        var tip = document.createElement("div");
        tip.className = "docky-graph-tip hidden";
        plot.appendChild(svg);
        plot.appendChild(tip);

        var state = document.createElement("div");
        state.className = "docky-graph-state";

        wrap.appendChild(head);
        wrap.appendChild(plot);
        wrap.appendChild(state);

        return {
            wrap: wrap,
            select: select,
            svg: svg,
            tip: tip,
            cpuLegend: cpuLegend,
            ramLegend: ramLegend,
            state: state,
        };
    }

    function seriesStats(points, field) {
        var vals = points.map(function (p) { return p[field]; }).filter(function (v) {
            return isFinite(v);
        });
        if (!vals.length) return { min: 0, avg: 0, max: 0 };
        var min = Math.min.apply(null, vals);
        var max = Math.max.apply(null, vals);
        var sum = vals.reduce(function (a, b) { return a + b; }, 0);
        return { min: min, avg: sum / vals.length, max: max };
    }

    function drawGraph(ui, points) {
        var W = 640, H = 240;
        var padL = 36, padR = 12, padT = 12, padB = 24;
        var plotW = W - padL - padR;
        var plotH = H - padT - padB;
        var svg = ui.svg;
        svg.innerHTML = "";

        // Grille horizontale (0, 25, 50, 75, 100 %).
        [0, 25, 50, 75, 100].forEach(function (v) {
            var y = padT + plotH - (v / 100) * plotH;
            svg.appendChild(svgEl("line", {
                x1: padL, y1: y, x2: W - padR, y2: y,
                stroke: "rgba(255,255,255,0.08)", "stroke-width": "1",
            }));
            var label = svgEl("text", {
                x: padL - 6, y: y + 3, "text-anchor": "end",
                "font-size": "9", fill: "#7e7e9e",
            });
            label.textContent = v + "%";
            svg.appendChild(label);
        });

        if (points.length < 2) {
            var empty = svgEl("text", {
                x: W / 2, y: H / 2, "text-anchor": "middle",
                "font-size": "12", fill: "#7e7e9e",
            });
            empty.textContent = "Pas encore de données sur cette fenêtre";
            svg.appendChild(empty);
            ui.cpuLegend.textContent = "CPU % —";
            ui.ramLegend.textContent = "RAM % —";
            return;
        }

        var t0 = points[0].ts;
        var t1 = points[points.length - 1].ts;
        var span = Math.max(1, t1 - t0);
        function xOf(ts) { return padL + ((ts - t0) / span) * plotW; }
        function yOf(v) { return padT + plotH - (clampPct(v) / 100) * plotH; }

        // Grille verticale + libellés d'heure (~5 repères).
        var ticks = 4;
        for (var i = 0; i <= ticks; i++) {
            var ts = t0 + (span * i) / ticks;
            var x = xOf(ts);
            svg.appendChild(svgEl("line", {
                x1: x, y1: padT, x2: x, y2: padT + plotH,
                stroke: "rgba(255,255,255,0.05)", "stroke-width": "1",
            }));
            var t = svgEl("text", {
                x: x, y: H - 8, "text-anchor": "middle",
                "font-size": "9", fill: "#7e7e9e",
            });
            t.textContent = formatClock(ts);
            svg.appendChild(t);
        }

        function drawSeries(field, color) {
            var line = points.map(function (p) {
                return xOf(p.ts).toFixed(1) + "," + yOf(p[field]).toFixed(1);
            }).join(" ");
            var areaPts = padL + "," + (padT + plotH) + " " + line + " " +
                (W - padR) + "," + (padT + plotH);
            svg.appendChild(svgEl("polygon", {
                points: areaPts, fill: color, "fill-opacity": "0.10",
            }));
            svg.appendChild(svgEl("polyline", {
                points: line, fill: "none", stroke: color, "stroke-width": "1.6",
                "stroke-linejoin": "round", "stroke-linecap": "round",
            }));
        }

        drawSeries("ram", RAM_COLOR);
        drawSeries("cpu", CPU_COLOR);

        var cpuStats = seriesStats(points, "cpu");
        var ramStats = seriesStats(points, "ram");
        ui.cpuLegend.textContent = "CPU % · min " + cpuStats.min.toFixed(1) +
            " · moy " + cpuStats.avg.toFixed(1) + " · max " + cpuStats.max.toFixed(1);
        ui.ramLegend.textContent = "RAM % · min " + ramStats.min.toFixed(1) +
            " · moy " + ramStats.avg.toFixed(1) + " · max " + ramStats.max.toFixed(1);

        // Tooltip au survol (repère vertical + valeurs du point le plus proche).
        var hover = svgEl("line", {
            x1: 0, y1: padT, x2: 0, y2: padT + plotH,
            stroke: "rgba(255,255,255,0.35)", "stroke-width": "1",
            visibility: "hidden",
        });
        svg.appendChild(hover);

        function onMove(ev) {
            var rect = svg.getBoundingClientRect();
            if (!rect.width) return;
            var px = ((ev.clientX - rect.left) / rect.width) * W;
            var ratio = Math.max(0, Math.min(1, (px - padL) / plotW));
            var targetTs = t0 + ratio * span;
            var best = points[0];
            for (var j = 1; j < points.length; j++) {
                if (Math.abs(points[j].ts - targetTs) < Math.abs(best.ts - targetTs)) best = points[j];
            }
            var hx = xOf(best.ts);
            hover.setAttribute("x1", hx);
            hover.setAttribute("x2", hx);
            hover.setAttribute("visibility", "visible");
            ui.tip.classList.remove("hidden");
            ui.tip.innerHTML =
                "<b>" + formatDateTime(best.ts) + "</b>" +
                "<span>CPU " + best.cpu.toFixed(1) + "%</span>" +
                "<span>RAM " + best.ram.toFixed(1) + "%</span>";
            var leftPct = (hx / W) * 100;
            ui.tip.style.left = Math.max(0, Math.min(80, leftPct)) + "%";
        }

        function onLeave() {
            hover.setAttribute("visibility", "hidden");
            ui.tip.classList.add("hidden");
        }

        svg.onmousemove = onMove;
        svg.onmouseleave = onLeave;
    }

    function openGraph(tileEl) {
        if (!tileEl) return;
        var agent = tileEl.dataset.agent || "";
        var container = tileEl.dataset.container;
        if (!container) return;
        if (typeof HolafModal === "undefined" || !HolafModal || typeof HolafModal.open !== "function") {
            return;
        }
        var rec = targetRec(agent, container);

        var ui = buildGraphContent(rec);
        var gstate = { win: HISTORY_WINDOW, points: [], handler: null, redrawTimer: null };

        function load(win) {
            gstate.win = win;
            rec.historyRequested = true;
            ui.state.textContent = "Chargement de l'historique…";
            requestJson(historyUrl(container, agent, win))
                .then(function (data) {
                    var cpuCount = cpuCountFor(rec);
                    gstate.points = (data && Array.isArray(data.points) ? data.points : [])
                        .filter(function (p) { return p && p.ts != null; })
                        .map(function (p) { return toPoint(p, cpuCount); });
                    mergeLiveTail();
                    ui.state.textContent = gstate.points.length
                        ? windowLabel(win) + " · " + gstate.points.length + " points"
                        : "Aucune donnée sur cette fenêtre";
                    drawGraph(ui, gstate.points);
                })
                .catch(function () {
                    ui.state.textContent = "Historique indisponible (agent injoignable ?)";
                    drawGraph(ui, gstate.points);
                });
        }

        function mergeLiveTail() {
            if (!rec.last) return;
            var pt = toPoint(rec.last, cpuCountFor(rec));
            var last = gstate.points[gstate.points.length - 1];
            if (last && last.ts === pt.ts) return;
            gstate.points.push(pt);
            var cutoff = Date.now() - gstate.win * 1000;
            while (gstate.points.length > 1 && gstate.points[0].ts < cutoff) {
                gstate.points.shift();
            }
        }

        gstate.handler = function (evt) {
            if (!gstate.points) return;
            mergeLiveTail();
            if (gstate.redrawTimer) return;
            gstate.redrawTimer = window.setTimeout(function () {
                gstate.redrawTimer = null;
                drawGraph(ui, gstate.points);
            }, 1000);
        };
        onSample(gstate.handler);

        ui.select.addEventListener("change", function () {
            load(Number(ui.select.value) || HISTORY_WINDOW);
        });

        graphCtrl = HolafModal.open({
            id: "docky-stats-graph-modal",
            title: "📈 Stats — " + (rec.container || container),
            content: ui.wrap,
            size: "lg",
            width: 720,
            buttons: [{ text: "Fermer", value: false, type: "cancel" }],
            onOpen: function () { load(HISTORY_WINDOW); },
            onClose: function () {
                if (gstate.handler) removeHandler(gstate.handler);
                if (gstate.redrawTimer) window.clearTimeout(gstate.redrawTimer);
                graphCtrl = null;
            },
        });
        return graphCtrl;
    }

    // ------------------------------------------------------------------
    // Intervalle / pause
    // ------------------------------------------------------------------

    function readPref() {
        var raw = null;
        try { raw = window.localStorage.getItem(STORAGE_KEY); } catch (e) { raw = null; }
        if (raw === "pause" || raw === "0") {
            intervalMs = 0;
            lastNonZeroInterval = DEFAULT_INTERVAL;
        } else {
            var n = parseInt(raw, 10);
            if (ALLOWED_INTERVALS.indexOf(n) !== -1) {
                intervalMs = n;
                lastNonZeroInterval = n;
            } else {
                intervalMs = DEFAULT_INTERVAL;
                lastNonZeroInterval = DEFAULT_INTERVAL;
            }
        }
        // Respecte un onglet déjà caché au chargement.
        hidden = pageHidden();
    }

    function persist() {
        try {
            window.localStorage.setItem(STORAGE_KEY, intervalMs > 0 ? String(intervalMs) : "pause");
        } catch (e) { /* ignore */ }
    }

    function syncSelect() {
        var sel = document.getElementById("stats-interval-select");
        if (sel) sel.value = intervalMs > 0 ? String(intervalMs) : "pause";
    }

    function setIntervalMs(ms) {
        if (ms === "pause" || ms === 0 || ms === "0") {
            intervalMs = 0;
        } else {
            var n = Number(ms);
            if (ALLOWED_INTERVALS.indexOf(n) === -1) n = DEFAULT_INTERVAL;
            intervalMs = n;
            lastNonZeroInterval = n;
        }
        persist();
        syncSelect();
        if (isActive()) connect();
        applySubscriptions();
        updateIndicator();
    }

    function pause() { setIntervalMs(0); }

    function resume() { setIntervalMs(lastNonZeroInterval || DEFAULT_INTERVAL); }

    // ------------------------------------------------------------------
    // Indicateur discret
    // ------------------------------------------------------------------

    function updateIndicator() {
        var el = document.getElementById("stats-live-indicator");
        if (!el) return;
        var state = "live";
        var title = "Stats temps réel (WebSocket)";
        if (!isActive()) {
            state = "paused";
            title = "Stats en pause";
        } else if (!connected || fallback) {
            state = "fallback";
            title = "WebSocket indisponible — repli sur le polling";
        }
        el.className = "stats-live-dot stats-live-" + state;
        el.dataset.state = state;
        el.title = title;
    }

    // ------------------------------------------------------------------
    // API publique
    // ------------------------------------------------------------------

    function subscribe(agent, container) {
        if (!container) return;
        var key = keyOf(agent, container);
        addCount(explicit, key);
        targetRec(agent, container);
        if (isActive()) connect();
        applySubscriptions();
    }

    function unsubscribe(agent, container) {
        if (!container) return;
        decCount(explicit, keyOf(agent, container));
        applySubscriptions();
    }

    function onSample(cb) {
        if (typeof cb !== "function") return function () {};
        handlers.push(cb);
        return function () { removeHandler(cb); };
    }

    function removeHandler(cb) {
        var idx = handlers.indexOf(cb);
        if (idx !== -1) handlers.splice(idx, 1);
    }

    function getLast(agent, container) {
        var rec = targets.get(keyOf(agent, container));
        return rec ? rec.last : null;
    }

    function hasLive(agent, container) {
        if (container === undefined) {
            // Appel avec un seul argument : identifiant de conteneur.
            var id = String(agent);
            var found = false;
            targets.forEach(function (rec) {
                if (rec.last && (rec.container === id || String(rec.last.id) === id)) found = true;
            });
            return found;
        }
        var rec = targets.get(keyOf(agent, container));
        return !!(rec && rec.last);
    }

    function shouldSuppressPolling(agent, container) {
        if (intervalMs === 0) return true;      // pause : gèle la dernière valeur
        return connected && hasLive(agent, container);
    }

    function getStatus() {
        return {
            connected: connected,
            fallback: fallback,
            paused: intervalMs === 0,
            hidden: hidden,
            intervalMs: intervalMs,
            subscribed: Array.from(subscribed),
            targets: Array.from(targets.keys()),
            pending: pendingAdd.size + pendingRemove.size,
            observed: observed.size,
            failCount: failCount,
            reconciles: reconcileCount,
        };
    }

    function init() {
        if (inited) return;
        inited = true;
        readPref();
        syncSelect();
        var sel = document.getElementById("stats-interval-select");
        if (sel) {
            sel.addEventListener("change", function () {
                setIntervalMs(sel.value === "pause" ? 0 : Number(sel.value));
            });
        }
        bindVisibilityObserver();
        bindGraphClick();
        bindMutationObserver();
        updateIndicator();
        connect();
        reconcile();
        // Reverrouille la préférence sur le select (JS peut être chargé avant
        // que la valeur ne soit restaurée par d'autres modules).
        syncSelect();
    }

    window.DockyStats = {
        // Cycle de vie
        init: init,
        reconcile: reconcile,
        openGraph: openGraph,
        // Abonnements
        subscribe: subscribe,
        unsubscribe: unsubscribe,
        onSample: onSample,
        getLast: getLast,
        // Intervalle / pause
        setInterval: setIntervalMs,
        pause: pause,
        resume: resume,
        // Lecture d'état
        isConnected: function () { return connected; },
        isPaused: function () { return intervalMs === 0; },
        hasLive: hasLive,
        shouldSuppressPolling: shouldSuppressPolling,
        getStatus: getStatus,
        // Helpers exposés (tests / intégration)
        formatRate: formatRate,
        _targets: targets,
        _subscribed: subscribed,
    };

    if (typeof document !== "undefined") {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", init);
        } else {
            init();
        }
    }
})();
