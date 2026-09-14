/* Headless chromium validation for Docky LOT 3 (stats frontend).
 *
 * No external dependency: a tiny Node HTTP server serves the real
 * ``static/js/stats.js`` (plus the real HolafModal vendor brick) and an
 * instrumented harness page; Chromium is driven through the DevTools
 * Protocol over a raw WebSocket (Node >= 21 global WebSocket).
 *
 * Scenarios:
 *   (a) WS connected + subscribe sent for visible tiles only;
 *   (b) injected sample updates the displayed gauge (CPU normalised);
 *   (c) tile scrolled out of the viewport -> unsubscribe;
 *   (d) sparkline SVG present (initialised from /stats/history);
 *   (e) click on the tile stats zone -> graph modal with curves;
 *   (f) interval selector persisted across reload;
 *   (g) hidden tab -> subscriptions released.
 */

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";

const REPO = "/projects/Docky";
const PORT = 8731;
const CDP_PORT = 9333;
const CHROME = "/usr/bin/chromium";

// ---------------------------------------------------------------------------
// Harness page
// ---------------------------------------------------------------------------
const HARNESS = `<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><title>stats harness</title>
<style>
  body { margin: 0; background: #1a1a2e; color: #eee; }
  #dashboard-content { display: flex; flex-direction: column; gap: 10px; padding: 10px; }
  .grid-container-card { height: 220px; border: 1px solid #333; box-sizing: border-box; padding: 8px; }
  .resource-line { display: flex; align-items: center; gap: 4px; margin: 4px 0; }
  .progress-bar { width: 120px; height: 6px; background: #222; }
  .progress-fill { height: 100%; width: 0; }
  .resource-value { min-width: 60px; }
</style>
</head>
<body>
<div id="stats-controls">
  <select id="stats-interval-select">
    <option value="1000">1 s</option>
    <option value="2000">2 s</option>
    <option value="5000">5 s</option>
    <option value="pause">Pause</option>
  </select>
  <span id="stats-live-indicator"></span>
</div>
<div id="dashboard-content">
  <div class="grid-container-card" data-agent="A" data-container="c1" data-status="running"><div class="grid-card-resources" id="resources-c1"><div class="resource-line"><span class="resource-label">CPU</span><div class="progress-bar"><div class="progress-fill"></div></div><span class="resource-value">—</span></div><div class="resource-line"><span class="resource-label">RAM</span><div class="progress-bar"><div class="progress-fill ram"></div></div><span class="resource-value">—</span></div></div><div id="stats-cpu-c1"></div></div>
  <div class="grid-container-card" data-agent="A" data-container="c2" data-status="running"><div class="grid-card-resources" id="resources-c2"><div class="resource-line"><span class="resource-label">CPU</span><div class="progress-bar"><div class="progress-fill"></div></div><span class="resource-value">—</span></div><div class="resource-line"><span class="resource-label">RAM</span><div class="progress-bar"><div class="progress-fill ram"></div></div><span class="resource-value">—</span></div></div></div>
  <div class="grid-container-card" data-agent="A" data-container="c3" data-status="running"><div class="grid-card-resources" id="resources-c3"><div class="resource-line"><span class="resource-label">CPU</span><div class="progress-bar"><div class="progress-fill"></div></div><span class="resource-value">—</span></div><div class="resource-line"><span class="resource-label">RAM</span><div class="progress-bar"><div class="progress-fill ram"></div></div><span class="resource-value">—</span></div></div></div>
  <div class="grid-container-card" data-agent="A" data-container="c4" data-status="running"><div class="grid-card-resources" id="resources-c4"><div class="resource-line"><span class="resource-label">CPU</span><div class="progress-bar"><div class="progress-fill"></div></div><span class="resource-value">—</span></div><div class="resource-line"><span class="resource-label">RAM</span><div class="progress-bar"><div class="progress-fill ram"></div></div><span class="resource-value">—</span></div></div></div>
  <div class="grid-container-card" data-agent="A" data-container="c5" data-status="running"><div class="grid-card-resources" id="resources-c5"><div class="resource-line"><span class="resource-label">CPU</span><div class="progress-bar"><div class="progress-fill"></div></div><span class="resource-value">—</span></div><div class="resource-line"><span class="resource-label">RAM</span><div class="progress-bar"><div class="progress-fill ram"></div></div><span class="resource-value">—</span></div></div></div>
</div>

<script type="module" src="/static/vendor/holaf/holaf-modal.js"></script>
<script type="module" src="/static/js/holaf-docky-theme.js"></script>

<script>
window.__wsSent = [];
window.__wsSockets = [];
window.__applied = {};
window.__historyCalls = [];
(function () {
  function makePoints() {
    var now = Date.now(), pts = [];
    for (var i = 0; i < 40; i++) {
      pts.push({ ts: now - (39 - i) * 1000, cpu_percent: 100 + (i % 10), mem_percent: 20 + (i % 5), mem_usage: 1000, mem_limit: 10000, net_rx: i * 100, net_tx: i * 50 });
    }
    return pts;
  }
  function FakeWebSocket(url) {
    this.url = url; this.readyState = 0;
    this.onopen = this.onmessage = this.onerror = this.onclose = null;
    window.__wsSockets.push(this);
    var self = this;
    setTimeout(function () { if (self.readyState === 3) return; self.readyState = 1; if (self.onopen) self.onopen(); }, 0);
  }
  FakeWebSocket.CONNECTING = 0; FakeWebSocket.OPEN = 1; FakeWebSocket.CLOSING = 2; FakeWebSocket.CLOSED = 3;
  FakeWebSocket.prototype.send = function (data) { try { window.__wsSent.push(JSON.parse(data)); } catch (e) { window.__wsSent.push(String(data)); } };
  FakeWebSocket.prototype.close = function () { this.readyState = 3; if (this.onclose) this.onclose(); };
  FakeWebSocket.prototype.__inject = function (obj) { if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) }); };
  window.WebSocket = FakeWebSocket;
  window.__injectSample = function (sample) {
    var list = window.__wsSockets.slice().reverse();
    for (var i = 0; i < list.length; i++) { if (list[i].readyState === 1) { list[i].__inject({ type: "sample", sample: sample }); return true; } }
    return false;
  };
  window.__ofType = function (type) { return window.__wsSent.filter(function (m) { return m && m.type === type; }); };
  window.__subscribedTargets = function () {
    var out = [];
    window.__ofType("subscribe").forEach(function (m) { (m.targets || []).forEach(function (t) { var k = t.agent + "||" + t.container; if (out.indexOf(k) === -1) out.push(k); }); });
    return out;
  };
  window.__unsubscribedTargets = function () {
    var out = [];
    window.__ofType("unsubscribe").forEach(function (m) { (m.targets || []).forEach(function (t) { out.push(t.agent + "||" + t.container); }); });
    return out;
  };
  window.DockyFetch = {
    request: function (url) {
      if (url.indexOf("/stats/history") !== -1) {
        window.__historyCalls.push(url);
        var m = url.match(/containers\\/([^\\/]+)\\/stats/);
        var cid = m ? decodeURIComponent(m[1]) : "c1";
        return Promise.resolve({ container: cid, window: 900, points: makePoints() });
      }
      return Promise.reject(new Error("no network"));
    }
  };
  window.DockyApp = {
    _statsCache: {},
    formatBytes: function (b) { if (!b) return "0 B"; var u = ["B", "KB", "MB", "GB", "TB"], i = 0, v = b; while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; } return v.toFixed(i > 0 ? 1 : 0) + " " + u[i]; },
    applyStatsDom: function (id, s) {
      window.__applied[id] = s;
      var res = document.getElementById("resources-" + id);
      if (res) {
        var lines = res.querySelectorAll(".resource-line");
        if (lines[0]) { lines[0].querySelector(".progress-fill").style.width = s.cpuNorm + "%"; lines[0].querySelector(".resource-value").textContent = s.cpuNorm.toFixed(1) + "%"; }
        if (lines[1]) { lines[1].querySelector(".progress-fill").style.width = s.memPercent + "%"; lines[1].querySelector(".resource-value").textContent = "mem"; }
      }
      var cv = document.getElementById("stats-cpu-val-" + id);
      if (cv) cv.textContent = s.cpuNorm.toFixed(1) + "%";
    }
  };
})();
</script>
<script src="/static/js/stats.js"></script>
</body></html>`;

// ---------------------------------------------------------------------------
// Static server
// ---------------------------------------------------------------------------
const MIME = {
  ".js": "text/javascript; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
};

function startServer() {
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url === "/" || url === "/index.html") {
      res.writeHead(200, { "content-type": MIME[".html"] });
      res.end(HARNESS);
      return;
    }
    const safe = path.normalize(url).replace(/^(\.\.[/\\])+/, "");
    const file = url.startsWith("/static/")
      ? path.join(REPO, "orchestrator", "app", safe)
      : path.join(REPO, safe);
    if (!file.startsWith(REPO) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404); res.end("not found"); return;
    }
    res.writeHead(200, { "content-type": MIME[path.extname(file)] || "application/octet-stream" });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((resolve) => server.listen(PORT, "127.0.0.1", () => resolve(server)));
}

// ---------------------------------------------------------------------------
// Minimal CDP client
// ---------------------------------------------------------------------------
class CDP {
  constructor(wsUrl) {
    this.id = 0;
    this.pending = new Map();
    this.ws = new WebSocket(wsUrl);
    this.ready = new Promise((resolve, reject) => {
      this.ws.onopen = () => resolve();
      this.ws.onerror = (e) => reject(new Error("CDP ws error"));
    });
    this.ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
    };
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() { try { this.ws.close(); } catch (e) { /* ignore */ } }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function fetchJson(url) {
  const r = await fetch(url);
  return r.json();
}

async function getPageTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await fetchJson(`http://127.0.0.1:${CDP_PORT}/json/list`);
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch (e) { /* not ready yet */ }
    await sleep(200);
  }
  throw new Error("no CDP page target");
}

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------
const results = [];
function record(name, pass, detail) {
  results.push({ name, pass, detail: detail || "" });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

async function main() {
  const server = await startServer();
  const userDir = "/tmp/docky-lot3-chrome-profile";
  fs.rmSync(userDir, { recursive: true, force: true });
  const chrome = spawn(CHROME, [
    "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
    "--no-first-run", "--no-default-browser-check",
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${userDir}`,
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  chrome.stderr.on("data", () => {});

  let cdp;
  try {
    const target = await getPageTarget();
    cdp = new CDP(target.webSocketDebuggerUrl);
    await cdp.ready;
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 800, height: 600, deviceScaleFactor: 1, mobile: false,
    });

    async function evalJs(expression) {
      const r = await cdp.send("Runtime.evaluate", {
        expression, returnByValue: true, awaitPromise: true,
      });
      if (r.exceptionDetails) {
        throw new Error(r.exceptionDetails.text + " :: " + expression);
      }
      return r.result.value;
    }
    async function waitFor(expression, timeout = 5000) {
      const start = Date.now();
      let last;
      while (Date.now() - start < timeout) {
        last = await evalJs(expression);
        if (last) return last;
        await sleep(60);
      }
      throw new Error("timeout waiting: " + expression + " (last=" + JSON.stringify(last) + ")");
    }
    async function navigate(url) {
      await cdp.send("Page.navigate", { url });
      await waitFor("window.DockyStats && window.__wsSockets.length > 0", 8000);
    }

    // ---- Load harness ---------------------------------------------------
    await navigate(`http://127.0.0.1:${PORT}/`);
    await sleep(300);

    // (a) subscribe for visible tiles only.
    try {
      await waitFor("window.__subscribedTargets().length > 0", 5000);
      const subs = await evalJs("window.__subscribedTargets()");
      const hasVisible = subs.includes("A||c1") && subs.includes("A||c2") && subs.includes("A||c3");
      const hasFar = subs.includes("A||c5");
      record("(a) subscribe envoyé pour tuiles visibles (c1..c3, pas c5)",
        hasVisible && !hasFar,
        JSON.stringify(subs));
    } catch (e) { record("(a) subscribe envoyé pour tuiles visibles", false, e.message); }

    // (h) diagnostic : le MutationObserver ne boucle pas avec les repaints SVG.
    try {
      const c0 = await evalJs("DockyStats.getStatus().reconciles");
      await sleep(1000);
      const c1 = await evalJs("DockyStats.getStatus().reconciles");
      record("(h) pas de boucle reconcile (MutationObserver)", (c1 - c0) < 10, `${c0} -> ${c1}`);
    } catch (e) { record("(h) pas de boucle reconcile (MutationObserver)", false, e.message); }

    // (d) sparkline initialised from /stats/history.
    try {
      await waitFor("document.querySelectorAll('#resources-c1 svg.docky-spark polygon').length > 0", 5000);
      const historyCalls = await evalJs("window.__historyCalls.length");
      record("(d) sparkline SVG initialisée depuis /stats/history", true, "historique appelé ×" + historyCalls);
    } catch (e) { record("(d) sparkline SVG initialisée depuis /stats/history", false, e.message); }

    // (b) injected sample updates gauge (CPU normalised 200/4 = 50).
    try {
      await evalJs(`window.__injectSample(${JSON.stringify({
        ts: Date.now(), id: "c1", name: "c1", state: "running",
        cpu_percent: 200, cpu_count: 4, mem_usage: 512, mem_limit: 2048,
        mem_percent: 25, mem_cache: 10, net_rx: 1000, net_tx: 2000,
      })})`);
      await waitFor("document.querySelector('#resources-c1 .resource-value').textContent === '50.0%'", 4000);
      const applied = await evalJs("window.__applied['c1']");
      record("(b) sample injecté met à jour la jauge (CPU normalisé)",
        applied && Math.abs(applied.cpuNorm - 50) < 0.01,
        "cpuNorm=" + (applied && applied.cpuNorm));
    } catch (e) { record("(b) sample injecté met à jour la jauge (CPU normalisé)", false, e.message); }

    // (e) click on stats zone -> graph modal with curves.
    try {
      await evalJs("document.getElementById('resources-c1').click()");
      await waitFor("!!document.getElementById('docky-stats-graph-modal')", 4000);
      await waitFor("document.querySelectorAll('#docky-stats-graph-modal .docky-graph-svg polyline').length >= 2", 5000);
      const curves = await evalJs("document.querySelectorAll('#docky-stats-graph-modal .docky-graph-svg polyline').length");
      record("(e) clic tuile -> modale graphique avec courbes", curves >= 2, curves + " courbes");
      await evalJs("(function(){var b=document.querySelector('#docky-stats-graph-modal .holaf-modal-close'); if(b) b.click();})()");
      await sleep(150);
    } catch (e) { record("(e) clic tuile -> modale graphique avec courbes", false, e.message); }

    // (c) scroll tile out -> unsubscribe.
    try {
      await evalJs("window.scrollTo(0, document.body.scrollHeight)");
      await waitFor("window.__unsubscribedTargets().indexOf('A||c1') !== -1", 5000);
      const unsubs = await evalJs("window.__unsubscribedTargets()");
      record("(c) tuile sortie du viewport -> unsubscribe", unsubs.includes("A||c1"), JSON.stringify(unsubs));
      await evalJs("window.scrollTo(0, 0)");
      await sleep(200);
    } catch (e) { record("(c) tuile sortie du viewport -> unsubscribe", false, e.message); }

    // (f) interval selector persisted across reload.
    try {
      await evalJs("(function(){var s=document.getElementById('stats-interval-select'); s.value='1000'; s.dispatchEvent(new Event('change',{bubbles:true}));})()");
      const stored = await evalJs("localStorage.getItem('docky-stats-interval')");
      await cdp.send("Page.reload");
      await waitFor("window.DockyStats && window.__wsSockets.length > 0", 8000);
      await sleep(150);
      const selVal = await evalJs("document.getElementById('stats-interval-select').value");
      const interval = await evalJs("DockyStats.getStatus().intervalMs");
      record("(f) sélecteur d'intervalle persisté après reload",
        stored === "1000" && selVal === "1000" && interval === 1000,
        `stored=${stored} select=${selVal} interval=${interval}`);
    } catch (e) { record("(f) sélecteur d'intervalle persisté après reload", false, e.message); }

    // (i) pause -> plus aucun abonnement, jauges figées.
    try {
      await waitFor("window.__subscribedTargets().length > 0", 5000);
      await evalJs("DockyStats.setInterval(0)");
      await waitFor("DockyStats.isPaused() && DockyStats.getStatus().subscribed.length === 0 && window.__unsubscribedTargets().length > 0", 3000);
      const sel = await evalJs("document.getElementById('stats-interval-select').value");
      const unsubs = await evalJs("window.__unsubscribedTargets().length");
      record("(i) Pause coupe les abonnements", sel === "pause" && unsubs > 0, `select=${sel} unsubs=${unsubs}`);
      await evalJs("DockyStats.setInterval(1000)");
      await waitFor("window.__subscribedTargets().length > 0", 3000);
    } catch (e) { record("(i) Pause coupe les abonnements", false, e.message); }

    // (g) hidden tab -> subscriptions released.
    try {
      await waitFor("window.__subscribedTargets().length > 0", 5000);
      await evalJs("Object.defineProperty(document,'hidden',{configurable:true,get:function(){return true;}}); document.dispatchEvent(new Event('visibilitychange'));");
      await waitFor("DockyStats.getStatus().subscribed.length === 0", 5000);
      const unsubs = await evalJs("window.__unsubscribedTargets()");
      record("(g) onglet caché -> abonnements libérés", unsubs.length > 0, JSON.stringify(unsubs.slice(-5)));
    } catch (e) { record("(g) onglet caché -> abonnements libérés", false, e.message); }
  } finally {
    if (cdp) cdp.close();
    chrome.kill("SIGKILL");
    server.close();
  }

  const failed = results.filter((r) => !r.pass);
  console.log(`\n${results.length - failed.length}/${results.length} scénarios OK`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error("FATAL", e); process.exit(2); });
