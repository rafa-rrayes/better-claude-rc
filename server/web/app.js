/* Better Claude RC — mobile SPA. No dependencies. */
"use strict";

// ---------------------------------------------------------------- utilities

const $ = (sel, root) => (root || document).querySelector(sel);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(c));
  }
  return node;
}

const esc = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

function relTime(ms) {
  if (!ms) return "";
  const d = Date.now() - ms;
  if (d < 60e3) return "now";
  if (d < 3600e3) return Math.floor(d / 60e3) + "m";
  if (d < 86400e3) return Math.floor(d / 3600e3) + "h";
  return Math.floor(d / 86400e3) + "d";
}

function fmtCost(v) {
  if (v === null || v === undefined) return "";
  return "$" + (v < 10 ? v.toFixed(2) : v.toFixed(0));
}

function fmtTokens(n) {
  if (n === null || n === undefined) return "";
  return n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
}

// Minimal markdown renderer (escape-first, safe).
function md(src) {
  if (!src) return "";
  const codeBlocks = [];
  src = String(src).replace(/```([^\n`]*)\n([\s\S]*?)```/g, (_, lang, body) => {
    codeBlocks.push(`<pre><code>${esc(body.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000B${codeBlocks.length - 1}\u0000`;
  });

  const inline = (text) => {
    const spans = [];
    text = text.replace(/`([^`]+)`/g, (_, c) => {
      spans.push(`<code>${esc(c)}</code>`);
      return `\u0000S${spans.length - 1}\u0000`;
    });
    text = esc(text)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return text.replace(/\u0000S(\d+)\u0000/g, (_, i) => spans[i]);
  };

  const lines = src.split("\n");
  const out = [];
  let list = null; // "ul" | "ol"
  let para = [];

  const flushPara = () => {
    if (para.length) { out.push(`<p>${inline(para.join("\n"))}</p>`); para = []; }
  };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const t = line.trim();

    if (/^\u0000B\d+\u0000$/.test(t)) {
      flushPara(); flushList();
      out.push(t); continue;
    }
    if (!t) { flushPara(); flushList(); continue; }

    // table
    if (t.startsWith("|") && lines[i + 1] && /^\|?[\s:|-]+\|?$/.test(lines[i + 1].trim())
        && lines[i + 1].includes("-")) {
      flushPara(); flushList();
      const cells = (row) => row.replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      let html = "<table><tr>" + cells(t).map((c) => `<th>${c}</th>`).join("") + "</tr>";
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        html += "<tr>" + cells(lines[i].trim()).map((c) => `<td>${c}</td>`).join("") + "</tr>";
        i++;
      }
      i--;
      out.push(html + "</table>"); continue;
    }

    const h = t.match(/^(#{1,4})\s+(.*)$/);
    if (h) { flushPara(); flushList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    if (/^(-{3,}|\*{3,})$/.test(t)) { flushPara(); flushList(); out.push("<hr>"); continue; }
    if (t.startsWith(">")) { flushPara(); flushList(); out.push(`<blockquote>${inline(t.replace(/^>\s?/, ""))}</blockquote>`); continue; }

    const ul = t.match(/^[-*+]\s+(.*)$/);
    const ol = t.match(/^\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      flushPara();
      const want = ul ? "ul" : "ol";
      if (list !== want) { flushList(); out.push(`<${want}>`); list = want; }
      out.push(`<li>${inline((ul || ol)[1])}</li>`);
      continue;
    }
    para.push(line);
  }
  flushPara(); flushList();
  return out.join("\n").replace(/\u0000B(\d+)\u0000/g, (_, i) => codeBlocks[i]);
}

// ---------------------------------------------------------------- state

const S = {
  authed: false,
  mode: null,          // "local" | "platform" (GET /api/env at boot)
  email: null,         // platform: signed-in account
  plan: null,          // platform: subscription plan from /api/me
  machines: [],        // platform: enrolled machines
  view: null,          // {name, ...params}
  sessions: [],
  tabs: [],            // sids open as tabs (insertion order)
  lastTab: null,       // sid of the most recently viewed tab
  events: {},          // sid -> [persisted events]
  seqs: {},            // sid -> last persisted seq
  meta: {},            // sid -> merged meta
  status: {},          // sid -> {state, detail}
  context: {},         // sid -> context usage
  subs: new Set(),
  wsUp: false,
  defaults: { models: ["default", "opus", "sonnet", "haiku"], modes: ["default", "acceptEdits", "plan"] },
  home: "",
  library: null,
  libraryAt: 0,
  projects: null,
  projectsAt: 0,
};

let _libPromise = null;
function loadLibrary() {
  if (S.library && Date.now() - S.libraryAt < 15000) return Promise.resolve(S.library);
  if (!_libPromise) {
    _libPromise = T.rpc("library")
      .then((lib) => { S.library = lib; S.libraryAt = Date.now(); return lib; })
      .finally(() => { _libPromise = null; });
  }
  return _libPromise;
}

let _projPromise = null;
function loadProjects() {
  if (S.projects && Date.now() - S.projectsAt < 30000) return Promise.resolve(S.projects);
  if (!_projPromise) {
    _projPromise = T.rpc("projects.recent")
      .then(({ projects }) => { S.projects = projects; S.projectsAt = Date.now(); return projects; })
      .finally(() => { _projPromise = null; });
  }
  return _projPromise;
}

const chatUI = { sid: null, byTool: {}, byReq: {}, provText: [], provThink: [], msgs: null };

// ---------------------------------------------------------------- tabs state

function loadTabs() {
  try { S.tabs = JSON.parse(localStorage.getItem("bcrc_tabs") || "[]"); } catch { S.tabs = []; }
  if (!Array.isArray(S.tabs)) S.tabs = [];
  S.lastTab = localStorage.getItem("bcrc_tab_active") || null;
}

function saveTabs() {
  localStorage.setItem("bcrc_tabs", JSON.stringify(S.tabs));
  if (S.lastTab) localStorage.setItem("bcrc_tab_active", S.lastTab);
  else localStorage.removeItem("bcrc_tab_active");
}

// drop tabs whose session no longer exists
function pruneTabs() {
  const live = new Set(S.sessions.map((s) => s.sid));
  const next = S.tabs.filter((sid) => live.has(sid));
  if (next.length !== S.tabs.length) { S.tabs = next; saveTabs(); }
}

function closeTab(sid) {
  S.tabs = S.tabs.filter((t) => t !== sid);
  if (S.lastTab === sid) S.lastTab = S.tabs[S.tabs.length - 1] || null;
  saveTabs();
}

loadTabs();

// ---------------------------------------------------------------- api
// REST is only used for auth (/api/login, /api/logout) and /api/env.
// Everything else rides the transport as rpc over the control channel.

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (res.status === 401) { S.authed = false; nav({ name: "login" }); throw new Error("unauthorized"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// ---------------------------------------------------------------- transport

let T = null; // set during boot (LocalTransport for now; see transport.js)

function wsSend(obj) {
  if (T && T.send(obj)) return true;
  toast("Not connected — retrying…", true);
  return false;
}

function onTransportStatus(st) {
  S.wsUp = !!st.up;
  updateConnUI();
  if (st.authRequired) { S.authed = false; nav({ name: "login" }); return; }
  if (st.pairRequired) {
    // the agent no longer trusts this device (revoked / never paired)
    const mid = localStorage.getItem("bcrc_machine");
    try { T?.close(); } catch {}
    T = null;
    if (mid) renderPairScreen(mid);
    return;
  }
  if (st.up) {
    if (S.mode === "platform" && !S._stateLoaded) { finishBoot(); }
    for (const sid of S.subs) {
      T.send({ type: "attach", session_id: sid, since_seq: S.seqs[sid] || 0 });
    }
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && S.authed && T) T.connect();
});

function handleWS(msg) {
  switch (msg.type) {
    case "sessions":
      S.sessions = msg.sessions || [];
      for (const s of S.sessions) S.status[s.sid] = { state: s.state, detail: s.detail };
      if (S.view?.name === "home") renderHome();
      if (S.view?.name === "sessions") renderSessions();
      if (S.view?.name === "tabs") renderTabs();
      if (S.view?.name === "chat") { updateChatHeader(); updateStatusLine(); }
      break;
    case "attached": {
      const sid = msg.session_id;
      if (msg.snapshot) upsertSession(msg.snapshot);
      if (msg.meta) S.meta[sid] = { ...(S.meta[sid] || {}), ...msg.meta };
      if (msg.context) S.context[sid] = msg.context;
      if (msg.status) S.status[sid] = msg.status;
      const evs = msg.events || [];
      if (!S.events[sid]) S.events[sid] = [];
      for (const ev of evs) {
        S.events[sid].push(ev);
        if (ev.seq) S.seqs[sid] = ev.seq;
        applyEventToState(sid, ev);
      }
      if (S.view?.name === "chat" && S.view.sid === sid) {
        if (evs.length && S.events[sid].length !== evs.length) {
          for (const ev of evs) renderLiveEvent(sid, ev);
        } else {
          renderChatTranscript(sid);
        }
        updateChatHeader(); updateStatusLine();
      }
      break;
    }
    case "event": {
      const sid = msg.session_id;
      const ev = msg.ev || {};
      if (ev.seq) {
        if ((S.seqs[sid] || 0) >= ev.seq) break; // duplicate
        S.seqs[sid] = ev.seq;
        (S.events[sid] = S.events[sid] || []).push(ev);
      }
      applyEventToState(sid, ev);
      if (S.view?.name === "chat" && S.view.sid === sid) {
        renderLiveEvent(sid, ev);
        updateChatHeader();
      }
      break;
    }
    case "error":
      toast(msg.message || "error", true);
      break;
  }
}

function upsertSession(snap) {
  const i = S.sessions.findIndex((s) => s.sid === snap.sid);
  if (i >= 0) S.sessions[i] = snap; else S.sessions.unshift(snap);
}

function applyEventToState(sid, ev) {
  switch (ev.kind) {
    case "status": S.status[sid] = { state: ev.state, detail: ev.detail }; if (S.view?.name === "chat" && S.view.sid === sid) updateStatusLine(); break;
    case "meta": S.meta[sid] = { ...(S.meta[sid] || {}), ...ev }; break;
    case "commands": S.meta[sid] = { ...(S.meta[sid] || {}), commands: ev.commands }; break;
    case "context": S.context[sid] = ev; if (S.view?.name === "chat" && S.view.sid === sid) updateChatHeader(); break;
  }
}

function subscribe(sid) {
  if (S.subs.has(sid)) return;
  S.subs.add(sid);
  // if the socket is down, T.send reconnects and onTransportStatus
  // re-attaches every S.subs entry once it is up again
  if (T) T.send({ type: "attach", session_id: sid, since_seq: S.seqs[sid] || 0 });
}

// ---------------------------------------------------------------- router

function nav(view) {
  S.view = view;
  closeSheet();
  closeDrawer();
  const hash = view.name === "chat" ? `#/chat/${view.sid}`
    : view.name === "term" ? `#/term/${view.sid}`
    : view.name === "project" ? `#/project?${encodeURIComponent(view.path)}`
    : `#/${view.name}`;
  if (location.hash !== hash) history.pushState(null, "", hash);
  render();
}

const LIST_VIEWS = ["sessions", "projects", "skills", "commands", "agents", "mcp"];

function routeFromHash() {
  const h = location.hash || "#/home";
  if (h.startsWith("#/chat/")) return { name: "chat", sid: h.slice(7) };
  if (h.startsWith("#/term/")) return { name: "term", sid: h.slice(7) };
  if (h.startsWith("#/tabs")) return { name: "tabs" };
  if (h.startsWith("#/project?")) return { name: "project", path: decodeURIComponent(h.slice(10)) };
  for (const name of LIST_VIEWS) if (h.startsWith("#/" + name)) return { name };
  if (h.startsWith("#/new")) return { name: "projects" };
  if (h.startsWith("#/library")) return { name: "skills" };
  return { name: "home" };
}

window.addEventListener("popstate", () => { S.view = routeFromHash(); closeSheet(); closeDrawer(); render(); });

// ---------------------------------------------------------------- shell

function setTopbar(...nodes) {
  const tb = $("#topbar");
  tb.replaceChildren(...nodes);
}

function backBtn(target) {
  return el("button", { class: "tb-btn", onclick: () => nav(target) }, "‹");
}

function menuBtn() {
  return el("button", { class: "tb-btn menu", "aria-label": "Open menu", onclick: openDrawer }, "❯");
}

function tbTitle(text) {
  return el("div", { class: "tb-title" }, el("span", { class: "crumb" }, text));
}

// Safari-style top-right button: open-tab count in a rounded square
function tabsBtn() {
  return el("button", { class: "tb-btn tabs-btn", "aria-label": "Open tabs",
    onclick: () => nav({ name: "tabs" }) },
    el("span", { class: "tabs-count" }, String(S.tabs.length)));
}

function updateConnUI() {
  const pill = $("#conn-pill");
  if (pill) pill.style.display = S.wsUp ? "none" : "block";
  if (S.view?.name === "chat") updateStatusLine();
}

function render() {
  if (!S.authed) { renderLogin(); return; }
  if (S.view?.name !== "term") disposeTerm();
  switch (S.view?.name) {
    case "chat": renderChat(S.view.sid); break;
    case "term": renderTerm(S.view.sid); break;
    case "tabs": renderTabs(); break;
    case "project": renderProject(S.view.path); break;
    case "sessions": renderSessions(); break;
    case "projects": renderProjects(); break;
    case "skills": case "commands": case "agents": case "mcp":
      renderLibraryList(S.view.name); break;
    default: renderHome();
  }
}

// ---------------------------------------------------------------- drawer

const NAV_TABS = [
  { name: "skills", label: "Skills", icon: "◆" },
  { name: "commands", label: "Commands", icon: "⌘" },
  { name: "agents", label: "Agents", icon: "⑃" },
  { name: "mcp", label: "MCPs", icon: "⚡" },
  { name: "sessions", label: "Sessions", icon: "❯" },
  { name: "projects", label: "Projects", icon: "▤" },
];

function navCount(name) {
  if (name === "sessions") return S.sessions.length;
  if (name === "projects") return (S.projects || []).filter((p) => p.exists).length;
  const lib = S.library;
  if (!lib) return "";
  return { skills: lib.skills.length, commands: lib.commands.length,
    agents: lib.agents.length, mcp: lib.mcp_servers.length }[name] ?? "";
}

function openDrawer() {
  closeDrawer();
  const active = S.view?.name;
  const backdrop = el("div", { class: "drawer-backdrop",
    onclick: (e) => { if (e.target === backdrop) closeDrawer(); } });
  const items = [{ name: "home", label: "Home", icon: "⌂" }, ...NAV_TABS];
  backdrop.append(el("aside", { class: "drawer" },
    el("div", { class: "drawer-head" }, el("span", { class: "glyph" }, "❯_"), "Claude RC"),
    el("nav", { class: "drawer-nav" },
      items.map((t) => el("button", {
        class: "nav-item" + (active === t.name ? " on" : ""),
        onclick: () => nav({ name: t.name }),
      },
        el("span", { class: "n-icon" }, t.icon),
        el("span", { class: "n-label" }, t.label),
        el("span", { class: "n-count" }, String(navCount(t.name) || ""))))),
    el("div", { class: "drawer-foot" },
      el("span", { class: S.wsUp ? "conn-ok" : "conn-bad" }, S.wsUp ? "● connected" : "○ offline"),
      S.mode === "platform"
        ? el("button", { class: "foot-btn", onclick: () => {
            closeDrawer();
            refreshMachines().finally(() => renderMachinePicker());
          } }, "machines")
        : null,
      S.mode === "platform"
        ? el("button", { class: "foot-btn", onclick: () => {
            closeDrawer();
            renderDevicesScreen();
          } }, "devices")
        : null,
      el("button", { class: "foot-btn", onclick: doLogout }, "log out"))));
  $("#drawer-root").append(backdrop);
  loadLibrary().then(() => {
    // fill in counts once the library arrives
    if (!$("#drawer-root").children.length) return;
    backdrop.querySelectorAll(".nav-item").forEach((btn, i) => {
      const c = btn.querySelector(".n-count");
      if (c) c.textContent = String(navCount(items[i].name) || "");
    });
  }).catch(() => {});
}

function closeDrawer() { $("#drawer-root").replaceChildren(); }

async function doLogout() {
  const path = S.mode === "platform" ? "/api/auth/logout" : "/api/logout";
  try { await api(path, { method: "POST" }); } catch {}
  localStorage.removeItem("bcrc_token"); // legacy cleanup
  localStorage.removeItem("bcrc_machine");
  S.authed = false;
  S._stateLoaded = false;
  closeDrawer(); closeSheet();
  try { T?.close(); } catch {}
  T = null;
  renderLogin();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeSheet(); closeDrawer(); }
});

// ---------------------------------------------------------------- shared components

// Large, readable list row. side may be a string or array of lines.
function bigItem({ icon, title, badge, sub, side, mono, onclick }) {
  const sides = side == null ? [] : (Array.isArray(side) ? side : [side]).filter(Boolean);
  return el("button", { class: "item", onclick },
    icon ? el("span", { class: "item-icon" }, icon) : null,
    el("div", { class: "item-main" },
      el("div", { class: "item-title" + (mono ? " mono" : "") },
        el("span", { class: "item-name" }, title), badge || null),
      sub ? el("div", { class: "item-sub" }, sub) : null),
    sides.length ? el("div", { class: "item-side" }, sides.map((x) => el("div", null, x))) : null);
}

// Search input + filtered list; filters items on every keystroke.
function searchableList({ items, keys, toNode, placeholder, empty }) {
  const input = el("input", { class: "search-input", type: "search",
    placeholder: placeholder || "Search…", autocapitalize: "none", autocorrect: "off" });
  const list = el("div", { class: "item-list" });
  const draw = () => {
    const f = input.value.trim().toLowerCase();
    const vis = f
      ? items.filter((it) => keys.some((k) => String(it[k] || "").toLowerCase().includes(f)))
      : items;
    list.replaceChildren(...(vis.length ? vis.map(toNode)
      : [el("div", { class: "empty" }, empty || "Nothing found.")]));
  };
  input.addEventListener("input", draw);
  draw();
  return { node: el("div", null, input, list), input };
}

function scopeBadge(it) {
  return el("span", { class: "badge " + it.scope }, it.scope + (it.source ? ":" + it.source : ""));
}

function kvRow(k, v) {
  return el("div", { class: "kv" },
    el("span", { class: "kv-k" }, k),
    el("span", { class: "kv-v" }, v || "—"));
}

const shortPath = (p) => (p || "").replace(S.home, "~");

// ---------------------------------------------------------------- login

function renderLogin() {
  if (S.mode === "platform") { renderPlatformLogin(); return; }
  setTopbar();
  $("#view").replaceChildren(
    el("div", { class: "login-box" },
      el("div", { class: "glyph" }, "❯_"),
      el("h1", null, "Claude RC"),
      el("p", null, "Paste the auth token shown in the server terminal, or scan its QR code."),
      el("input", { type: "password", placeholder: "auth token", id: "tok",
        autocapitalize: "none", autocorrect: "off" }),
      el("button", { class: "btn primary", onclick: doLogin }, "Connect"),
      el("div", { class: "login-err", id: "login-err" })
    )
  );
}

// -------- platform login (magic link by email) --------

function renderPlatformLogin(sentTo) {
  setTopbar();
  $("#view").replaceChildren(
    el("div", { class: "login-box" },
      el("div", { class: "glyph" }, "❯_"),
      el("h1", null, "Claude RC"),
      sentTo
        ? el("p", null, `Check ${sentTo} for a sign-in link. It works for 15 minutes.`)
        : el("p", null, "Sign in with your email — we'll send you a magic link."),
      sentTo ? null : el("input", { type: "email", placeholder: "you@example.com", id: "email",
        autocapitalize: "none", autocorrect: "off", autocomplete: "email" }),
      sentTo
        ? el("button", { class: "btn", onclick: () => renderPlatformLogin() }, "Use a different email")
        : el("button", { class: "btn primary", onclick: requestMagicLink }, "Email me a link"),
      el("div", { class: "login-err", id: "login-err" })
    )
  );
}

async function requestMagicLink() {
  const email = $("#email").value.trim();
  if (!email || !email.includes("@")) {
    $("#login-err").textContent = "Enter a valid email address";
    return;
  }
  try {
    await api("/api/auth/request-link", {
      method: "POST", body: JSON.stringify({ email }),
    });
    renderPlatformLogin(email);
  } catch (e) {
    $("#login-err").textContent = e.message || "Could not send the link";
  }
}

// -------- platform machine picker --------

function renderMachinePicker() {
  setTopbar();
  const view = $("#view");
  const box = el("div", { class: "pad machines" },
    el("h2", { class: "home-h" }, "Your Machines"));
  const machines = S.machines || [];
  if (!machines.length) {
    box.append(
      el("div", { class: "card home-empty" },
        "No machines yet. Run this on the computer you want to control:"),
      el("div", { class: "enroll-box", id: "enroll-box" },
        el("button", { class: "btn primary", onclick: showEnrollCommand },
          "Generate enroll command")));
  } else {
    box.append(el("div", { class: "vs-list" }, machines.map((m) => bigItem({
      icon: m.online ? "●" : "○",
      title: m.name || m.id.slice(0, 8),
      sub: m.online ? "online"
        : m.last_seen_at ? "last seen " + relTime(Date.parse(m.last_seen_at))
        : "never connected",
      onclick: () => connectMachine(m.id),
    }))));
    box.append(el("div", { class: "enroll-box", id: "enroll-box" },
      el("button", { class: "btn small", onclick: showEnrollCommand },
        "+ Add a machine")));
  }
  box.append(el("div", { class: "a-buttons", style: "margin-top:18px" },
    el("button", { class: "btn small", onclick: refreshMachines }, "Refresh"),
    el("button", { class: "btn small", onclick: doLogout }, "Log out")));
  view.replaceChildren(box);
}

async function refreshMachines() {
  try {
    S.machines = (await api("/api/machines")).machines || [];
    renderMachinePicker();
  } catch {}
}

async function showEnrollCommand() {
  try {
    const r = await api("/api/machines/enroll-token", { method: "POST" });
    const boxEl = $("#enroll-box");
    if (!boxEl) return;
    boxEl.replaceChildren(
      el("div", { class: "enroll-cmd" }, r.command),
      el("div", { class: "notice" },
        "Run it in a terminal on that computer (valid 10 minutes), then hit Refresh."),
      el("button", { class: "btn small", onclick: () => {
        navigator.clipboard?.writeText(r.command).then(() => toast("Copied"));
      } }, "Copy command"));
  } catch (e) {
    toast(e.message || "Could not create an enroll token", true);
  }
}

async function connectMachine(mid, pairing) {
  localStorage.setItem("bcrc_machine", mid);
  let session = null;
  try {
    // E2E session: device identity + pinned agent key (crypto.js).
    session = await BcrcCrypto.session(mid, pairing || null);
  } catch (e) {
    toast("Crypto init failed: " + (e.message || e), true);
    return;
  }
  if (!session) { renderPairScreen(mid); return; } // not paired yet
  if (T) { try { T.close(); } catch {} }
  T = new RelayTransport(mid, session);
  T.onMessage(handleWS);
  T.onStatus(onTransportStatus);
  T.connect();
  finishBoot();
}

// -------- pairing screen (scan the agent terminal's QR / paste the code) --

let _scanStop = null;

function stopScanner() {
  if (_scanStop) { try { _scanStop(); } catch {} _scanStop = null; }
}

function renderPairScreen(mid) {
  stopScanner();
  setTopbar();
  const err = el("div", { class: "login-err", id: "pair-err" });
  const video = el("video", { class: "pair-video", playsinline: "",
    muted: "", style: "display:none" });
  const paste = el("input", { type: "text", id: "pair-code",
    placeholder: "BCRC1.…", autocapitalize: "none", autocorrect: "off",
    spellcheck: "false" });
  $("#view").replaceChildren(
    el("div", { class: "login-box" },
      el("div", { class: "glyph" }, "❯_"),
      el("h1", null, "Pair this machine"),
      el("p", null, "In the terminal on that computer, better-claude-rc "
        + "connect shows a QR code and a pairing code. Scan or paste it "
        + "here — it never touches the server."),
      video,
      el("button", { class: "btn", onclick: () =>
        startScanner(video, err, (code) => submitPairing(mid, code)) },
        "Scan QR with camera"),
      paste,
      el("button", { class: "btn primary",
        onclick: () => submitPairing(mid, paste.value) }, "Pair"),
      el("button", { class: "btn", onclick: () => {
        stopScanner(); refreshMachines().finally(() => renderMachinePicker());
      } }, "Back"),
      err));
}

async function submitPairing(mid, code) {
  const qr = BcrcCrypto.parseQr(code);
  const errEl = $("#pair-err");
  if (!qr) {
    if (errEl) errEl.textContent = "That doesn't look like a pairing code";
    return;
  }
  if (qr.agentId !== mid) {
    if (errEl) errEl.textContent = "This code belongs to a different machine";
    return;
  }
  stopScanner();
  toast("Pairing…");
  await connectMachine(mid, qr);
}

async function startScanner(video, err, onCode) {
  stopScanner();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia(
      { video: { facingMode: "environment" } });
  } catch (e) {
    err.textContent = "Camera unavailable — paste the code instead";
    return;
  }
  video.srcObject = stream;
  video.style.display = "block";
  try { await video.play(); } catch {}
  let stopped = false;
  _scanStop = () => {
    stopped = true;
    stream.getTracks().forEach((t) => t.stop());
    video.style.display = "none";
    video.srcObject = null;
  };
  const detector = "BarcodeDetector" in window
    ? new BarcodeDetector({ formats: ["qr_code"] }) : null;
  const jsqr = detector ? null : await BcrcCrypto.jsQR().catch(() => null);
  if (!detector && !jsqr) {
    err.textContent = "No QR scanner available — paste the code instead";
    stopScanner();
    return;
  }
  const canvas = document.createElement("canvas");
  const tick = async () => {
    if (stopped) return;
    if (video.readyState >= 2 && video.videoWidth) {
      try {
        let text = null;
        if (detector) {
          const codes = await detector.detect(video);
          if (codes.length) text = codes[0].rawValue;
        } else {
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          const ctx = canvas.getContext("2d", { willReadFrequently: true });
          ctx.drawImage(video, 0, 0);
          const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
          const res = jsqr(img.data, img.width, img.height);
          if (res && res.data) text = res.data;
        }
        if (text && text.startsWith("BCRC1.")) {
          stopScanner();
          onCode(text);
          return;
        }
      } catch {}
    }
    setTimeout(tick, 250);
  };
  tick();
}

// -------- paired devices management (agent-side registry via rpc) --------

async function renderDevicesScreen() {
  setTopbar(menuBtn(), tbTitle("Devices"), el("div", { class: "tb-spacer" }));
  const view = $("#view");
  view.replaceChildren(el("div", { class: "pad" },
    el("h2", { class: "home-h" }, "Paired Devices"),
    el("div", { class: "empty" }, "Loading…")));
  let devices;
  try {
    devices = (await T.rpc("devices.list")).devices || [];
  } catch (e) {
    view.replaceChildren(el("div", { class: "pad" },
      el("div", { class: "empty" }, "Could not load devices: " + e.message)));
    return;
  }
  const mine = T && T.session ? T.session.devicePkB64 : null;
  view.replaceChildren(el("div", { class: "pad" },
    el("h2", { class: "home-h" }, "Paired Devices"),
    devices.length
      ? el("div", { class: "vs-list" }, devices.map((d) => bigItem({
          icon: "▣",
          title: (d.name || "device") + (d.device_pk === mine ? " · this device" : ""),
          sub: d.device_pk.slice(0, 20) + "…",
          side: "revoke",
          onclick: async () => {
            if (!confirm("Revoke this device? It loses access immediately."))
              return;
            try {
              await T.rpc("devices.revoke", { device_pk: d.device_pk });
            } catch (e) {
              if (d.device_pk !== mine) {  // self-revoke kills the pipe
                toast(e.message || "Revoke failed", true);
                return;
              }
            }
            toast("Device revoked");
            if (d.device_pk === mine) doLogout();
            else renderDevicesScreen();
          },
        })))
      : el("div", { class: "empty" },
          "No paired devices. Run better-claude-rc connect --pair on the "
          + "machine to pair another phone."),
    el("div", { class: "notice", style: "margin-top:12px" },
      "Devices are stored on the machine itself (devices.json) — the "
      + "platform never sees them.")));
}

async function doLogin() {
  const token = $("#tok").value.trim();
  if (!token) return;
  try {
    await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    }).then((r) => { if (!r.ok) throw new Error("Wrong token"); });
    await boot();
  } catch (e) {
    $("#login-err").textContent = e.message || "Login failed";
  }
}

// ---------------------------------------------------------------- home

function stateRank(s) {
  return { awaiting: 0, working: 1, compacting: 1, starting: 2, idle: 3, error: 4, detached: 5 }[s.state] ?? 6;
}

function sortedSessions() {
  return [...S.sessions].sort((a, b) => stateRank(a) - stateRank(b) || b.last_active - a.last_active);
}

async function renderHome() {
  if (S.view?.name !== "home") return;
  setTopbar(menuBtn(), el("div", { class: "tb-spacer" }), tabsBtn());
  const view = $("#view");
  const prevScroll = $(".carousel")?.scrollLeft ?? 0;
  const wrap = el("div", { class: "pad home" });
  wrap.append(el("div", { class: "conn-pill", id: "conn-pill", style: S.wsUp ? "display:none" : "" }, "reconnecting…"));

  // 1 — last session, swipeable
  wrap.append(el("h2", { class: "home-h" }, "Last Session"));
  const recent = sortedSessions().slice(0, 10);
  let carousel = null;
  if (recent.length) {
    carousel = el("div", { class: "carousel" }, recent.map((s) => sessionCard(s)));
    wrap.append(carousel);
  } else {
    wrap.append(el("div", { class: "card home-empty" },
      "No sessions yet. Pick a project below to start one."));
  }

  // 2 — projects, VS Code style
  wrap.append(el("h2", { class: "home-h" }, "Projects"));
  const plist = el("div", { class: "vs-list" }, el("div", { class: "notice" }, "loading…"));
  wrap.append(plist, el("div", { style: "height:40px" }));
  view.replaceChildren(wrap);
  if (carousel) carousel.scrollLeft = prevScroll;

  try {
    const projects = await loadProjects();
    if (S.view?.name !== "home") return;
    const existing = projects.filter((p) => p.exists);
    const rows = existing.slice(0, 6).map(vsRow);
    if (existing.length > 6) {
      rows.push(el("button", { class: "vs-row", onclick: projectQuickOpen },
        el("span", { class: "vs-name" }, "More…")));
    }
    plist.replaceChildren(...rows);
    if (!existing.length) plist.replaceChildren(el("div", { class: "notice" }, "No projects found."));
  } catch (e) {
    plist.replaceChildren(el("div", { class: "notice error" }, "Could not load projects: " + e.message));
  }
}

// VS Code style recent row: accent name + dimmed parent path
function vsRow(p) {
  return el("button", { class: "vs-row", onclick: () => nav({ name: "project", path: p.path }) },
    el("span", { class: "vs-name" }, p.name),
    el("span", { class: "vs-path" }, shortPath(p.parent)));
}

// quick-open search over all projects (the "More…" behavior)
function projectQuickOpen() {
  const { node, input } = searchableList({
    items: (S.projects || []).filter((p) => p.exists),
    keys: ["name", "path"],
    placeholder: "Search projects…",
    toNode: vsRow,
    empty: "No matching projects.",
  });
  const panel = el("div", { class: "qo-panel" }, node);
  const backdrop = el("div", { class: "qo-backdrop",
    onclick: (e) => { if (e.target === backdrop) closeSheet(); } }, panel);
  closeSheet();
  $("#sheet-root").append(backdrop);
  setTimeout(() => input.focus(), 60);
}

// ---------------------------------------------------------------- sessions list

function renderSessions() {
  if (S.view?.name !== "sessions") return;
  setTopbar(menuBtn(), tbTitle("Sessions"), tabsBtn());
  const view = $("#view");
  const wrap = el("div", { class: "pad" });
  wrap.append(el("div", { class: "conn-pill", id: "conn-pill", style: S.wsUp ? "display:none" : "" }, "reconnecting…"));

  const sorted = sortedSessions();
  const live = sorted.filter((s) => s.live || ["working", "awaiting", "starting", "compacting"].includes(s.state));
  const rest = sorted.filter((s) => !live.includes(s));

  if (!sorted.length) {
    wrap.append(el("div", { class: "empty" },
      el("span", { class: "glyph" }, "❯_"),
      "No sessions yet.", el("br"), "Pick a project to start one."));
  }
  if (live.length) {
    wrap.append(el("div", { class: "eyebrow" }, "active"));
    live.forEach((s) => wrap.append(sessionCard(s)));
  }
  if (rest.length) {
    wrap.append(el("div", { class: "eyebrow" }, "recent"));
    rest.forEach((s) => wrap.append(sessionCard(s)));
  }
  wrap.append(el("div", { style: "height:90px" }));
  view.replaceChildren(wrap);
  view.append(el("button", { class: "fab", onclick: () => nav({ name: "projects" }) }, "+"));
}

function sessionCard(s) {
  const st = S.status[s.sid] || { state: s.state, detail: s.detail };
  const busy = ["working", "awaiting", "starting", "compacting"].includes(st.state);

  // body: live ticker while busy, otherwise the last exchanged message
  let body;
  if (busy) {
    body = el("div", { class: "ticker" },
      el("span", { class: "caret" }, "❯"),
      el("span", { class: "t-text" }, st.detail || st.state));
  } else if (s.last_msg?.text) {
    body = el("div", { class: "last-msg " + s.last_msg.role },
      el("span", { class: "lm-who" }, s.last_msg.role === "user" ? "you" : "claude"),
      el("span", { class: "lm-text" }, s.last_msg.text));
  } else {
    body = el("div", { class: "ticker quiet" },
      el("span", { class: "caret" }, "❯"),
      el("span", { class: "t-text" }, st.detail || "no messages yet"));
  }

  const metaBits = [];
  if (busy) metaBits.push(el("span", { class: "active" }, "active"));
  else if (st.state === "error") metaBits.push(el("span", { class: "error" }, "error"));
  if (s.model && s.model !== "default") metaBits.push(el("span", null, shortModel(s.model)));
  if (s.cost_usd) metaBits.push(el("span", null, fmtCost(s.cost_usd)));
  if (s.context_pct != null) metaBits.push(el("span", null, `ctx ${Math.round(s.context_pct)}%`));
  metaBits.push(el("span", null, relTime(s.last_active)));

  return el("button", { class: "card s-card", onclick: () => openChat(s.sid) },
    el("div", { class: "row1" },
      el("span", { class: "dot " + st.state }),
      el("span", { class: "title" }, s.title || "(new session)"),
      el("span", { class: "menu-btn", onclick: (e) => { e.stopPropagation(); sessionMenu(s); } }, "⋯")),
    el("div", { class: "proj" }, s.project + (s.pending_permissions ? "  ·  ⚠ needs approval" : "")),
    body,
    el("div", { class: "meta-row" }, metaBits)
  );
}

function shortModel(m) {
  if (!m || m === "default") return "default";
  return m.replace(/^claude-/, "").replace(/-\d{8}$/, "");
}

function sessionMenu(s) {
  sheet(s.title || "(session)", [
    { icon: "▶", label: "Open", action: () => openChat(s.sid) },
    { icon: "■", label: "Stop process", sub: "Keeps history; resumes on next message",
      action: async () => { await T.rpc("sessions.stop", { sid: s.sid }); toast("Stopped"); } },
    { icon: "✕", label: "Delete session", danger: true,
      action: () => confirmSheet(`Delete “${s.title || "session"}”?`, "This removes it from Claude RC. The Claude Code transcript on disk is kept.", async () => {
        await T.rpc("sessions.delete", { sid: s.sid });
        delete S.events[s.sid]; S.subs.delete(s.sid); closeTab(s.sid);
        toast("Deleted");
      }) },
  ]);
}

// ---------------------------------------------------------------- tabs view

function renderTabs() {
  if (S.view?.name !== "tabs") return;
  pruneTabs();
  const bar = [
    el("button", { class: "tb-btn", onclick: () => {
      if (history.length > 1) history.back(); else nav({ name: "home" });
    } }, "‹"),
    tbTitle(S.tabs.length ? `Tabs · ${S.tabs.length}` : "Tabs"),
  ];
  if (S.tabs.length) {
    bar.push(el("button", { class: "tb-btn tb-text", onclick: () =>
      confirmSheet("Close all tabs?", "Sessions keep running — this only empties the tab switcher.", () => {
        S.tabs = []; S.lastTab = null; saveTabs(); renderTabs();
      }) }, "close all"));
  }
  setTopbar(...bar);

  const view = $("#view");
  const wrap = el("div", { class: "pad" });
  const open = S.tabs.map((sid) => S.sessions.find((s) => s.sid === sid)).filter(Boolean);
  if (open.length) {
    wrap.append(el("div", { class: "tabs-grid" }, open.map(tabCard)));
  } else {
    wrap.append(el("div", { class: "empty" },
      el("span", { class: "glyph" }, "▦"),
      "No open tabs.", el("br"), "Tap + to start a session."));
  }
  wrap.append(el("div", { style: "height:90px" }));
  view.replaceChildren(wrap);
  view.append(el("button", { class: "fab", onclick: () => nav({ name: "projects" }) }, "+"));
}

function tabCard(s) {
  const st = S.status[s.sid] || { state: s.state, detail: s.detail };
  const busy = ["working", "awaiting", "starting", "compacting"].includes(st.state);
  const body = busy ? (st.detail || st.state) : (s.last_msg?.text || "no messages yet");
  return el("button", { class: "tab-card" + (s.sid === S.lastTab ? " on" : ""),
    onclick: () => openChat(s.sid) },
    el("div", { class: "tc-head" },
      el("span", { class: "dot " + st.state }),
      el("span", { class: "tc-title" }, s.title || "(new session)"),
      el("span", { class: "tc-close", role: "button", "aria-label": "Close tab",
        onclick: (e) => { e.stopPropagation(); closeTab(s.sid); renderTabs(); } }, "✕")),
    el("div", { class: "tc-proj" }, s.project),
    el("div", { class: "tc-body" + (busy ? " busy" : "") }, body),
    el("div", { class: "tc-meta" },
      busy ? el("span", { class: "active" }, "active") : null,
      el("span", null, relTime(s.last_active))));
}

// ---------------------------------------------------------------- projects list

async function renderProjects() {
  if (S.view?.name !== "projects") return;
  setTopbar(menuBtn(), tbTitle("Projects"), tabsBtn());
  const view = $("#view");
  view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, "Loading projects…")));
  let projects;
  try {
    projects = await loadProjects();
  } catch (e) { view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, "Could not load projects: " + e.message))); return; }
  if (S.view?.name !== "projects") return;

  const { node } = searchableList({
    items: projects.filter((p) => p.exists),
    keys: ["name", "path"],
    placeholder: "Search projects…",
    empty: "No matching projects.",
    toNode: (p) => bigItem({
      icon: "❯",
      title: p.name,
      sub: shortPath(p.path),
      side: [relTime(p.last_active_ms), p.session_count ? p.session_count + " sessions" : ""],
      onclick: () => nav({ name: "project", path: p.path }),
    }),
  });
  view.replaceChildren(el("div", { class: "pad" },
    bigItem({ icon: "▸", title: "Browse folders…", sub: "start a session in any directory",
      onclick: () => browseSheet((path) => nav({ name: "project", path })) }),
    node));
}

async function browseSheet(pick) {
  let current = null;
  const body = el("div");
  const load = async (path) => {
    const data = await T.rpc("browse", path ? { path } : {});
    current = data.path;
    body.replaceChildren(
      el("div", { class: "sheet-title", style: "word-break:break-all" }, current.replace(S.home, "~")),
      el("div", { class: "a-buttons", style: "margin-bottom:10px" },
        data.parent ? el("button", { class: "btn small", onclick: () => load(data.parent) }, "‹ up") : null,
        el("button", { class: "btn small primary", onclick: () => { closeSheet(); pick(current); } }, "Use this folder")),
      ...data.dirs.map((d) => el("button", { class: "sheet-item", onclick: () => load(d.path) },
        el("span", { class: "si-icon" }, d.is_project ? "❯" : "▸"),
        el("div", { class: "si-main" }, d.name)))
    );
  };
  rawSheet(body);
  await load(null);
}

// ---------------------------------------------------------------- project screen

async function renderProject(path) {
  setTopbar(
    el("button", { class: "tb-btn", onclick: () => {
      if (history.length > 1) history.back(); else nav({ name: "home" });
    } }, "‹"),
    el("div", { class: "tb-title" },
      el("span", { class: "prompt-glyph" }, "❯"),
      el("span", { class: "crumb" }, path.split("/").pop())),
    tabsBtn()
  );
  const view = $("#view");
  const opts = { model: "default", mode: "default" };

  const modelRow = el("div", { class: "a-buttons", style: "margin-bottom:14px" });
  const renderModels = () => {
    modelRow.replaceChildren(...S.defaults.models.map((m) =>
      el("button", { class: "btn small" + (opts.model === m ? " primary" : ""),
        onclick: () => { opts.model = m; renderModels(); } }, m)));
  };
  renderModels();

  const modeRow = el("div", { class: "a-buttons", style: "margin-bottom:16px" });
  const renderModes = () => {
    modeRow.replaceChildren(...["default", "acceptEdits", "plan"].map((m) =>
      el("button", { class: "btn small" + (opts.mode === m ? " primary" : ""),
        onclick: () => { opts.mode = m; renderModes(); } }, m === "acceptEdits" ? "accept edits" : m)));
  };
  renderModes();

  const wrap = el("div", { class: "pad" },
    el("div", { class: "card" },
      el("div", { class: "eyebrow", style: "margin-top:0" }, "model"), modelRow,
      el("div", { class: "eyebrow" }, "permissions"), modeRow,
      el("button", { class: "btn primary", style: "width:100%; padding:13px",
        onclick: () => startSession(path, opts) }, "Start new session")),
    el("div", { class: "eyebrow" }, "resume a past session"),
    el("div", { id: "past-list" }, el("div", { class: "notice" }, "loading…")));
  view.replaceChildren(wrap);

  try {
    const { sessions } = await T.rpc("projects.sessions", { cwd: path });
    const list = $("#past-list");
    if (!list) return;
    list.replaceChildren();
    if (!sessions.length) list.append(el("div", { class: "notice" }, "No past sessions in this project."));
    for (const ps of sessions) {
      list.append(bigItem({
        title: ps.summary,
        sub: (ps.git_branch ? ps.git_branch + " · " : "") + (ps.session_id || "").slice(0, 8),
        side: relTime(ps.last_modified_ms),
        onclick: () => pastSessionSheet(path, ps, opts),
      }));
    }
  } catch (e) {
    const list = $("#past-list");
    if (list) list.replaceChildren(el("div", { class: "notice error" }, "Could not list sessions: " + e.message));
  }
}

function pastSessionSheet(path, ps, opts) {
  const body = el("div");
  body.append(
    el("div", { class: "sheet-title" }, ps.summary),
    el("button", { class: "sheet-item", onclick: async () => {
      closeSheet(); await startSession(path, { ...opts, resume: ps.session_id, title: ps.summary });
    } }, el("span", { class: "si-icon" }, "▶"), el("div", { class: "si-main" }, "Resume",
      el("div", { class: "si-sub" }, "continue this conversation"))),
    el("button", { class: "sheet-item", onclick: async () => {
      closeSheet(); await startSession(path, { ...opts, resume: ps.session_id, fork: true, title: ps.summary + " (fork)" });
    } }, el("span", { class: "si-icon" }, "⑂"), el("div", { class: "si-main" }, "Resume as fork",
      el("div", { class: "si-sub" }, "new branch — original session untouched"))),
    el("div", { id: "peek", class: "sheet-body", style: "margin-top:10px" },
      el("div", { class: "notice" }, "loading preview…"))
  );
  rawSheet(body);
  T.rpc("projects.preview", { session_id: ps.session_id, cwd: path })
    .then(({ messages }) => {
      const peek = $("#peek");
      if (!peek) return;
      peek.replaceChildren(...(messages.length ? messages : []).map((m) =>
        el("div", { class: "notice", style: m.role === "user" ? "color:var(--text)" : "" },
          (m.role === "user" ? "you: " : "claude: ") + m.text.slice(0, 200))));
      if (!messages.length) peek.replaceChildren(el("div", { class: "notice" }, "(no preview available)"));
    }).catch(() => {});
}

async function startSession(cwd, opts) {
  try {
    const snap = await T.rpc("sessions.create", {
      cwd, model: opts.model, mode: opts.mode,
      resume: opts.resume || null, fork: !!opts.fork, title: opts.title || "",
    });
    upsertSession(snap);
    openChat(snap.sid);
  } catch (e) { toast("Start failed: " + e.message, true); }
}

// ---------------------------------------------------------------- chat

function openChat(sid) {
  if (!S.tabs.includes(sid)) S.tabs.push(sid);
  S.lastTab = sid;
  saveTabs();
  nav({ name: "chat", sid });
}

function currentSession() {
  return S.sessions.find((s) => s.sid === S.view?.sid);
}

function renderChat(sid) {
  const s = S.sessions.find((x) => x.sid === sid);
  chatUI.sid = sid;
  chatUI.byTool = {}; chatUI.byReq = {}; chatUI.provText = []; chatUI.provThink = [];

  setTopbar(
    backBtn({ name: "home" }),
    el("div", { class: "tb-title", onclick: () => infoSheet(sid), style: "cursor:pointer" },
      el("span", { class: "dot " + ((S.status[sid] || {}).state || "detached"), id: "hdr-dot" }),
      el("div", { style: "min-width:0" },
        el("div", { class: "crumb", id: "hdr-title" }, s?.title || "session"),
        el("div", { class: "tb-sub", id: "hdr-sub" }, s?.project || ""))),
    el("div", { class: "tb-chips" },
      el("button", { class: "tb-btn tb-text", "aria-label": "True View (terminal)",
        onclick: () => openTrueView(sid) }, ">_"),
      tabsBtn(),
      el("button", { class: "tb-btn", onclick: () => infoSheet(sid) }, "ⓘ"))
  );

  const msgs = el("div", { id: "msgs" });
  chatUI.msgs = msgs;
  const statusLine = el("div", { id: "status-line", class: "quiet" },
    el("span", { class: "caret" }, "❯"), el("span", { class: "s-text" }, ""));
  const input = el("textarea", { class: "c-input", id: "chat-input", rows: 1,
    placeholder: "Message Claude…", enterkeyhint: "send" });
  const sendBtn = el("button", { class: "c-btn send", id: "send-btn", onclick: () => sendOrStop() }, "↑");

  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 132) + "px";
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey || (matchMedia("(pointer:fine)").matches && !e.shiftKey))) {
      e.preventDefault(); sendOrStop(true);
    }
  });

  const composer = el("div", { id: "composer" },
    el("button", { class: "c-btn", onclick: () => palette(sid) }, "/"),
    input, sendBtn);

  $("#view").replaceChildren(el("div", { id: "chat-wrap" }, msgs, statusLine, composer));

  if (S.events[sid]) renderChatTranscript(sid);
  else msgs.append(el("div", { class: "notice", id: "chat-loading" }, "loading transcript…"));
  subscribe(sid);
  updateChatHeader();
  updateStatusLine();
}

function sendOrStop(fromKeyboard) {
  const sid = chatUI.sid;
  const st = (S.status[sid] || {}).state;
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text && ["working", "compacting", "starting"].includes(st)) {
    wsSend({ type: "interrupt", session_id: sid });
    return;
  }
  if (!text) return;
  if (handleLocalCommand(sid, text)) { input.value = ""; input.style.height = "auto"; return; }
  if (wsSend({ type: "send", session_id: sid, text })) {
    input.value = ""; input.style.height = "auto";
    if (!fromKeyboard) input.blur();
  }
}

// slash commands the app itself owns (instant feedback, no guessing)
function handleLocalCommand(sid, text) {
  const m = text.match(/^\/([a-z-]+)\s*(.*)$/i);
  if (!m) return false;
  const [, cmd, rest] = m;
  switch (cmd.toLowerCase()) {
    case "clear": confirmClear(sid); return true;
    case "compact": wsSend({ type: "compact", session_id: sid, instructions: rest }); return true;
    case "model": modelSheet(sid); return true;
    case "mode": case "permissions": modeSheet(sid); return true;
    case "info": case "context": infoSheet(sid); return true;
    case "term": case "terminal": case "trueview": openTrueView(sid); return true;
    case "library": case "skills": nav({ name: "skills" }); return true;
  }
  return false;
}

function updateChatHeader() {
  const s = currentSession();
  if (!s) return;
  const dot = $("#hdr-dot"); const title = $("#hdr-title"); const sub = $("#hdr-sub");
  const st = S.status[s.sid] || {};
  if (dot) dot.className = "dot " + (st.state || "detached");
  if (title) title.textContent = s.title || "session";
  if (sub) {
    const ctx = S.context[s.sid];
    const bits = [s.project, shortModel((S.meta[s.sid] || {}).model || s.model)];
    if (ctx?.percentage != null) bits.push(`ctx ${Math.round(ctx.percentage)}%`);
    if (s.cost_usd) bits.push(fmtCost(s.cost_usd));
    sub.textContent = bits.filter(Boolean).join(" · ");
  }
}

function updateStatusLine() {
  const line = $("#status-line");
  if (!line || S.view?.name !== "chat") return;
  const sid = S.view.sid;
  const st = S.status[sid] || { state: "detached", detail: "" };
  const busy = ["working", "awaiting", "starting", "compacting"].includes(st.state);
  line.classList.toggle("quiet", !busy);
  const textEl = line.querySelector(".s-text");
  if (!S.wsUp) {
    textEl.replaceChildren(el("span", { class: "s-conn" }, "reconnecting…"));
  } else {
    textEl.textContent = st.detail || (st.state === "detached" ? "stopped — will resume on next message" : st.state);
  }
  const btn = $("#send-btn");
  if (btn) {
    const input = $("#chat-input");
    const showStop = busy && !(input && input.value.trim());
    btn.textContent = showStop ? "■" : "↑";
    btn.className = "c-btn " + (showStop ? "stop" : "send");
  }
}

// ---------------------------------------------------------------- true view (real claude TUI)

// The server runs `claude --resume <session>` in a PTY and streams raw bytes
// here; xterm.js renders the actual terminal UI, so this view mirrors Claude
// Code exactly. The PTY survives leaving the view — reattach replays scrollback.

const termUI = { sid: null, ws: null, term: null, cleanup: null };

const _assets = {};
function loadAsset(url, kind) {
  if (!_assets[url]) {
    _assets[url] = new Promise((resolve, reject) => {
      const node = kind === "css"
        ? el("link", { rel: "stylesheet", href: url })
        : el("script", { src: url });
      node.onload = resolve;
      node.onerror = () => { delete _assets[url]; reject(new Error("failed to load " + url)); };
      document.head.append(node);
    });
  }
  return _assets[url];
}

function ensureXterm() {
  return Promise.all([
    loadAsset("/vendor/xterm/xterm.css", "css"),
    loadAsset("/vendor/xterm/xterm.js"),
    loadAsset("/vendor/xterm/addon-fit.js"),
  ]);
}

function disposeTerm() {
  if (termUI.cleanup) termUI.cleanup();
  if (termUI.ws) { termUI.ws.onclose = null; termUI.ws.onmessage = null; termUI.ws.close(); }
  if (termUI.term) termUI.term.dispose();
  termUI.sid = null; termUI.ws = null; termUI.term = null; termUI.cleanup = null;
}

function openTrueView(sid) {
  const s = S.sessions.find((x) => x.sid === sid);
  if (s && !s.claude_session_id) {
    toast("No Claude session yet — send a message first", true);
    return;
  }
  nav({ name: "term", sid });
}

const TERM_KEYS = [
  ["esc", "\u001b"], ["tab", "\t"], ["\u21e7\u21e5", "\u001b[Z"], ["^C", "\u0003"],
  ["\u2190", "\u001b[D"], ["\u2191", "\u001b[A"], ["\u2193", "\u001b[B"], ["\u2192", "\u001b[C"],
  ["\u23ce", "\r"],
];

function termKeyBar() {
  return el("div", { id: "term-keys" },
    TERM_KEYS.map(([label, seq]) => el("button", {
      class: "term-key",
      // pointerdown + preventDefault: send the key without stealing focus
      // from xterm (which would close the mobile keyboard)
      onpointerdown: (e) => {
        e.preventDefault();
        if (termUI.ws && termUI.ws.readyState === 1) {
          termUI.ws.send(JSON.stringify({ type: "input", data: seq }));
        }
      },
    }, label)));
}

function renderTerm(sid) {
  disposeTerm();
  const s = S.sessions.find((x) => x.sid === sid);
  setTopbar(
    backBtn({ name: "chat", sid }),
    el("div", { class: "tb-title" },
      el("span", { class: "prompt-glyph" }, "❯_"),
      el("div", { style: "min-width:0" },
        el("div", { class: "crumb" }, "True View"),
        el("div", { class: "tb-sub" }, s?.project || ""))),
    el("div", { class: "tb-chips" },
      el("button", { class: "tb-btn tb-text", onclick: () => {
        confirmSheet("End terminal?", "The Claude TUI process stops. The session stays resumable from the chat.", () => {
          if (termUI.ws && termUI.ws.readyState === 1) termUI.ws.send('{"type":"kill"}');
        });
      } }, "end")),
  );

  const mount = el("div", { id: "term" });
  const overlay = el("div", { id: "term-overlay", class: "hidden" });
  $("#view").replaceChildren(el("div", { id: "term-wrap" },
    el("div", { id: "term-host" }, mount, overlay), termKeyBar()));

  termUI.sid = sid;
  overlay.classList.remove("hidden");
  overlay.replaceChildren(el("div", { class: "term-msg" }, "connecting…"));
  ensureXterm()
    .then(() => {
      if (termUI.sid !== sid || S.view?.name !== "term") return;
      openTerm(sid, mount, overlay);
    })
    .catch(() => showTermOverlay(overlay, sid, "Could not load terminal assets"));
}

function openTerm(sid, mount, overlay) {
  const term = new Terminal({
    fontFamily: 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace',
    fontSize: 13,
    cursorBlink: true,
    scrollback: 4000,
    theme: {
      background: "#141210", foreground: "#ede6de",
      cursor: "#e8825e", cursorAccent: "#141210",
      selectionBackground: "#3a2a22",
    },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(mount);
  fit.fit();
  termUI.term = term;

  const ws = T.openTerm(sid);
  ws.binaryType = "arraybuffer";
  termUI.ws = ws;

  const sendResize = () => {
    if (ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  };
  ws.onopen = () => {
    overlay.classList.add("hidden");
    fit.fit(); sendResize(); term.focus();
  };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      let msg; try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "exit") showTermOverlay(overlay, sid, msg.reason || "Terminal ended");
      return;
    }
    term.write(new Uint8Array(e.data));
  };
  ws.onclose = () => {
    if (termUI.ws !== ws) return;
    if (overlay.classList.contains("hidden")) showTermOverlay(overlay, sid, "Disconnected");
  };
  term.onData((d) => {
    if (ws.readyState === 1) ws.send(JSON.stringify({ type: "input", data: d }));
  });

  let fitTimer = null;
  const refit = () => {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(() => { fit.fit(); sendResize(); }, 120);
  };
  window.addEventListener("resize", refit);
  const vv = window.visualViewport;
  if (vv) vv.addEventListener("resize", refit);
  const ping = setInterval(() => { if (ws.readyState === 1) ws.send('{"type":"ping"}'); }, 25000);

  termUI.cleanup = () => {
    clearTimeout(fitTimer);
    clearInterval(ping);
    window.removeEventListener("resize", refit);
    if (vv) vv.removeEventListener("resize", refit);
  };
}

function showTermOverlay(overlay, sid, reason) {
  overlay.classList.remove("hidden");
  overlay.replaceChildren(
    el("div", { class: "term-msg" }, reason),
    el("div", { class: "term-actions" },
      el("button", { onclick: () => renderTerm(sid) }, "reopen"),
      el("button", { onclick: () => nav({ name: "chat", sid }) }, "back to chat")));
}

// ---------------------------------------------------------------- transcript rendering

function renderChatTranscript(sid) {
  const msgs = chatUI.msgs;
  if (!msgs) return;
  chatUI.byTool = {}; chatUI.byReq = {}; chatUI.provText = []; chatUI.provThink = [];
  msgs.replaceChildren();
  for (const ev of S.events[sid] || []) renderEvent(sid, ev, false);
  scrollBottom(true);
}

function renderLiveEvent(sid, ev) {
  const loading = $("#chat-loading");
  if (loading) loading.remove();
  renderEvent(sid, ev, true);
}

function nearBottom() {
  const m = chatUI.msgs;
  return m ? m.scrollHeight - m.scrollTop - m.clientHeight < 140 : false;
}

function scrollBottom(force) {
  const m = chatUI.msgs;
  if (m && (force || nearBottom())) m.scrollTop = m.scrollHeight;
}

function appendMsg(node, parentToolId) {
  if (parentToolId && chatUI.byTool[parentToolId]) {
    let nest = chatUI.byTool[parentToolId].querySelector(".nested-tools");
    if (!nest) {
      nest = el("div", { class: "nested-tools" });
      chatUI.byTool[parentToolId].append(nest);
    }
    nest.append(node);
  } else {
    chatUI.msgs.append(node);
  }
  scrollBottom(false);
}

function renderEvent(sid, ev, live) {
  switch (ev.kind) {
    case "user_text":
      finalizeProvisionals();
      appendMsg(el("div", { class: ev.external ? "msg m-user external" : "msg m-user" }, ev.text));
      if (live) scrollBottom(true);
      break;

    case "delta": {
      let blk = chatUI.provText[chatUI.provText.length - 1];
      if (!blk || blk.dataset.done) {
        blk = el("div", { class: "msg m-assist streaming" });
        blk._buf = "";
        chatUI.provText.push(blk);
        appendMsg(blk);
      }
      blk._buf += ev.text || "";
      blk.textContent = blk._buf;
      scrollBottom(false);
      break;
    }
    case "text_open": {
      const blk = el("div", { class: "msg m-assist streaming" });
      blk._buf = "";
      chatUI.provText.push(blk);
      appendMsg(blk);
      break;
    }
    case "text": {
      const prov = chatUI.provText.find((b) => !b.dataset.replaced);
      const html = md(ev.text);
      if (prov) {
        prov.dataset.replaced = "1";
        prov.className = "msg m-assist";
        prov.innerHTML = html;
      } else {
        appendMsg(el("div", { class: "msg m-assist", html }));
      }
      scrollBottom(false);
      break;
    }

    case "thinking_open": case "thinking_delta": {
      let blk = chatUI.provThink[chatUI.provThink.length - 1];
      if (!blk || blk.dataset.done) {
        blk = el("details", { class: "m-think" },
          el("summary", null, "◈ thinking"),
          el("div", { class: "think-body" }, ""));
        chatUI.provThink.push(blk);
        appendMsg(blk);
      }
      if (ev.kind === "thinking_delta") {
        const body = blk.querySelector(".think-body");
        body.textContent += ev.text || "";
      }
      break;
    }
    case "thinking": {
      const prov = chatUI.provThink.find((b) => !b.dataset.replaced);
      if (prov) {
        prov.dataset.replaced = "1";
        prov.querySelector(".think-body").textContent = ev.text;
        prov.querySelector("summary").textContent = "◈ thought";
      } else {
        appendMsg(el("details", { class: "m-think" },
          el("summary", null, "◈ thought"),
          el("div", { class: "think-body" }, ev.text)));
      }
      break;
    }
    case "block_stop": {
      const t = chatUI.provText[chatUI.provText.length - 1];
      if (t && !t.dataset.replaced) t.dataset.done = "1";
      const th = chatUI.provThink[chatUI.provThink.length - 1];
      if (th && !th.dataset.replaced) th.dataset.done = "1";
      break;
    }

    case "tool_start": appendMsg(toolCard(ev), ev.parent_tool_use_id); break;
    case "tool_result": fillToolResult(ev); break;

    case "permission": appendMsg(permissionCard(sid, ev)); if (live) scrollBottom(true); break;
    case "question": appendMsg(questionCard(sid, ev)); if (live) scrollBottom(true); break;
    case "plan_approval": appendMsg(planCard(sid, ev)); if (live) scrollBottom(true); break;
    case "permission_resolved": resolveActionCard(ev); break;

    case "cleared":
      finalizeProvisionals();
      appendMsg(el("div", { class: "divider" }, "conversation cleared"));
      break;
    case "resumed": {
      const skipped = (ev.total || 0) - (ev.shown || 0);
      appendMsg(el("div", { class: "divider" },
        "resumed session — continuing below"
        + (skipped > 0 ? ` · ${skipped} older messages not shown` : "")));
      if (live) scrollBottom(true);
      break;
    }
    case "compact":
      appendMsg(el("div", { class: "divider" },
        "compacted" + (ev.pre_tokens ? ` · was ${fmtTokens(ev.pre_tokens)} tokens` : "")
        + (ev.trigger === "auto" ? " (auto)" : "")
        + (ev.trigger === "past" ? " (earlier)" : "")));
      break;

    case "result": {
      finalizeProvisionals();
      const bits = [];
      if (ev.duration_ms) bits.push((ev.duration_ms / 1000).toFixed(0) + "s");
      if (ev.cost_usd != null) bits.push(fmtCost(ev.cost_usd) + " total");
      if (ev.is_error) bits.push("⚠ " + (ev.subtype || "error"));
      appendMsg(el("div", { class: "turn-result" }, bits.join(" · ")));
      break;
    }

    case "notice":
      appendMsg(el("div", { class: "notice " + (ev.level || "") }, ev.text));
      break;

    case "meta": case "commands": case "context": case "status": case "system": case "debug":
      break; // state-only
  }
}

function finalizeProvisionals() {
  for (const b of chatUI.provText) if (!b.dataset.replaced) { b.dataset.replaced = "1"; b.className = "msg m-assist"; b.innerHTML = md(b._buf || b.textContent); }
  for (const b of chatUI.provThink) if (!b.dataset.replaced) b.dataset.replaced = "1";
  chatUI.provText = []; chatUI.provThink = [];
}

// ---------- tool cards ----------

const TOOL_ICONS = {
  Bash: "❯", Read: "▤", Edit: "✎", Write: "✎", Grep: "⌕", Glob: "⌕",
  Task: "⑃", WebFetch: "⇩", WebSearch: "⌕", TodoWrite: "☑", Skill: "◆",
  NotebookEdit: "✎", ExitPlanMode: "▤", KillShell: "■", BashOutput: "❯",
};

function toolIcon(name) {
  if (name.startsWith("mcp__")) return "⚡";
  return TOOL_ICONS[name] || "⚙";
}

function toolCard(ev) {
  const body = el("div", { class: "tool-body" });
  const inputPre = toolInputView(ev.tool, ev.input || {});
  if (inputPre) body.append(inputPre);
  const state = el("span", { class: "t-state spin" }, "●");
  const card = el("div", { class: "tool-card", "data-tool-id": ev.tool_use_id },
    el("button", { class: "tool-head", onclick: () => card.classList.toggle("open") },
      el("span", { class: "t-icon" }, toolIcon(ev.tool)),
      el("span", { class: "t-name" }, ev.tool.replace(/^mcp__/, "")),
      el("span", { class: "t-sum" }, ev.summary || ""),
      state),
    body);
  card._state = state;
  chatUI.byTool[ev.tool_use_id] = card;
  return card;
}

function toolInputView(tool, input) {
  if (tool === "Bash" && input.command) {
    return el("div", null, el("div", { class: "out-label" }, "command"),
      el("pre", null, input.command));
  }
  if (tool === "Edit" && (input.old_string || input.new_string)) {
    const wrap = el("div", null, el("div", { class: "out-label" }, input.file_path || ""));
    const pre = el("pre");
    if (input.old_string) pre.append(el("span", { class: "diff-old" },
      input.old_string.split("\n").map((l) => "- " + l).join("\n") + "\n"));
    if (input.new_string) pre.append(el("span", { class: "diff-new" },
      input.new_string.split("\n").map((l) => "+ " + l).join("\n")));
    wrap.append(pre);
    return wrap;
  }
  if (tool === "Write" && input.content) {
    return el("div", null, el("div", { class: "out-label" }, input.file_path || "content"),
      el("pre", null, input.content.length > 3000 ? input.content.slice(0, 3000) + "\n…" : input.content));
  }
  if (tool === "TodoWrite" && Array.isArray(input.todos)) {
    return el("ul", { class: "todo-list" }, input.todos.map((t) =>
      el("li", { class: t.status === "completed" ? "td-done" : t.status === "in_progress" ? "td-active" : "" },
        (t.status === "completed" ? "☑ " : t.status === "in_progress" ? "◐ " : "☐ ") + (t.content || ""))));
  }
  const keys = Object.keys(input || {});
  if (!keys.length) return null;
  const compact = keys.map((k) => {
    let v = input[k];
    if (typeof v === "object") v = JSON.stringify(v);
    v = String(v);
    return k + ": " + (v.length > 400 ? v.slice(0, 400) + "…" : v);
  }).join("\n");
  return el("pre", null, compact);
}

function fillToolResult(ev) {
  const card = chatUI.byTool[ev.tool_use_id];
  if (!card) return;
  card._state.className = "t-state " + (ev.is_error ? "err" : "ok");
  card._state.textContent = ev.is_error ? "✕" : "✓";
  const body = card.querySelector(".tool-body");
  if (ev.text) {
    body.append(el("div", { class: "out-label" }, ev.is_error ? "error" : "output"),
      el("pre", null, ev.text));
    if (ev.is_error) card.classList.add("open");
  }
}

// ---------- permission / question / plan cards ----------

function permissionCard(sid, ev) {
  const btns = el("div", { class: "a-buttons" });
  const card = el("div", { class: "action-card", "data-req": ev.request_id },
    el("div", { class: "a-head" }, "⚠ approval needed"),
    el("div", { class: "a-title" }, ev.title || `${ev.tool}${ev.summary ? " — " + ev.summary : ""}`),
    ev.description ? el("div", { class: "a-desc" }, ev.description) : null,
    ev.reason ? el("div", { class: "a-desc" }, ev.reason) : null,
    permInputPreview(ev),
    btns);
  chatUI.byReq[ev.request_id] = card;

  const respond = (payload, label) => {
    if (wsSend({ type: "permission", session_id: sid, request_id: ev.request_id, ...payload })) {
      markResolved(card, label);
    }
  };
  btns.append(
    el("button", { class: "btn primary", onclick: () => respond({ behavior: "allow" }, "allowed") }, "Allow"),
    ev.suggestions?.length
      ? el("button", { class: "btn", onclick: () => respond({ behavior: "allow", apply_suggestions: ev.suggestions.map((_, i) => i) }, "always allowed") }, "Always")
      : null,
    el("button", { class: "btn danger", onclick: () => respond({ behavior: "deny", message: "User denied this action" }, "denied") }, "Deny"),
    el("button", { class: "btn small", style: "flex-basis:100%", onclick: () => {
      const note = prompt("Tell Claude why / what to do instead:");
      if (note !== null) respond({ behavior: "deny", message: note || "User denied" }, "denied");
    } }, "Deny with note…"),
  );
  return card;
}

function permInputPreview(ev) {
  const input = ev.input || {};
  if (ev.tool === "Bash" && input.command) return el("pre", null, input.command);
  if (ev.tool === "Edit" || ev.tool === "Write") {
    const view = toolInputView(ev.tool, input);
    return view || null;
  }
  const keys = Object.keys(input);
  if (!keys.length) return null;
  const txt = keys.map((k) => {
    let v = input[k];
    if (typeof v === "object") v = JSON.stringify(v, null, 1);
    return `${k}: ${String(v).slice(0, 500)}`;
  }).join("\n");
  return el("pre", null, txt.slice(0, 1200));
}

function questionCard(sid, ev) {
  const questions = ev.questions || [];
  const answers = {};
  const card = el("div", { class: "action-card", "data-req": ev.request_id },
    el("div", { class: "a-head" }, "? claude asks"));
  chatUI.byReq[ev.request_id] = card;

  const done = () => {
    if (wsSend({ type: "permission", session_id: sid, request_id: ev.request_id, behavior: "allow", answers })) {
      markResolved(card, Object.values(answers).join(" · ").slice(0, 80) || "answered");
    }
  };

  const single = questions.length === 1 && !questions[0].multiSelect;
  questions.forEach((q) => {
    card.append(el("div", { class: "a-title" }, q.question));
    const sel = new Set();
    const optWrap = el("div");
    (q.options || []).forEach((o) => {
      const label = typeof o === "string" ? o : o.label;
      const btn = el("button", { class: "q-option", onclick: () => {
        if (q.multiSelect) {
          sel.has(label) ? sel.delete(label) : sel.add(label);
          answers[q.question] = [...sel].join(", ");
          btn.classList.toggle("sel");
        } else {
          answers[q.question] = label;
          if (single) done();
          else {
            optWrap.querySelectorAll(".q-option").forEach((b) => b.classList.remove("sel"));
            btn.classList.add("sel");
          }
        }
      } },
        el("div", { class: "q-label" }, label),
        (typeof o === "object" && o.description) ? el("div", { class: "q-desc" }, o.description) : null);
      optWrap.append(btn);
    });
    const other = el("input", { class: "q-other-input", placeholder: "Other…" });
    other.addEventListener("input", () => { answers[q.question] = other.value; });
    other.addEventListener("keydown", (e) => { if (e.key === "Enter" && single && other.value.trim()) done(); });
    optWrap.append(other);
    card.append(optWrap);
  });

  if (!single) {
    card.append(el("div", { class: "a-buttons" },
      el("button", { class: "btn primary", onclick: done }, "Send answers")));
  }
  return card;
}

function planCard(sid, ev) {
  const card = el("div", { class: "action-card", "data-req": ev.request_id },
    el("div", { class: "a-head" }, "▤ plan ready for review"),
    el("div", { class: "msg m-assist", html: md(ev.plan || "") }));
  chatUI.byReq[ev.request_id] = card;
  const respond = (payload, label, mode) => {
    if (wsSend({ type: "permission", session_id: sid, request_id: ev.request_id, ...payload })) {
      if (mode) wsSend({ type: "set_mode", session_id: sid, mode });
      markResolved(card, label);
    }
  };
  card.append(el("div", { class: "a-buttons" },
    el("button", { class: "btn primary", onclick: () => respond({ behavior: "allow" }, "approved — auto-accepting edits", "acceptEdits") }, "Approve, accept edits"),
    el("button", { class: "btn", onclick: () => respond({ behavior: "allow" }, "approved", "default") }, "Approve, ask each time"),
    el("button", { class: "btn danger", onclick: () => {
      const note = prompt("What should change in the plan?") || "Keep planning";
      respond({ behavior: "deny", message: note }, "sent back");
    } }, "Keep planning")));
  return card;
}

function markResolved(card, label) {
  card.classList.add("resolved");
  card.querySelectorAll(".a-buttons, .q-option, .q-other-input").forEach((n) => n.remove());
  card.append(el("div", { class: "a-outcome" }, "→ " + label));
}

function resolveActionCard(ev) {
  const card = chatUI.byReq[ev.request_id];
  if (card && !card.classList.contains("resolved")) {
    markResolved(card, ev.behavior === "allow" ? "allowed" : "denied" + (ev.note ? ": " + ev.note.slice(0, 60) : ""));
  }
}

// ---------------------------------------------------------------- palette & sheets

function palette(sid) {
  const meta = S.meta[sid] || {};
  const appCmds = [
    { name: "clear", desc: "Start fresh context in this chat", act: () => confirmClear(sid) },
    { name: "compact", desc: "Summarize old messages to free context", act: () => wsSend({ type: "compact", session_id: sid, instructions: "" }) },
    { name: "model", desc: "Change model", act: () => modelSheet(sid) },
    { name: "mode", desc: "Change permission mode", act: () => modeSheet(sid) },
    { name: "info", desc: "Session info, context, MCP status", act: () => infoSheet(sid) },
    { name: "library", desc: "Skills, commands, agents, MCP", act: () => nav({ name: "skills" }) },
  ];
  const skip = new Set(["clear", "compact", "model", "info", "context", "library",
    "login", "logout", "quit", "exit", "resume", "help", "doctor", "ide", "vim",
    "terminal-setup", "install-github-app", "rc", "remote-control"]);
  const cliCmds = (meta.commands || [])
    .filter((c) => !skip.has(c.name.replace(/^\//, "")))
    .map((c) => ({
      name: c.name.replace(/^\//, ""),
      desc: c.description || "",
      hint: c.argument_hint || "",
      insert: true,
    }));

  const body = el("div");
  const search = el("input", { type: "search", placeholder: "Filter commands…" });
  const list = el("div", { class: "sheet-body" });

  const draw = (filter) => {
    const f = (filter || "").toLowerCase();
    list.replaceChildren();
    const add = (title, items) => {
      const vis = items.filter((c) => !f || c.name.toLowerCase().includes(f) || c.desc.toLowerCase().includes(f));
      if (!vis.length) return;
      list.append(el("div", { class: "eyebrow" }, title));
      for (const c of vis) {
        list.append(el("button", { class: "sheet-item", onclick: () => {
          closeSheet();
          if (c.act) c.act();
          else {
            const input = $("#chat-input");
            input.value = "/" + c.name + " ";
            input.focus();
            input.dispatchEvent(new Event("input"));
          }
        } },
          el("div", { class: "si-main" },
            el("div", { style: "font-family:var(--mono); font-size:14px" }, "/" + c.name + (c.hint ? " " + c.hint : "")),
            c.desc ? el("div", { class: "si-sub" }, c.desc.slice(0, 90)) : null)));
      }
    };
    add("app", appCmds);
    add("commands", cliCmds);
  };
  search.addEventListener("input", () => draw(search.value));
  body.append(search, list);
  draw("");
  rawSheet(body);
}

function confirmClear(sid) {
  confirmSheet("Clear conversation?", "Context resets to zero. The transcript stays visible above the divider.", () => {
    wsSend({ type: "clear", session_id: sid });
  });
}

function modelSheet(sid) {
  const cur = (S.sessions.find((s) => s.sid === sid) || {}).model || "default";
  const items = S.defaults.models.map((m) => ({
    icon: shortModel(cur) === m || cur === m ? "✓" : " ",
    label: m,
    action: () => wsSend({ type: "set_model", session_id: sid, model: m }),
  }));
  items.push({ icon: "✎", label: "Custom model id…", action: () => {
    const m = prompt("Model id (e.g. claude-opus-4-8):");
    if (m) wsSend({ type: "set_model", session_id: sid, model: m.trim() });
  } });
  sheet("Model", items);
}

const MODE_INFO = {
  default: "Ask before risky tools",
  acceptEdits: "Auto-accept file edits",
  plan: "Plan first — no execution",
  dontAsk: "Deny anything not pre-allowed",
  bypassPermissions: "Run everything without asking",
};

function modeSheet(sid) {
  const cur = (S.sessions.find((s) => s.sid === sid) || {}).mode;
  sheet("Permission mode", S.defaults.modes.map((m) => ({
    icon: cur === m ? "✓" : " ",
    label: m,
    sub: MODE_INFO[m] || "",
    danger: m === "bypassPermissions",
    action: () => wsSend({ type: "set_mode", session_id: sid, mode: m }),
  })));
}

function infoSheet(sid) {
  const s = S.sessions.find((x) => x.sid === sid) || {};
  const meta = S.meta[sid] || {};
  const ctx = S.context[sid];
  const body = el("div", { class: "sheet-body" });

  body.append(el("div", { class: "sheet-title" }, s.title || "session"));

  body.append(
    kvRow("project", shortPath(s.cwd)),
    kvRow("model", meta.model || s.model),
    kvRow("mode", s.mode),
    kvRow("cost", s.cost_usd != null ? "$" + s.cost_usd.toFixed(3) : "—"),
    kvRow("claude session", (s.claude_session_id || "").slice(0, 13)),
    kvRow("process", s.live ? "running" : "stopped"),
  );

  if (ctx) {
    const pct = Math.round(ctx.percentage || 0);
    body.append(el("div", { style: "padding:10px 0 4px" },
      el("div", { style: "display:flex; justify-content:space-between; font-size:12px; font-family:var(--mono); color:var(--dim)" },
        el("span", null, "context"),
        el("span", null, `${fmtTokens(ctx.total_tokens)} / ${fmtTokens(ctx.max_tokens)} · ${pct}%`)),
      el("div", { class: "ctx-bar" },
        el("div", { class: "fill" + (pct > 80 ? " high" : pct > 55 ? " mid" : ""), style: `width:${pct}%` }))));
  }

  if (meta.mcp_servers?.length) {
    body.append(el("div", { class: "eyebrow" }, "mcp servers"));
    for (const m of meta.mcp_servers) {
      body.append(kvRow(m.name, m.status || "?"));
    }
  }

  body.append(el("div", { class: "a-buttons", style: "margin-top:14px" },
    el("button", { class: "btn", onclick: () => { closeSheet(); wsSend({ type: "compact", session_id: sid, instructions: "" }); } }, "Compact"),
    el("button", { class: "btn", onclick: () => { closeSheet(); confirmClear(sid); } }, "Clear"),
    el("button", { class: "btn", onclick: () => { closeSheet(); wsSend({ type: "refresh_context", session_id: sid }); } }, "Refresh"),
    el("button", { class: "btn danger", onclick: async () => { closeSheet(); await T.rpc("sessions.stop", { sid }); } }, "Stop process")));
  rawSheet(body);
}

// generic sheets

function rawSheet(bodyNode) {
  closeSheet();
  const backdrop = el("div", { class: "sheet-backdrop", onclick: (e) => { if (e.target === backdrop) closeSheet(); } },
    el("div", { class: "sheet" }, el("div", { class: "grab" }), bodyNode));
  $("#sheet-root").append(backdrop);
}

function sheet(title, items) {
  const body = el("div", { class: "sheet-body" });
  if (title) body.append(el("div", { class: "sheet-title" }, title));
  for (const it of items) {
    if (!it) continue;
    body.append(el("button", { class: "sheet-item" + (it.danger ? " danger" : ""), onclick: async () => {
      closeSheet();
      try { await it.action?.(); } catch (e) { toast(e.message, true); }
    } },
      el("span", { class: "si-icon" }, it.icon || ""),
      el("div", { class: "si-main" }, it.label, it.sub ? el("div", { class: "si-sub" }, it.sub) : null)));
  }
  rawSheet(body);
}

function confirmSheet(title, subtitle, action) {
  const body = el("div");
  body.append(
    el("div", { class: "sheet-title" }, title),
    el("div", { class: "a-desc", style: "margin-bottom:14px" }, subtitle),
    el("div", { class: "a-buttons" },
      el("button", { class: "btn primary", onclick: async () => { closeSheet(); try { await action(); } catch (e) { toast(e.message, true); } } }, "Confirm"),
      el("button", { class: "btn", onclick: closeSheet }, "Cancel")));
  rawSheet(body);
}

function closeSheet() { $("#sheet-root").replaceChildren(); }

function toast(text, isErr) {
  const t = el("div", { class: "toast" + (isErr ? " error" : "") }, text);
  $("#toast-root").append(t);
  setTimeout(() => t.remove(), 3200);
}

// ---------------------------------------------------------------- library lists

const LIB_KINDS = {
  skills: { title: "Skills", key: "skills", icon: "◆" },
  commands: { title: "Commands", key: "commands", icon: "⌘" },
  agents: { title: "Agents", key: "agents", icon: "⑃" },
  mcp: { title: "MCPs", key: "mcp_servers", icon: "⚡" },
};

async function renderLibraryList(kind) {
  const cfg = LIB_KINDS[kind];
  setTopbar(menuBtn(), tbTitle(cfg.title), tabsBtn());
  const view = $("#view");
  view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, "Loading…")));
  let lib;
  try {
    lib = await loadLibrary();
  } catch (e) { view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, e.message))); return; }
  if (S.view?.name !== kind) return;

  const items = lib[cfg.key] || [];
  const { node } = searchableList({
    items,
    keys: ["name", "description", "detail", "source"],
    placeholder: `Search ${items.length} ${cfg.title.toLowerCase()}…`,
    empty: "Nothing here.",
    toNode: (it) => bigItem({
      mono: kind === "commands" || kind === "mcp",
      title: kind === "commands" ? "/" + it.name : it.name,
      badge: scopeBadge(it),
      sub: kind === "mcp"
        ? it.transport + (it.detail ? " · " + it.detail : "")
        : (it.description || ""),
      onclick: () => libDetailSheet(kind, it),
    }),
  });
  view.replaceChildren(el("div", { class: "pad" }, node));
}

function libDetailSheet(kind, it) {
  const body = el("div", { class: "sheet-body" });
  body.append(el("div", { class: "sheet-title", style: "font-size:16px" },
    (kind === "commands" ? "/" : "") + it.name, " ", scopeBadge(it)));
  if (it.description) body.append(el("div", { class: "lib-detail-desc" }, it.description));
  if (it.argument_hint) body.append(kvRow("arguments", it.argument_hint));
  if (it.model) body.append(kvRow("model", it.model));
  if (it.tools) body.append(kvRow("tools", it.tools));
  if (it.transport) body.append(kvRow("transport", it.transport));
  if (it.detail) body.append(kvRow("endpoint", it.detail));
  if (it.path) body.append(kvRow("path", shortPath(it.path)));
  rawSheet(body);
}

// ---------------------------------------------------------------- iOS keyboard

if (window.visualViewport) {
  const vv = window.visualViewport;
  const update = () => {
    const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    document.documentElement.style.setProperty("--kb", kb + "px");
    if (kb > 0) scrollBottom(false);
  };
  vv.addEventListener("resize", update);
  vv.addEventListener("scroll", update);
}

// ---------------------------------------------------------------- boot

async function boot() {
  if (!S.mode) S.mode = await fetchEnvMode(); // transport.js; GET /api/env
  if (S.mode === "platform") { await bootPlatform(); return; }

  if (!T) {
    T = new LocalTransport();
    T.onMessage(handleWS);
    T.onStatus(onTransportStatus);
  }

  // one-time login code from the QR link — fragment only, never query strings
  const mLogin = (location.hash || "").match(/^#login=([A-Za-z0-9_-]+)$/);
  if (mLogin) {
    history.replaceState(null, "", location.pathname);
    try {
      await fetch("/api/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: mLogin[1] }),
      });
    } catch {}
  }

  await finishBoot();
}

async function bootPlatform() {
  // magic-link token arrives in the URL fragment — never a query string
  const mAuth = (location.hash || "").match(/^#auth=([A-Za-z0-9_-]+)$/);
  if (mAuth) {
    history.replaceState(null, "", location.pathname);
    try {
      await api("/api/auth/redeem", {
        method: "POST", body: JSON.stringify({ token: mAuth[1] }),
      });
    } catch {}
  }

  let me;
  try {
    me = await api("/api/me");
  } catch {
    S.authed = false;
    renderLogin();
    return;
  }
  S.authed = true;
  S.email = me.email;
  S.plan = me.plan;
  try {
    S.machines = (await api("/api/machines")).machines || [];
  } catch {
    S.machines = [];
  }

  let mid = localStorage.getItem("bcrc_machine");
  if (!S.machines.some((m) => m.id === mid)) mid = null;
  if (!mid && S.machines.length === 1) mid = S.machines[0].id; // auto-skip
  if (!mid) { renderMachinePicker(); return; }
  connectMachine(mid);
}

async function finishBoot() {
  try {
    const state = await T.rpc("state");
    S.authed = true;
    S._stateLoaded = true;
    S.sessions = state.sessions;
    S.defaults = state.defaults;
    S.home = state.home;
    for (const s of S.sessions) S.status[s.sid] = { state: s.state, detail: s.detail };
    pruneTabs();
    // warm caches so home and the drawer paint instantly
    loadProjects().then(() => { if (S.view?.name === "home") renderHome(); }).catch(() => {});
    loadLibrary().catch(() => {});
    S.view = routeFromHash();
    render();
  } catch {
    if (S.mode === "platform") {
      // the machine is offline or still connecting: show the app shell with
      // the offline pill; events flow in once machine_status goes online
      S.view = routeFromHash();
      render();
      return;
    }
    S.authed = false;
    renderLogin();
  }
}

boot();
