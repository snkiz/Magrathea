"use strict";
/* MCS web UI. No dependencies. All text goes in via textContent, never innerHTML. */

const $app = document.getElementById("app");
const state = { servers: [], current: null, tab: "console", settings: {}, es: null, poll: null, pending: {} };

/* ---------- helpers ---------- */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
}
const fmtBytes = (n) => n == null ? "–" : n > 1e9 ? (n / 1e9).toFixed(1) + " GB" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : n > 1e3 ? (n / 1e3).toFixed(0) + " KB" : n + " B";
const fmtNum = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(0) + "k" : String(n);

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), bad ? 8000 : 3500);
}

async function api(method, path, body) {
  const opt = { method, headers: { "X-MCS": "1" }, credentials: "same-origin" };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (r.status === 401 && !path.endsWith("/login")) { showLogin(); throw new Error("login required"); }
  if (!r.ok) throw new Error((data && data.error) || `request failed (${r.status})`);
  return data;
}
const act = (p) => p.catch((e) => { toast(e.message, true); throw e; });

/* ---------- login ---------- */
function showLogin() {
  stopStreams();
  const err = h("div", { class: "err", role: "alert" });
  const input = h("input", { type: "password", id: "tok", autocomplete: "current-password", placeholder: "paste your key", required: true });
  $app.replaceChildren(h("div", { class: "login" }, h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    try { await api("POST", "/api/login", { key: input.value }); boot(); } catch (x) { err.textContent = x.message; }
  } },
    h("div", { class: "brand" }, h("i"), "MCS"),
    h("p", { class: "muted" }, "Paste your access key. Keys are made only in a terminal on the server, one per person, and shown once. Ask whoever runs this server, or if that is you, run:"),
    h("p", {}, h("code", { class: "mono" }, "sudo MCSkey add <your name>")),
    h("div", { class: "field" }, h("label", { for: "tok" }, "Access key"), input),
    err, h("button", { class: "btn primary", type: "submit" }, "Sign in")), footer()));
  input.focus();
}

/* ---------- app shell ---------- */
async function boot() {
  try {
    const a = await api("GET", "/api/auth");
    if (!a.authed) return showLogin();
    state.settings = await api("GET", "/api/settings");
  } catch { return showLogin(); }
  buildShell();
  window.addEventListener("hashchange", route);
  await refreshServers();
  route();
  refreshNotices();
  clearInterval(state.poll);
  let tick = 0;
  state.poll = setInterval(() => { if (!document.hidden) { refreshServers(); if (++tick % 8 === 0) refreshNotices(); } }, 4000);
}

/* Said once, in plain words, on every page. */
const footer = () => h("footer", { class: "foot" },
  h("p", {}, h("strong", {}, "NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.")),
  h("p", {}, "MCS is an independent community tool. It is not affiliated with or endorsed by Mojang, Microsoft, Modrinth or CurseForge. Minecraft is a trademark of Mojang Synergies AB."),
  h("p", {}, "Typeface: Shantell Sans, SIL Open Font License."));

function buildShell() {
  $app.replaceChildren(h("div", { class: "shell" },
    h("header", { class: "topbar" }, h("a", { class: "brand", href: "#/", style: false }, h("i"), "MCS"), h("span", { class: "spacer" }),
      h("button", { class: "btn small notice-btn", id: "noticebtn", hidden: true, "aria-haspopup": "true", onclick: () => { noticeUi.open = !noticeUi.open; renderNotices(); } }),
      h("button", { class: "btn small", onclick: openSettings }, "Settings"),
      h("button", { class: "btn small", onclick: async () => { await api("POST", "/api/logout", {}); location.hash = ""; showLogin(); } }, "Sign out")),
    h("nav", { class: "side", id: "side", "aria-label": "Servers" }),
    h("main", { class: "main", id: "main" }),
    h("div", { class: "notices", id: "notices", role: "status", hidden: true }),
    footer()));
  if (!noticeUi.wired) {
    noticeUi.wired = true;  // click anywhere else, or press Escape, to close the list
    document.addEventListener("click", (e) => { if (noticeUi.open && !e.target.closest("#notices, #noticebtn")) { noticeUi.open = false; renderNotices(); } });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && noticeUi.open) { noticeUi.open = false; renderNotices(); } });
  }
}

/* ---------- cautions for a running server ----------
   Mods, plugins, the jar and most settings are only read when the server starts. Changing them while it runs can crash it or
   simply do nothing. MCS does not forbid it (caution tape, not a cage): it asks first, then reminds you to restart. */
const isUp = (s) => !!s && ["active", "activating"].includes(s.state);
const liveServer = (name) => state.servers.find((x) => x.name === name);
try { state.pending = JSON.parse(sessionStorage.getItem("mcs-pending") || "{}"); } catch { state.pending = {}; }
function savePending() { try { sessionStorage.setItem("mcs-pending", JSON.stringify(state.pending)); } catch { /* fine without it */ } }
function okToChange(name, what) {
  if (!isUp(liveServer(name))) return true;
  return confirm(`${name} is running.\n\n${what} while a server runs can make it crash or behave strangely, because it only loads these when it starts. The safest way is to stop it first.\n\nGo ahead anyway? You will get a reminder to restart.`);
}
function markPending(name, reason) {
  const srv = liveServer(name);
  if (!isUp(srv)) return;  // stopped: the next start picks the change up
  const p = state.pending[name] || { since: srv.since, reasons: [] };
  if (!p.reasons.includes(reason)) p.reasons.push(reason);
  state.pending[name] = p; savePending();
  noticeUi.open = true; renderNotices();
}
function prunePending() {  // a stop or a restart applies the changes
  let changed = false;
  for (const [name, p] of Object.entries(state.pending)) {
    const srv = liveServer(name);
    if (!srv || !isUp(srv) || srv.since !== p.since) { delete state.pending[name]; changed = true; }
  }
  if (changed) { savePending(); renderNotices(); }
}
function pendingNotices() {
  return Object.entries(state.pending).map(([name, p]) => ({
    id: "pending:" + name, level: "warn", server: name, tab: null,
    text: `${name} is running, so these only take effect after a restart: ${p.reasons.join(", ")}.`,
    action: { label: "Restart now", fn: () => { if (confirm(`Restart ${name} now? Players are warned first and disconnected for a minute or two.`)) serverAction(name, "restart"); } },
  }));
}
const RUNNING_NOTE = "This server is running. Mods, plugins, the jar and most settings are only read when it starts, so changes wait for a restart. MCS will remind you with a Restart now button.";

/* ---------- notices: a button in the header that opens a list of things the owner should hear about ---------- */
const noticeUi = { open: false, list: [], seen: new Set(), wired: false };
const SNOOZE_MS = 7 * 24 * 3600 * 1000;  // a dismissed nudge comes back after a week: it is there for a reason
function dismissed() {
  try {
    const m = JSON.parse(localStorage.getItem("mcs-dismissed") || "{}");
    return Array.isArray(m) ? {} : m;
  } catch { return {}; }
}
function dismiss(id) { try { localStorage.setItem("mcs-dismissed", JSON.stringify({ ...dismissed(), [id]: Date.now() })); } catch { /* private window: it just comes back */ } }
const isSnoozed = (id) => Date.now() - (dismissed()[id] || 0) < SNOOZE_MS;
/* text with `commands` in backticks: the commands are shown as code */
const rich = (t) => h("span", {}, t.split("`").map((part, i) => i % 2 ? h("code", {}, part) : part));
function showHowto(title, lines) {
  const bg = modal(h("div", {}, h("h2", {}, title), ...lines.map((l) => h("p", {}, rich(l))), h("div", { class: "row end" }, h("button", { class: "btn primary", onclick: () => bg.remove() }, "Got it"))));
}
async function refreshNotices() {
  let list;
  try { list = await api("GET", "/api/notices"); } catch { return; }
  noticeUi.list = list.filter((n) => n.level !== "info" || !isSnoozed(n.id));
  const fresh = noticeUi.list.filter((n) => n.level === "bad" && !noticeUi.seen.has(n.id));  // a new problem opens the list once
  fresh.forEach((n) => noticeUi.seen.add(n.id));
  if (fresh.length) noticeUi.open = true;
  renderNotices();
}
function renderNotices() {
  const btn = document.getElementById("noticebtn"), box = document.getElementById("notices");
  if (!btn || !box) return;
  const list = [...pendingNotices(), ...noticeUi.list];
  const worst = list.some((n) => n.level === "bad") ? "bad" : list.some((n) => n.level === "warn") ? "warn" : "info";
  btn.hidden = !list.length;
  btn.className = "btn small notice-btn " + worst;
  btn.setAttribute("aria-expanded", String(noticeUi.open && list.length > 0));
  btn.replaceChildren(h("span", { class: "lbl" }, "Notices "), h("b", {}, String(list.length)));
  box.hidden = !(noticeUi.open && list.length > 0);
  box.replaceChildren(...list.map((n) => h("div", { class: "notice " + n.level },
    h("div", { class: "grow" }, n.text),
    h("div", { class: "row end" },
      n.level === "info" ? h("button", { class: "btn small", title: "Hide for a week", onclick: () => { dismiss(n.id); refreshNotices(); } }, "Remind me later") : null,
      n.howto ? h("button", { class: "btn small", onclick: () => { noticeUi.open = false; renderNotices(); showHowto("A separate copy of your backups", n.howto); } }, "How to set it up") : null,
      n.action ? h("button", { class: "btn small primary", onclick: () => { noticeUi.open = false; renderNotices(); n.action.fn(); } }, n.action.label) : null,
      n.server && n.tab ? h("button", { class: "btn small", onclick: () => { noticeUi.open = false; renderNotices(); location.hash = `#/s/${n.server}/${n.tab}`; } }, n.tab === "mods" ? "Open mods" : "Open backups") : null))));
}

async function refreshServers() {
  try { state.servers = await api("GET", "/api/instances"); } catch { return; }
  prunePending();
  renderSide();
  const cur = state.servers.find((s) => s.name === state.current);
  if (cur) updateHeader(cur);
}

function renderSide() {
  const side = document.getElementById("side");
  if (!side) return;
  const sig = JSON.stringify([state.current, state.servers.map((x) => [x.name, x.state, x.type, x.mc])]);
  if (side.dataset.sig === sig) return;
  side.dataset.sig = sig;
  side.replaceChildren(h("h3", {}, "Servers"),
    ...state.servers.map((s) => h("button", { class: "srv", "aria-current": s.name === state.current ? "page" : false, onclick: () => { location.hash = `#/s/${s.name}/${state.tab}`; } },
      h("span", { class: "dot " + s.state }),
      h("span", {}, h("span", { class: "nm" }, s.name), h("span", { class: "sub" }, s.type ? `${s.type} ${s.mc}` : "no server jar yet")))),
    h("button", { class: "btn small", onclick: openCreate }, "+ New server"));
}

/* ---------- routing ---------- */
function stopStreams() { if (state.es) { state.es.close(); state.es = null; } }

function route() {
  const main = document.getElementById("main");
  if (!main) return;
  stopStreams();
  const m = location.hash.match(/^#\/s\/([^/]+)(?:\/([a-z]+))?/);
  if (!m) {
    state.current = null; renderSide();
    main.replaceChildren(h("div", { class: "empty" },
      h("h2", {}, state.servers.length ? "Pick a server" : "No servers yet"),
      h("p", {}, state.servers.length ? "Choose one from the list to manage it." : "Create your first server to get started."),
      h("button", { class: "btn primary", onclick: openCreate }, "+ New server")));
    return;
  }
  state.current = decodeURIComponent(m[1]);
  state.tab = m[2] || "console";
  renderSide();
  renderServer();
}

const TABS = [["console", "Console"], ["settings", "Settings"], ["mods", "Mods & jar"], ["files", "Files"], ["backups", "Backups"]];

function renderServer() {
  const main = document.getElementById("main");
  const s = state.servers.find((x) => x.name === state.current);
  if (!s) { main.replaceChildren(h("div", { class: "empty" }, "That server does not exist.")); return; }
  const body = h("div", { id: "tabbody" });
  main.replaceChildren(
    h("div", { class: "row", id: "head" }),
    h("div", { class: "tabs", role: "tablist" }, TABS.map(([id, label]) =>
      h("button", { class: "tab", role: "tab", "aria-selected": String(id === state.tab), onclick: () => { location.hash = `#/s/${s.name}/${id}`; } }, label))),
    body);
  updateHeader(s);
  ({ console: tabConsole, settings: tabSettings, mods: tabMods, files: tabFiles, backups: tabBackups }[state.tab] || tabConsole)(body, s);
}

function updateHeader(s) {
  const head = document.getElementById("head");
  if (!head) return;
  const sig = JSON.stringify([s.name, s.state, s.status_text, s.result, s.type, s.mc, s.port, s.state === "active" ? Math.round((s.memory || 0) / 5e7) : 0]);
  if (head.dataset.sig === sig) return;  // unchanged: leave the DOM alone so clicks are not lost
  head.dataset.sig = sig;
  const busy = ["activating", "deactivating"].includes(s.state);
  const up = s.state === "active";
  const bits = [`port ${s.port}`];
  if (s.type) bits.unshift(`${s.type} ${s.mc}`);
  if (up && s.memory) bits.push(fmtBytes(s.memory) + " RAM");
  head.replaceChildren(h("h1", { class: "grow" }, s.name),
    h("span", { class: "badge " + s.state }, h("span", { class: "dot " + s.state }), s.state === "active" && s.status_text ? s.status_text : s.state),
    h("span", { class: "muted small" }, bits.join(" · ")),
    h("button", { class: "btn primary", disabled: up || busy, onclick: () => serverAction(s.name, "start") }, "Start"),
    h("button", { class: "btn", disabled: !up && !busy, onclick: () => serverAction(s.name, "restart") }, "Restart"),
    h("button", { class: "btn danger", disabled: !up && !busy, onclick: () => serverAction(s.name, "stop") }, "Stop"));
  if (s.state === "failed" && s.result) head.append(h("div", { class: "note warn grow", style: false }, `Last run failed (${s.result}). `, h("a", { href: `#/s/${s.name}/console` }, "Show what happened in the console"), ". Mods that cannot work together are listed on the Mods tab."));
}

async function serverAction(name, action) {
  if (action === "start" || action === "restart") {  // caution tape: say what is wrong before a start that will probably fail
    try {
      const bad = (await api("GET", `/api/instances/${name}/modcheck`)).filter((p) => p.level === "bad");
      if (bad.length && !confirm(`${name} will probably not start:\n\n${bad.slice(0, 4).map((p) => "• " + p.text).join("\n")}\n\nStart it anyway?`)) return;
    } catch { /* the check is a courtesy: never stand in the way */ }
  }
  try { await api("POST", `/api/instances/${name}/action`, { action }); toast(`${action} requested`); setTimeout(refreshServers, 700); } catch (e) { toast(e.message, true); }
}

/* ---------- console ---------- */
/* Colour by what kind of line it is. The words stay in the line (ERROR, WARN), so colour is never the only clue. */
function lineClass(t) {
  if (/\/(ERROR|FATAL)\]|\bFATAL\b/.test(t)) return "err";
  if (/\/WARN\]|\bWARN\b/.test(t)) return "warn";  // the level the game gave the line wins over words inside it
  if (/^\s+at [\w$.]+\(|^Caused by:|Exception\b|Incompatible mods|Mod resolution failed|Crash/.test(t)) return "err";
  if (/\]: <[^>]+> /.test(t)) return "chat";
  if (/ (joined|left) the game|lost connection/.test(t)) return "join";
  if (/^(Started|Stopped|Stopping|Starting|Succeeded|Deactivated|Consumed)\b|^MCS@|systemd/.test(t)) return "dim";
  return "";
}

function tabConsole(body, s) {
  const log = h("div", { class: "console", tabindex: "0", role: "log", "aria-label": "Server console" });
  const input = h("input", { type: "text", placeholder: "Type a command, e.g. list, say hi, op <name>", "aria-label": "Console command", autocomplete: "off", maxlength: "1000" });
  const hist = []; let hi = 0;
  async function send(text) {
    if (!text.trim()) return;
    hist.push(text); hi = hist.length;
    try { await api("POST", `/api/instances/${s.name}/command`, { command: text }); } catch (e) { toast(e.message, true); }
  }
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp" && hi > 0) { input.value = hist[--hi]; e.preventDefault(); }
    if (e.key === "ArrowDown") { input.value = hi < hist.length - 1 ? hist[++hi] : ""; hi = Math.min(hi, hist.length); e.preventDefault(); }
  });
  body.append(h("div", { class: "card" }, log,
    h("form", { class: "cmd", onsubmit: (e) => { e.preventDefault(); send(input.value); input.value = ""; } }, input, h("button", { class: "btn primary", type: "submit" }, "Send")),
    h("div", { class: "chips" }, ["list", "save-all", "whitelist list", "tps"].map((c) => h("button", { class: "btn small", onclick: () => send(c) }, c)))));
  const es = new EventSource(`/api/instances/${encodeURIComponent(s.name)}/console?n=300`);
  state.es = es;
  let lines = 0;
  es.onmessage = (ev) => {
    const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
    log.append(h("span", { class: "cl " + lineClass(ev.data) }, ev.data + "\n"));
    if (++lines > 2500) { log.firstChild.remove(); lines--; }
    if (stick) log.scrollTop = log.scrollHeight;
  };
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) log.append(document.createTextNode("[console disconnected]\n")); };
}

/* ---------- settings ---------- */
/* The settings people actually look for, in this order, ahead of the long alphabetical list. */
const PROP_TOP = ["level-seed", "motd", "difficulty", "gamemode", "max-players", "pvp", "view-distance", "white-list", "hardcore"];
const PROP_HINT = {
  "level-seed": "Only used when a NEW world is made. Changing it does not change a world that already exists. To use it, make a backup, then delete the world folder in Files and start the server. Blank = random.",
  "white-list": "Only listed players can join. Leave this on unless you know why.",
};
const PROP_SELECT = { difficulty: ["peaceful", "easy", "normal", "hard"], gamemode: ["survival", "creative", "adventure", "spectator"] };

async function tabSettings(body, s) {
  let info;
  try { info = await api("GET", `/api/instances/${s.name}`); } catch (e) { body.append(h("p", { class: "err" }, e.message)); return; }
  const running = ["active", "activating"].includes(s.state);
  const note = running ? h("div", { class: "note warn" }, "The server is running. Changes apply after a restart.") : null;
  const c = info.conf;
  const f = {};
  const jarOpts = info.jars.length ? info.jars : [c.SERVER || ""];
  f.SERVER = h("select", { id: "f-SERVER" }, jarOpts.map((j) => h("option", { value: j, selected: j === c.SERVER }, j || "(none)")));
  const jdks = info.jdks.includes(c.JAVA_HOME) ? info.jdks : [c.JAVA_HOME, ...info.jdks].filter(Boolean);
  f.JAVA_HOME = h("select", { id: "f-JAVA_HOME" }, jdks.map((j) => h("option", { value: j, selected: j === c.JAVA_HOME }, j)));
  f.RAM = h("input", { type: "text", id: "f-RAM", value: c.RAM || "" });
  f.TIMER = h("input", { type: "number", id: "f-TIMER", min: "0", max: "300", value: c.TIMER || "10" });
  f.SIGNOFF = h("input", { type: "text", id: "f-SIGNOFF", value: c.SIGNOFF || "" });
  f.SAVEnX = h("input", { type: "text", id: "f-SAVEnX", value: c.SAVEnX || "" });
  f.ARGS = h("textarea", { id: "f-ARGS", spellcheck: "false" }, c.ARGS || "");
  const fld = (k, label, hint) => h("div", { class: "field" }, h("label", { for: "f-" + k }, label), f[k], hint ? h("div", { class: "muted small" }, hint) : null);
  const svcCard = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    const values = {}; for (const k of Object.keys(f)) values[k] = f[k].value;
    try { await api("PUT", `/api/instances/${s.name}/service`, { values }); markPending(s.name, "server settings changed"); toast("Saved. Restart the server to apply."); refreshServers(); } catch (x) { toast(x.message, true); }
  } }, h("h2", {}, "Server"),
    h("div", { class: "grid" }, fld("SERVER", "Server jar", info.jars.length ? null : "No jar yet. Use the Mods & jar tab."), fld("JAVA_HOME", "Java", "Newer Minecraft needs newer Java."),
      fld("RAM", "Memory", "Keep -Xmx and -Xms equal, e.g. -Xmx4G -Xms4G"), fld("TIMER", "Shutdown warning (seconds)")),
    h("div", { class: "grid" }, fld("SIGNOFF", "Warning message", "@TIMER@ becomes the seconds"), fld("SAVEnX", "Goodbye message")),
    h("details", {}, h("summary", {}, "Advanced: Java arguments"), fld("ARGS", "Java arguments", "Leave alone unless you know why.")),
    h("div", { class: "row end" }, h("button", { class: "btn primary", type: "submit" }, "Save server settings")));

  const inputs = {};
  const filter = h("input", { type: "text", placeholder: "Filter settings…", "aria-label": "Filter settings" });
  const grid = h("div", { class: "grid" });
  const ordered = [...info.properties].sort((a, b) => {
    const ia = PROP_TOP.indexOf(a.key), ib = PROP_TOP.indexOf(b.key);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);  // stable: the rest keep their order
  });
  for (const { key, value, locked } of ordered) {
    let el;
    if (locked) el = h("input", { type: "text", value, disabled: true });
    else if (PROP_SELECT[key]) el = h("select", {}, PROP_SELECT[key].map((o) => h("option", { value: o, selected: o === value }, o)));
    else if (value === "true" || value === "false") el = h("select", {}, ["true", "false"].map((o) => h("option", { value: o, selected: o === value }, o)));
    else el = h("input", { type: /^\d+$/.test(value) ? "number" : "text", value });
    el.dataset.orig = value; el.id = "p-" + key; inputs[key] = el;
    grid.append(h("div", { class: "field", "data-key": key }, h("label", { for: "p-" + key }, key), el, locked ? h("div", { class: "muted small" }, "Set on the server with MCSrcon.") : PROP_HINT[key] ? h("div", { class: "muted small" }, PROP_HINT[key]) : null));
  }
  filter.addEventListener("input", () => {
    const q = filter.value.toLowerCase();
    for (const d of grid.children) d.classList.toggle("hidden", !d.dataset.key.includes(q));
  });
  const propCard = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    const values = {};
    for (const [k, el] of Object.entries(inputs)) if (el.value !== el.dataset.orig) values[k] = el.value;
    if (!Object.keys(values).length) return toast("Nothing changed");
    try { await api("PUT", `/api/instances/${s.name}/properties`, { values }); for (const k of Object.keys(values)) inputs[k].dataset.orig = values[k]; markPending(s.name, "game settings changed"); toast("Saved. Restart the server to apply."); if (values["online-mode"] !== undefined) setTimeout(() => { const b = document.getElementById("tabbody"); if (b && state.tab === "settings") { b.replaceChildren(); tabSettings(b, s); } }, 300); } catch (x) { toast(x.message, true); }
  } }, h("h2", {}, "Game (server.properties)"),
    info.properties.length ? null : h("p", { class: "muted" }, "No server.properties yet. It is created on first run."),
    h("div", { class: "field" }, filter), grid,
    h("div", { class: "row end" }, h("button", { class: "btn primary", type: "submit" }, "Save game settings")));
  const onlineOff = info.properties.some((p) => p.key === "online-mode" && p.value === "false");
  const onlineNote = onlineOff ? h("div", { class: "note warn" }, "online-mode is false: the server does not check players against Mojang, so anyone can join as any name, including an operator's. Use this only on purpose, for example behind a trusted proxy.") : null;
  const pname = h("input", { type: "text", maxlength: "16", placeholder: "Minecraft name", "aria-label": "Minecraft name" });
  const pop = h("input", { type: "checkbox", id: "pop" });
  const playerCard = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault(); if (!pname.value.trim()) return;
    try { const r = await api("POST", `/api/instances/${s.name}/players`, { name: pname.value.trim(), op: pop.checked }); toast(r.live ? `Added ${r.name} through the server console. It is live now.` : `Added ${r.name}.`); pname.value = ""; pop.checked = false; } catch (x) { toast(x.message, true); }
  } }, h("h2", {}, "Players"),
    h("p", { class: "muted small" }, "The whitelist is on: only these players can join. Names are checked with Mojang."),
    h("div", { class: "row" }, h("div", { class: "grow" }, pname), h("label", { for: "pop", style: "margin:0" }, pop, " Operator"), h("button", { class: "btn primary", type: "submit" }, "Add player")));
  body.append(...[note, onlineNote, svcCard, playerCard, propCard].filter(Boolean));
}

/* ---------- jobs ---------- */
function runJob(job, onDone) {
  const dock = h("div", { class: "dock", role: "status" });
  document.body.append(dock);
  const title = h("strong", {}), stateEl = h("span", { class: "badge" }), log = h("div", { class: "joblog" });
  const close = h("button", { class: "btn small", onclick: () => dock.remove() }, "Close");
  dock.append(h("div", { class: "card" }, h("div", { class: "row" }, title, stateEl, h("span", { class: "spacer" }), close), log));
  async function tick() {
    let j; try { j = await api("GET", `/api/jobs/${job.id}`); } catch { return setTimeout(tick, 2000); }
    title.textContent = j.title; stateEl.textContent = j.state; stateEl.className = "badge " + (j.state === "done" ? "active" : j.state === "failed" ? "failed" : "activating");
    log.textContent = j.lines.join("\n"); log.scrollTop = log.scrollHeight;
    if (j.state === "running") return setTimeout(tick, 900);
    if (onDone) onDone(j);
    refreshServers();
  }
  tick();
}
async function startJob(promise, onDone) { try { runJob(await promise, onDone); } catch (e) { toast(e.message, true); } }

/* ---------- mods & jar ---------- */
async function tabMods(body, s) {
  let info;
  try { info = await api("GET", `/api/instances/${s.name}`); } catch (e) { body.append(h("p", { class: "err" }, e.message)); return; }
  const refresh = () => { const b = document.getElementById("tabbody"); if (b && state.tab === "mods") { b.replaceChildren(); tabMods(b, s); } };
  const serverBusy = ["active", "activating", "deactivating"].includes(s.state);
  const isPlugin = info.loader === "paper";
  const noun = isPlugin ? "plugin" : "mod";
  const changed = (reason) => (j) => { if (j && j.state === "done") markPending(s.name, reason); refresh(); };
  if (isUp(liveServer(s.name))) body.append(h("div", { class: "note warn" }, RUNNING_NOTE));

  try {
    const probs = await api("GET", `/api/instances/${s.name}/modcheck`);
    if (probs.length) body.append(h("div", { class: "card" }, h("h2", {}, "Problems found"),
      h("p", { class: "muted small" }, "Read from the files in the mods folder, the way the game's loader would, before it starts."),
      h("div", { class: "list" }, probs.map((p) => h("div", { class: "item" }, h("div", { class: "grow" },
        h("div", { class: p.level === "bad" ? "err" : "" }, p.text), p.fix ? h("div", { class: "muted small" }, p.fix) : null, h("div", { class: "muted small mono" }, p.file)))))));
  } catch { /* no check, no card */ }
  /* server software */
  const typeSel = h("select", { "aria-label": "Server type" }, ["vanilla", "paper", "fabric"].map((t) => h("option", { value: t, selected: t === info.type }, t)));
  const verSel = h("select", { "aria-label": "Minecraft version" }, h("option", { value: "latest" }, "latest"));
  async function loadVersions() {
    verSel.replaceChildren(h("option", { value: "latest" }, "latest"));
    try { for (const v of await api("GET", `/api/versions?type=${typeSel.value}`)) verSel.append(h("option", { value: v }, v)); } catch (e) { toast(e.message, true); }
  }
  typeSel.addEventListener("change", loadVersions); loadVersions();
  const jarCard = h("div", { class: "card" }, h("h2", {}, "Server software"),
    info.type ? h("p", { class: "muted" }, `Current: ${info.type} ${info.mc}` + (info.jar ? ` (${info.jar})` : "")) : h("div", { class: "note" }, "No server jar yet. Pick a type and version, then download. The right Java version is chosen for you."),
    h("div", { class: "row" }, h("div", { class: "grow" }, typeSel), h("div", { class: "grow" }, verSel),
      h("button", { class: "btn primary", disabled: serverBusy, title: serverBusy ? "Stop the server first" : false, onclick: () => {
        if (info.mods && Object.keys(info.mods).length && !confirm("Changing the Minecraft version can break installed mods. Continue?")) return;
        startJob(api("POST", `/api/instances/${s.name}/jar`, { type: typeSel.value, mc: verSel.value }), refresh);
      } }, "Download")),
    h("p", { class: "muted small" }, "Forge and NeoForge are not supported yet: their downloads are installers, not server jars. Stop the server before changing its jar."));
  body.append(jarCard);

  if (!info.loader) { body.append(h("div", { class: "note" }, "Vanilla servers take no mods. Choose Fabric (mods) or Paper (plugins) above.")); return; }

  /* search */
  const q = h("input", { type: "text", placeholder: `Search ${noun}s for ${info.mc}…`, "aria-label": `Search ${noun}s`, maxlength: "100" });
  const src = h("select", { "aria-label": "Source" }, h("option", { value: "modrinth" }, "Modrinth"),
    !isPlugin && state.settings.curseforge_available ? h("option", { value: "curseforge" }, "CurseForge") : null,
    !isPlugin && state.settings.curseforge_available ? h("option", { value: "both" }, "Both") : null);
  const results = h("div", { class: "list" });
  /* Plain link-outs to the sites themselves: MCS never embeds or proxies their pages. */
  const mrBrowse = isPlugin ? "https://modrinth.com/plugins" : "https://modrinth.com/mods";
  const mrQuery = "?" + [info.loader ? `g=categories:${encodeURIComponent(info.loader)}` : "", info.mc ? `v=${encodeURIComponent(info.mc)}` : ""].filter(Boolean).join("&");
  const browseLink = (href, label) => h("a", { class: "btn small", href, target: "_blank", rel: "noopener noreferrer" }, `${label} ↗`);
  const searchCard = h("div", { class: "card" }, h("div", { class: "row" }, h("h2", { class: "grow" }, `Add ${noun}s`),
    browseLink(mrBrowse + mrQuery, "Browse Modrinth"),
    isPlugin ? null : browseLink("https://www.curseforge.com/minecraft/mc-mods", "Browse CurseForge")),
    h("form", { class: "row", onsubmit: async (e) => {
      e.preventDefault(); if (!q.value.trim()) return;
      results.replaceChildren(h("p", { class: "muted" }, "Searching…"));
      try {
        const rows = await api("GET", `/api/instances/${s.name}/search?q=${encodeURIComponent(q.value)}&source=${src.value}`);
        results.replaceChildren(...(rows.length ? rows.map(resultRow) : [h("p", { class: "muted" }, "Nothing found for this version and loader (client-only mods are hidden).")]));
      } catch (x) { results.replaceChildren(h("p", { class: "err" }, x.message)); }
    } }, h("div", { class: "grow" }, q), src, h("button", { class: "btn primary", type: "submit" }, "Search")),
    results, !isPlugin && !state.settings.curseforge_available ? h("p", { class: "muted small" }, "CurseForge search is not available: there is no key. You can still download from their site and upload the jar on the Files tab.") : null);
  function resultRow(r) {
    const installed = Object.values(info.mods || {}).some((m) => m.project === r.id || m.slug === r.id);
    return h("div", { class: "item" }, r.icon ? h("img", { class: "icon", src: r.icon, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: "icon" }),
      h("div", { class: "grow" }, h("div", {}, h("strong", {}, r.title), " ", h("span", { class: "tag" }, r.source)),
        h("div", { class: "muted small" }, r.description), h("div", { class: "muted small" }, `${fmtNum(r.downloads)} downloads`)),
      h("div", { class: "row" }, r.url ? h("a", { href: r.url, target: "_blank", rel: "noopener noreferrer", class: "small" }, `View on ${r.source === "modrinth" ? "Modrinth" : "CurseForge"} ↗`) : null,
        h("button", { class: "btn small primary", disabled: installed, onclick: () => okToChange(s.name, `Adding ${r.title}`) && startJob(api("POST", `/api/instances/${s.name}/mods`, { specs: [`${r.source}:${r.id}`], source: r.source }), changed(`${r.title} added`)) }, installed ? "Installed" : "Install")));
  }
  body.append(searchCard);

  /* installed */
  const mods = Object.entries(info.mods || {}).sort((a, b) => a[1].title.localeCompare(b[1].title));
  const pageUrl = (m) => m.source === "modrinth" ? `https://modrinth.com/project/${m.slug}` : (m.slug ? `https://www.curseforge.com/minecraft/mc-mods/${m.slug}` : null);
  body.append(h("div", { class: "card" }, h("div", { class: "row" }, h("h2", { class: "grow" }, `Installed ${noun}s`),
    h("button", { class: "btn small", onclick: () => startJob(api("POST", `/api/instances/${s.name}/mods/update`, { apply: false })) }, "Check for updates"),
    h("button", { class: "btn small", onclick: () => confirm("Install all available updates?") && okToChange(s.name, "Updating mods") && startJob(api("POST", `/api/instances/${s.name}/mods/update`, { apply: true }), changed("mods updated")) }, "Update all")),
    mods.length ? h("div", { class: "list" }, mods.map(([key, m]) => h("div", { class: "item" },
      h("div", { class: "grow" }, h("strong", {}, m.title), " ", m.explicit ? null : h("span", { class: "tag" }, "dependency"), h("div", { class: "muted small" }, `${m.version} · ${m.file}`)),
      pageUrl(m) ? h("a", { class: "small", href: pageUrl(m), target: "_blank", rel: "noopener noreferrer" }, "View ↗") : null,
      h("button", { class: "btn small danger", onclick: () => confirm(`Remove ${m.title}? Dependencies nothing else needs are removed too.`) && okToChange(s.name, `Removing ${m.title}`) && startJob(api("POST", `/api/instances/${s.name}/mods/remove`, { key, prune: true }), changed(`${m.title} removed`)) }, "Remove"))))
      : h("p", { class: "muted" }, `No ${noun}s installed through MCS yet.`)));
}

/* ---------- files ---------- */
const FILE_REASON = { mods: "mod files changed", plugins: "plugin files changed", config: "config files changed", root: "the server jar changed" };
async function tabFiles(body, s) {
  let files;
  try { files = await api("GET", `/api/instances/${s.name}/files`); } catch (e) { body.append(h("p", { class: "err" }, e.message)); return; }
  const refresh = () => { const b = document.getElementById("tabbody"); if (b && state.tab === "files") { b.replaceChildren(); tabFiles(b, s); } };
  const dest = h("select", { "aria-label": "Upload to" }, [["mods", "Mods"], ["plugins", "Plugins"], ["config", "Config"], ["root", "Server jar"]].map(([v, l]) => h("option", { value: v }, l)));
  const pick = h("input", { type: "file", multiple: true, "aria-label": "Files to upload" });
  const prog = h("progress", { max: "100", value: "0", class: "hidden" });
  function upload(file) {
    return new Promise((resolve) => {
      const x = new XMLHttpRequest();
      x.open("PUT", `/api/instances/${encodeURIComponent(s.name)}/upload?dir=${dest.value}&name=${encodeURIComponent(file.name)}`);
      x.setRequestHeader("X-MCS", "1");
      x.upload.onprogress = (e) => { prog.classList.remove("hidden"); prog.value = e.lengthComputable ? (e.loaded / e.total) * 100 : 0; };
      x.onload = () => { let m = ""; try { m = JSON.parse(x.responseText).error; } catch { /* ok */ } if (x.status === 200) toast(`Uploaded ${file.name}`); else toast(`${file.name}: ${m || x.status}`, true); resolve(); };
      x.onerror = () => { toast(`${file.name}: upload failed`, true); resolve(); };
      x.send(file);
    });
  }
  const form = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    if (!pick.files.length || !okToChange(s.name, "Changing files")) return;
    for (const f of pick.files) await upload(f);
    markPending(s.name, FILE_REASON[dest.value]);
    prog.classList.add("hidden"); refresh(); refreshServers();
  } }, h("h2", {}, "Upload"), h("div", { class: "row" }, h("div", {}, dest), h("div", { class: "grow" }, pick), h("button", { class: "btn primary", type: "submit" }, "Upload")), prog,
    h("p", { class: "muted small" }, "Jars are code that runs on your server. Only upload files from sources you trust. Uploaded jars are not tracked for updates."));
  if (isUp(liveServer(s.name))) body.append(h("div", { class: "note warn" }, RUNNING_NOTE));
  body.append(form);
  const group = (key, title, canDelete) => h("div", { class: "card" }, h("h2", {}, title),
    files[key].length ? h("div", { class: "list" }, files[key].map((f) => h("div", { class: "item" }, h("div", { class: "grow" }, f.name, " ", f.managed ? h("span", { class: "tag" }, "managed") : null), h("span", { class: "muted small" }, fmtBytes(f.size)),
      canDelete ? h("button", { class: "btn small danger", onclick: async () => { if (!confirm(`Delete ${f.name}?`) || !okToChange(s.name, `Deleting ${f.name}`)) return; try { await api("DELETE", `/api/instances/${s.name}/files?dir=${key}&name=${encodeURIComponent(f.name)}`); markPending(s.name, FILE_REASON[key]); refresh(); } catch (e) { toast(e.message, true); } } }, "Delete") : null)))
      : h("p", { class: "muted" }, "Nothing here."));
  body.append(group("root", "Server jars", false), group("mods", "mods/", true), group("plugins", "plugins/", true), group("config", "config/", true));
}

/* ---------- backups ---------- */
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const KIND_LABEL = { clean: "safe copy", live: "quick copy (taken while running)", prerestore: "saved just before a restore" };
async function tabBackups(body, s) {
  let list, sched, off;
  try {
    [list, sched, off] = await Promise.all([api("GET", `/api/instances/${s.name}/backups`), api("GET", `/api/instances/${s.name}/backup-schedule`), api("GET", "/api/offbox")]);
  } catch (e) { body.append(h("p", { class: "err" }, e.message)); return; }
  const refresh = () => { refreshNotices(); const b = document.getElementById("tabbody"); if (b && state.tab === "backups") { b.replaceChildren(); tabBackups(b, s); } };
  const cfg = sched.config, st = sched.state || {};

  /* schedule */
  const on = h("input", { type: "checkbox", id: "bk-on", checked: cfg.enabled });
  const time = h("input", { type: "time", value: cfg.time, "aria-label": "Backup time", required: true });
  const days = DAY_NAMES.map((d, i) => h("label", { class: "chip" }, h("input", { type: "checkbox", checked: cfg.days.includes(i), "data-day": String(i) }), " " + d));
  const keep = h("input", { type: "number", min: "1", max: "60", value: String(cfg.keep), "aria-label": "Backups to keep" });
  const weekly = h("input", { type: "number", min: "0", max: "52", value: String(cfg.keep_weekly), "aria-label": "Weekly backups to keep" });
  const warn = h("input", { type: "number", min: "0", max: "30", value: String(cfg.warn_minutes), "aria-label": "Warning minutes" });
  const newest = list.find((b) => b.kind === "clean");
  const est = h("p", { class: "muted small" });
  const updateEst = () => {
    const n = (Number(keep.value) || 0) + (Number(weekly.value) || 0);
    est.textContent = newest
      ? `Each backup is about ${fmtBytes(newest.size)}, so this keeps about ${n} of them: roughly ${fmtBytes(newest.size * n)} of disk (a growing world takes more).`
      : "Once the first backup exists, this shows how much disk the settings will use.";
  };
  keep.addEventListener("input", updateEst); weekly.addEventListener("input", updateEst); updateEst();
  const downNote = st.last_downtime ? `Last time the server was down for about ${Math.max(1, Math.round(st.last_downtime / 60))} minute(s).` : "The server is usually down for a few minutes.";
  const lastLine = st.last_status
    ? h("p", { class: "small " + (st.last_status === "failed" ? "err" : "muted") },
      `Last scheduled backup: ${st.last_status}${st.last_time ? " (" + new Date(st.last_time * 1000).toLocaleString() + ")" : ""}. ${st.last_message || ""}`)
    : h("p", { class: "muted small" }, "No scheduled backup has run yet.");
  const skippedNote = (st.last_skipped || []).length
    ? h("p", { class: "note warn" }, "Not backed up (links or special files): " + st.last_skipped.join(", ") + ". Replace each with a real file or folder, and the next backup will include it.") : null;
  const schedCard = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    const chosen = days.map((l) => l.firstChild).filter((c) => c.checked).map((c) => Number(c.dataset.day));
    try {
      await api("PUT", `/api/instances/${s.name}/backup-schedule`, { enabled: on.checked, time: time.value, days: chosen, keep: Number(keep.value), keep_weekly: Number(weekly.value), warn_minutes: Number(warn.value) });
      toast("Backup schedule saved."); refresh();
    } catch (x) { toast(x.message, true); }
  } },
    h("h2", {}, "Automatic backups"),
    h("p", { class: "muted small" }, "A running server can only be copied safely while it is stopped. So at backup time: the server tells players it is restarting, saves the world, stops, is copied and checked, and starts again. " + downNote),
    h("div", { class: "row" }, h("label", { for: "bk-on", style: "margin:0" }, on, " Back up automatically"), h("div", {}, time)),
    h("div", { class: "row wrap" }, days),
    h("div", { class: "row" },
      h("div", { class: "field grow" }, h("label", {}, "Keep the newest backups"), keep),
      h("div", { class: "field grow" }, h("label", {}, "Plus one a week for (weeks)"), weekly),
      h("div", { class: "field grow" }, h("label", {}, "Warn players (minutes)"), warn)),
    est,
    h("p", { class: "muted small" }, "A griefed world is sometimes noticed days later. The weekly copies let you go back further without keeping every day. Set the weekly number to 0 if disk space is tight."),
    sched.next ? h("p", { class: "small" }, `Next backup: ${new Date(sched.next).toLocaleString()}`) : h("p", { class: "small muted" }, "Automatic backups are off."),
    lastLine, skippedNote,
    h("p", { class: "muted small" }, `If the computer is off or busy for more than ${sched.grace_hours} hours at backup time, that backup is skipped instead of stopping the server in the middle of the day.`),
    h("div", { class: "row end" }, h("button", { class: "btn primary", type: "submit" }, "Save schedule")));

  /* manual + list */
  const lt = st.last_test;
  const testLine = lt
    ? h("p", { class: "small " + (lt.ok === false ? "err" : "muted") }, `Last backup test (${new Date(lt.time * 1000).toLocaleString()}, ${lt.file}): ${lt.message}`)
    : h("p", { class: "muted small" }, "A backup you have never tested is a guess. The newest backup is tested automatically once a week, and you can press Test on any of them. Testing unpacks it into a scratch folder and checks it, without touching your server.");
  const running = ["active", "activating", "deactivating"].includes(s.state);
  const restore = async (b) => {
    const warnTxt = `Restore ${s.name} from ${b.name}?\n\nThis replaces the server's world, mods and settings with that backup.` +
      (running ? "\nThe server will be stopped and started again." : "") +
      "\nYour current state is saved first as its own backup, so you can undo this.";
    if (!confirm(warnTxt)) return;
    startJob(api("POST", `/api/instances/${s.name}/backups/${encodeURIComponent(b.name)}/restore`, { confirm: true }), refresh);
  };
  const listCard = h("div", { class: "card" },
    h("div", { class: "row" }, h("h2", { class: "grow" }, "Backups"),
      h("button", { class: "btn primary", title: "Warns players, stops the server, copies it, starts it again", onclick: () => {
        if (running && !confirm("Back up now? The server will warn players, stop, be copied, and start again. It will be down for a few minutes.")) return;
        startJob(api("POST", `/api/instances/${s.name}/backups`, { mode: "clean" }), refresh);
      } }, "Back up now (safe)")),
    running ? h("div", { class: "row end" }, h("button", { class: "btn small", onclick: () => {
      if (!confirm("A copy taken while the server runs can be damaged (the world is changing as it is copied), so it might not restore. Use the safe backup if you can. Make a quick copy anyway?")) return;
      startJob(api("POST", `/api/instances/${s.name}/backups`, { mode: "live" }), refresh);
    } }, "Quick copy without stopping (can be damaged)")) : null,
    h("p", { class: "muted small" }, "Each backup is read back and checked when it is made. Worlds, mods, configs and settings are included. Libraries and logs are not (they download again)."),
    testLine,
    list.length ? h("div", { class: "list" }, list.map((b) => h("div", { class: "item" },
      h("div", { class: "grow" }, h("div", {}, b.name), h("div", { class: "muted small" }, `${new Date(b.time * 1000).toLocaleString()} · ${fmtBytes(b.size)} · ${KIND_LABEL[b.kind] || b.kind}`)),
      h("a", { class: "btn small", href: `/api/instances/${encodeURIComponent(s.name)}/backups/${encodeURIComponent(b.name)}`, download: b.name }, "Download"),
      b.kind === "prerestore" ? null : h("button", { class: "btn small", title: "Unpacks it into a scratch folder and checks it. Your server is not touched.", onclick: () => startJob(api("POST", `/api/instances/${s.name}/backups/${encodeURIComponent(b.name)}/test`, {}), refresh) }, "Test"),
      h("button", { class: "btn small", onclick: () => restore(b) }, "Restore"),
      h("button", { class: "btn small danger", onclick: async () => { if (!confirm(`Delete ${b.name}? This cannot be undone.`)) return; try { await api("DELETE", `/api/instances/${s.name}/backups/${encodeURIComponent(b.name)}`); refresh(); } catch (e) { toast(e.message, true); } } }, "Delete"))))
      : h("p", { class: "muted" }, "No backups yet."));
  const ago = (t) => { if (!t) return "never"; const m = Math.round((Date.now() / 1000 - t) / 60); return m < 90 ? `${Math.max(m, 1)} minute(s) ago` : m < 2880 ? `${Math.round(m / 60)} hour(s) ago` : `${Math.round(m / 1440)} days ago`; };
  const offCard = h("div", { class: "card" }, h("h2", {}, "Separate copy"),
    off.configured
      ? h("div", {},
        h("p", { class: "small" }, "Copying new backups to ", h("code", {}, off.dest || "?"), ". Last success: " + ago(off.last_ok) + (off.copied_files != null ? `. ${off.copied_files} backup(s) there${off.missing ? `, ${off.missing} still to copy` : ""}.` : ".")),
        off.error ? h("p", { class: "err" }, off.error) : null,
        off.same_disk ? h("p", { class: "note warn" }, "This is on the same disk as the servers, so it does not protect against the disk dying.") : null,
        h("p", { class: "muted small" }, "It never deletes anything there. Stop or change it with sudo MCSoffbox off / set, on the server computer."))
      : h("div", {},
        h("p", {}, "Your backups are only on this computer. If its disk dies or someone deletes them, they are gone too."),
        h("div", { class: "row end" }, h("button", { class: "btn primary", onclick: () => showHowto("A separate copy of your backups", off.howto || []) }, "How to set it up"))));
  body.append(schedCard, listCard, offCard);
}

/* ---------- modals ---------- */
function modal(content) {
  const bg = h("div", { class: "modal-bg", onclick: (e) => { if (e.target === bg) bg.remove(); } }, h("div", { class: "modal", role: "dialog", "aria-modal": "true" }, content));
  document.body.append(bg);
  const first = bg.querySelector("input,button,select"); if (first) first.focus();
  document.addEventListener("keydown", function esc(e) { if (e.key === "Escape") { bg.remove(); document.removeEventListener("keydown", esc); } });
  return bg;
}

async function openCreate() {
  let eula = { agreed: true, port: 25565 };
  try { eula = await api("GET", "/api/newserver"); } catch { /* ignore */ }
  const name = h("input", { type: "text", id: "nn", maxlength: "64", placeholder: "survival", autocomplete: "off", required: true });
  const op = h("input", { type: "text", id: "op", maxlength: "16", placeholder: "YourMinecraftName", autocomplete: "off", required: true });
  const seed = h("input", { type: "text", id: "sd", maxlength: "128", placeholder: "blank = random", autocomplete: "off" });
  const port = h("input", { type: "number", id: "pt", min: "1024", max: "65535", value: String(eula.port || 25565), required: true });
  const agree = h("input", { type: "checkbox", id: "ag" });
  const err = h("div", { class: "err", role: "alert" });
  const bg = modal(h("form", { onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    try { await api("POST", "/api/instances", { name: name.value.trim(), agree: agree.checked, op_name: op.value.trim(), port: Number(port.value), seed: seed.value.trim() }); bg.remove(); await refreshServers(); location.hash = `#/s/${name.value.trim()}/mods`; toast("Server created. Pick its software next."); } catch (x) { err.textContent = x.message; }
  } }, h("h2", {}, "New server"),
    h("div", { class: "field" }, h("label", { for: "nn" }, "Name"), name, h("div", { class: "muted small" }, "Letters, numbers, dot, underscore, dash.")),
    h("div", { class: "field" }, h("label", { for: "op" }, "Your Minecraft name"), op, h("div", { class: "muted small" }, "You become the operator and are the first name on the whitelist. Only whitelisted players can join; add friends later on the Settings tab.")),
    h("div", { class: "field" }, h("label", { for: "sd" }, "World seed (optional)"), seed, h("div", { class: "muted small" }, "Makes the same world every time. Leave blank for a random one. It cannot be changed later for this world.")),
    h("div", { class: "field" }, h("label", { for: "pt" }, "Game port"), port, h("div", { class: "muted small" }, "Each server needs its own. A port other than 25565 cuts down random scanner traffic, but it is not security.")),
    eula.agreed ? h("p", { class: "muted small" }, "You have already accepted the Minecraft EULA on this machine.")
      : h("div", { class: "field" }, h("label", {}, agree, " I accept the ", h("a", { href: eula.url, target: "_blank", rel: "noopener noreferrer" }, "Minecraft EULA ↗")), h("div", { class: "muted small" }, "Required by Mojang to run a server. Nothing is set without your yes.")),
    err, h("div", { class: "row end" }, h("button", { class: "btn", type: "button", onclick: () => bg.remove() }, "Cancel"), h("button", { class: "btn primary", type: "submit" }, "Create"))));
}

async function openSettings() {
  state.settings = await api("GET", "/api/settings").catch(() => state.settings);
  const bg = modal(h("div", {}, h("h2", {}, "Settings"),
    h("p", { class: "small" }, state.settings.curseforge_available ? "Mod search: Modrinth and CurseForge." : "Mod search: Modrinth. CurseForge is not available: the owner can add a key on the server with sudo MCScred set curseforge."),
    h("p", { class: "muted small" }, `This interface is listening on ${state.settings.listen}:${state.settings.port}.`),
    h("div", { class: "row end" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Close"))));
}

boot();
