/* Shared drawers + widgets used across views: want detail, candidate picker,
   job log viewer, library album manager, MusicBrainz artist discography,
   the guarded modify dialog. */
import { api, h, mount, clear, fmt, toast, errToast, busy, action, drawer, modal, confirm, cover, artUrl, libArtUrl, navigate, link, qs, refreshStatusSoon, store, can } from "../app.js";

// ---------- small widgets ----------
export const statusBadge = (s) => h("span", { class: "badge " + s }, s);
export const modeBadge = (m) => h("span", { class: "badge " + (m === "apply" ? "warn" : m === "dry-run" ? "info" : "dim") }, m);
export function covBar(have, expected, { cls = "" } = {}) {
  if (!expected) return h("span", { class: "cov muted" }, `${have} tracks`);
  const p = Math.min(1, have / expected);
  return h("span", { class: "cov " + cls }, h("span", { class: "progress " + (p >= 1 ? "good" : p >= .5 ? "warn" : "bad") }, h("div", { style: { width: (p * 100) + "%" } })), `${have}/${expected}`);
}
export function qualityBadge(a) {
  const share = a.lossless_share ?? (a.tracks ? (a.lossless || 0) / a.tracks : 0);
  const cls = share >= 1 ? "ok" : share > 0 ? "warn" : "dim";
  const label = share >= 1 ? "lossless" : share > 0 ? `${Math.round(share * 100)}% lossless` : (a.formats || "lossy").toLowerCase();
  const extra = a.max_bitdepth && a.max_bitdepth > 16 ? ` · ${a.max_bitdepth}-bit` : "";
  return h("span", { class: "badge " + cls }, label + extra);
}
export function wantBadge(w) {
  if (!w) return null;
  const s = w.status;
  return h("a", { class: "badge " + s, href: "#/queue?q=", title: `want #${w.id}`, onClick: (e) => { e.preventDefault(); e.stopPropagation(); openWant(w.id); } }, s === "wanted" ? "wanted" : s === "grabbed" ? "downloading" : s);
}
export function ownedBadge(x) {
  if (x.owned) return h("span", { class: "badge ok", title: "in your beets library" }, "Owned", x.have ? ` · ${x.have}` : "");
  if (x.have) return h("span", { class: "badge warn", title: "partial — request completes the gap" }, `${x.have}/${x.expected || "?"}`);
  return null;
}
export function ownedOrWant(x) { return x.want && ["wanted", "grabbed"].includes(x.want.status) ? wantBadge(x.want) : ownedBadge(x); }

// request an MB release-group; second click forces
export async function requestAlbum(btn, x, force = false) {
  const r = await busy(btn, api("/api/request", { rgid: x.rgid, artist: x.artist, album: x.album, year: x.year, force }));
  if (r.added) { toast(`Requested ${x.artist} — ${x.album}`, "ok"); refreshStatusSoon(); if (btn) { btn.textContent = "Requested"; btn.disabled = true; } return true; }
  if (r.owned) { toast(`Already owned (${r.have}/${r.expected}) — tap again to force`, "warn"); if (btn) { btn.textContent = "Force"; btn.onclick = action(btn, () => requestAlbum(btn, x, true)); } return false; }
  toast("Already in the queue", ""); return false;
}

export function requestBtn(x, { cls = "btn sm primary" } = {}) {
  if (!can("operator")) return null;
  if (x.owned && !x.want) return h("button", { class: "btn sm ghost", title: "owned — tap to force a re-grab", onClick: action(null, (e) => requestAlbum(e.currentTarget, x, true)) }, "Owned");
  if (x.want && ["wanted", "grabbed"].includes(x.want.status)) return h("button", { class: "btn sm ghost", onClick: (e) => { e.stopPropagation(); openWant(x.want.id); } }, x.want.status === "grabbed" ? "Downloading" : "Queued");
  const b = h("button", { class: cls }, x.have ? "Complete" : "Request");
  b.onclick = action(b, () => requestAlbum(b, x));
  return b;
}

export function logView(text, { tall = false } = {}) {
  const el = h("pre", { class: "log" + (tall ? " tall" : "") });
  setLog(el, text);
  return el;
}
export function setLog(el, text) {
  clear(el);
  for (const line of (text || "").split("\n")) {
    let cls = "";
    if (/\b(ERROR|CRITICAL|Traceback|exit=[1-9])/.test(line)) cls = "err";
    else if (/\bWARN/i.test(line)) cls = "warn";
    else if (/\b(imported successfully|exit=0|done|✓)/.test(line)) cls = "ok";
    else if (line.startsWith("#")) cls = "meta";
    el.appendChild(h("div", { class: cls }, line));
  }
}

// ---------- want drawer ----------
export async function openWant(id) {
  const d = drawer({ title: "Want", body: h("div", { class: "skel" }, h("div"), h("div")) });
  let data;
  try { data = await api(`/api/want?id=${encodeURIComponent(id)}`); } catch (e) { mount(d.body, h("div", { class: "notice bad" }, e.message)); return; }
  const w = data.want;
  d.setTitle(`${w.artist} — ${w.album}`);
  const rg = w.spotify_album_id?.startsWith("request:rg:") || w.spotify_album_id?.startsWith("release:rg:") ? w.spotify_album_id.slice(11) : "";
  const op = can("operator");
  const render = () => {
    mount(d.body,
      h("div", { class: "row mb" }, rg ? cover(artUrl(rg), { size: "lg" }) : cover("", { size: "lg" }),
        h("div", { class: "stack", style: { gap: "4px" } }, h("div", { class: "row" }, statusBadge(w.status), h("span", { class: "badge dim" }, w.source), w.year ? h("span", { class: "muted" }, w.year) : null),
          h("div", { class: "muted small" }, `${w.track_count ? w.track_count + " tracks · " : ""}${w.attempts} grab${w.attempts === 1 ? "" : "s"} · ${w.search_misses || 0} search misses · priority ${w.priority ?? 0}`),
          data.owned ? h("div", { class: "row small" }, data.owned.owned ? h("span", { class: "badge ok" }, `owned ${data.owned.have}/${data.owned.expected || "?"}`) : h("span", { class: "badge dim" }, `library: ${data.owned.have}/${data.owned.expected || "?"}`),
            data.owned.album_id ? h("a", { class: "btn xs ghost", href: "#", onClick: (e) => { e.preventDefault(); openAlbum(data.owned.album_id); } }, "Open in library") : null) : null)),
      h("div", { class: "row mb" },
        op ? h("button", { class: "btn sm primary", onClick: action(null, async () => { await api("/api/retry", { id }); toast("Retrying now — pass spawned", "ok"); refreshStatusSoon(); }) }, "Retry now") : null,
        op ? h("button", { class: "btn sm", onClick: () => pickCandidates(id, { title: `${w.artist} — ${w.album}`, tracks: w.track_count }) }, "Pick source…") : null,
        op ? h("button", { class: "btn sm", onClick: () => editWant(w, () => openWant(id)) }, "Edit") : null,
        h("button", { class: "btn sm ghost", onClick: () => navigate(link("discover", { q: `${w.artist} ${w.album}` })) }, "Search MB"),
        h("span", { class: "ml-auto" }),
        op && ["wanted", "grabbed", "exhausted"].includes(w.status) ? h("button", { class: "btn sm danger", onClick: action(null, async () => { await api("/api/drop", { id }); toast("Dropped"); refreshStatusSoon(); d.close(); }) }, "Drop") : null),
      w.note ? h("div", { class: "notice info mb" }, h("div", { class: "body" }, w.note)) : null,
      h("dl", { class: "kv card tight mb" },
        h("dt", {}, "key"), h("dd", { class: "mono" }, w.spotify_album_id),
        h("dt", {}, "created"), h("dd", {}, fmt.datetime(w.created_ts)),
        h("dt", {}, "updated"), h("dd", {}, fmt.datetime(w.updated_ts)),
        w.next_attempt_ts ? h("dt", {}, "next attempt") : null, w.next_attempt_ts ? h("dd", {}, w.next_attempt_ts > Date.now() / 1000 ? "in " + fmt.in(w.next_attempt_ts) : "eligible now") : null,
        w.grabbed_username ? h("dt", {}, "peer") : null, w.grabbed_username ? h("dd", { class: "mono" }, w.grabbed_username, " · ", fmt.datetime(w.grabbed_ts)) : null,
        w.grabbed_dir ? h("dt", {}, "remote dir") : null, w.grabbed_dir ? h("dd", { class: "mono xs" }, w.grabbed_dir) : null),
      h("div", { class: "section-title" }, `Attempts (${data.attempts.length})`),
      data.attempts.length ? h("div", { class: "list card" }, ...data.attempts.map(a => h("div", { class: "item" }, h("div", { class: "main" }, h("div", { class: "t mono small" }, a.username), h("div", { class: "s mono xs" }, a.dir)), h("div", { class: "end muted xs" }, a.at || "")))) : h("div", { class: "hint" }, "No grabs yet."),
      h("div", { class: "section-title" }, "History"),
      data.events.length ? h("div", { class: "timeline card tight" }, ...data.events.map(e => h("div", { class: "tl" }, h("span", { class: "ts" }, fmt.datetime(e.ts)), h("span", { class: "badge dim" }, e.kind), h("span", { class: "what" }, e.detail)))) : h("div", { class: "hint" }, "No events."));
  };
  render();
}

export function editWant(w, after) {
  const artist = h("input", { class: "input", value: w.artist }), album = h("input", { class: "input", value: w.album });
  const tracks = h("input", { class: "input", type: "number", value: w.track_count || "" }), year = h("input", { class: "input", type: "number", value: w.year || "" });
  const prio = h("input", { class: "input", type: "number", value: w.priority ?? 0, min: -100, max: 100 });
  modal({ title: "Edit want", body: h("div", { class: "grid c2" }, field("Artist", artist), field("Album", album), field("Expected tracks", tracks), field("Year", year), field("Priority (-100…100)", prio)),
    actions: [{ label: "Save", cls: "primary", onClick: async () => {
      await api("/api/want/edit", { id: w.id, artist: artist.value, album: album.value, track_count: +tracks.value || 0, year: +year.value || 0 });
      if (+prio.value !== (w.priority ?? 0)) await api("/api/want/priority", { id: w.id, priority: +prio.value });
      toast("Saved", "ok"); after && after();
    } }] });
}
export const field = (label, input, hint) => h("div", { class: "field" }, h("label", {}, label), input, hint ? h("div", { class: "hint" }, hint) : null);

// ---------- candidate picker ----------
export async function pickCandidates(wantId, { title = "", tracks = null } = {}) {
  const d = drawer({ title: "Pick a source · " + title, wide: true, body: h("div", { class: "skel" }, h("div"), h("div"), h("div")) });
  const txt = h("input", { class: "input", placeholder: "custom Soulseek search text (optional)" });
  const list = h("div", {});
  const run = async () => {
    mount(list, h("div", { class: "skel" }, h("div"), h("div"), h("div")), h("div", { class: "hint mt" }, "Searching Soulseek — up to 30 s…"));
    try {
      const r = await api("/api/candidates", { id: wantId, text: txt.value.trim() });
      mount(list, h("div", { class: "hint mb" }, `query: “${r.query}” · ${r.candidates.length} candidate dirs (ranked lossless → fit → bitrate → slot)`),
        r.candidates.length ? h("div", { class: "list card" }, ...r.candidates.map(c => candidateRow(c, { tracks, onGrab: async (btn) => { await busy(btn, api("/api/grab", { id: wantId, username: c.username, dir: c.dir, files: c.files })); toast("Grabbed — watch Queue → Downloading", "ok"); refreshStatusSoon(); d.close(); } }))) : h("div", { class: "empty" }, h("div", { class: "big" }, "∅"), "Nothing on Soulseek matched. Try a custom search text (e.g. drop the year or a subtitle)."));
    } catch (e) { mount(list, h("div", { class: "notice bad" }, e.message)); }
  };
  mount(d.body, h("div", { class: "row mb nowrap" }, txt, h("button", { class: "btn", onClick: run }, "Search")), list);
  txt.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  run();
}

export function candidateRow(c, { tracks = null, onGrab, label = "Grab" } = {}) {
  const fit = tracks ? ` · want ${tracks}` : "";
  const files = h("details", { class: "acc mt", style: { fontSize: "12px" } }, h("summary", {}, `${c.files.length} files · ${fmt.bytes(c.size)}`), h("div", { class: "acc-body mono xs" }, ...c.files.map(f => h("div", {}, f.filename.replace(/\\/g, "/").split("/").pop(), h("span", { class: "dim2" }, "  " + fmt.bytes(f.size))))));
  const row = h("div", { class: "item", style: { alignItems: "flex-start" } },
    h("div", { class: "main" },
      h("div", { class: "t" }, c.dir_leaf, c.tried ? h("span", { class: "badge dim", style: { marginLeft: "6px" } }, "tried") : null),
      h("div", { class: "s" }, h("span", { class: "badge " + (c.lossless ? "ok" : "dim") }, c.lossless ? "lossless" : (c.bitrate ? c.bitrate + " kbps" : "lossy")), h("span", {}, `${c.count} audio${fit}`), h("span", {}, c.slot ? "free slot" : `queue ${c.queue}`), h("span", { class: "mono" }, c.username)),
      files),
    h("div", { class: "end" },
      h("button", { class: "btn sm primary", onClick: async (e) => { try { await onGrab(e.currentTarget); } catch (err) { errToast(err); } } }, label),
      h("button", { class: "btn sm ghost danger", title: "ban this peer stack-wide", onClick: action(null, async () => { if (await confirm({ title: `Ban ${c.username}?`, body: "Their results are excluded from every future hunt.", ok: "Ban", danger: true })) { await api("/api/ban", { username: c.username, reason: "via candidate picker" }); toast("Banned " + c.username); row.style.opacity = ".4"; } }) }, "Ban")));
  return row;
}

// Resolve when a job leaves "running" (polls /api/library/job_result every 2 s,
// gives up after an hour). Used to refresh lists only once the beets-side work
// is really done.
export function waitForJob(id, { every = 2000, maxMs = 3_600_000 } = {}) {
  const t0 = Date.now();
  return new Promise((resolve) => {
    const tick = async () => {
      let r = null;
      try { r = await api(`/api/library/job_result?id=${encodeURIComponent(id)}`); } catch (e) { if (e.status === 404) return resolve({ job: {}, result: null }); }
      if (r && r.job && r.job.status !== "running") return resolve(r);
      if (Date.now() - t0 > maxMs) return resolve(r || { job: {}, result: null });
      setTimeout(tick, document.hidden ? every * 3 : every);
    };
    tick();
  });
}

// ---------- job drawer ----------
export async function openJob(id) {
  let follow = true, timer = null, stopped = false, job = null;
  const d = drawer({
    title: "Job",
    wide: true,
    body: h("div", { class: "skel" }, h("div"), h("div")),
    onClose: () => { stopped = true; clearTimeout(timer); },
  });
  const logEl = h("pre", { class: "log tall follow" });
  const head = h("div", {});
  logEl.addEventListener("scroll", () => { follow = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20; });
  const tick = async () => {
    if (stopped) return;
    let r;
    try { r = await api(`/api/jobs/tail?id=${encodeURIComponent(id)}&lines=600`); }
    catch (e) { if (!stopped) mount(head, h("div", { class: "notice bad" }, e.message)); return; }
    if (stopped) return;
    job = r.job;
    d.setTitle(job.title);
    const p = r.progress || {};
    mount(head,
      h("div", { class: "row mb" }, statusBadge(job.status), modeBadge(job.mode), h("span", { class: "badge dim" }, job.kind), h("span", { class: "badge dim" }, job.runner),
        h("span", { class: "muted small" }, `started ${fmt.datetime(job.started_ts)} · ${fmt.dur(job.duration)}${job.exit_code !== null && job.exit_code !== undefined ? " · exit " + job.exit_code : ""}`),
        h("span", { class: "ml-auto" }),
        job.status === "running" ? h("button", { class: "btn sm danger", onClick: action(null, async () => { const r = await api("/api/jobs/cancel", { id }); toast("Sent SIG" + r.signal); }) }, "Cancel") : null,
        job.status === "running" ? h("button", { class: "btn sm ghost danger", title: "SIGKILL", onClick: action(null, async () => { if (await confirm({ title: "Force kill?", body: "SIGKILL leaves no chance to clean up.", ok: "Kill", danger: true })) { await api("/api/jobs/cancel", { id, force: true }); toast("Killed"); } }) }, "Kill") : null,
        job.status !== "running" && job.mode === "dry-run" && job.kind !== "hunter" ? h("button", { class: "btn sm primary", onClick: action(null, () => rerun(job, true)) }, "Run for real") : null,
        job.status !== "running" ? h("button", { class: "btn sm", onClick: action(null, () => rerun(job, job.mode === "apply")) }, "Re-run") : null),
      h("div", { class: "mono xs muted mb", style: { overflowWrap: "anywhere" } }, job.command),
      job.status === "running" ? h("div", { class: "mb" }, h("div", { class: "progress lg striped" + (p.pct === null ? "" : "") }, h("div", { style: { width: (p.pct ?? 100) + "%", opacity: p.pct === null ? ".35" : "1" } })), h("div", { class: "xs muted mt", style: { marginTop: "4px" } }, p.pct !== null ? `${p.pct}%${p.total ? ` · ${p.done}/${p.total}` : ""}` : "running…", p.last ? " · " + p.last : "")) : null,
      job.meta?.query ? h("div", { class: "hint mb" }, "query: ", h("code", { class: "mono" }, job.meta.query)) : null);
    setLog(logEl, r.text || "(no output yet)");
    if (follow) logEl.scrollTop = logEl.scrollHeight;
    if (job.status === "running" && !stopped) timer = setTimeout(tick, 2000);
  };
  const quoteArg = (a) => (a === "" || /[\s"'\\]/.test(a)) ? JSON.stringify(a) : a;
  const rerun = async (job, apply) => {
    if (job.kind === "script" && job.meta?.script) { const r = await api("/api/beets/scripts/run", { id: job.meta.script, apply, date: job.meta.date, limit: job.meta.limit }); if (r.ok) { d.close(); openJob(r.job.id); } else toast(r.error, "bad"); return; }
    if (job.kind === "beet") {
      const argv = (job.argv || []).slice(2).filter(a => a !== "-y");
      const raw = argv.map(quoteArg).join(" ");
      const r = await api("/api/beets/run", { raw, apply, confirm_count: -1 }).catch(async (e) => {
        if (e.status === 409 && e.data.needs_confirm) {
          if (await confirm({ title: `Touch ${e.data.count} rows?`, body: "Blast radius exceeds the soft cap. Confirm to run.", ok: "Run", danger: true })) {
            return api("/api/beets/run", { raw, apply, confirm_count: e.data.count });
          }
        }
        throw e;
      });
      if (r?.ok) { d.close(); openJob(r.job.id); } else if (r?.error) toast(r.error, "bad");
      return;
    }
    if (job.kind === "hunter") {
      const rest = (job.argv || []).slice(4);
      const flags = [], ints = {};
      let verbose = false;
      for (let i = 0; i < rest.length; i++) {
        const a = rest[i];
        if (a === "-v") { verbose = true; continue; }
        if (a.startsWith("--")) {
          const eq = a.indexOf("=");
          if (eq > 0) {
            const k = a.slice(0, eq), v = a.slice(eq + 1);
            if (/^-?\d+$/.test(v)) ints[k] = Number(v);
            else flags.push(a);
          } else if (i + 1 < rest.length && !rest[i + 1].startsWith("-")) {
            const v = rest[++i];
            if (/^-?\d+$/.test(v)) ints[a] = Number(v);
            else flags.push(a, v);
          } else {
            flags.push(a);
          }
        }
      }
      const r = await api("/api/hunter/run", { command: job.argv[3], flags, ints, verbose });
      if (r.ok) { d.close(); openJob(r.job.id); } else toast(r.error, "bad");
      return;
    }
    toast("cannot re-run this job kind", "warn");
  };
  mount(d.body, head, logEl);
  d.setActions(h("a", { class: "btn sm ghost", href: "#/jobs" }, "All jobs"));
  tick();
}

// ---------- library album drawer ----------
export async function openAlbum(albumId) {
  const d = drawer({ title: "Album", wide: true, body: h("div", { class: "skel" }, h("div"), h("div"), h("div")) });
  let a;
  const load = async () => { a = await api(`/api/library/album?id=${encodeURIComponent(albumId)}`); render(); };
  const beet = async (btn, act, extra = {}) => { const r = await busy(btn, api("/api/library/beet", { action: act, album_id: albumId, ...extra })); if (r.ok) { toast(`${act} started`, "ok"); openJob(r.job.id); } else toast(r.error, "bad"); };
  const op = can("operator");
  const render = () => {
    d.setTitle(`${a.artist} — ${a.album}`);
    const missing = a.tracktotal ? Math.max(0, a.tracktotal - a.tracks) : 0;
    const w = a.wants || {};
    mount(d.body,
      h("div", { class: "row mb", style: { alignItems: "flex-start" } },
        cover(a.has_art ? libArtUrl(a.id) : (a.rgid ? artUrl(a.rgid) : ""), { size: "xl" }),
        h("div", { class: "stack", style: { flex: 1, minWidth: 0, gap: "6px" } },
          h("div", { class: "row" }, h("a", { class: "b", href: link("library", {}, "artist", a.artist), onClick: () => d.dismiss() }, a.artist), a.year ? h("span", { class: "muted" }, a.year) : null, a.albumtype ? h("span", { class: "badge dim" }, a.albumtype) : null, a.genre ? h("span", { class: "badge dim" }, a.genre) : null),
          h("div", { class: "row" }, qualityBadge(a), covBar(a.tracks, a.tracktotal), h("span", { class: "muted small" }, fmt.mmss(a.length), " · ", (a.formats || "").toLowerCase(), a.max_samplerate ? ` · ${(a.max_samplerate / 1000).toFixed(1)} kHz` : "", a.max_bitdepth ? ` / ${a.max_bitdepth}-bit` : ""),
            a.missing_files ? h("span", { class: "badge bad" }, `${a.missing_files} file${a.missing_files > 1 ? "s" : ""} missing on disk`) : null,
            !a.has_art ? h("span", { class: "badge warn" }, "no art") : null,
            !a.mb_albumid ? h("span", { class: "badge warn", title: "no MusicBrainz id — mbsync/gap-fill can't verify" }, "no MBID") : null),
          h("div", { class: "row" }, w.gapfill ? h("span", {}, "gap-fill: ", wantBadge(w.gapfill)) : null, w.upgrade ? h("span", {}, "upgrade: ", wantBadge(w.upgrade)) : null, w.request ? h("span", {}, "request: ", wantBadge(w.request)) : null),
          h("div", { class: "mono xs muted ellip", title: a.dirs.join("\n") }, a.dirs[0] || ""))),
      h("div", { class: "section-title" }, "Acquire"),
      h("div", { class: "row mb" },
        op && missing && !w.gapfill ? h("button", { class: "btn sm primary", onClick: action(null, async () => { const r = await api("/api/library/want", { album_id: albumId, kind: "gapfill" }); toast(r.added ? `Hunting ${missing} missing track${missing > 1 ? "s" : ""}` : (r.detail || "already queued"), r.added ? "ok" : ""); refreshStatusSoon(); load(); }) }, `Complete album (${missing} missing)`) : null,
        op && a.lossless_share < 1 && !w.upgrade ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/library/want", { album_id: albumId, kind: "upgrade" }); toast(r.added ? "Upgrade want queued (lossless only)" : "already queued", r.added ? "ok" : ""); refreshStatusSoon(); load(); }) }, "Upgrade to lossless") : null,
        a.rgid ? h("button", { class: "btn sm ghost", onClick: () => navigate(link("discover", { q: `${a.artist} ${a.album}` })) }, "Search MB") : null,
        op ? h("button", { class: "btn sm ghost", onClick: () => navigate(link("discover", { q: `${a.artist} ${a.album}`, tab: "soulseek" })) }, "Browse Soulseek") : null),
      h("div", { class: "section-title" }, "Files"),
      h("div", { class: "row mb" }, h("button", { class: "btn sm", onClick: action(null, async () => { const { queueDownload } = await import("./_filters.js"); await queueDownload({ title: `${a.artist} — ${a.album}`, album_ids: [albumId] }); }) }, "Download album (zip)"), h("span", { class: "muted small" }, `${a.items.length} files · ${fmt.mmss(a.length)}`)),
      op ? h("div", { class: "section-title" }, "beets") : null,
      op ? h("div", { class: "row mb" },
        h("button", { class: "btn sm", onClick: (e) => beet(e.currentTarget, "fetchart", { force: a.has_art }) }, a.has_art ? "Re-fetch art" : "Fetch art"),
        h("button", { class: "btn sm", onClick: (e) => beet(e.currentTarget, "embedart") }, "Embed art"),
        h("button", { class: "btn sm", onClick: (e) => beet(e.currentTarget, "write") }, "Write tags"),
        h("button", { class: "btn sm", onClick: (e) => beet(e.currentTarget, "mbsync") }, "MB sync"),
        h("button", { class: "btn sm", onClick: (e) => beet(e.currentTarget, "move") }, "Re-path"),
        h("button", { class: "btn sm", onClick: (e) => beet(e.currentTarget, "update") }, "Update from files"),
        h("button", { class: "btn sm", onClick: (e) => beet(e.currentTarget, "replaygain") }, "ReplayGain"),
        h("button", { class: "btn sm", onClick: (e) => beet(e.currentTarget, "lyrics") }, "Lyrics"),
        h("button", { class: "btn sm", onClick: (e) => beet(e.currentTarget, "lastgenre") }, "Genre"),
        h("button", { class: "btn sm primary", onClick: () => modifyDialog({ albumId, album: true, current: { albumartist: a.artist, album: a.album, year: a.year, genre: a.genre, albumtype: a.albumtype }, after: load }) }, "Edit tags…"),
        h("button", { class: "btn sm ghost", onClick: () => navigate(link("beets", { tab: "console", cmd: "list", query: `album_id:${albumId}` })) }, "Console"),
        h("span", { class: "ml-auto" }),
        h("button", { class: "btn sm danger", onClick: () => removeDialog({ albumIds: [albumId], title: `${a.artist} — ${a.album}`, after: () => d.close() }) }, "Remove album…")) : null,
      a.jobs?.length ? h("div", { class: "list card mb" }, ...a.jobs.map(j => h("div", { class: op ? "item click" : "item", onClick: op ? () => openJob(j.id) : null }, h("div", { class: "main" }, h("div", { class: "t small" }, j.title), h("div", { class: "s" }, fmt.datetime(j.started_ts), " · ", fmt.dur(j.duration))), h("div", { class: "end" }, statusBadge(j.status))))) : null,
      h("div", { class: "section-title" }, `Tracks (${a.items.length}${a.tracktotal ? " of " + a.tracktotal : ""})`, a.missing_tracks?.length ? h("span", { class: "right warn" }, "missing #", a.missing_tracks.join(", ")) : null),
      h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", { class: "num" }, "#"), h("th", {}, "Title"), h("th", {}, "Artist"), h("th", {}, "Format"), h("th", { class: "num" }, "kbps"), h("th", { class: "num" }, "Time"), h("th", { class: "num" }, "BPM"), h("th", {}, ""))),
        h("tbody", {}, ...a.items.map(it => h("tr", { class: "click", onClick: () => openTrack(it.id) },
          h("td", { class: "num muted" }, (a.disctotal > 1 ? it.disc + "-" : "") + (it.track || "")),
          h("td", {}, h("div", { class: "b" }, it.title || it.file), !it.exists ? h("span", { class: "badge bad xs" }, "missing file") : null, !it.mb_trackid ? h("span", { class: "badge dim xs", title: "no MusicBrainz track id" }, "no MBID") : null),
          h("td", { class: "muted" }, it.artist !== a.artist ? it.artist : ""),
          h("td", {}, h("span", { class: "badge " + (it.lossless ? "ok" : "dim") }, (it.format || "?").toLowerCase(), it.bitdepth && it.bitdepth > 16 ? ` ${it.bitdepth}b` : "")),
          h("td", { class: "num" }, it.kbps || ""), h("td", { class: "num" }, fmt.mmss(it.length)), h("td", { class: "num" }, it.bpm || ""),
          h("td", {}, h("div", { class: "actions" }, op && store.status?.navidrome && store.status?.audiomuse ? h("button", { class: "btn xs ghost", title: "Sonic radio (AudioMuse → Navidrome)", onClick: action(null, async (e) => { e.stopPropagation(); const r = await api("/api/radio", { title: it.title, artist: it.artist }); toast(`Playlist “${r.name}” · ${r.tracks} tracks`, "ok"); }) }, "◉ radio") : null))))))));
  };
  try { await load(); } catch (e) { mount(d.body, h("div", { class: "notice bad" }, e.message)); }
}

export async function openTrack(itemId) {
  const t = await api(`/api/library/track?id=${encodeURIComponent(itemId)}`).catch(e => { errToast(e); return null; });
  if (!t) return;
  const op = can("operator");
  const keys = ["title", "artist", "album", "albumartist", "track", "tracktotal", "disc", "year", "genre", "bpm", "initial_key", "format", "bitrate", "samplerate", "bitdepth", "length", "mb_trackid", "mb_albumid", "mb_artistid", "acoustid_id", "isrc", "label", "comments", "composer", "lyricist", "added", "mtime", "path"];
  const d = drawer({ title: `${t.artist} — ${t.title}`, narrow: true, body: h("div", {},
    h("div", { class: "row mb" },
      op ? h("button", { class: "btn sm primary", onClick: () => modifyDialog({ itemIds: [itemId], current: { title: t.title, artist: t.artist, genre: t.genre, track: t.track, bpm: t.bpm }, after: () => { d.close(); openTrack(itemId); } }) }, "Edit tags…") : null,
      op ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/library/beet", { action: "write", item_id: itemId }); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); }) }, "Write") : null,
      op ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/library/beet", { action: "fingerprint", item_id: itemId }); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); }) }, "Fingerprint") : null,
      h("a", { class: "btn sm", href: `/api/dl/track?id=${encodeURIComponent(itemId)}`, download: "" }, "Download"),
      h("button", { class: "btn sm ghost", onClick: () => { d.close(); openAlbum(t.album_id); } }, "Album"),
      op ? h("button", { class: "btn sm danger ml-auto", onClick: () => removeDialog({ itemIds: [itemId], title: `${t.artist} — ${t.title}`, after: () => d.close() }) }, "Remove…") : null),
    !t.exists ? h("div", { class: "notice bad mb" }, h("div", { class: "body" }, "File not found on disk at this path.")) : null,
    h("table", { class: "mini-tbl" }, ...keys.filter(k => t[k] !== undefined && t[k] !== "" && t[k] !== null).map(k => h("tr", {}, h("td", {}, k), h("td", { style: { overflowWrap: "anywhere" } }, k === "length" ? fmt.mmss(t[k]) : k === "added" || k === "mtime" ? fmt.datetime(t[k]) : k === "bitrate" ? Math.round(t[k] / 1000) + " kbps" : String(t[k]))))),
    t.lyrics ? h("details", { class: "acc mt" }, h("summary", {}, "Lyrics"), h("div", { class: "acc-body", style: { whiteSpace: "pre-wrap", fontSize: "13px" } }, t.lyrics)) : null) });
}

// ---------- modify dialog (guarded) ----------
const COMMON_FIELDS = ["albumartist", "album", "artist", "title", "year", "genre", "albumtype", "track", "disc", "bpm", "comp", "label", "mb_albumid", "mb_releasegroupid", "comments", "grouping"];
export function modifyDialog({ albumId = null, itemIds = null, query = "", album = false, current = {}, after } = {}) {
  const rows = h("div", { class: "stack" });
  const addRow = (k = "", v = "") => {
    const key = h("input", { class: "input sm mono", placeholder: "field", value: k, list: "beet-fields", style: { maxWidth: "180px" } });
    const val = h("input", { class: "input sm", placeholder: "value (empty = clear the field)", value: v ?? "" });
    const r = h("div", { class: "row nowrap" }, key, val, h("button", { class: "btn xs ghost", onClick: () => r.remove() }, "✕"));
    rows.appendChild(r);
  };
  Object.entries(current).filter(([, v]) => v !== undefined).slice(0, 4).forEach(([k, v]) => addRow(k, v));
  if (!rows.children.length) addRow();
  const dl = h("datalist", { id: "beet-fields" }, ...COMMON_FIELDS.map(f => h("option", { value: f })));
  const noMove = h("input", { type: "checkbox" }), noWrite = h("input", { type: "checkbox" });
  const scope = albumId ? `album_id:${albumId}${album ? " (album row + its tracks)" : ""}` : itemIds ? `${itemIds.length} track${itemIds.length > 1 ? "s" : ""}` : query;
  const collect = () => { const mods = {}; for (const r of rows.children) { const k = r.children[0].value.trim(); if (k) mods[k] = r.children[1].value; } return mods; };
  const run = async (apply) => {
    const mods = collect(); if (!Object.keys(mods).length) throw new Error("no fields");
    const body = { album_id: albumId, item_ids: itemIds, query, album, mods, no_move: noMove.checked, no_write: noWrite.checked, apply };
    let r;
    try {
      r = await api("/api/library/modify", body);
    } catch (e) {
      if (e.status === 409 && e.data?.needs_confirm) {
        const n = e.data.count;
        if (!(await confirm({ title: `Modify ${fmt.num(n)} rows?`, body: `This will update ${fmt.num(n)} matching rows (${Object.keys(mods).join(", ")}).`, ok: "Apply", danger: true, typed: n > 50 ? String(n) : "" }))) return false;
        r = await api("/api/library/modify", { ...body, confirm_count: n });
      } else throw e;
    }
    if (!r.ok) throw new Error(r.error);
    toast(apply ? "modify running — check the job log" : "dry-run started — review the log, then apply", apply ? "ok" : "");
    openJob(r.job.id); if (apply && after) setTimeout(after, 1500);
    return true;
  };
  modal({ title: "Modify tags", wide: true, body: h("div", { class: "stack" },
    h("div", { class: "hint" }, "Scope: ", h("code", { class: "mono" }, scope), ". Runs ", h("code", { class: "mono" }, "beet modify"), " in the beets container. ", h("b", {}, "Dry-run"), " prints the plan without ", h("code", {}, "-y"), " (nothing changes); ", h("b", {}, "Apply"), " writes tags and re-paths files unless told not to."),
    dl, rows, h("button", { class: "btn xs ghost", onClick: () => addRow() }, "+ field"),
    h("div", { class: "row" }, h("label", { class: "row small" }, noMove, " don't move files (-M)"), h("label", { class: "row small" }, noWrite, " don't write tags (-W)"))),
    actions: [{ label: "Dry-run", cls: "", keep: true, onClick: () => run(false) }, { label: "Apply", cls: "primary", keep: true, onClick: async (close) => { if (await confirm({ title: "Apply modify?", body: `Fields ${Object.keys(collect()).join(", ")} on ${scope}.`, ok: "Apply" })) { if (await run(true)) close(); } } }] });
}

// ---------- MusicBrainz artist discography drawer ----------
export async function openMbArtist(id, name) {
  const d = drawer({ title: name, wide: true, body: h("div", { class: "skel" }, h("div"), h("div"), h("div")) });
  let albums = [];
  try { albums = (await api(`/api/artist_albums${qs({ id, name })}`)).albums; } catch (e) { mount(d.body, h("div", { class: "notice bad" }, e.message)); return; }
  const types = ["album", "ep", "single", "live", "compilation", "other"];
  let filt = new Set(["album", "ep"]);
  const list = h("div", { class: "grid covers" });
  const counts = { owned: albums.filter(a => a.owned).length, total: albums.length };
  const render = () => {
    const rows = albums.filter(a => filt.has((a.type || "other").toLowerCase()) || (filt.has("other") && !types.includes((a.type || "").toLowerCase())));
    mount(list, ...rows.map(a => h("div", { class: "cover-card" },
      cover(artUrl(a.rgid), { size: "xxl" }),
      h("div", { class: "t", title: a.album }, a.album), h("div", { class: "s" }, a.year || "", a.type ? " · " + a.type : "", a.secondary ? " · " + a.secondary : ""),
      h("div", { class: "m" }, ownedOrWant(a), requestBtn(a, { cls: "btn xs primary" })))));
    if (!rows.length) mount(list, h("div", { class: "empty" }, "no releases of these types"));
  };
  mount(d.body,
    h("div", { class: "row mb" }, h("span", { class: "muted" }, `${counts.owned}/${counts.total} owned`),
      h("div", { class: "chips" }, ...types.map(t => { const c = h("span", { class: "chip" + (filt.has(t) ? " active" : ""), onClick: () => { filt.has(t) ? filt.delete(t) : filt.add(t); c.classList.toggle("active"); render(); } }, t, h("span", { class: "n" }, albums.filter(a => (a.type || "other").toLowerCase() === t).length)); return c; })),
      h("span", { class: "ml-auto" }),
      can("operator") ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/follow", { id, name }); toast(r.added ? `Following ${name} — new releases auto-want` : "Already following", "ok"); }) }, "Follow") : null,
      can("operator") ? h("button", { class: "btn sm primary", onClick: action(null, async () => { if (!(await confirm({ title: `Request all ${[...filt].join("/")} releases?`, body: "Owned albums are skipped automatically.", ok: "Request" }))) return; const r = await api("/api/request_artist", { id, name, types: [...filt] }); toast(`${r.added} queued · ${r.skipped_owned} already owned`, "ok"); refreshStatusSoon(); }) }, "Request all") : null),
    list);
  render();
}


// ---------- library removal (journaled, undoable) ----------
// One dialog for every entry point: artist shelf, album drawer, track drawer,
// bulk selections. It previews the exact blast radius from /remove/plan,
// carries the plan fingerprint into /remove, and hands off to the job drawer.
export function removeDialog({ artists = [], albumIds = [], itemIds = [], title = "", after } = {}) {
  const body = h("div", { class: "stack" }, h("div", { class: "skel" }, h("div"), h("div"), h("div")));
  let plan = null;
  const block = h("input", { type: "checkbox" }), park = h("input", { type: "checkbox", checked: true }), unfollow = h("input", { type: "checkbox", checked: true }), huge = h("input", { type: "checkbox" });
  const reason = h("input", { class: "input sm", placeholder: "reason (optional, lands in the manifest + audit trail)", maxlength: 200 });
  const typed = h("input", { class: "input mono", placeholder: "" });
  const optRow = (inp, lbl, sub, on = true) => h("label", { class: "opt-row" + (on ? "" : " off") }, inp, h("div", {}, h("div", { class: "lbl" }, lbl), h("div", { class: "sub" }, sub)));
  const m = modal({ title: "Remove from library", wide: true, body, actions: [
    { label: "Remove", cls: "danger", keep: true, onClick: async (close) => {
      if (!plan || plan.empty) throw new Error("nothing to remove");
      if (plan.preflight && !plan.preflight.ok) throw new Error("storage is not ready in the beets container — " + plan.preflight.error);
      if (plan.typed_confirm) { const want = plan.artists.length === 1 ? plan.artists[0] : "REMOVE"; if (typed.value.trim() !== want) { toast(`type ${want} to confirm`, "warn"); typed.focus(); return; } }
      let r;
      try {
        r = await api("/api/library/remove", { artists, album_ids: albumIds, item_ids: itemIds, confirm: { fingerprint: plan.fingerprint, tracks: plan.tracks }, block: block.checked, drop_wants: park.checked, unfollow: unfollow.checked, reason: reason.value.trim(), allow_huge: huge.checked });
      } catch (e) { if (e.status === 409 && e.data.plan) { toast("The library changed — review the updated plan", "warn"); await load(); return; } throw e; }
      if (!r.ok) throw new Error(r.error);
      close();
      const side = r.side || {};
      toast(`Removal started${side.wants_parked ? ` · ${side.wants_parked} wants parked until it finishes` : ""}`, "ok");
      refreshStatusSoon(); openJob(r.job.id);
      // The job moves files + drops rows in the beets container; the list
      // underneath only reflects that once it has finished. Refresh then,
      // not now (the old code refreshed immediately and showed stale rows).
      waitForJob(r.job.id).then((res) => {
        const j = res.job || {}, out = res.result || {};
        const good = out.ok === true || j.exit_code === 0 || j.status === "ok" || j.status === "done";
        toast(good ? `Removal finished · ${fmt.num(out.moved ?? 0)} files → quarantine · ${fmt.num(out.albums_removed ?? 0)} albums${out.errors ? ` · ${out.errors} errors` : ""}` : `Removal ended: ${j.status || "unknown"} — check the job log`, good && !out.errors ? "ok" : "warn", 7000);
        refreshStatusSoon(); after && after(r, res);
      });
    } }] });
  async function load() {
    try { plan = await api("/api/library/remove/plan", { artists, album_ids: albumIds, item_ids: itemIds }); }
    catch (e) { mount(body, h("div", { class: "notice bad" }, e.message)); return; }
    if (plan.empty) { mount(body, h("div", { class: "empty" }, h("div", { class: "big" }, "∅"), "Nothing matched — the library may have changed.")); return; }
    const artistLevel = artists.length > 0;
    block.checked = artistLevel && !plan.blocked.length;
    const singleArtist = plan.artists.length === 1;
    typed.placeholder = `type ${singleArtist ? plan.artists[0] : "REMOVE"} to confirm`;
    const stat = (k, v, d) => h("div", { class: "stat" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v), d ? h("div", { class: "d" }, d) : null);
    mount(body,
      title ? h("div", { class: "b" }, title) : null,
      h("div", { class: "hint" }, "Files ", h("b", {}, "move"), " to ", store.status?.quarantine_dir ? h("code", { class: "mono" }, store.status.quarantine_dir + "/library_prune/<run>/") : "the quarantine library_prune directory", " and their beets rows are dropped through the Library API. Every file, row and attribute is journaled, so the removal can be ", h("b", {}, "undone"), " from Library → Removed. Nothing is deleted."),
      h("div", { class: "plan-stats" },
        stat("Tracks", fmt.num(plan.tracks), plan.lossless ? `${fmt.num(plan.lossless)} lossless` : ""),
        stat("Albums", fmt.num(plan.album_rows), plan.partial_albums ? `+${plan.partial_albums} partial` : "whole rows"),
        stat("Size", "~" + fmt.bytes(plan.est_bytes), "estimated"),
        stat("Artists", fmt.num(plan.artists.length), plan.artists.slice(0, 2).join(", ") + (plan.artists.length > 2 ? "…" : ""))),
      h("div", { class: "plan-list list" }, ...plan.albums.map(a => h("div", { class: "item" }, cover(a.has_art && a.album_id ? libArtUrl(a.album_id) : (a.rgid ? artUrl(a.rgid) : ""), {}), h("div", { class: "main" }, h("div", { class: "t" }, a.album || "(no album)"), h("div", { class: "s" }, a.artist, a.year ? ` · ${a.year}` : "")), h("div", { class: "end" }, h("span", { class: "badge " + (a.full ? "bad" : "warn") }, a.full ? `${a.tracks} tracks · whole album` : `${a.tracks} of ${a.total} tracks`)))),
        plan.albums_total > plan.albums.length ? h("div", { class: "item muted small" }, `… ${plan.albums_total - plan.albums.length} more albums`) : null),
      plan.too_many ? h("div", { class: "notice warn" }, h("div", { class: "body" }, h("b", {}, `${fmt.num(plan.tracks)} tracks. `), "That is above the safety cap — tick the override below if this is really intended.")) : null,
      !plan.console ? h("div", { class: "notice bad" }, h("div", { class: "body" }, "The beets console is offline (docker socket not mounted) — removal jobs cannot start.")) : null,
      plan.preflight && !plan.preflight.ok ? h("div", { class: "notice bad" }, h("div", { class: "body" }, h("b", {}, "Storage is not ready in the beets container. "), plan.preflight.error, " — the job would refuse. Check your volume mount in the beets container and retry.")) : null,
      plan.blocked.length ? h("div", { class: "notice info" }, h("div", { class: "body" }, "Already on the import blocklist: ", plan.blocked.join(", "))) : null,
      h("div", { class: "stack", style: { gap: "6px" } },
        optRow(block, `Block ${singleArtist ? plan.artists[0] : "these artists"} from future imports`, "Adds the exact albumartist name(s) to artist_blocklist.txt. The watcher routes future downloads to the quarantine directory and hunter stops wanting them.", true),
        plan.open_wants ? optRow(park, `Park ${plan.open_wants} open want${plan.open_wants > 1 ? "s" : ""}`, "Skips hunter wants for these artists/albums so nothing is re-downloaded while the files sit in quarantine.") : null,
        plan.followed?.length ? optRow(unfollow, `Unfollow ${plan.followed.join(", ")}`, "Stops the release watch for them.") : null,
        plan.too_many ? optRow(huge, "Override the size cap", `Allow more than ${fmt.num(5000)} tracks in one removal.`) : null),
      reason,
      plan.typed_confirm ? h("div", { class: "stack", style: { gap: "4px" } }, h("div", { class: "hint" }, "This is a large or artist-wide removal."), typed) : null);
    if (plan.typed_confirm) setTimeout(() => typed.focus(), 30);
  }
  load();
  return m;
}

// Block / Unblock toggle for one exact artist name. Blocking also parks wants.
export function blockButton(name, blocked, { cls = "btn sm", after } = {}) {
  if (!can("operator")) return blocked ? h("span", { class: "badge bad", title: "on the import blocklist" }, "blocked") : null;
  const b = h("button", { class: cls + (blocked ? " good" : " danger") }, blocked ? "Unblock" : "Block");
  b.onclick = action(b, async (e) => {
    e?.stopPropagation?.();
    if (!blocked) {
      if (!(await confirm({ title: `Block ${name}?`, body: "Future downloads tagged with this albumartist go to the quarantine directory instead of the library, and any open hunter wants for the artist are parked. Existing files stay.", ok: "Block", danger: true }))) return;
      const r = await api("/api/library/blocklist", { add: [name] });
      toast(r.added?.length ? `Blocked ${name}${r.wants_parked ? ` · ${r.wants_parked} wants parked` : ""}` : `${name} was already blocked`, "ok");
    } else {
      const r = await api("/api/library/blocklist", { remove: [name] });
      toast(r.removed?.length ? `Unblocked ${name}` : `${name} was not on the list`, "ok");
    }
    after && after(!blocked);
  });
  return b;
}

// Removal manifest drawer (files, rows, undo)
export async function openRemoval(id) {
  const d = drawer({ title: "Removal", wide: true, body: h("div", { class: "skel" }, h("div"), h("div")) });
  let r;
  try { r = await api(`/api/library/removal?id=${encodeURIComponent(id)}`); } catch (e) { mount(d.body, h("div", { class: "notice bad" }, e.message)); return; }
  d.setTitle(`Removal · ${r.artists.join(", ") || r.id}`);
  const op = can("operator");
  const undoBtn = r.undone_ts || !op ? null : h("button", { class: "btn sm primary", onClick: action(null, async () => {
    if (!(await confirm({ title: "Undo this removal?", body: `Moves ${fmt.num(r.tracks)} files back to their original paths (never overwriting) and re-inserts the exact beets rows${r.blocked?.length ? `, and unblocks ${r.blocked.join(", ")}` : ""}.`, ok: "Undo" }))) return;
    const j = await api("/api/library/removal/undo", { id: r.id }); if (!j.ok) throw new Error(j.error);
    toast("Undo started", "ok"); d.close(); openJob(j.job.id);
  }) }, "Undo removal");
  mount(d.body,
    h("div", { class: "row mb" }, r.undone_ts ? h("span", { class: "badge ok" }, `undone ${fmt.datetime(r.undone_ts)}`) : h("span", { class: "badge bad" }, "in quarantine"), r.errors ? h("span", { class: "badge warn" }, `${r.errors} errors`) : null, r.blocked?.length ? h("span", { class: "badge dim" }, `blocked: ${r.blocked.join(", ")}`) : null, h("span", { class: "muted small" }, `${fmt.datetime(r.ts)} · by ${r.requested_by || "?"}${r.reason ? ` · ${r.reason}` : ""}`), h("span", { class: "ml-auto" }), undoBtn),
    h("div", { class: "plan-stats mb" }, h("div", { class: "stat" }, h("div", { class: "k" }, "Files"), h("div", { class: "v" }, fmt.num(r.tracks))), h("div", { class: "stat" }, h("div", { class: "k" }, "Album rows"), h("div", { class: "v" }, fmt.num(r.albums))), h("div", { class: "stat" }, h("div", { class: "k" }, "Size"), h("div", { class: "v" }, fmt.bytes(r.bytes))), h("div", { class: "stat" }, h("div", { class: "k" }, "Extras"), h("div", { class: "v" }, fmt.num(r.extras)))),
    h("div", { class: "mono xs muted mb", style: { overflowWrap: "anywhere" } }, r.run_dir),
    r.errors_detail?.length ? h("div", { class: "notice bad mb" }, h("div", { class: "body" }, ...r.errors_detail.slice(0, 5).map(e => h("div", {}, e.error || JSON.stringify(e))))) : null,
    r.jobs?.length ? h("div", { class: "list card mb" }, ...r.jobs.map(j => h("div", { class: op ? "item click" : "item", onClick: op ? () => openJob(j.id) : null }, h("div", { class: "main" }, h("div", { class: "t small" }, j.title), h("div", { class: "s" }, fmt.datetime(j.started_ts))), h("div", { class: "end" }, statusBadge(j.status))))) : null,
    r.album_rows.length ? h("div", { class: "section-title" }, `Albums (${r.album_rows.length})`) : null,
    r.album_rows.length ? h("div", { class: "list card mb" }, ...r.album_rows.map(a => h("div", { class: "item" }, h("div", { class: "main" }, h("div", { class: "t small" }, `${a.artist} — ${a.album}`), h("div", { class: "s" }, a.year || "", a.art ? " · art moved" : ""))))) : null,
    h("div", { class: "section-title" }, `Files (${r.items.length})`),
    h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "Title"), h("th", {}, "Artist"), h("th", {}, "Album"), h("th", {}, "State"), h("th", {}, "Original path"))),
      h("tbody", {}, ...r.items.slice(0, 500).map(e => h("tr", {}, h("td", { class: "b" }, e.title || ""), h("td", { class: "muted" }, e.artist || ""), h("td", { class: "muted" }, e.album || ""), h("td", {}, e.error ? h("span", { class: "badge bad" }, "error") : e.moved ? h("span", { class: "badge ok" }, "moved") : h("span", { class: "badge dim" }, "row only")), h("td", { class: "mono xs muted", style: { overflowWrap: "anywhere" } }, e.src)))))));
}
