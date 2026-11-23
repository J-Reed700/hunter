import { api, h, mount, fmt, toast, action, poll, setQuery, setTopActions } from "../app.js";
import { openJob, statusBadge, modeBadge } from "./_shared.js";

export default {
  async render(root, r) {
    if (r.q.id) { openJob(r.q.id); setQuery({ id: "" }); }
    let kind = r.q.kind || "", status = r.q.status || "";
    const list = h("div", {});
    const chips = (items, cur, set) => h("div", { class: "chips" }, ...items.map(([k, l]) => h("span", { class: "chip" + (cur === k ? " active" : ""), onClick: () => set(k) }, l)));
    const bar = h("div", { class: "filterbar" });
    setTopActions(h("a", { class: "btn sm", href: "#/beets?tab=scripts" }, "Scripts"), h("a", { class: "btn sm", href: "#/beets?tab=console" }, "Console"));
    mount(root, h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Jobs"), h("div", { class: "sub" }, "Every process the UI started: beet commands, automation scripts, hunter passes. Live logs, exit codes, cancel."))), bar, list);
    const paintBar = () => mount(bar, chips([["", "All kinds"], ["beet", "beet"], ["script", "scripts"], ["library", "library"], ["hunter", "hunter"]], kind, (k) => { kind = k; setQuery({ kind }); paintBar(); load(); }), chips([["", "Any status"], ["running", "Running"], ["ok", "OK"], ["failed", "Failed"], ["killed", "Killed"], ["lost", "Lost"]], status, (k) => { status = k; setQuery({ status }); paintBar(); load(); }),
      h("button", { class: "btn xs ghost ml-auto", onClick: action(null, async () => { const r = await api("/api/jobs/prune_logs", {}); toast(`${r.pruned} old logs removed`); }) }, "Prune logs >14d"));
    paintBar();
    async function load() {
      const res = await api(`/api/jobs?kind=${encodeURIComponent(kind)}&status=${encodeURIComponent(status)}&limit=120`);
      if (!root.isConnected) return;
      const running = res.jobs.filter(j => j.status === "running"), done = res.jobs.filter(j => j.status !== "running");
      const row = (j) => h("tr", { class: "click", onClick: () => openJob(j.id) },
        h("td", {}, statusBadge(j.status)), h("td", {}, h("div", { class: "b ellip", style: { maxWidth: "520px" } }, j.title), j.status === "running" && j.progress ? h("div", { class: "row small muted", style: { gap: "6px" } }, h("div", { class: "progress striped", style: { width: "120px" } }, h("div", { style: { width: (j.progress.pct ?? 100) + "%", opacity: j.progress.pct === null ? ".35" : "1" } })), j.progress.pct !== null ? `${j.progress.pct}%` : "", h("span", { class: "ellip xs", style: { maxWidth: "360px" } }, j.progress.last || "")) : h("div", { class: "muted xs ellip", style: { maxWidth: "520px" } }, j.command)),
        h("td", {}, h("span", { class: "badge dim" }, j.kind)), h("td", {}, modeBadge(j.mode)), h("td", { class: "muted small nowrap" }, fmt.datetime(j.started_ts)), h("td", { class: "num" }, fmt.dur(j.duration)), h("td", { class: "num" }, j.exit_code ?? ""),
        h("td", {}, h("div", { class: "actions" }, j.status === "running" ? h("button", { class: "btn xs danger", onClick: action(null, async (e) => { e.stopPropagation(); await api("/api/jobs/cancel", { id: j.id }); toast("Cancel sent"); }) }, "Cancel") : null)));
      const table = (rows) => h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "Status"), h("th", {}, "Job"), h("th", {}, "Kind"), h("th", {}, "Mode"), h("th", {}, "Started"), h("th", { class: "num" }, "Duration"), h("th", { class: "num" }, "Exit"), h("th", {}))), h("tbody", {}, ...rows.map(row))));
      mount(list, h("div", { class: "section-title" }, `Running (${running.length})`), running.length ? table(running) : h("div", { class: "hint mb" }, "Nothing running."),
        h("div", { class: "section-title" }, `History (${done.length})`), done.length ? table(done) : h("div", { class: "empty" }, h("div", { class: "big" }, "▶"), "No jobs yet. Start one from Beets → Scripts, the Console, or any album page."));
    }
    poll(load, 4000);
  },
};
