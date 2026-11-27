import { api, h, mount, fmt, poll, setQuery, busy, link, isAdmin } from "../app.js";
import { createTabs, debouncedInput } from "./_shared.js";
import { kindCls } from "./dashboard.js";

export default {
  async render(root, r) {
    const defs = [["events", "Events"], ...(isAdmin() ? [["audit", "Audit trail"]] : [])];
    const tabCtl = createTabs(defs, r.q.tab || "events", (tab, ctx) => ({ events, audit })[tab](ctx));
    mount(root, h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Activity"), h("div", { class: "sub" }, "Events: what the pipeline did (grabs, landings, removals, blocks). Audit: who did what in this console, from where."))), tabCtl.el, tabCtl.out);

    async function events({ out, alive, setPoll }) {
      let kind = r.q.kind || "", offset = 0, rows = [], loadGen = 0;
      const chips = h("div", { class: "chips" });
      const list = h("div", {});
      mount(out, h("div", { class: "filterbar" }, chips), list);
      const paintChips = (kinds) => mount(chips, ...[["", "All"], ...kinds.map(k => [k, k])].map(([k, l]) => h("span", { class: "chip" + (kind === k ? " active" : ""), onClick: () => { kind = k; setQuery({ kind }); load(); } }, l)));
      async function load(more = false) {
        const myGen = ++loadGen;
        const nextOffset = more ? offset + 100 : 0;
        let res;
        try { res = await api(`/api/events?kind=${encodeURIComponent(kind)}&offset=${encodeURIComponent(nextOffset)}`); } catch (e) { if (myGen === loadGen && alive()) mount(list, h("div", { class: "notice bad" }, e.message)); return; }
        if (myGen !== loadGen || !alive()) return;
        offset = nextOffset;
        rows = more ? rows.concat(res.events) : res.events;
        paintChips(res.kinds || []);
        mount(list, h("div", { class: "card tight timeline" }, ...rows.map(e => h("div", { class: "tl" }, h("span", { class: "ts", title: e.at }, (e.at || "").slice(5, 16)), h("span", { class: "badge " + kindCls(e.kind) }, e.kind),
          h("span", { class: "what" }, e.artist ? h("a", { class: "b", href: e.kind === "removed" || e.kind === "blocked" || e.kind === "unblocked" || e.kind === "restored" ? link("library", {}, "artist", e.artist) : link("queue", { q: e.artist, status: "wanted,grabbed,exhausted,done,skipped" }) }, `${e.artist}${e.album ? " — " + e.album : ""}`) : null, e.detail ? h("div", { class: "d" }, e.detail) : null)))),
          res.events.length >= 100 ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, "Load more") : null);
      }
      setPoll(poll(() => offset === 0 ? load() : null, 10000));
    }

    async function audit({ out, alive }) {
      let user = "", q = "", offset = 0, rows = [], total = 0, loadGen = 0;
      const userSel = h("select", { class: "select sm", onChange: (e) => { user = e.target.value; load(); } }, h("option", { value: "" }, "Everyone"));
      const input = debouncedInput({ placeholder: "filter action / target / detail", style: { maxWidth: "320px" }, onInput: (v) => { q = v; load(); } });
      const list = h("div", {});
      mount(out, h("div", { class: "filterbar" }, input, userSel), list);
      let usersPainted = false;
      async function load(more = false) {
        const myGen = ++loadGen;
        const nextOffset = more ? offset + 100 : 0;
        let res;
        try { res = await api(`/api/audit?user=${encodeURIComponent(user)}&q=${encodeURIComponent(q)}&offset=${encodeURIComponent(nextOffset)}`); } catch (e) { if (myGen === loadGen && alive()) mount(list, h("div", { class: "notice bad" }, e.message)); return; }
        if (myGen !== loadGen || !alive()) return;
        offset = nextOffset;
        rows = more ? rows.concat(res.rows) : res.rows; total = res.total;
        if (!usersPainted) { usersPainted = true; mount(userSel, h("option", { value: "" }, "Everyone"), ...res.users.map(u => h("option", { value: u }, u))); }
        mount(list, h("div", { class: "muted small mb" }, `${fmt.num(rows.length)} of ${fmt.num(total)}`),
          h("div", { class: "table-wrap audit" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "User"), h("th", {}, "Action"), h("th", {}, "Target"), h("th", {}, "Detail"), h("th", {}, "From"))),
            h("tbody", {}, ...rows.map(a => h("tr", { class: a.ok ? "" : "ok0" }, h("td", { class: "muted small nowrap" }, (a.at || "").slice(5, 16)), h("td", {}, h("span", { class: "b" }, a.user), a.role ? h("span", { class: "badge dim", style: { marginLeft: "4px" } }, a.role) : null), h("td", { class: "mono small" }, a.action.replace(/^POST /, ""), !a.ok ? h("span", { class: "badge bad", style: { marginLeft: "4px" } }, "denied") : null), h("td", { class: "small" }, a.target), h("td", { class: "mono xs muted", style: { overflowWrap: "anywhere", maxWidth: "420px" } }, a.detail), h("td", { class: "mono xs muted" }, a.ip)))))),
          rows.length < total ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, "Load more") : null);
      }
      await load();
    }
    tabCtl.go();
  },
};
