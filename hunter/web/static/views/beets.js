import { api, h, mount, fmt, toast, errToast, action, setQuery, poll, confirm, link, qs, setTopActions, store, modal, ls } from "../app.js";
import { openJob, statusBadge, modeBadge, logView, setLog, field } from "./_shared.js";

export default {
  async render(root, r) {
    const defs = [["pipeline", "Pipeline"], ["console", "Console"], ["scripts", "Scripts"], ["quarantine", "Quarantine"], ["logs", "Logs"]];
    let tab = defs.some(([k]) => k === r.q.tab) ? r.q.tab : "pipeline";
    const tabs = h("div", { class: "tabs" });
    const out = h("div", {});
    let stopSubPoll = null, tabGen = 0;
    const go = () => {
      if (stopSubPoll) { stopSubPoll(); stopSubPoll = null; }
      const myGen = ++tabGen;
      return ({ pipeline, console: consoleView, scripts, quarantine, logs })[tab](myGen);
    };
    const paintTabs = () => mount(tabs, ...defs.map(([k, l]) => h("button", { class: k === tab ? "active" : "", onClick: () => { tab = k; setQuery({ tab }); paintTabs(); go(); } }, l)));
    setTopActions(h("a", { class: "btn sm", href: "#/jobs" }, "Jobs"));
    const offlineBanner = !store.status?.console ? h("div", { class: "notice bad mb" }, h("div", { class: "body" }, h("b", {}, "Console offline. "), "The docker socket isn't mounted in this container. Mount /var/run/docker.sock (read-only) with Docker group access to enable the beets console.")) : null;
    mount(root, h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Beets"), h("div", { class: "sub" }, "The import pipeline and the beets CLI, driven from here. Every command runs inside the beets container with a live log; mutations dry-run first."))), offlineBanner, tabs, out);
    paintTabs();

    // ---------------- pipeline ----------------
    async function pipeline(myGen) {
      const box = h("div", {});
      mount(out, box);
      const paint = async () => {
        const p = await api("/api/beets/pipeline");
        if (myGen !== tabGen || !out.isConnected) return;
        const w = p.watcher;
        const m = p.maintenance || {};
        const qt = p.quarantine || {};
        const running = m.lock_mtime && Date.now() / 1000 - m.lock_mtime < 3600;
        const watcherCard = h("div", { class: "card" + (w && !w.alive ? " bad" : "") }, h("h3", {}, h("span", { class: "dot " + (w?.alive ? "ok" : "bad") }), "Import watcher", h("span", { class: "right" }, w ? `log ${w.log_age_s !== null ? fmt.dur(w.log_age_s) + " ago" : "?"}` : "")),
          w ? h("div", { class: "stack" },
            h("div", { class: "row small" }, h("span", { class: "badge " + (w.alive ? "ok" : "bad") }, w.alive ? "alive" : "DOWN"), h("span", { class: "muted" }, `pids ${w.pids || "—"}`), h("span", { class: "muted" }, `last 500 lines: ${w.counts.queued} queued · ${w.counts.imported} imported · ${w.counts.quarantined} quarantined`)),
            h("div", { class: "row" }, h("button", { class: "btn sm", onClick: action(null, async () => { if (await confirm({ title: "Bounce the watcher?", body: "pkill import_watcher — s6 respawns it in seconds. In-flight imports finish on the next pass.", ok: "Bounce" })) { const r = await api("/api/beets/watcher/restart", {}); toast(r.ok ? "Bounced" : r.error, r.ok ? "ok" : "bad"); } }) }, "Bounce watcher"), h("button", { class: "btn sm ghost", onClick: () => { tab = "logs"; setQuery({ tab, log: "beets-watcher" }); paintTabs(); go(); } }, "Watcher log"), h("button", { class: "btn sm ghost", onClick: showConfig }, "Config")),
            h("div", { class: "timeline", style: { maxHeight: "300px", overflow: "auto" } }, ...(w.events || []).slice(0, 30).map(e => h("div", { class: "tl" }, h("span", { class: "ts" }, e.ts.slice(11)), h("span", { class: "badge " + ({ imported: "ok", quarantined: "bad", importing: "warn", queued: "info" }[e.kind] || (e.level === "ERROR" ? "bad" : "dim")) }, e.kind === "info" ? e.level.toLowerCase() : e.kind), h("span", { class: "what xs mono" }, e.msg)))))
          : h("div", { class: "hint" }, "console offline"));
        const srcCard = h("div", { class: "card" }, h("h3", {}, "Download sources", h("span", { class: "right" }, "what's waiting for import")),
          h("div", { class: "stack" }, ...p.sources.map(s => h("details", { class: "acc" }, h("summary", {}, h("span", { class: "dot " + (s.ok ? (s.total || s.files ? "warn" : "ok") : "bad") }), s.name, h("span", { class: "badge dim" }, s.mode), h("span", { class: "muted small ml-auto" }, s.ok ? (s.mode === "flat" ? `${s.files} audio files` : `${s.total} dirs`) : s.error)),
            h("div", { class: "acc-body" }, h("div", { class: "mono xs muted mb" }, s.path), s.dirs?.length ? h("div", { class: "list" }, ...s.dirs.map(d => h("div", { class: "item" }, h("div", { class: "main" }, h("div", { class: "t small" }, d.name), h("div", { class: "s" }, fmt.ago(d.mtime))), h("div", { class: "end" }, h("button", { class: "btn xs", onClick: () => showSourceDir(s.name, d.name) }, "Files"), h("button", { class: "btn xs primary", onClick: action(null, async () => { const r = await api("/api/beets/import", { source: s.name, dir: d.name }); if (r.ok) { toast("Import started", "ok"); openJob(r.job.id); } else toast(r.error, "bad"); }) }, "Import"), h("button", { class: "btn xs", title: "smart-merge into an existing partial album (dry-run)", onClick: action(null, async () => { const r = await api("/api/beets/smart_merge", { source: s.name, dir: d.name, apply: false }); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); }) }, "Merge…"))))) : h("div", { class: "hint" }, s.mode === "flat" ? "flat source — the whole tree is one import unit; the watcher handles it" : "empty"))))));
        const maintCard = h("div", { class: "card" }, h("h3", {}, h("span", { class: "dot " + (running ? "warn pulse" : m.ok ? "ok" : "bad") }), "Maintenance", h("span", { class: "right" }, m.log_mtime ? fmt.ago(m.log_mtime) : "")),
          m.ok ? h("dl", { class: "kv small mb" }, ...Object.entries(m).filter(([k]) => !["ok", "lock_mtime", "log", "log_mtime"].includes(k)).slice(0, 12).map(([k, v]) => [h("dt", {}, k), h("dd", { class: "mono" }, typeof v === "object" ? JSON.stringify(v) : String(v))])) : h("div", { class: "hint mb" }, "no maintenance_last.json yet"),
          h("div", { class: "row" }, h("button", { class: "btn sm primary", onClick: action(null, async () => { if (await confirm({ title: "Run nightly maintenance now?", body: "Backup → duplicates report → bad → missing → orphan prune → move → write → rescans. Hours long, flock-guarded.", ok: "Run" })) { const r = await api("/api/beets/maintenance", {}); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); } }) }, running ? "Running…" : "Run now"),
            h("button", { class: "btn sm", onClick: action(null, async (e) => { const r = await api("/api/beets/rescan", { app: "all" }); toast(r.ok ? "Rescans triggered" : r.error, r.ok ? "ok" : "bad"); }) }, "Sync media servers"),
            m.log ? h("button", { class: "btn sm ghost", onClick: () => { tab = "logs"; setQuery({ tab, log: "beets-maintenance" }); paintTabs(); go(); } }, "Log") : null),
          h("div", { class: "section-title" }, "Reports"), p.reports.length ? h("div", { class: "chips" }, ...p.reports.slice(0, 12).map(rep => h("span", { class: "chip", onClick: () => showReport(rep.name) }, rep.name.replace(/_\d{8}_\d{6}/, ""), h("span", { class: "n" }, fmt.bytes(rep.size))))) : h("div", { class: "hint" }, "none"),
          ...(p.schedule && p.schedule.length > 0 ? [h("div", { class: "section-title" }, "Scheduled jobs"), h("table", { class: "mini-tbl" }, ...p.schedule.map(s => h("tr", {}, h("td", {}, s.time), h("td", {}, h("b", {}, s.job), h("div", { class: "muted xs" }, s.what)))))] : []));
        const qCard = h("div", { class: "card" }, h("h3", {}, "Quarantine", h("a", { class: "right", href: "#/beets?tab=quarantine" }, `${fmt.num(qt.total || 0)} entries →`)),
          qt.ok ? h("div", { class: "bars" }, ...Object.entries(qt.by_reason || {}).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([k, v]) => h("div", { class: "bar" }, h("span", { class: "lbl", title: k }, k), h("div", { class: "progress warn" }, h("div", { style: { width: (100 * v / Math.max(1, qt.total)) + "%" } })), h("span", { class: "n" }, fmt.num(v))))) : h("div", { class: "hint" }, qt.error));
        const jobsCard = h("div", { class: "card" }, h("h3", {}, "Recent jobs", h("a", { class: "right", href: "#/jobs" }, "all →")), p.jobs.length ? h("div", { class: "list" }, ...p.jobs.map(j => h("div", { class: "item click", onClick: () => openJob(j.id) }, h("div", { class: "main" }, h("div", { class: "t small" }, j.title), h("div", { class: "s" }, fmt.ago(j.started_ts), " · ", fmt.dur(j.duration))), h("div", { class: "end" }, modeBadge(j.mode), statusBadge(j.status))))) : h("div", { class: "hint" }, "none yet"));
        mount(box, h("div", { class: "spread" }, h("div", { class: "stack", style: { gap: "12px" } }, watcherCard, srcCard), h("div", { class: "stack", style: { gap: "12px" } }, maintCard, qCard, jobsCard)));
      };
      stopSubPoll = poll(paint, 15000);
    }
    async function showConfig() {
      const c = await api("/api/beets/config").catch(errToast); if (!c) return;
      modal({ title: "beets container environment", wide: true, body: h("table", { class: "mini-tbl" }, ...c.rows.map(r => h("tr", {}, h("td", {}, r.key), h("td", { class: "mono xs", style: { overflowWrap: "anywhere" } }, r.value)))) });
    }
    async function showSourceDir(source, dir) {
      const d = await api(`/api/beets/source_dir${qs({ source, dir })}`).catch(errToast); if (!d) return;
      modal({ title: dir, wide: true, body: h("div", {}, h("div", { class: "mono xs muted mb" }, d.path, ` · ${d.audio} audio / ${d.files.length} files`), h("table", { class: "mini-tbl" }, ...d.files.map(f => h("tr", {}, h("td", {}, fmt.bytes(f.size)), h("td", { class: f.audio ? "" : "muted" }, f.name))))) });
    }
    async function showReport(name) {
      const r = await api(`/api/beets/report?name=${encodeURIComponent(name)}`).catch(errToast); if (!r) return;
      modal({ title: name, wide: true, body: logView(r.text, { tall: true }) });
    }

    // ---------------- console ----------------
    async function consoleView(myGen) {
      const cat = await api("/api/beets/commands");
      if (myGen !== tabGen || !out.isConnected) return;
      const cmds = cat.commands;
      let cur = cmds.find(c => c.name === (r.q.cmd || "list")) || cmds[0];
      let lastPreview = null;
      const groups = [...new Set(cmds.map(c => c.group))];
      const picker = h("div", { class: "stack", style: { gap: "4px" } });
      // phones: the command list lives in a select above the form instead of a card below it
      const pickSel = h("select", { class: "select mob-only mb", "aria-label": "beet command", onChange: () => { cur = cmds.find(c => c.name === pickSel.value) || cur; lastPreview = null; setQuery({ cmd: cur.name }); paintPicker(); paintForm(); } },
        ...groups.map(g => h("optgroup", { label: g }, ...cmds.filter(c => c.group === g).map(c => h("option", { value: c.name }, c.name + (c.cls === "blocked" ? " (blocked)" : c.cls === "mutate" ? " ✎" : ""))))));
      const form = h("div", { class: "stack" });
      const preview = h("div", {});
      const rawInp = h("textarea", { class: "input mono", rows: 2, placeholder: "beet modify -a albumartist:Foo year=1999   (free-form; flags validated against the allowlist)" });
      const rawMode = h("input", { type: "checkbox" });
      const queryInp = h("input", { class: "input mono", placeholder: "beets query, e.g.  albumartist:Radiohead year:1997   or  album_id:123", value: r.q.query || "" });
      const posInp = h("input", { class: "input mono", placeholder: "" });
      rawInp.addEventListener("input", () => { lastPreview = null; });
      queryInp.addEventListener("input", () => { lastPreview = null; });
      posInp.addEventListener("input", () => { lastPreview = null; });
      let hist = []; try { hist = JSON.parse(ls.get("beet_hist", "[]")); } catch { hist = []; }
      const paintPicker = () => (pickSel.value = cur.name, mount(picker, ...groups.map(g => h("div", {}, h("div", { class: "nav-group", style: { padding: "8px 6px 2px" } }, g), ...cmds.filter(c => c.group === g).map(c => h("div", { class: "nav-item" + (c === cur ? " active" : "") + (c.cls === "blocked" ? " muted" : ""), style: { padding: "5px 8px", fontSize: "13px" }, onClick: () => { cur = c; lastPreview = null; setQuery({ cmd: c.name }); paintPicker(); paintForm(); } }, c.title, h("span", { class: "cnt " + (c.cls === "mutate" ? "warn" : "") }, c.cls)))))));
      const flagEls = {};
      const paintForm = () => {
        lastPreview = null;
        const isBlocked = cur.cls === "blocked";
        for (const k in flagEls) delete flagEls[k];
        posInp.placeholder = cur.positional || ""; posInp.hidden = !cur.positional;
        queryInp.hidden = cur.query === "none";
        mount(form,
          h("div", { class: "row" }, h("h3", { style: { fontSize: "16px" } }, "beet ", cur.name), h("span", { class: "badge " + (cur.cls === "mutate" ? "warn" : cur.cls === "blocked" ? "bad" : "ok") }, cur.cls), cur.aliases.length ? h("span", { class: "muted xs" }, "aliases: " + cur.aliases.join(", ")) : null),
          h("div", { class: "hint" }, cur.blurb),
          isBlocked ? null : h("label", { class: "row small" }, rawMode, " free-form command line"),
          isBlocked ? null : h("div", { id: "structured" }, field(cur.query === "required" ? "Query (required)" : cur.query === "none" ? "" : "Query (empty = whole library — needs confirmation)", queryInp), cur.positional ? field(cur.positional, posInp) : null,
            cur.flags.length ? h("div", { class: "card tight mt" }, ...cur.flags.map(f => { const cb = h("input", { type: "checkbox", onChange: () => { lastPreview = null; } }); const val = f.arg ? h("input", { type: "text", class: "input sm mono", placeholder: f.arg, onInput: () => { lastPreview = null; } }) : null; flagEls[f.flag] = { cb, val }; return h("label", { class: "cmd-flag" }, cb, h("code", {}, f.flag), h("span", {}, h("div", { class: "lbl" }, f.label), f.help ? h("div", { class: "hlp" }, f.help) : null), val); })) : null,
            Object.keys(cur.blocked).length ? h("div", { class: "hint mt" }, "blocked flags: ", ...Object.entries(cur.blocked).map(([k, v]) => h("span", { class: "badge bad", style: { marginRight: "4px" }, title: v }, k))) : null),
          isBlocked ? null : h("div", { id: "rawbox", hidden: true }, rawInp),
          isBlocked ? null : h("div", { class: "row mt" },
            cur.quick ? h("button", { class: "btn", onClick: action(null, () => run({ quick: true })) }, "Run (inline)") : null,
            h("button", { class: "btn", onClick: action(null, doPreview) }, cur.cls === "mutate" ? "Preview blast radius" : "Preview matches"),
            cur.cls === "mutate" ? (cur.can_dry_run !== false ? h("button", { class: "btn", onClick: action(null, () => run({ apply: false })) }, "Dry-run (job)") : null) : h("button", { class: "btn primary", onClick: action(null, () => run({ apply: false })) }, "Run as job"),
            cur.cls === "mutate" ? h("button", { class: "btn primary", onClick: action(null, () => run({ apply: true })) }, "Apply (job)") : null),
          h("div", { class: "hint mt" }, "Runs as ", h("code", { class: "mono xs" }, cat.wrapper), " — every HTTP call capped at 30 s. ", cur.confirm.length ? h("span", {}, h("code", {}, cur.confirm.join(" ")), " is added only on Apply; a dry-run prints beets' plan and aborts at the prompt.") : (cur.can_dry_run === false ? "No dry-run mode — preview matches first, then Apply." : null)));
        rawMode.onchange = () => { lastPreview = null; form.querySelector("#structured").hidden = rawMode.checked; form.querySelector("#rawbox").hidden = !rawMode.checked; if (rawMode.checked && !rawInp.value) rawInp.value = cur.name + " " + queryInp.value; };
      };
      const body = () => {
        if (rawMode.checked) return { raw: rawInp.value.trim() };
        const options = {}; for (const [flag, { cb, val }] of Object.entries(flagEls)) if (cb.checked) options[flag] = val ? val.value : true;
        return { command: cur.name, query: queryInp.value.trim(), options, positional: posInp.value.trim() ? posInp.value.trim().split(/\s+/) : [] };
      };
      async function doPreview() {
        mount(preview, h("div", { class: "skel" }, h("div")));
        try {
          const p = await api("/api/beets/preview", body()); lastPreview = p;
          mount(preview, h("div", { class: "card" }, h("h3", {}, "Preview", h("span", { class: "right mono" }, p.command)),
            p.error ? h("div", { class: "notice bad mb" }, h("div", { class: "body mono xs" }, p.error)) : null,
            p.count === null ? h("div", { class: "hint" }, "takes no query") : h("div", { class: "row mb" }, h("span", { class: "badge " + (p.count > cat.soft_max ? "warn" : "ok") }, `${fmt.num(p.count)} ${p.album ? "albums" : "tracks"} match`), p.needs_confirm ? h("span", { class: "badge warn" }, "apply needs confirmation") : null, p.too_many ? h("span", { class: "badge bad" }, "too many — narrow the query") : null),
            p.sample?.length ? h("table", { class: "mini-tbl" }, ...p.sample.map(s => h("tr", {}, h("td", {}, s[0]), h("td", {}, s.slice(1).join(" — "))))) : null));
        } catch (e) { lastPreview = null; mount(preview, h("div", { class: "notice bad" }, e.message)); }
      }
      async function run({ apply = false, quick = false } = {}) {
        const b = { ...body(), apply, quick };
        if (apply && cur.cls === "mutate") {
          if (!lastPreview) await doPreview();
          if (lastPreview?.needs_confirm) { if (!(await confirm({ title: `Apply to ${fmt.num(lastPreview.count)} rows?`, body: `This exceeds the soft cap (${cat.soft_max}) or has an empty query. Type the count to confirm.`, ok: "Apply", danger: true, typed: String(lastPreview.count) }))) return; b.confirm_count = lastPreview.count; }
          else if (!(await confirm({ title: "Apply?", body: lastPreview ? `${lastPreview.command} → ${lastPreview.count} rows` : b.raw || `beet ${cur.name}`, ok: "Apply" }))) return;
        }
        try {
          const res = await api("/api/beets/run", b);
          const quoteArg = (a) => (a === "" || /[\s"'\\]/.test(a)) ? JSON.stringify(a) : a;
          const cmdline = res.argv ? "beet " + res.argv.map(quoteArg).join(" ") : (res.job?.title || "");
          if (cmdline) { hist.unshift({ cmd: cmdline, ts: Date.now() / 1000 }); ls.set("beet_hist", JSON.stringify(hist.slice(0, 30))); paintHist(); }
          if (res.quick) { mount(preview, h("div", { class: "card" }, h("h3", {}, "Output", h("span", { class: "right" }, `exit ${res.exit_code}`)), logView(res.output || "(no output)", { tall: true }))); return; }
          if (res.ok) { toast("Job started", "ok"); openJob(res.job.id); } else toast(res.error, "bad");
        } catch (e) {
          if (e.status === 409 && e.data.needs_confirm) { if (await confirm({ title: `Touch ${e.data.count} rows?`, ok: "Apply", danger: true, typed: String(e.data.count) })) { const res = await api("/api/beets/run", { ...b, confirm_count: e.data.count }); if (res.ok) openJob(res.job.id); } }
          else errToast(e);
        }
      }
      const histBox = h("div", {});
      const paintHist = () => mount(histBox, hist.length ? h("div", { class: "card tight" }, h("h3", {}, "History"), ...hist.slice(0, 12).map(x => h("div", { class: "row small", style: { padding: "3px 0" } }, h("code", { class: "mono xs ellip", style: { flex: 1, cursor: "pointer" }, title: "load", onClick: () => { rawMode.checked = true; rawMode.onchange(); rawInp.value = x.cmd.replace(/^beet /, ""); lastPreview = null; } }, x.cmd), h("span", { class: "muted xs" }, fmt.ago(x.ts))))) : null);
      const cheat = h("details", { class: "acc" }, h("summary", {}, "Query cheat-sheet"), h("div", { class: "acc-body hint" }, h("table", { class: "mini-tbl" },
        ...[["artist:Radiohead", "field match (substring)"], ["albumartist:\"Kanye West\"", "quoted multi-word"], ["album_id:123", "one album row"], ["id:456", "one track row"], ["year:1990..1999", "range"], ["format:FLAC", "exact-ish"], ["^genre:Rock", "negate"], ["title::^Blue$", "regex (::)"], ["added:-1w..", "added last week"], ["bitrate:..192000", "≤ 192 kbps"], ["a , b", "OR (comma)"], ["mb_albumid:", "empty field"]].map(([q, d]) => h("tr", {}, h("td", {}, h("a", { href: "#", onClick: (e) => { e.preventDefault(); queryInp.value = (queryInp.value + " " + q).trim(); lastPreview = null; } }, q)), h("td", {}, d))))));
      mount(out, h("div", { class: "spread" }, h("div", { class: "stack" }, h("div", { class: "card" }, pickSel, form), preview), h("div", { class: "stack" }, h("div", { class: "card tight desk-only" }, picker), cheat, histBox)));
      paintPicker(); paintForm(); paintHist();
      if (r.q.query) doPreview();
    }

    // ---------------- scripts ----------------
    async function scripts(myGen) {
      const cat = await api("/api/beets/scripts");
      if (myGen !== tabGen || !out.isConnected) return;
      const hcmds = (await api("/api/hunter/commands")).commands;
      if (myGen !== tabGen || !out.isConnected) return;
      const tierCls = { safe: "ok", recovery: "warn", heavy: "info", maint: "dim" };
      const card = (s) => {
        const date = s.accepts_date ? h("input", { class: "input sm", type: "date", style: { maxWidth: "150px" } }) : null;
        const limit = s.accepts_limit ? h("input", { class: "input sm", type: "number", placeholder: "limit", style: { maxWidth: "90px" } }) : null;
        const go = async (apply) => { const r = await api("/api/beets/scripts/run", { id: s.id, apply, date: date?.value || "", limit: +limit?.value || 0 }); if (r.ok) { toast(`${s.title} started`, "ok"); openJob(r.job.id); } else toast(r.error, "bad"); };
        return h("div", { class: "card" }, h("h3", {}, s.title, h("span", { class: "badge " + (tierCls[s.tier] || "dim") }, s.tier), s.last ? h("span", { class: "right" }, statusBadge(s.last.status), " ", fmt.ago(s.last.started_ts)) : null),
          h("div", { class: "hint mb" }, s.blurb),
          h("div", { class: "row" }, date, limit, s.dry_run ? h("button", { class: "btn sm", onClick: action(null, () => go(false)) }, "Dry-run") : null,
            h("button", { class: "btn sm " + (s.dry_run ? "primary" : ""), onClick: action(null, async () => { if (!s.dry_run || await confirm({ title: `Run ${s.title} for real?`, body: "Review the dry-run log first if you haven't.", ok: "Run" })) await go(true); }) }, s.dry_run ? "Apply" : "Run"),
            s.last ? h("button", { class: "btn sm ghost", onClick: () => openJob(s.last.id) }, "Last log") : null));
      };
      const hcard = (c) => {
        const flags = (c.flags || []).map(f => ({ f, cb: h("input", { type: "checkbox" }) }));
        const ints = Object.entries(c.ints || {}).map(([k, [lo, hi]]) => ({ k, inp: h("input", { class: "input sm", type: "number", placeholder: `${k.slice(2)} (${lo}–${hi})`, style: { maxWidth: "150px" } }) }));
        return h("div", { class: "card" }, h("h3", {}, c.title), h("div", { class: "hint mb" }, c.blurb),
          h("div", { class: "row" }, ...flags.map(x => h("label", { class: "row small mono" }, x.cb, x.f)), ...ints.map(x => x.inp),
            h("button", { class: "btn sm primary", onClick: action(null, async () => { const r = await api("/api/hunter/run", { command: c.name, flags: flags.filter(x => x.cb.checked).map(x => x.f), ints: Object.fromEntries(ints.map(x => [x.k, +x.inp.value || 0])) }); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); }) }, "Run")));
      };
      mount(out, h("div", { class: "hint mb" }, "Automation scripts detected in your configured automation directory. Dry-run is the default where supported."),
        h("div", { class: "section-title" }, "beets scripts"),
        cat.scripts?.length
          ? h("div", { class: "grid c2" }, ...cat.scripts.map(card))
          : h("div", { class: "empty small card" }, "No automation scripts found. Optional automation scripts can be mounted via ", h("code", { class: "mono" }, "HUNTER_BEETS_AUTOMATION"), "."),
        h("div", { class: "section-title" }, "hunter CLI"), h("div", { class: "grid c2" }, ...hcmds.map(hcard)));
    }

    // ---------------- quarantine ----------------
    async function quarantine(myGen) {
      const tree = h("div", { class: "tree card tight" });
      const pane = h("div", {});
      mount(out, h("div", { class: "hint mb" }, "Failed imports by date and reason. Each reason dir says what went wrong: ", h("code", {}, "beet_rc_N"), " non-zero exit · ", h("code", {}, "unverified"), " no fingerprint/MB match · ", h("code", {}, "duplicates"), " identical existing · ", h("code", {}, "recovery"), " what dedup/cleanup scripts moved (never deleted)."),
        h("div", { class: "spread tree" }, tree, pane));
      const q = await api("/api/beets/quarantine?refresh=" + (r.q.refresh ? "1" : "0"));
      if (myGen !== tabGen || !out.isConnected) return;
      if (!q.ok) { mount(pane, h("div", { class: "notice bad" }, q.error)); return; }
      mount(tree, h("div", { class: "row between", style: { padding: "4px 8px" } }, h("b", {}, `${fmt.num(q.total)} entries`), h("button", { class: "btn xs ghost", onClick: () => { r.q.refresh = 1; quarantine(myGen); } }, "↻")),
        ...q.buckets.map(b => h("details", { open: b === q.buckets[0] }, h("summary", { class: "node" }, b.name, h("span", { class: "n" }, fmt.num(b.total))), h("div", { class: "kids" }, ...b.reasons.map(rs => h("div", { class: "node", onClick: (e) => { tree.querySelectorAll(".node.active").forEach(n => n.classList.remove("active")); e.currentTarget.classList.add("active"); showReason(b.name, rs.name); } }, rs.name, h("span", { class: "n" }, rs.count + (rs.capped ? "+" : ""))))))));
      mount(pane, h("div", { class: "empty" }, "Pick a bucket → reason."));
      async function showReason(bucket, reason, offset = 0) {
        mount(pane, h("div", { class: "skel" }, h("div"), h("div")));
        const d = await api(`/api/beets/quarantine/entries${qs({ bucket, reason, offset })}`);
        const isDate = /^\d{4}-\d{2}-\d{2}$/.test(bucket);
        mount(pane, h("div", { class: "row mb" }, h("h3", { style: { fontSize: "15px" } }, `${bucket} / ${reason}`), h("span", { class: "muted small" }, `${fmt.num(d.total)} entries`), h("span", { class: "ml-auto" }),
            isDate ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/beets/scripts/run", { id: "recover_quarantine", apply: false, date: bucket }); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); }) }, "Recover (dry-run)") : null,
            isDate ? h("button", { class: "btn sm primary", onClick: action(null, async () => { if (await confirm({ title: `Recover ${bucket}?`, body: "Album-aware resolver: relocate into existing albums, import new ones, park verified duplicates.", ok: "Recover" })) { const r = await api("/api/beets/scripts/run", { id: "recover_quarantine", apply: true, date: bucket }); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); } }) }, "Recover") : null,
            isDate ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/beets/scripts/run", { id: "quarantine_retry", date: bucket }); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); }) }, "Retry autotag") : null),
          h("div", { class: "mono xs muted mb" }, d.container_path),
          h("div", { class: "list card" }, ...d.entries.map(e => h("div", { class: "item click", onClick: () => showEntry(bucket, reason, e.name) }, h("div", { class: "main" }, h("div", { class: "t small" }, e.is_dir ? "📁 " : "", e.name), h("div", { class: "s" }, fmt.ago(e.mtime), e.is_dir ? ` · ${e.audio} audio` : ` · ${fmt.bytes(e.size)}`, e.reason_txt ? h("span", { class: "ellip", style: { maxWidth: "420px" } }, " · " + e.reason_txt.split("\n")[0]) : null))))),
          d.total > offset + 100 ? h("button", { class: "btn mt", onClick: () => showReason(bucket, reason, offset + 100) }, "Next page") : null);
      }
      async function showEntry(bucket, reason, name) {
        const e = await api(`/api/beets/quarantine/entry${qs({ bucket, reason, name })}`).catch(errToast); if (!e) return;
        modal({ title: name, wide: true, body: h("div", {}, h("div", { class: "mono xs muted mb" }, e.container_path), e.reason_txt ? h("pre", { class: "log mb", style: { maxHeight: "160px" } }, e.reason_txt) : null,
          h("table", { class: "mini-tbl" }, ...e.files.map(f => h("tr", {}, h("td", {}, fmt.bytes(f.size)), h("td", { class: f.audio ? "" : "muted" }, f.name)))),
          h("div", { class: "hint mt" }, "Inspect with the console: ", h("a", { href: link("beets", { tab: "console", cmd: "info" }) }, "beet info"), " (needs the file path — copy from above).")) });
      }
    }

    // ---------------- logs ----------------
    async function logs(myGen) {
      const names = (await api("/api/beets/logs")).sources;
      if (myGen !== tabGen || !out.isConnected) return;
      let name = r.q.log || "beets-watcher", lines = 300, follow = true;
      const sel_ = h("select", { class: "select sm", onChange: (e) => { name = e.target.value; setQuery({ log: name }); tick(); } }, ...names.map(n => h("option", { value: n, selected: n === name }, n)));
      const linesSel = h("select", { class: "select sm", onChange: (e) => { lines = +e.target.value; tick(); } }, ...[100, 300, 1000, 2000].map(n => h("option", { value: n, selected: n === lines }, n + " lines")));
      const filt = h("input", { class: "input sm", placeholder: "grep", style: { maxWidth: "220px" } });
      const logEl = h("pre", { class: "log tall" });
      let raw = "";
      const paintLog = () => { const f = filt.value.trim().toLowerCase(); setLog(logEl, f ? raw.split("\n").filter(l => l.toLowerCase().includes(f)).join("\n") : raw); if (follow) logEl.scrollTop = logEl.scrollHeight; };
      filt.addEventListener("input", paintLog);
      logEl.addEventListener("scroll", () => { follow = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 30; });
      mount(out, h("div", { class: "filterbar" }, sel_, linesSel, filt, h("label", { class: "row small ml-auto" }, h("input", { type: "checkbox", checked: true, onChange: (e) => { auto = e.target.checked; } }), "auto-refresh")), logEl);
      let auto = true;
      const tick = async () => {
        if (!auto && raw) return;
        try {
          const res = await api(`/api/beets/logs${qs({ name, lines })}`);
          if (myGen !== tabGen || !out.isConnected) return;
          raw = res.text;
          paintLog();
        } catch (e) { if (myGen === tabGen && out.isConnected) setLog(logEl, e.message); }
      };
      stopSubPoll = poll(tick, 5000);
    }
    go();
  },
};
