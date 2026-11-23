import { api, h, mount, fmt, poll, setQuery, busy, link, isAdmin } from "../app.js";
import { kindCls } from "./dashboard.js";

export default {
  async render(root, r) {
    const defs = [["events", "Events"], ...(isAdmin() ? [["audit", "Audit trail"]] : [])];
    let tab = defs.some(([k]) => k === r.q.tab) ? r.q.tab : "events";
    const tabs = h("div", { class: "tabs" });
    const out = h("div", {});
    let stopSubPoll = null, tabGen = 0;
    const go = () => {
      if (stopSubPoll) { stopSubPoll(); stopSubPoll = null; }
      const myGen = ++tabGen;
      return ({ events, audit })[tab](myGen);
    };
    const paintTabs = () => mount(tabs, ...defs.map(([k, l]) => h("button", { class: k === tab ? "active" : "", onClick: () => { tab = k; setQuery({ tab }); paintTabs(); go(); } }, l)));
    mount(root, h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Activity"), h("div", { class: "sub" }, "Events: what the pipeline did (grabs, landings, removals, blocks). Audit: who did what in this console, from where."))), tabs, out);
    paintTabs();

    async function events(myGen) {
      let kind = r.q.kind || "", offset = 0, rows = [];
      const chips = h("div", { class: "chips" });
      const list = h("div", {});
      mount(out, h("div", { class: "filterbar" }, chips), list);
      const paintChips = (kinds) => mount(chips, ...[["", "All"], ...kinds.map(k => [k, k])].map(([k, l]) => h("span", { class: "chip" + (kind === k ? " active" : ""), onClick: () => { kind = k; setQuery({ kind }); load(); } }, l)));
      async function load(more = false) {
        offset = more ? offset + 100 : 0;
        const res = await api(`/api/events?kind=${encodeURIComponent(kind)}&offset=${encodeURIComponent(offset)}`);
        if (myGen !== tabGen || !out.isConnected) return;
        rows = more ? rows.concat(res.events) : res.events;
        paintChips(res.kinds || []);
        mount(list, h("div", { class: "card tight timeline" }, ...rows.map(e => h("div", { class: "tl" }, h("span", { class: "ts", title: e.at }, (e.at || "").slice(5, 16)), h("span", { class: "badge " + kindCls(e.kind) }, e.kind),
          h("span", { class: "what" }, e.artist ? h("a", { class: "b", href: e.kind === "removed" || e.kind === "blocked" || e.kind === "unblocked" || e.kind === "restored" ? link("library", {}, "artist", e.artist) : link("queue", { q: e.artist, status: "wanted,grabbed,exhausted,done,skipped" }) }, `${e.artist}${e.album ? " — " + e.album : ""}`) : null, e.detail ? h("div", { class: "d" }, e.detail) : null)))),
          res.events.length >= 100 ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, "Load more") : null);
      }
      stopSubPoll = poll(() => offset === 0 ? load() : null, 10000);
    }

    async function audit(myGen) {
      let user = "", q = "", offset = 0, rows = [], total = 0, timer;
      const userSel = h("select", { class: "select sm", onChange: (e) => { user = e.target.value; load(); } }, h("option", { value: "" }, "Everyone"));
      const input = h("input", { class: "input", placeholder: "filter action / target / detail", style: { maxWidth: "320px" } });
      input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { q = input.value.trim(); load(); }, 300); });
      const list = h("div", {});
      mount(out, h("div", { class: "filterbar" }, input, userSel), list);
      let usersPainted = false;
      async function load(more = false) {
        offset = more ? offset + 100 : 0;
        const res = await api(`/api/audit?user=${encodeURIComponent(user)}&q=${encodeURIComponent(q)}&offset=${encodeURIComponent(offset)}`);
        if (myGen !== tabGen || !out.isConnected) return;
        rows = more ? rows.concat(res.rows) : res.rows; total = res.total;
        if (!usersPainted) { usersPainted = true; mount(userSel, h("option", { value: "" }, "Everyone"), ...res.users.map(u => h("option", { value: u }, u))); }
        mount(list, h("div", { class: "muted small mb" }, `${fmt.num(rows.length)} of ${fmt.num(total)}`),
          h("div", { class: "table-wrap audit" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "User"), h("th", {}, "Action"), h("th", {}, "Target"), h("th", {}, "Detail"), h("th", {}, "From"))),
            h("tbody", {}, ...rows.map(a => h("tr", { class: a.ok ? "" : "ok0" }, h("td", { class: "muted small nowrap" }, (a.at || "").slice(5, 16)), h("td", {}, h("span", { class: "b" }, a.user), a.role ? h("span", { class: "badge dim", style: { marginLeft: "4px" } }, a.role) : null), h("td", { class: "mono small" }, a.action.replace(/^POST /, ""), !a.ok ? h("span", { class: "badge bad", style: { marginLeft: "4px" } }, "denied") : null), h("td", { class: "small" }, a.target), h("td", { class: "mono xs muted", style: { overflowWrap: "anywhere", maxWidth: "420px" } }, a.detail), h("td", { class: "mono xs muted" }, a.ip)))))),
          rows.length < total ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, "Load more") : null);
      }
      await load();
    }
    go();
  },
};
