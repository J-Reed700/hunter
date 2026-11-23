import { api, h, mount, fmt, toast, action, busy, sel, setQuery, poll, confirm, refreshStatusSoon, setTopActions, store, onCleanup, can } from "../app.js";
import { openWant, pickCandidates, statusBadge, debouncedInput } from "./_shared.js";

const STATUSES = ["wanted", "grabbed", "exhausted", "done", "skipped"];

export default {
  async render(root, r) {
    const op = can("operator");
    let status = r.q.status || "wanted,grabbed,exhausted", source = r.q.source || "", q = r.q.q || "", sort = r.q.sort || "updated", offset = 0, rows = [], total = 0, loadGen = 0;
    const input = debouncedInput({ placeholder: "filter artist / album", value: q, delay: 350, onInput: (v) => { q = v; setQuery({ q }); load(); } });
    const chips = h("div", { class: "chips" });
    const srcSel = h("select", { class: "select sm" });
    const sortSel = h("select", { class: "select sm" }, ...[["updated", "Recently updated"], ["created", "Newest"], ["artist", "Artist"], ["attempts", "Most attempts"], ["status", "Status"]].map(([v, l]) => h("option", { value: v, selected: sort === v }, l)));
    const table = h("div", {});
    const head = h("div", { class: "row between mb" });
    if (op) setTopActions(h("button", { class: "btn sm primary", onClick: action(null, async () => { const r = await api("/api/pass", {}); toast(r.detail); }) }, "Run pass"));
    mount(root,
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Queue"), h("div", { class: "sub" }, "Every want and where it is in its life. Grabbed wants show live slskd progress; exhausted ones need a manual pick."))),
      h("div", { class: "filterbar" }, input, chips, srcSel, sortSel, h("button", { class: "btn sm ghost ml-auto", onClick: () => load() }, "↻")),
      head, table);
    const paintChips = (counts = store.status?.counts || {}) => mount(chips, ...STATUSES.map(s => { const on = status.split(",").includes(s); return h("span", { class: "chip" + (on ? " active" : ""), onClick: () => { const set = new Set(status.split(",").filter(Boolean)); set.has(s) ? set.delete(s) : set.add(s); status = [...set].join(","); setQuery({ status }); load(); } }, s, counts[s] !== undefined ? h("span", { class: "n" }, fmt.num(counts[s])) : null); }));
    paintChips();
    onCleanup(store.on((s) => { if (root.isConnected) paintChips(s?.counts || {}); }));
    srcSel.addEventListener("change", () => { source = srcSel.value; setQuery({ source }); load(); });
    sortSel.addEventListener("change", () => { sort = sortSel.value; setQuery({ sort }); load(); });
    sel.configure("wants", [
      { label: "Retry", role: "operator", onClick: async (items) => { const r = await api("/api/retry", { ids: items.map(w => w.id) }); toast(`${r.retried} retried`, "ok"); sel.clear(); load(); } },
      { label: "Drop", cls: "danger", role: "operator", onClick: async (items) => { if (!(await confirm({ title: `Drop ${items.length} wants?`, body: "They become skipped; a later request resurrects them.", ok: "Drop", danger: true }))) return; await api("/api/drop", { ids: items.map(w => w.id) }); toast("Dropped"); sel.clear(); load(); refreshStatusSoon(); } },
    ]);

    let dl = {};
    async function load(more = false) {
      const myGen = ++loadGen;
      const nextOffset = more ? offset + 100 : 0;
      let res, t;
      try {
        [res, t] = await Promise.all([
          api(`/api/wants?status=${encodeURIComponent(status)}&source=${encodeURIComponent(source)}&q=${encodeURIComponent(q)}&sort=${encodeURIComponent(sort)}&limit=100&offset=${encodeURIComponent(nextOffset)}`),
          status.includes("grabbed") ? api("/api/transfers").catch(() => null) : null,
        ]);
      } catch (e) {
        if (myGen === loadGen && root.isConnected) mount(table, h("div", { class: "notice bad" }, e.message));
        return;
      }
      if (myGen !== loadGen || !root.isConnected) return;
      offset = nextOffset;
      rows = more ? rows.concat(res.wants) : res.wants; total = res.total;
      if (t) { dl = {}; for (const w of t.grabbed) dl[w.id] = w.dl; }
      mount(srcSel, h("option", { value: "" }, "All sources"), ...res.sources.map(s => h("option", { value: s, selected: s === source }, s)));
      paintChips(store.status?.counts || {});
      paint();
    }
    function paint() {
      mount(head, h("span", { class: "muted small" }, `${fmt.num(rows.length)} of ${fmt.num(total)}`),
        op && source ? h("button", { class: "btn xs danger", onClick: action(null, async () => { if (await confirm({ title: `Clear every open ${source} want?`, body: "Done wants stay. This is the bulk 'stop this program' switch.", ok: "Clear", danger: true, typed: source })) { const r = await api("/api/drop_source", { source }); toast(`${r.dropped} wants skipped`); sel.clear(); load(); refreshStatusSoon(); } }) }, `Clear source “${source}”`) : null);
      if (!rows.length) { mount(table, h("div", { class: "empty" }, h("div", { class: "big" }, "≡"), "No wants match.")); return; }
      mount(table, h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, op ? h("th", {}) : null, h("th", {}, "Status"), h("th", {}, "Album"), h("th", {}, "Source"), h("th", { class: "num" }, "Tracks"), h("th", { class: "num" }, "Tries"), h("th", {}, "Updated"), h("th", {}))),
        h("tbody", {}, ...rows.map(row)))),
        rows.length < total ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, `Load more (${fmt.num(total - rows.length)} left)`) : null);
    }
    function row(w) {
      const d = dl[w.id];
      const chk = op ? h("span", { class: "check" + (sel.has(w.id) ? " on" : ""), onClick: (e) => { e.stopPropagation(); chk.classList.toggle("on", sel.toggle(w.id, w)); tr.classList.toggle("sel", sel.has(w.id)); } }, "✓") : null;
      const tr = h("tr", { class: "click" + (sel.has(w.id) ? " sel" : ""), onClick: () => openWant(w.id) },
        op ? h("td", {}, chk) : null,
        h("td", {}, statusBadge(w.status), w.status === "grabbed" && d ? h("div", { class: "progress mt" + (d.errored ? " bad" : ""), style: { width: "70px", marginTop: "4px" } }, h("div", { style: { width: d.pct + "%" } })) : null),
        h("td", {}, h("div", { class: "b ellip", style: { maxWidth: "420px" } }, `${w.artist} — ${w.album}`), h("div", { class: "muted xs ellip", style: { maxWidth: "420px" } }, w.note || (w.grabbed_username ? `from ${w.grabbed_username}` : ""), d ? ` · ${d.succeeded}/${d.total} files${d.errored ? ` · ${d.errored} failed` : ""}` : "")),
        h("td", {}, h("span", { class: "badge dim" }, w.source.replace(/^playlist:/, "pl:").slice(0, 26))),
        h("td", { class: "num" }, w.track_count || ""), h("td", { class: "num", title: `${w.search_misses || 0} search misses` }, w.attempts, w.search_misses ? h("span", { class: "dim2" }, `/${w.search_misses}`) : null),
        h("td", { class: "muted small nowrap" }, fmt.agoStr(w.upd)),
        h("td", {}, op ? h("div", { class: "actions" },
          ["wanted", "grabbed", "exhausted"].includes(w.status) ? h("button", { class: "btn xs", title: "retry now", onClick: action(null, async (e) => { e.stopPropagation(); await api("/api/retry", { id: w.id }); toast("Retrying", "ok"); }) }, "↻") : null,
          h("button", { class: "btn xs", title: "pick a source", onClick: (e) => { e.stopPropagation(); pickCandidates(w.id, { title: `${w.artist} — ${w.album}`, tracks: w.track_count }); } }, "Pick"),
          ["wanted", "grabbed", "exhausted"].includes(w.status) ? h("button", { class: "btn xs danger", title: "drop", onClick: action(null, async (e) => { e.stopPropagation(); await api("/api/drop", { id: w.id }); sel.set(w.id, w, false); rows = rows.filter(x => x.id !== w.id); total = Math.max(0, total - 1); paint(); refreshStatusSoon(); }) }, "✕") : null) : null));
      return tr;
    }
    await load();
    if (!root.isConnected) return;
    poll(async () => { if (status.includes("grabbed")) { const t = await api("/api/transfers").catch(() => null); if (t && root.isConnected) { dl = {}; for (const w of t.grabbed) dl[w.id] = w.dl; paint(); } } }, 8000, { immediate: false });
  },
};
