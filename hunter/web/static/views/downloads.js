/* Downloads — bundles built from selections (tracks / albums / artists),
   progress while they build, Save when ready, expiry + disk budget. */
import { api, h, mount, fmt, toast, action, confirm, poll, setTopActions, session } from "../app.js";

export default {
  async render(root) {
    const list = h("div", {});
    const head = h("div", { class: "row between mb" });
    setTopActions(h("a", { class: "btn sm", href: "#/library?tab=albums" }, "Library"));
    mount(root, h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Downloads"), h("div", { class: "sub" }, "Zip bundles of library files. Select tracks, albums or artists anywhere in Library and choose Download; single tracks download directly from their drawer. Bundles are resumable and expire automatically."))), head, list);
    const paint = (res) => {
      const s = res.summary || {};
      mount(head, h("span", { class: "muted small" }, `${fmt.num(res.bundles.length)} bundles · ${fmt.bytes(s.used_bytes)} of ${fmt.bytes(s.keep_bytes)} on disk · expire after ${s.ttl_hours} h · max ${fmt.bytes(s.max_bundle_bytes)} per bundle`),
        s.building ? h("span", { class: "badge acc" }, h("span", { class: "dot ok pulse" }), ` ${s.building} building`) : null);
      if (!res.bundles.length) { mount(list, h("div", { class: "empty" }, h("div", { class: "big" }, "⤓"), "No bundles yet. In Library, select albums or tracks and choose Download, or use “Download all” on an artist.")); return; }
      mount(list, h("div", { class: "list card" }, ...res.bundles.map(b => {
        const pct = b.bytes_total ? Math.min(100, Math.round(100 * (b.bytes_done || 0) / b.bytes_total)) : 0;
        const mine = session.me && (session.me.role === "admin" || session.me.username === b.requested_by);
        const state = b.status === "ready" ? "ok" : b.status === "building" || b.status === "queued" ? "running" : b.status === "cancelled" ? "dim" : "failed";
        return h("div", { class: "item", style: { alignItems: "flex-start" } },
          h("div", { class: "main" },
            h("div", { class: "t" }, b.title, " ", h("span", { class: "badge " + state }, b.status)),
            h("div", { class: "s" }, `${fmt.num(b.files)} files · ${fmt.bytes(b.bytes_total)}`, b.missing ? ` · ${b.missing} missing on disk` : "", ` · by ${b.requested_by}`, ` · ${fmt.ago(b.created_ts)}`, b.served ? ` · saved ${b.served}×` : ""),
            b.status === "building" || b.status === "queued" ? h("div", { class: "mt", style: { marginTop: "6px" } }, h("div", { class: "progress lg striped dl-bar" }, h("div", { style: { width: pct + "%" } })), h("div", { class: "xs muted", style: { marginTop: "3px" } }, b.status === "queued" ? "queued — waiting for the builder" : `${pct}% · ${fmt.bytes(b.bytes_done)} · ${fmt.num(b.files_done)}/${fmt.num(b.files)} files`)) : null,
            b.status === "ready" ? h("div", { class: "xs muted" }, `expires ${fmt.datetime(b.expires_ts)}`) : null,
            b.error ? h("div", { class: "xs bad" }, b.error) : null),
          h("div", { class: "end", style: { flexWrap: "wrap" } },
            b.status === "ready" ? h("a", { class: "btn sm primary", href: `/api/downloads/file?id=${encodeURIComponent(b.id)}`, download: "" }, "Save zip") : null,
            (b.status === "building" || b.status === "queued") && mine ? h("button", { class: "btn sm danger", onClick: action(null, async () => { await api("/api/downloads/cancel", { id: b.id }); toast("Cancelled"); load(); }) }, "Cancel") : null,
            mine && b.status !== "building" ? h("button", { class: "btn sm ghost", onClick: action(null, async () => { if (b.status === "ready" && !(await confirm({ title: "Delete this bundle?", body: "The zip is removed from the server; the library files are untouched.", ok: "Delete", danger: true }))) return; await api("/api/downloads/delete", { id: b.id }); load(); }) }, "Delete") : null));
      })));
    };
    let building = false, ticks = 0;
    async function load() {
      const res = await api("/api/downloads");
      if (!root.isConnected) return;
      building = !!(res.summary && res.summary.building);
      paint(res);
    }
    await load();
    if (!root.isConnected) return;
    poll(async () => {
      ticks += 1;
      if (building || ticks % 6 === 0) await load();
    }, 2500, { immediate: false });
  },
};
