/* hunter — app core: API client, DOM builder, hash router, view lifecycle,
   polling, overlays (drawer / modal / toast / palette), selection bar. */

// ---------- API + session ----------
const TOKEN_KEY = "hunter_token";
export function token() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } }
export function setToken(t) { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch {} }

// Who am I. Filled by boot() from /api/auth/me; every 401 re-opens the gate.
export const session = { me: null, roles: ["viewer", "operator", "admin"] };
const ROLE_RANK = { viewer: 0, operator: 1, admin: 2 };
export const can = (role) => !!session.me && (ROLE_RANK[session.me.role] ?? -1) >= (ROLE_RANK[role] ?? 99);
export const isAdmin = () => can("admin");

export class ApiError extends Error {
  constructor(status, data) { super((data && data.error) || `HTTP ${status}`); this.status = status; this.data = data || {}; }
}

export async function api(path, body, opts = {}) {
  const headers = { "Accept": "application/json" };
  const t = token(); if (t) headers["X-Hunter-Token"] = t;
  const init = { method: body === undefined ? "GET" : "POST", headers, credentials: "same-origin", ...opts };
  if (body !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, init); }
  catch (e) { throw new ApiError(0, { error: "network: " + e.message }); }
  let data = {};
  const txt = await res.text();
  try { data = txt ? JSON.parse(txt) : {}; } catch { data = { error: txt.slice(0, 200) }; }
  if (res.status === 401 && !path.startsWith("/api/auth/")) { authGate(data.setup_required ? "setup" : "login"); throw new ApiError(401, data); }
  if (res.status === 403 && data.must_change_pw) { authGate("password"); throw new ApiError(403, data); }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}
export const qs = (obj) => { const p = new URLSearchParams(); for (const [k, v] of Object.entries(obj || {})) if (v !== undefined && v !== null && v !== "") p.set(k, v); const s = p.toString(); return s ? "?" + s : ""; };
export const artUrl = (rgid) => rgid ? `/api/art/${encodeURIComponent(rgid)}` : "";
export const libArtUrl = (id) => `/api/library/album_art?id=${encodeURIComponent(id)}`;

// ---------- auth gate (setup / login / forced password change) ----------
let gateEl = null;
function stopBackgroundForGate() {
  current.cleanups.forEach(f => { try { f(); } catch {} });
  current.cleanups = [];
  closeDrawers();
  closePalette();
}
function authUnavailable(err) {
  stopBackgroundForGate();
  if (gateEl) { gateEl.remove(); gateEl = null; }
  $("#app").hidden = true;
  const retryBtn = h("button", {
    class: "btn primary lg",
    type: "button",
    onClick: async () => { await busy(retryBtn, boot()); },
  }, "Retry");
  gateEl = h("div", { class: "gate", dataset: { mode: "error" } },
    h("div", { class: "gate-card" },
      h("div", { class: "brand" }, h("span", { class: "brand-mark" }), h("span", { class: "brand-name" }, "hunter")),
      h("h1", {}, "Unable to reach Hunter"),
      h("p", { class: "hint" }, err?.message ? `Could not check session status (${err.message}). Your data is safe; try again in a moment.` : "Could not check session status. Try again in a moment."),
      retryBtn));
  document.body.appendChild(gateEl);
}
export function authGate(mode) {
  stopBackgroundForGate();
  if (gateEl) { if (gateEl.dataset.mode === mode) return; gateEl.remove(); gateEl = null; }
  $("#app").hidden = true;
  const err = h("div", { class: "notice bad", hidden: true });
  const showErr = (m) => { err.textContent = m; err.hidden = false; };
  const user = h("input", { class: "input", placeholder: "username", autocomplete: "username", autocapitalize: "none", spellcheck: "false" });
  const display = h("input", { class: "input", placeholder: "display name (optional)", autocomplete: "name" });
  const pw = h("input", { class: "input", type: "password", placeholder: "password", autocomplete: mode === "login" ? "current-password" : "new-password" });
  const pw2 = h("input", { class: "input", type: "password", placeholder: "repeat password", autocomplete: "new-password" });
  const cur = h("input", { class: "input", type: "password", placeholder: "current password", autocomplete: "current-password" });
  const remember = h("input", { type: "checkbox", checked: true });
  const btn = h("button", { class: "btn primary lg", type: "submit" }, mode === "setup" ? "Create admin account" : mode === "login" ? "Sign in" : "Set new password");
  const form = h("form", { class: "stack", onSubmit: async (e) => {
    e.preventDefault(); err.hidden = true;
    try {
      if (mode === "setup") {
        if (pw.value !== pw2.value) return showErr("passwords do not match");
        const r = await busy(btn, api("/api/auth/setup", { username: user.value.trim(), display: display.value.trim(), password: pw.value }));
        session.me = r.user;
      } else if (mode === "login") {
        const r = await busy(btn, api("/api/auth/login", { username: user.value.trim(), password: pw.value, remember: remember.checked }));
        session.me = r.user;
      } else {
        if (pw.value !== pw2.value) return showErr("passwords do not match");
        await busy(btn, api("/api/auth/password", { current: cur.value, new: pw.value }));
        session.me = { ...session.me, must_change_pw: false };
      }
      gateEl.remove(); gateEl = null; $("#app").hidden = false;
      toast(mode === "setup" ? `Welcome, ${session.me.display || session.me.username}` : mode === "login" ? `Signed in as ${session.me.username}` : "Password updated", "ok");
      boot(true);
    } catch (ex) { showErr(ex.message); }
  } },
    mode === "setup" ? [user, display, pw, pw2] : mode === "login" ? [user, pw, h("label", { class: "row small muted" }, remember, " keep me signed in for 30 days")] : [cur, pw, pw2],
    err, btn);
  const copy = { setup: ["Set up hunter", "No accounts exist yet. This first account is the administrator — it manages users, the beets console and everything else. Keep the password somewhere safe; recovery is CLI-only."],
                 login: ["Sign in", "Your session is a secure cookie on this browser."],
                 password: ["Choose a new password", `An administrator reset your password${session.me ? `, ${session.me.username}` : ""}. Pick a new one to continue.`] }[mode];
  gateEl = h("div", { class: "gate", dataset: { mode } },
    h("div", { class: "gate-card" },
      h("div", { class: "brand" }, h("span", { class: "brand-mark" }), h("span", { class: "brand-name" }, "hunter")),
      h("h1", {}, copy[0]), h("p", { class: "hint" }, copy[1]), form,
      mode === "password" ? h("button", { class: "btn ghost sm", onClick: () => logout() }, "Sign out instead") : null));
  document.body.appendChild(gateEl);
  setTimeout(() => (mode === "password" ? cur : user).focus(), 40);
}
export async function logout() {
  try { await api("/api/auth/logout", {}); } catch {}
  setToken("");
  session.me = null; location.reload();
}

// ---------- DOM ----------
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const k of kids) {
    if (k === null || k === undefined || k === false) continue;
    if (Array.isArray(k)) append(el, k);
    else if (k instanceof Node) el.appendChild(k);
    else el.appendChild(document.createTextNode(String(k)));
  }
}
export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
export const mount = (el, ...kids) => { clear(el); append(el, kids); return el; };
export const $ = (sel, root = document) => root.querySelector(sel);

// ---------- formatting ----------
export const fmt = {
  num: (n) => (n ?? 0).toLocaleString(),
  ago(ts) { if (!ts) return "never"; const s = Math.floor(Date.now() / 1000 - ts); if (s < 5) return "just now"; if (s < 60) return s + "s ago"; if (s < 3600) return Math.floor(s / 60) + "m ago"; if (s < 86400) return Math.floor(s / 3600) + "h ago"; return Math.floor(s / 86400) + "d ago"; },
  agoStr(str) { if (!str) return "—"; const t = Date.parse(str.replace(" ", "T")); return isNaN(t) ? str : fmt.ago(t / 1000); },
  in(ts) { const s = Math.floor(ts - Date.now() / 1000); if (s <= 0) return "now"; if (s < 60) return s + "s"; if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s"; return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m"; },
  dur(s) { s = Math.round(s || 0); if (s < 60) return s + "s"; if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s"; if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m"; return Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h"; },
  mmss(s) { s = Math.round(s || 0); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); },
  hours(hrs) { if (!hrs) return "0h"; if (hrs < 48) return hrs.toFixed(1) + "h"; return Math.round(hrs / 24) + "d " + Math.round(hrs % 24) + "h"; },
  bytes(n) { if (!n && n !== 0) return "—"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; n = Number(n); while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return (i ? n.toFixed(i > 2 ? 2 : 1) : n) + " " + u[i]; },
  date(ts) { if (!ts) return "—"; return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }); },
  datetime(ts) { if (!ts) return "—"; return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); },
};

// ---------- toast ----------
export function toast(msg, kind = "", ms = 3200) {
  const el = h("div", { class: "toast " + kind }, msg);
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .25s"; setTimeout(() => el.remove(), 260); }, ms);
}
export function errToast(e) { toast(e?.message || String(e), "bad", 5000); }

// busy button helper
export async function busy(btn, promise) {
  if (btn) btn.classList.add("busy");
  try { return await promise; } finally { if (btn) btn.classList.remove("busy"); }
}
export function action(btn, fn) {
  return async (ev) => {
    ev?.stopPropagation?.();
    const target = btn || (ev?.currentTarget instanceof Element && ev.currentTarget.classList.contains("btn") ? ev.currentTarget : null);
    try { await busy(target, fn(ev)); } catch (e) { errToast(e); }
  };
}

// ---------- overlay history ----------
// Every sheet (drawer, modal, mobile menu) pushes a history entry so the
// phone's Back gesture closes the top sheet instead of leaving the view.
// UI closes go through history.back() and let popstate do the teardown;
// programmatic teardown (route change) skips history entirely.
const overlays = [];
let ovlSeq = 0;
function overlayOpen(destroy) {
  const ctl = { id: ++ovlSeq, destroy };
  overlays.push(ctl);
  try { history.pushState({ ...(history.state || {}), ovl: ctl.id }, "", location.href); } catch {}
  return ctl;
}
function overlayDestroy(ctl) {
  const i = overlays.indexOf(ctl); if (i < 0) return;
  overlays.splice(i, 1);
  try { ctl.destroy(); } catch (e) { console.warn("overlay teardown", e); }
}
function overlayClose(ctl) {
  if (!overlays.includes(ctl)) return;
  if (history.state?.ovl === ctl.id) { history.back(); return; }   // popstate → overlayDestroy
  overlayDestroy(ctl);
}
window.addEventListener("popstate", () => {
  const cur = history.state?.ovl || 0;
  for (const ctl of [...overlays].reverse()) if (ctl.id > cur) overlayDestroy(ctl);
});
export const isPhone = () => matchMedia("(max-width: 640px)").matches;

// ---------- modal / confirm ----------
export function modal({ title, body, actions = [], wide = false, onClose }) {
  const host = $("#modal-host");
  const scrim = h("div", { class: "modal-scrim" });
  const ctl = overlayOpen(() => { scrim.remove(); onClose && onClose(); });
  const close = () => overlayClose(ctl);
  const foot = h("div", { class: "modal-foot" }, h("button", { class: "btn ghost", onClick: close }, "Close"),
    ...actions.map(a => h("button", { class: "btn " + (a.cls || ""), onClick: async (ev) => { try { await busy(ev.currentTarget, Promise.resolve(a.onClick(close))); if (!a.keep) close(); } catch (e) { errToast(e); } } }, a.label)));
  const box = h("div", { class: "modal" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-label": typeof title === "string" ? title : "dialog" }, h("div", { class: "modal-head" }, title), h("div", { class: "modal-body" }, body), foot);
  scrim.appendChild(box);
  scrim.addEventListener("click", (e) => { if (e.target === scrim) close(); });
  host.appendChild(scrim);
  return { close, box };
}
// Mobile-friendly replacement for window.prompt(): resolves the trimmed text, or null.
export function promptText({ title = "Name", value = "", placeholder = "", ok = "OK", hint = "" } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    const inp = h("input", { class: "input", value, placeholder, enterkeyhint: "done" });
    const m = modal({ title, body: h("div", { class: "stack" }, inp, hint ? h("div", { class: "hint" }, hint) : null), onClose: () => done(null),
      actions: [{ label: ok, cls: "primary", onClick: () => done(inp.value.trim()) }] });
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { done(inp.value.trim()); m.close(); } });
    setTimeout(() => inp.focus(), 40);
  });
}
// localStorage that never throws (private mode / storage blocked)
export const ls = { get(k, d = null) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch {} } };
export function confirm({ title = "Are you sure?", body = "", ok = "Confirm", danger = false, typed = "" }) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    let inp = null;
    const content = h("div", { class: "stack" }, typeof body === "string" ? h("p", { class: "hint" }, body) : body);
    if (typed) { inp = h("input", { class: "input mono", placeholder: `type ${typed} to confirm` }); content.appendChild(inp); }
    modal({ title, body: content, onClose: () => done(false),
      actions: [{ label: ok, cls: danger ? "danger" : "primary", keep: true, onClick: (close) => {
        if (typed && inp.value.trim() !== typed) { toast("confirmation text does not match", "warn"); inp.focus(); return; }
        done(true); close();
      } }] });
    if (inp) setTimeout(() => inp.focus(), 40);
  });
}

// ---------- drawer ----------
export function drawer({ title, body, wide = false, narrow = false, onClose, actions }) {
  const host = $("#drawer-host");
  const scrim = h("div", { class: "drawer-scrim" });
  const bodyEl = h("div", { class: "drawer-body" }, body);
  const titleEl = h("h2", {}, title);
  const actEl = h("div", { class: "row nowrap" }, actions);
  const head = h("div", { class: "drawer-head" }, titleEl, actEl, h("button", { class: "icon-btn", onClick: () => close(), "aria-label": "Close" }, "✕"));
  const box = h("div", { class: "drawer" + (wide ? " wide" : "") + (narrow ? " narrow" : ""), role: "dialog", "aria-modal": "true" }, head, bodyEl);
  const ovl = overlayOpen(() => { scrim.remove(); box.remove(); onClose && onClose(); });
  const close = () => overlayClose(ovl);
  scrim.addEventListener("click", close);
  // phone: drag the sheet handle down to dismiss
  let y0 = null, dy = 0;
  head.addEventListener("touchstart", (e) => { if (!isPhone()) return; y0 = e.touches[0].clientY; dy = 0; box.classList.add("dragging"); }, { passive: true });
  head.addEventListener("touchmove", (e) => { if (y0 === null) return; dy = Math.max(0, e.touches[0].clientY - y0); box.style.transform = `translateY(${dy}px)`; }, { passive: true });
  head.addEventListener("touchend", () => { if (y0 === null) return; box.classList.remove("dragging"); box.style.transform = ""; if (dy > 90) close(); y0 = null; });
  host.append(scrim, box);
  return { close, dismiss: () => overlayDestroy(ovl), body: bodyEl, setTitle: (t) => mount(titleEl, t), setActions: (a) => mount(actEl, a), _ovl: ovl };
}
// route change: tear everything down without touching history
export function closeDrawers() { [...overlays].forEach(o => overlayDestroy(o)); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const top = overlays[overlays.length - 1]; if (top) overlayClose(top); } });

// ---------- selection bar ----------
class Selection {
  constructor() { this.items = new Map(); this.label = "Selected"; this.actions = []; }
  configure(label, actions) { this.clear(); this.label = label; this.actions = actions; this.render(); }
  toggle(key, item) { this.items.has(key) ? this.items.delete(key) : this.items.set(key, item); this.render(); return this.items.has(key); }
  has(key) { return this.items.has(key); }
  set(key, item, on) { on ? this.items.set(key, item) : this.items.delete(key); this.render(); }
  clear() {
    this.items.clear();
    this.render();
    document.querySelectorAll(".check.on:not([data-local])").forEach(c => c.classList.remove("on"));
    document.querySelectorAll(".sel:not([data-local])").forEach(c => c.classList.remove("sel"));
  }
  values() { return [...this.items.values()]; }
  render() {
    const bar = $("#selbar");
    const allowed = this.actions.filter(a => !a.role || can(a.role));
    const visible = !!this.items.size && allowed.length > 0;
    document.body.classList.toggle("has-sel", visible);   // app.css pads #view so the bar never covers the last rows
    if (!visible) { bar.hidden = true; return; }
    bar.hidden = false;
    const run = (a) => async (ev) => { try { await busy(ev.currentTarget, a.onClick(this.values())); } catch (e) { errToast(e); } };
    if (isPhone()) {
      // Phone: one slim strip docked above the tab bar — count, the primary
      // action, and an Actions button that opens the full list as a sheet.
      // The old floating blob wrapped 5–7 buttons and sat on top of the content.
      const [first, ...rest] = allowed;
      const sheet = () => {
        let m = null;
        m = modal({ title: `${this.items.size} ${this.label}`, body: h("div", { class: "stack" },
          ...allowed.map(a => h("button", { class: "btn " + (a.cls || "primary"), onClick: async (ev) => { await run(a)(ev); m && m.close(); } }, a.label)),
          h("button", { class: "btn ghost", onClick: () => { this.clear(); m && m.close(); } }, "Clear selection")) });
      };
      mount(bar, h("span", { class: "sel-count" }, `${this.items.size} ${this.label}`),
        first ? h("button", { class: "btn sm " + (first.cls || "primary"), onClick: run(first) }, first.label) : null,
        rest.length ? h("button", { class: "btn sm", onClick: sheet }, `Actions (${allowed.length})`) : null,
        h("button", { class: "icon-btn", "aria-label": "Clear selection", onClick: () => this.clear() }, "✕"));
      return;
    }
    mount(bar, h("span", {}, `${this.items.size} ${this.label}`),
      ...allowed.map(a => h("button", { class: "btn sm " + (a.cls || "primary"), onClick: run(a) }, a.label)),
      h("button", { class: "btn sm ghost", onClick: () => this.clear() }, "Clear"));
  }
}
export const sel = new Selection();

// ---------- cover ----------
export function cover(src, { size = "", fallback = "♪", alt = "" } = {}) {
  const box = h("div", { class: "cover " + size });
  const ph = h("div", { class: "ph" }, fallback);
  box.appendChild(ph);
  if (src) { const img = h("img", { alt, loading: "lazy", src }); img.addEventListener("load", () => ph.remove()); img.addEventListener("error", () => img.remove()); box.appendChild(img); }
  return box;
}

// ---------- router + view lifecycle ----------
const VIEWS = {
  dashboard: { title: "Dashboard", ico: "◐", group: "Overview" },
  discover: { title: "Discover", ico: "⌕", group: "Acquire" },
  queue: { title: "Queue", ico: "≡", group: "Acquire", count: "open" },
  artists: { title: "Artists", ico: "♫", group: "Acquire" },
  spotify: { title: "Spotify", ico: "◉", group: "Acquire" },
  library: { title: "Library", ico: "▤", group: "Library" },
  beets: { title: "Beets", ico: "⚙", group: "Library", role: "operator" },
  downloads: { title: "Downloads", ico: "⤓", group: "Library" },
  recovery: { title: "Recovery", ico: "⟲", group: "Library", role: "operator" },
  jobs: { title: "Jobs", ico: "▶", group: "System", count: "jobs", role: "operator" },
  activity: { title: "Activity", ico: "⋯", group: "System" },
  settings: { title: "Settings", ico: "⚒", group: "System" },
};
const MOBILE_TABS = ["dashboard", "discover", "queue", "library"];   // + "More" → full menu sheet
let current = { name: "", mod: null, cleanups: [] };
let navGen = 0;
let _readyResolve;
export const store = {
  status: null, listeners: new Set(),
  // resolves after the first /api/status answer (or 1.5 s) so views never read status before it exists
  ready: null,
  resetReady() { this.ready = new Promise((res) => { _readyResolve = res; setTimeout(res, 1500); }); },
  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); },
  emit() { this.listeners.forEach(f => { try { f(this.status); } catch {} }); },
};
store.resetReady();

export const safeDecode = (s) => { try { return decodeURIComponent(s); } catch { return s; } };
export function route() {
  const hash = location.hash.replace(/^#\/?/, "");
  const [pathPart, queryPart] = hash.split("?");
  const segs = pathPart.split("/").filter(Boolean);
  const name = safeDecode(segs[0] || "dashboard");
  const q = Object.fromEntries(new URLSearchParams(queryPart || ""));
  return { name, path: segs.slice(1).map(safeDecode), q };
}
export function navigate(hash) { if (location.hash === hash) render(); else location.hash = hash; }
export function link(name, params, ...path) { return "#/" + [name, ...path.map(encodeURIComponent)].join("/") + qs(params); }
export function setQuery(params, replace = true) {
  const r = route(); const merged = { ...r.q, ...params };
  const hash = "#/" + [r.name, ...r.path.map(encodeURIComponent)].join("/") + qs(merged);
  if (replace) history.replaceState(history.state, "", hash); else location.hash = hash;
}
export function onCleanup(fn) { current.cleanups.push(fn); }
export function poll(fn, ms, { immediate = true } = {}) {
  let stopped = false, timer = null, running = false;
  const tick = async () => {
    if (stopped || document.hidden || gateEl || session.me?.must_change_pw) {
      if (!stopped) timer = setTimeout(tick, ms);
      return;
    }
    if (!running) { running = true; try { await fn(); } catch (e) { console.warn("poll", e); } running = false; }
    if (!stopped) timer = setTimeout(tick, ms);
  };
  if (immediate) tick(); else timer = setTimeout(tick, ms);
  const stop = () => { stopped = true; clearTimeout(timer); };
  onCleanup(stop);
  return stop;
}
export function setCrumbs(parts) { mount($("#crumbs"), parts.flatMap((p, i) => [i ? h("span", { class: "sep" }, "›") : null, typeof p === "string" ? h("span", { class: i === parts.length - 1 ? "cur" : "muted" }, p) : p])); }
export function setTopActions(...els) { mount($("#top-actions"), els); }

async function render() {
  const gen = ++navGen;
  const r = route();
  const def = VIEWS[r.name] || VIEWS.dashboard;
  const name = VIEWS[r.name] ? r.name : "dashboard";
  current.cleanups.forEach(f => { try { f(); } catch {} });
  current.cleanups = [];
  sel.clear();
  closeDrawers();
  setTopActions();
  const viewRoot = $("#view");
  document.querySelectorAll("#nav .nav-item, #tabbar a").forEach(a => a.classList.toggle("active", a.dataset.view === name));
  $("#tabbar .more")?.classList.toggle("active", !MOBILE_TABS.includes(name));
  document.title = `${def.title} · hunter`;
  setCrumbs([def.title]);
  if (def.role && !can(def.role)) {
    mount(viewRoot, h("div", { class: "notice warn" }, h("div", { class: "body" }, h("b", {}, "Restricted. "), `This page requires ${def.role} access.`)));
    return;
  }
  const host = h("div", { class: "view-host" });
  mount(viewRoot, host);
  mount(host, h("div", { class: "skel" }, h("div"), h("div"), h("div")));
  await store.ready;
  if (gen !== navGen) return;
  try {
    const mod = (current.mod && current.name === name) ? current.mod : (await import(`./views/${name}.js`)).default;
    if (gen !== navGen) return;
    current.mod = mod;
    current.name = name;
    await mod.render(host, r);
    if (gen !== navGen) return;
  } catch (e) {
    if (gen !== navGen) return;
    console.error(e);
    mount(host, h("div", { class: "notice bad" }, h("div", { class: "body" }, h("b", {}, "This view failed to load. "), String(e?.message || e))));
  }
  window.scrollTo(0, 0);
}

// ---------- nav ----------
let navBound = false;
function buildNav() {
  const nav = $("#nav"); clear(nav);
  let group = "";
  for (const [name, d] of Object.entries(VIEWS)) {
    if (d.role && !can(d.role)) continue;
    if (d.group !== group) { group = d.group; nav.appendChild(h("div", { class: "nav-group" }, group)); }
    nav.appendChild(h("a", { class: "nav-item", href: "#/" + name, dataset: { view: name } }, h("span", { class: "ico" }, d.ico), d.title, d.count ? h("span", { class: "cnt", dataset: { count: d.count } }, "") : null));
  }
  const tabs = $("#tabbar"); clear(tabs);
  for (const name of MOBILE_TABS) {
    const d = VIEWS[name];
    if (d.role && !can(d.role)) continue;
    tabs.appendChild(h("a", { href: "#/" + name, dataset: { view: name } }, h("span", { class: "ico" }, d.ico), d.title));
  }
  tabs.appendChild(h("button", { class: "more", type: "button", "aria-label": "More pages", onClick: openMenu }, h("span", { class: "ico" }, "☰"), "More"));
  if (!navBound) {
    navBound = true;
    nav.addEventListener("click", (e) => { if (e.target.closest(".nav-item") && menuCtl) overlayDestroy(menuCtl); });
  }
}
// the sidebar as a sheet on small screens — Back / scrim / picking a page all close it
let menuCtl = null;
function openMenu() {
  if (menuCtl) return;
  const side = $("#sidebar");
  const scrim = h("div", { id: "sidebar-scrim" });
  menuCtl = overlayOpen(() => { side.classList.remove("open"); scrim.remove(); menuCtl = null; });
  scrim.addEventListener("click", () => overlayClose(menuCtl));
  document.body.appendChild(scrim);
  side.classList.add("open");
}

// ---------- progressive enhancement of rendered DOM ----------
// Views build plain tables; on phones app.css turns them into cards using the
// data-label / data-role stamps applied here. Also: keep the active tab in
// view, and make click-only rows/chips reachable from the keyboard.
const PRIMARY_LABELS = new Set(["album", "job", "title", "artist", "name", "playlist", "track", "file", "entry", "peer", "what", "release", "command", "script", "dir", "path", "song", "want", "series"]);
const A11Y_SEL = ".item.click, tr.click, .stat:not(a), .chip, .check, .cover-card, .group-card, .dup-row, .nav-item:not(a), .tree .node, .palette .opt";
const headersOf = (tbl) => [...tbl.querySelectorAll(":scope > thead th")].map(th => th.textContent.trim());
function labelRow(tr, ths) {
  if (tr.dataset.labeled) return; tr.dataset.labeled = "1";
  const tds = [...tr.children].filter(c => c.tagName === "TD");
  tds.forEach((td, i) => {
    td.dataset.label = ths[i] ?? "";
    td.dataset.role = td.querySelector(":scope > .check") ? "check" : td.querySelector(":scope > .actions") ? "actions" : "field";
    if (!td.children.length && !td.textContent.trim()) td.dataset.empty = "1";
  });
  const fields = tds.filter(td => td.dataset.role === "field" && !td.dataset.empty);
  const primary = fields.find(td => td.querySelector(".b")) || fields.find(td => PRIMARY_LABELS.has(td.dataset.label.toLowerCase())) || fields[0];
  if (primary) primary.dataset.role = "primary";
}
function labelTable(tbl) { const ths = headersOf(tbl); tbl.querySelectorAll(":scope > tbody > tr").forEach(tr => labelRow(tr, ths)); }
function enhance(n) {
  if (n.matches("table.table")) labelTable(n);
  n.querySelectorAll("table.table").forEach(labelTable);
  if (n.matches("tr:not([data-labeled])")) { const t = n.closest("table.table"); if (t) labelRow(n, headersOf(t)); }
  n.querySelectorAll("tr:not([data-labeled])").forEach(tr => { const t = tr.closest("table.table"); if (t) labelRow(tr, headersOf(t)); });
  const act = n.matches(".tabs button.active") ? n : n.querySelector(".tabs button.active");
  if (act && matchMedia("(max-width: 900px)").matches) act.scrollIntoView({ inline: "center", block: "nearest" });
  const targets = n.matches(A11Y_SEL) ? [n, ...n.querySelectorAll(A11Y_SEL)] : [...n.querySelectorAll(A11Y_SEL)];
  for (const el of targets) if (!el.closest(".plan-stats") && !el.hasAttribute("tabindex")) { el.tabIndex = 0; if (el.tagName !== "TR" && !el.hasAttribute("role")) el.setAttribute("role", el.classList.contains("check") ? "checkbox" : "button"); }
}
new MutationObserver((muts) => { for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1) enhance(n); }).observe($("#app"), { childList: true, subtree: true });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const el = e.target; if (!(el instanceof Element) || !el.matches(A11Y_SEL) || /input|textarea|select|button|a/i.test(el.tagName)) return;
  e.preventDefault(); el.click();
});

// ---------- status pill + heartbeat ----------
let statusTimer = null;
async function refreshStatus() {
  if (gateEl || session.me?.must_change_pw) { _readyResolve(); return; }
  try { store.status = await api("/api/status"); } catch (e) { if (e.status !== 401 && e.status !== 403) console.warn(e); _readyResolve(); return; }
  _readyResolve();
  const s = store.status;
  const c = s.counts || {};
  const open = (c.wanted || 0) + (c.grabbed || 0);
  document.querySelectorAll('[data-count="open"]').forEach(el => el.textContent = open ? fmt.num(open) : "");
  document.querySelectorAll('[data-count="jobs"]').forEach(el => { el.textContent = s.jobs_running ? String(s.jobs_running) : ""; el.classList.toggle("acc", !!s.jobs_running); });
  const hb = s.heartbeat;
  const next = hb?.ts ? hb.ts + s.interval : null;
  const pill = h("button", { class: "pill-status " + (s.apply ? "live" : "shadow"), title: "Toggle shadow/live", onClick: toggleApply }, h("span", { class: "dot " + (s.apply ? "ok" : "warn") }), s.apply ? "LIVE" : "SHADOW");
  mount($("#top-status"), can("operator") ? pill : h("span", { class: "pill-status shadow", title: "your account is read-only" }, h("span", { class: "dot" }), "READ-ONLY"),
    s.jobs_running ? (can("operator") ? h("a", { class: "badge acc", href: "#/jobs", title: s.jobs.map(j => j.title).join("\n") }, h("span", { class: "dot ok pulse" }), `${s.jobs_running} job${s.jobs_running > 1 ? "s" : ""}`) : h("span", { class: "badge acc" }, h("span", { class: "dot ok pulse" }), `${s.jobs_running} job${s.jobs_running > 1 ? "s" : ""}`)) : null,
    s.spotify?.error && can("operator") ? h("a", { class: "badge bad", href: "#/settings?tab=spotify", title: s.spotify.error }, "Spotify") : null,
    s.storage && !s.storage.ok ? h("a", { class: "badge bad", href: "#/", title: s.storage.error }, "Storage") : null);
  mount($("#side-status"),
    userChip(),
    h("div", { class: "row" }, h("span", {}, "Last pass"), h("span", { class: "mono", title: hb?.status || "" }, hb?.ts ? fmt.ago(hb.ts) : "never")),
    h("div", { class: "row" }, h("span", {}, "Next pass"), h("span", { class: "mono" }, next ? fmt.in(next) : "—")),
    h("div", { class: "row" }, h("span", {}, "Open wants"), h("span", { class: "mono" }, fmt.num(open))),
    h("div", { class: "row" }, h("span", {}, "Console"), h("span", { class: "dot " + (s.console ? "ok" : "bad"), title: s.console ? "docker socket mounted" : "docker socket missing" })),
    h("div", { class: "row" }, h("span", {}, "Storage"), h("span", { class: "dot " + (!s.storage ? "warn" : s.storage.ok ? "ok" : "bad"), title: !s.storage ? "unknown" : s.storage.ok ? `${s.storage.merged?.path || "storage"} is live` : s.storage.error })),
    h("div", { class: "row" }, can("operator") ? h("button", { class: "btn xs ghost", onClick: action(null, async () => { const r = await api("/api/pass", {}); toast(r.detail); }) }, "Run pass") : null,
      h("button", { class: "btn xs ghost", onClick: () => navigate("#/settings") }, "Settings")));
  store.emit();
}
function userChip() {
  const me = session.me; if (!me) return null;
  return h("div", { class: "user-chip" }, h("span", { class: "avatar" }, (me.display || me.username).slice(0, 1).toUpperCase()),
    h("div", { class: "who" }, h("div", { class: "nm ellip" }, me.display || me.username), h("span", { class: "badge " + (me.role === "admin" ? "acc" : me.role === "operator" ? "info" : "dim") }, me.role)),
    h("a", { class: "icon-btn", href: "#/settings?tab=account", title: "Account" }, "⚙"),
    h("button", { class: "icon-btn", title: "Sign out", onClick: logout }, "⏻"));
}
async function toggleApply() {
  const s = store.status; if (!s) return;
  if (!can("operator")) { toast("Your account is read-only", "warn"); return; }
  const target = !s.apply;
  if (target && !(await confirm({ title: "Go LIVE?", body: "hunter will enqueue real downloads in slskd and write Navidrome playlists on the next pass.", ok: "Go live" }))) return;
  try { await api("/api/apply", { apply: target }); toast(target ? "LIVE — downloads will be enqueued" : "SHADOW — nothing will be enqueued", target ? "ok" : "warn"); refreshStatus(); } catch (e) { errToast(e); }
}
export function refreshStatusSoon() { clearTimeout(statusTimer); statusTimer = setTimeout(refreshStatus, 400); }

// ---------- command palette ----------
let palTimer = null, palIdx = 0, palOpts = [], palCtl = null;
function openPalette() {
  if (gateEl || session.me?.must_change_pw || palCtl) return;
  const host = $("#palette"); host.hidden = false;
  const inp = h("input", { placeholder: "Search MusicBrainz, your library, or jump to a page…", autocomplete: "off" });
  const res = h("div", { class: "res" });
  mount(host, h("div", { class: "palette" }, inp, res));
  palCtl = overlayOpen(() => { clearTimeout(palTimer); palTimer = null; palCtl = null; host.hidden = true; clear(host); });
  const renderOpts = () => mount(res, palOpts.length ? palOpts : h("div", { class: "empty small" }, "Type to search albums / artists, or a page name"));
  const run = async () => {
    const q = inp.value.trim(); palOpts = []; palIdx = 0;
    const pages = Object.entries(VIEWS).filter(([n, d]) => (!d.role || can(d.role)) && (!q || d.title.toLowerCase().includes(q.toLowerCase()))).slice(0, q ? 3 : 10)
      .map(([n, d]) => opt(d.ico, d.title, "page", () => navigate("#/" + n)));
    if (pages.length) palOpts.push(h("div", { class: "grp" }, "Pages"), ...pages);
    if (q.length >= 2) {
      palOpts.push(h("div", { class: "grp" }, "Actions"),
        opt("⌕", `Search MusicBrainz for “${q}”`, "albums · artists · songs", () => navigate(link("discover", { q }))),
        opt("▤", `Search library for “${q}”`, "beets", () => navigate(link("library", { q, tab: "albums" }))),
        opt("≡", `Filter queue by “${q}”`, "wants", () => navigate(link("queue", { q }))));
      renderOpts();
      try {
        const [lib, mb] = await Promise.all([api(`/api/library/albums${qs({ q, limit: 6 })}`), api(`/api/search${qs({ kind: "album", q })}`).catch(() => ({ results: [] }))]);
        if (!palCtl || inp.value.trim() !== q) return;
        if (lib.albums?.length) palOpts.push(h("div", { class: "grp" }, "In your library"), ...lib.albums.map(a => opt("▤", `${a.artist} — ${a.album}`, `${a.year || ""} · ${a.tracks} tracks`, () => navigate(link("library", {}, "album", String(a.id))))));
        if (mb.results?.length) palOpts.push(h("div", { class: "grp" }, "MusicBrainz"), ...mb.results.slice(0, 6).map(a => opt(a.owned ? "✓" : "⌕", `${a.artist} — ${a.album}`, `${a.year || ""}${a.owned ? " · owned" : ""}`, () => navigate(link("discover", { q: `${a.artist} ${a.album}` })))));
      } catch {}
    }
    if (palCtl) renderOpts();
  };
  const opt = (ico, t, s, go) => h("div", { class: "opt", onClick: () => { if (palCtl) overlayDestroy(palCtl); go(); } }, h("span", { class: "ico" }, ico), h("span", { class: "t" }, t), h("span", { class: "s" }, s));
  inp.addEventListener("input", () => { clearTimeout(palTimer); palTimer = setTimeout(run, 220); });
  inp.addEventListener("keydown", (e) => {
    const opts = [...res.querySelectorAll(".opt")];
    if (e.key === "ArrowDown") { palIdx = Math.min(opts.length - 1, palIdx + 1); } else if (e.key === "ArrowUp") { palIdx = Math.max(0, palIdx - 1); }
    else if (e.key === "Enter") { (opts[palIdx] || opts[0])?.click(); return; } else return;
    e.preventDefault(); opts.forEach((o, i) => o.classList.toggle("active", i === palIdx)); opts[palIdx]?.scrollIntoView({ block: "nearest" });
  });
  host.onclick = (e) => { if (e.target === host) closePalette(); };
  run(); setTimeout(() => inp.focus(), 30);
}
function closePalette() { if (palCtl) overlayClose(palCtl); else { clearTimeout(palTimer); const host = $("#palette"); host.hidden = true; clear(host); } }
document.addEventListener("keydown", (e) => {
  if (gateEl || session.me?.must_change_pw) return;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    if (!palCtl && overlays.length) return;
    e.preventDefault(); palCtl ? closePalette() : openPalette();
  }
  if (e.key === "/" && !overlays.length && !/input|textarea|select/i.test(document.activeElement?.tagName || "")) { e.preventDefault(); openPalette(); }
});

// ---------- boot ----------
let booted = false;
async function boot(afterGate = false) {
  if (!afterGate) {
    let me;
    try { me = await api("/api/auth/me"); }
    catch (e) { return authUnavailable(e); }
    if (me.setup_required) return authGate("setup");
    if (!me.user) return authGate("login");
    session.me = me.user; session.roles = me.roles || session.roles;
    if (me.user.must_change_pw) return authGate("password");
  } else if (session.me?.must_change_pw) { return authGate("password"); }
  if (gateEl) { gateEl.remove(); gateEl = null; }
  $("#app").hidden = false;
  document.body.dataset.role = session.me?.role || "";
  buildNav();
  if (!booted) {
    booted = true;
    $("#search-btn").addEventListener("click", openPalette);
    setInterval(() => { if (!document.hidden && !gateEl && !session.me?.must_change_pw) refreshStatus(); }, 20000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && !gateEl && !session.me?.must_change_pw) refreshStatus(); });
    window.addEventListener("hashchange", render);
  }
  if (afterGate || !store.status) store.resetReady();
  refreshStatus();
  render();
}
// a failed background load used to leave a skeleton with no explanation
let lastRej = 0;
window.addEventListener("unhandledrejection", (e) => { const now = Date.now(); if (now - lastRej < 1500) return; lastRej = now; const r = e.reason; if (r?.status === 401) return; errToast(r?.message ? r : new Error(String(r))); });
boot();
