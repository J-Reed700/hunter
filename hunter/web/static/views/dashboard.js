import { api, h, mount, fmt, toast, action, poll, link, cover, artUrl, setTopActions, refreshStatusSoon, can } from "../app.js";
import { openWant, openJob, statusBadge } from "./_shared.js";

export default {
  async render(root) {
    const op = can("operator");
    if (op) setTopActions(h("button", { class: "btn sm primary", onClick: action(null, async () => { const r = await api("/api/pass", {}); toast(r.detail); refreshStatusSoon(); }) }, "Run pass"));
    const stats = h("div", { class: "stats mb" });
    const left = h("div", { class: "stack" }), right = h("div", { class: "stack" });
    mount(root, stats, h("div", { class: "spread" }, left, right));

    const paint = async () => {
      const [p, t, svc] = await Promise.all([
        api("/api/pulse"),
        api("/api/transfers").catch(() => null),
        op ? api("/api/services").catch(() => null) : Promise.resolve(null),
      ]);
      if (!root.isConnected) return;
      const c = p.counts || {};
      const stat = (k, v, d, href, cls = "") => h("a", { class: "stat " + cls, href }, h("div", { class: "k" }, k), h("div", { class: "v" }, v), d ? h("div", { class: "d" }, d) : null);
      mount(stats,
        stat("Wanted", fmt.num(c.wanted || 0), "hunting on next pass", "#/queue?status=wanted"),
        stat("Downloading", fmt.num(c.grabbed || 0), t?.slskd_ok ? `${t.queue_active} files active in slskd` : "slskd unreachable", "#/queue?status=grabbed"),
        stat("Done", fmt.num(c.done || 0), "landed in beets", "#/queue?status=done"),
        stat("Exhausted", fmt.num(c.exhausted || 0), "need a manual pick", "#/queue?status=exhausted", c.exhausted ? "warn" : ""),
        p.library ? stat("Library", fmt.num(p.library.items), `${fmt.num(p.library.albums)} albums · +${p.library.added_24h} today · +${p.library.added_7d} this week`, "#/library") : null,
        stat("Jobs", String(p.jobs.filter(j => j.status === "running").length), p.jobs.length ? `last: ${p.jobs[0].title.slice(0, 28)}` : "none yet", op ? "#/jobs" : "#/dashboard"));

      // in-flight downloads
      const inflight = (t?.grabbed || []).slice(0, 8);
      const dlCard = h("div", { class: "card" }, h("h3", {}, "Downloading", h("a", { class: "right", href: "#/queue?status=grabbed" }, "all →")),
        inflight.length ? h("div", { class: "list" }, ...inflight.map(w => { const dl = w.dl; const pct = dl ? dl.pct : 0; return h("div", { class: "item click", onClick: () => openWant(w.id) },
          h("div", { class: "main" }, h("div", { class: "t" }, `${w.artist} — ${w.album}`), h("div", { class: "s" }, dl ? `${dl.succeeded}/${dl.total} files · ${pct}%${dl.errored ? ` · ${dl.errored} failed` : ""}` : "waiting for slskd…", h("span", { class: "mono" }, w.grabbed_username || "")), h("div", { class: "progress mt", style: { marginTop: "6px" } }, h("div", { class: dl?.errored ? "" : "", style: { width: pct + "%" } }))),
          h("div", { class: "end" }, dl?.errored ? h("span", { class: "badge bad" }, "errors") : null)); })) : h("div", { class: "empty small" }, "Nothing in flight."));

      // passes
      const passes = p.passes || [];
      const passCard = h("div", { class: "card" }, h("h3", {}, "Recent passes", h("span", { class: "right" }, `every ${Math.round(p.interval / 60)} min`)),
        passes.length ? h("div", { class: "table-wrap", style: { border: 0 } }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Mode"), h("th", { class: "num" }, "Dur"), h("th", { class: "num" }, "Grabbed"), h("th", { class: "num" }, "Done"), h("th", { class: "num" }, "Miss"), h("th", { class: "num" }, "Sync"))),
          h("tbody", {}, ...passes.map(r => { const s = r.stats || {}; return h("tr", { class: !r.ok ? "bad" : "" }, h("td", { class: "muted" }, fmt.datetime(r.started_ts)), h("td", {}, h("span", { class: "badge " + (r.apply ? "ok" : "warn") }, r.apply ? "live" : "shadow"), !r.ok ? h("span", { class: "badge bad" }, "crashed") : null), h("td", { class: "num" }, fmt.dur(r.duration)), h("td", { class: "num" }, s.hunt?.grabbed ?? "—"), h("td", { class: "num" }, s.reconcile?.done ?? "—"), h("td", { class: "num" }, s.hunt?.no_candidate ?? "—"), h("td", { class: "num " + (s.sync?.status === "error" ? "bad" : "") }, s.sync?.status || "—")); })))) : h("div", { class: "empty small" }, "No passes yet."));

      // recent done
      const doneCard = h("div", { class: "card" }, h("h3", {}, "Recently landed", h("a", { class: "right", href: "#/queue?status=done" }, "all →")),
        p.recent_done.length ? h("div", { class: "list" }, ...p.recent_done.map(w => {
          const rg = w.spotify_album_id?.startsWith?.("request:rg:") || w.spotify_album_id?.startsWith?.("release:rg:") ? w.spotify_album_id.slice(11) : "";
          return h("div", { class: "item click", onClick: () => openWant(w.id) }, cover(rg ? artUrl(rg) : "", { size: "" }), h("div", { class: "main" }, h("div", { class: "t" }, `${w.artist} — ${w.album}`), h("div", { class: "s" }, w.source, " · ", fmt.agoStr(w.upd))), h("div", { class: "end" }, h("span", { class: "badge ok" }, "done")));
        })) : h("div", { class: "empty small" }, "Nothing yet."));

      // activity
      const actCard = h("div", { class: "card" }, h("h3", {}, "Activity", h("a", { class: "right", href: "#/activity" }, "feed →")),
        h("div", { class: "timeline" }, ...(p.events || []).map(e => h("div", { class: "tl" }, h("span", { class: "ts" }, (e.at || "").slice(11, 16)), h("span", { class: "badge " + kindCls(e.kind) }, e.kind), h("span", { class: "what" }, e.artist ? h("b", {}, `${e.artist}${e.album ? " — " + e.album : ""}`) : null, e.detail ? h("div", { class: "d" }, e.detail) : null)))));

      // jobs + services
      const jobsCard = h("div", { class: "card" }, h("h3", {}, "Jobs", op ? h("a", { class: "right", href: "#/jobs" }, "all →") : null),
        p.jobs.length ? h("div", { class: "list" }, ...p.jobs.map(j => h("div", { class: op ? "item click" : "item", onClick: op ? () => openJob(j.id) : null }, h("div", { class: "main" }, h("div", { class: "t small" }, j.title), h("div", { class: "s" }, fmt.ago(j.started_ts), " · ", fmt.dur(j.duration), " · ", j.mode)), h("div", { class: "end" }, statusBadge(j.status))))) : h("div", { class: "empty small" }, "No jobs yet — try Beets → Scripts."));
      const svcCard = !op ? null : (svc?.available ? h("div", { class: "card" }, h("h3", {}, "Services"), ...(svc.services?.length ? svc.services.map(s => h("div", { class: "svc" }, h("span", { class: "dot " + (!s.exists ? "" : s.running ? (s.health === "unhealthy" ? "warn" : "ok") : "bad") }), h("span", { class: "nm" }, s.name), h("span", { class: "st" }, s.exists ? (s.health || s.status) : "absent"))) : [h("div", { class: "hint" }, "No monitored containers found (set HUNTER_SERVICES to customize).")])) : h("div", { class: "card warn" }, h("h3", {}, "Console offline"), h("div", { class: "hint" }, "The docker socket isn't mounted, so beets jobs and container health are unavailable. Mount /var/run/docker.sock (read-only) to enable container health and beets jobs.")));

      // sources breakdown
      const src = p.sources || {};
      const srcCard = h("div", { class: "card" }, h("h3", {}, "Programs", op ? h("a", { class: "right", href: "#/settings?tab=automation" }, "manage →") : null),
        h("div", { class: "bars" }, ...Object.entries(src).sort((a, b) => sum(b[1]) - sum(a[1])).map(([name, st]) => { const open = (st.wanted || 0) + (st.grabbed || 0); const tot = sum(st); return h("div", { class: "bar" }, h("a", { class: "lbl", href: link("queue", { source: name }) }, name), h("div", { class: "progress info" }, h("div", { style: { width: (tot ? 100 * (st.done || 0) / tot : 0) + "%" } })), h("span", { class: "n", title: `${open} open · ${st.done || 0} done · ${st.skipped || 0} parked` }, `${open}/${tot}`)); })));

      mount(left, dlCard, passCard, doneCard);
      mount(right, jobsCard, srcCard, actCard, svcCard);
    };
    poll(paint, 15000);
  },
};
const sum = (o) => Object.values(o || {}).reduce((a, b) => a + b, 0);
export function kindCls(k) { return ({ grab: "warn", done: "ok", exhausted: "bad", revert: "warn", request: "info", follow: "info", automation: "dim", quarantined: "bad", removed: "bad", blocked: "bad", unblocked: "ok", restored: "ok" })[k] || "dim"; }
