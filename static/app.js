/* critter-bridge UI — plain JS, no build step. Refreshes /api/status every few seconds. */

let STATE = null;
let boardSig = "";

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

async function api(method, url, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  let data = {};
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
  return data;
}

function fmtAge(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 30) return s + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}

function toast(msg, ok = true) {
  const t = document.createElement("div");
  t.className = "toast " + (ok ? "ok" : "err");
  t.textContent = msg;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 400); }, 3200);
}

async function load() {
  try {
    STATE = await api("GET", "/api/status");
    render();
  } catch (e) {
    console.warn("load failed:", e);
  }
}

const KEYABLES = "input:not([type=checkbox]), input[type=checkbox], select, textarea";

function render() {
  if (!STATE) return;
  $("ver").textContent = "v" + STATE.version;

  // don't clobber fields the user is typing into right now
  if (!document.forms["settings-form"].matches(":focus-within")) {
    const s = STATE.settings || {};
    $("set-interval").value = s.poll_interval || 15;
    $("set-beacon").value = s.udp_beacon_port ?? 1234;
    $("set-auto").checked = s.auto_add !== false;
    $("set-token").value = s.telegram_token || "";
  }
  if (!document.querySelector("#board-list :focus")) renderBoards();
  renderDiscovered();
  renderRuleFormSelects();
  if (!document.querySelector("#rule-list :focus")) renderRules();
  renderEvents();
}

/* ---------- boards ---------- */

const BACKENDS = [
  { id: "", label: "— select backend —" },
  { id: "thingspeak", label: "ThingSpeak" },
  { id: "adafruit", label: "Adafruit IO" },
  { id: "influx", label: "InfluxDB Cloud" },
];

function panelOptions(panels) {
  return panels.map((p) =>
    `<option value="${esc(p.id)}">${esc(p.name)}${esc(p.unit ? " (" + p.unit + ")" : "")}</option>`).join("");
}

function renderBoards() {
  const list = $("board-list");
  list.innerHTML = "";
  for (const b of STATE.boards) {
    const st = b.status || {};
    const panels = b.panels || [];
    const cfg = b.backend_cfg || {};
    const backend = b.backend || "";
    const dot = st.ok === true ? "ok" : (st.last_poll ? "bad" : "idle");
    const err = st.last_error ? `<span class="bad">⚠ ${esc(st.last_error)}</span>`
                              : `<span class="mut">ok</span>`;
    const opts = panelOptions(panels);

    const tsFields = cfg.fields || {};
    const tsChips = Object.entries(tsFields).map(([pid, f]) => {
      const pn = (panels.find((p) => p.id === pid) || {}).name || pid;
      return `<span class="chip">${esc(pn)} → f${esc(f)}` +
        ` <button data-act="unmap" data-kind="fields" data-pid="${esc(pid)}" title="unmap">×</button></span>`;
    }).join(" ");
    const aioFeeds = cfg.feeds || {};
    const aioChips = Object.entries(aioFeeds).map(([pid, feed]) => {
      const pn = (panels.find((p) => p.id === pid) || {}).name || pid;
      return `<span class="chip">${esc(pn)} → ${esc(feed)}` +
        ` <button data-act="unmap" data-kind="feeds" data-pid="${esc(pid)}" title="unmap">×</button></span>`;
    }).join(" ");

    const card = document.createElement("div");
    card.className = "card";
    card.dataset.id = b.id;
    card.innerHTML = `
      <div class="row">
        <span class="dot ${dot}"></span>
        <b>${esc(b.name || b.host)}</b>
        ${b.direct_cloud ? '<span class="mut" title="ESP32 direct cloud mode (board handles its own push)">· direct</span>' : ""}
        <span class="mut">${esc(b.host)} · seen ${fmtAge(st.last_seen)}</span> ${err}
        <span class="spacer"></span>
        <button data-act="bdel">Delete</button>
      </div>
      <div class="row">
        <input class="bname" value="${esc(b.name || "")}" placeholder="name">
        <input class="bhost" value="${esc(b.host || "")}" placeholder="host/IP">
        <input class="bint" type="number" min="0" value="${esc(b.poll_interval || 0)}"
               placeholder="0 = global" title="poll interval override (0 = global)" style="max-width:8rem">
        <button data-act="bsave">Save</button>
      </div>
      <div class="row">
        <span class="mut">Cloud backend:</span>
        <select class="bb">${BACKENDS.map((o) =>
          `<option value="${esc(o.id)}"${o.id === backend ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select>
      </div>
      <div class="cfg cfg-thingspeak">
        <div class="row">
          <input class="tskey" type="password" value="${esc(cfg.write_key || "")}" placeholder="ThingSpeak write key" style="min-width:15rem">
          <span class="mut">one channel per this board· fields 1–8</span>
        </div>
        <div class="row">
          <span class="mut">Fields:</span>
          ${tsChips
            ? `<span class="chips">${tsChips}</span>`
            : `<span class="mut">${cfg.write_key ? "key set — map a panel to a field below" : "write key needed for cloud updates"}</span>`}
        </div>
        <div class="row">
          <span class="mut">Panel → field:</span>
          <select class="ts-panel" style="max-width:14rem">${opts || "<option value=''>poll the board first…</option>"}</select>
          <input class="ts-field" type="number" min="1" max="8" placeholder="1–8" style="max-width:5rem">
          <button data-act="tsmap">Map</button>
        </div>
      </div>
      <div class="cfg cfg-adafruit">
        <div class="row">
          <input class="aio-user" value="${esc(cfg.username || "")}" placeholder="Adafruit username" style="max-width:12rem">
          <input class="aio-key" type="password" value="${esc(cfg.key || "")}" placeholder="AIO key (Active Key)" style="min-width:14rem">
          <span class="mut">one feed per panel · feeds auto-created on first write</span>
        </div>
        <div class="row">
          <span class="mut">Feeds:</span>
          ${aioChips
            ? `<span class="chips">${aioChips}</span>`
            : `<span class="mut">map a panel to a feed name below</span>`}
        </div>
        <div class="row">
          <span class="mut">Panel → feed:</span>
          <select class="aio-panel" style="max-width:14rem">${opts || "<option value=''>poll the board first…</option>"}</select>
          <input class="aio-feed" placeholder="e.g. fireplace.pressure" style="max-width:13rem">
          <button data-act="aiomap">Map</button>
        </div>
      </div>
      <div class="cfg cfg-influx">
        <div class="row">
          <input class="i-url" value="${esc(cfg.url || "")}" placeholder="https://eu-central-1-1.aws.cloud2.influxdata.com" style="min-width:19rem">
          <input class="i-org" value="${esc(cfg.org || "")}" placeholder="org" style="max-width:9rem">
          <input class="i-bk" value="${esc(cfg.bucket || "")}" placeholder="bucket" style="max-width:11rem">
        </div>
        <div class="row">
          <input class="i-token" type="password" value="${esc(cfg.token || "")}" placeholder="token" style="min-width:15rem">
          <input class="i-meas" value="${esc(cfg.measurement || "")}" placeholder="measurement (optional)" style="max-width:12rem">
          <span class="mut">all panels batched: one write per poll, tagged device/panel</span>
        </div>
      </div>`;
    card.querySelector(".bb").addEventListener("change", (e) => showCfg(card, e.target.value));
    showCfg(card, backend);
    card.addEventListener("click", onBoardAction);
    list.appendChild(card);
  }
}

function showCfg(card, backend) {
  for (const b of ["thingspeak", "adafruit", "influx"]) {
    const el = card.querySelector(".cfg-" + b);
    el.classList.toggle("on", b === backend);
  }
}

async function onBoardAction(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const card = btn.closest(".card");
  const id = card.dataset.id;
  const act = btn.dataset.act;
  try {
    if (act === "bsave") {
      const backend = card.querySelector(".bb").value;
      const cfg = {};
      if (backend === "thingspeak") {
        cfg.write_key = card.querySelector(".tskey").value;
      } else if (backend === "adafruit") {
        cfg.username = card.querySelector(".aio-user").value;
        cfg.key = card.querySelector(".aio-key").value;
      } else if (backend === "influx") {
        cfg.url = card.querySelector(".i-url").value;
        cfg.org = card.querySelector(".i-org").value;
        cfg.bucket = card.querySelector(".i-bk").value;
        cfg.token = card.querySelector(".i-token").value;
        cfg.measurement = card.querySelector(".i-meas").value;
      }
      await api("POST", "/api/boards/" + id, { backend, backend_cfg: cfg });
      toast(backend ? "Board saved (" + backend + ")" : "Board saved (no cloud backend)");
    } else if (act === "bdel") {
      if (!confirm("Delete this board and its rules?")) return;
      await api("DELETE", "/api/boards/" + id);
      toast("Board removed");
    } else if (act === "tsmap") {
      const pid = card.querySelector(".ts-panel").value;
      const f = Number(card.querySelector(".ts-field").value);
      if (!pid || !f) { toast("pick a panel and a field 1–8", false); return; }
      const b = STATE.boards.find((x) => x.id === id) || {};
      const fields = Object.assign({}, ((b.backend_cfg || {}).fields) || {});
      fields[pid] = f;
      await api("POST", "/api/boards/" + id, { backend: "thingspeak", backend_cfg: { fields } });
      toast("Panel mapped to field " + f);
    } else if (act === "aiomap") {
      const pid = card.querySelector(".aio-panel").value;
      const feed = card.querySelector(".aio-feed").value.trim();
      if (!pid || !feed) { toast("pick a panel and a feed name", false); return; }
      const b = STATE.boards.find((x) => x.id === id) || {};
      const feeds = Object.assign({}, ((b.backend_cfg || {}).feeds) || {});
      feeds[pid] = feed;
      await api("POST", "/api/boards/" + id, { backend: "adafruit", backend_cfg: { feeds } });
      toast('Panel mapped to feed "' + feed + '"');
    } else if (act === "unmap") {
      const b = STATE.boards.find((x) => x.id === id) || {};
      const kind = btn.dataset.kind === "feeds" ? "feeds" : "fields";
      const cfg = Object.assign({}, (b.backend_cfg || {}));
      const map = Object.assign({}, cfg[kind] || {});
      delete map[btn.dataset.pid];
      const backend = card.querySelector(".bb").value;
      await api("POST", "/api/boards/" + id, { backend, backend_cfg: { [kind]: map } });
      toast("Unmapped");
    }
    load();
  } catch (err) {
    toast(err.message, false);
  }
}

/* ---------- discovered beacons ---------- */

function renderDiscovered() {
  const sec = $("discovered-sec");
  const d = STATE.discovered || [];
  sec.hidden = d.length === 0;
  const list = $("discovered-list");
  list.innerHTML = "";
  for (const b of d) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <span class="dot idle"></span>
      <b>${esc(b.device || "unknown device")}</b>
      <span class="mut">${esc(b.ip)} · ${esc(b.mac)}${(b.panel_ids && b.panel_ids.length) ? " · " + esc(b.panel_ids.join(", ")) : ""}</span>
      <span class="spacer"></span>
      <button data-act="adopt" data-mac="${esc(b.mac)}">Add</button>`;
    card.addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-act=adopt]");
      if (!btn) return;
      try {
        await api("POST", "/api/discovery/adopt", { mac: btn.dataset.mac });
        toast("Board added");
        load();
      } catch (err) { toast(err.message, false); }
    });
    list.appendChild(card);
  }
}

/* ---------- rules ---------- */

function renderRuleFormSelects() {
  const sel = $("rule-board");
  const sig = STATE.boards.map((b) => b.id + b.name).join(",");
  if (sig !== boardSig) {
    boardSig = sig;
    sel.innerHTML = STATE.boards.map((b) =>
      `<option value="${esc(b.id)}">${esc(b.name || b.host)}</option>`).join("");
  }
  refreshRulePanels();
}

function refreshRulePanels() {
  const psel = $("rule-panel");
  const b = (STATE.boards || []).find((x) => x.id === $("rule-board").value);
  const panels = (b && b.panels) || [];
  psel.innerHTML = panels.map((p) =>
    `<option value="${esc(p.id)}">${esc(p.name)}${esc(p.unit ? " (" + p.unit + ")" : "")}</option>`).join("");
  psel.disabled = panels.length === 0;
}

function renderRules() {
  const list = $("rule-list");
  list.innerHTML = "";
  for (const r of STATE.rules) {
    const b = STATE.boards.find((x) => x.id === r.board_id) || {};
    const p = ((b.panels || []).find((p2) => p2.id === r.panel_id) || {}).name || r.panel_id;
    const d = document.createElement("div");
    d.className = "card";
    d.innerHTML = `
      <input type="checkbox" class="ren" ${r.enabled ? "checked" : ""}>
      <b>${esc(b.name || r.board_id)}.${esc(p)}</b>
      <span>${esc(r.op)} ${esc(r.threshold)}</span>
      <span class="mut">hyst ${esc(r.hysteresis || 0)} · cooldown ${esc(r.cooldown_s)}s · chat ${esc(r.chat_id)}</span>
      <span class="msg">“${esc(r.message)}”</span>
      <span class="spacer"></span>
      <button data-act="rdel">Delete</button>`;
    d.addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-act=rdel]");
      if (btn) {
        if (!confirm("Delete this rule?")) return;
        try {
          await api("DELETE", "/api/rules/" + r.id);
          toast("Rule deleted");
          d.remove(); // instant visual removal
          load();     // refresh full state
        } catch (err) { toast(err.message, false); }
        return;
      }
      if (e.target.classList && e.target.classList.contains("ren")) {
        try {
          await api("POST", "/api/rules/" + r.id, { enabled: e.target.checked });
          toast(e.target.checked ? "Rule enabled" : "Rule disabled");
          load();
        } catch (err) { toast(err.message, false); }
      }
    });
    list.appendChild(d);
  }
}

/* ---------- events ---------- */

function renderEvents() {
  $("events").innerHTML = (STATE.events || []).map((ev) =>
    `<li><span class="lv lv-${esc(ev.level)}">${esc(ev.level)}</span>` +
    `<span>${esc(ev.msg)}</span><time class="mut">${fmtAge(ev.ts)}</time></li>`).join("");
}

/* ---------- forms ---------- */

$("board-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("POST", "/api/boards", {
      name: $("board-name").value,
      host: $("board-host").value,
      poll_interval: Number($("board-interval").value || 0),
    });
    e.target.reset();
    toast("Board added");
    load();
  } catch (err) { toast(err.message, false); }
});

$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("POST", "/api/settings", {
      poll_interval: Number($("set-interval").value),
      udp_beacon_port: Number($("set-beacon").value || 0),
      auto_add: $("set-auto").checked,
      telegram_token: $("set-token").value,
    });
    toast("Settings saved");
    load();
  } catch (err) { toast(err.message, false); }
});

$("tg-test").addEventListener("click", async () => {
  try {
    const r = await api("POST", "/api/test/telegram", { chat_id: $("set-chat").value });
    toast("telegram: " + r.detail);
  } catch (err) { toast("telegram failed: " + err.message, false); }
});

$("rule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pid = $("rule-panel").value;
  if (!pid) { toast("poll the board first, then pick a panel", false); return; }
  try {
    await api("POST", "/api/rules", {
      board_id: $("rule-board").value,
      panel_id: pid,
      op: $("rule-op").value,
      threshold: Number($("rule-threshold").value),
      hysteresis: Number($("rule-hyst").value || 0),
      cooldown_s: Number($("rule-cooldown").value || 300),
      chat_id: $("rule-chat").value,
      message: $("rule-msg").value || "{device}: {panel} = {value}",
    });
    toast("Rule added — fires on the next matching poll");
    load();
  } catch (err) { toast(err.message, false); }
});

$("rule-board").addEventListener("change", refreshRulePanels);

load();
setInterval(load, 4000);
