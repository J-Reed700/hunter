import { api, h, mount, fmt, toast, action, busy, sel, setQuery, setCrumbs, navigate, link, cover, artUrl, qs, confirm, refreshStatusSoon, setTopActions, promptText, ls, can, safeDecode, store } from "../app.js";
import { openAlbum, openTrack, qualityBadge, covBar, wantBadge, modifyDialog, openJob, openMbArtist, removeDialog, blockButton, openRemoval, queueDownload, createTabs, statCard, debouncedInput, confirmBeetsRun, coverArtUrl } from "./_shared.js";
import { libraryArtists, followedRoster } from "./artists.js";
import { facetBar, facetParams, groupView, applyGroupPick, selectAllButton } from "./_filters.js";

export default {
  async render(root, r) {
    if (r.path[0] === "album" && r.path[1]) {
      const aid = +r.path[1];
      r.path = [];
      history.replaceState(history.state, "", "#/library");
      openAlbum(aid);
    }
    if (r.path[0] === "artist" && r.path[1]) return artistShelf(root, r.path[1]);
    const defs = [["overview", "Overview"], ["artists", "Artists"], ["albums", "Albums"], ["tracks", "Tracks"], ["genres", "Genres"], ["duplicates", "Duplicates"], ["junk", "Junk"], ["health", "Health"], ["removed", "Removed"]];
    const tabCtl = createTabs(defs, r.q.tab || "overview", (tab, { alive, out }) => ({
      overview: () => overview(out, alive),
      artists: () => artistsTab(out, r, alive),
      albums: () => albumsView(out, r, alive),
      tracks: () => tracksView(out, r, alive),
      genres: () => genres(out, alive),
      duplicates: () => duplicatesView(out, r, alive),
      junk: () => junkView(out, r, alive),
      health: () => health(out, alive),
      removed: () => removed(out, alive),
    })[tab]());
    if (can("operator")) setTopActions(h("a", { class: "btn sm", href: "#/beets?tab=console" }, "Console"));
    mount(root, h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Library"), h("div", { class: "sub" }, "beets is the source of truth. Browse it, fix it, complete it — every action is a beet job you can watch."))), tabCtl.el, tabCtl.out);

    async function overview(out, alive) {
      mount(out, h("div", { class: "skel" }, h("div"), h("div"), h("div")));
      let o, fr;
      try { [o, fr] = await Promise.all([api("/api/library/overview"), api("/api/artists").catch(() => ({ artists: [] }))]); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
      if (!alive()) return;
      const following = (fr.artists || []).length;
      const max = Math.max(1, ...o.timeline.map(d => d.count));
      const fmtBar = (rows, key) => { const top = Math.max(1, ...rows.map(x => x.count)); return h("div", { class: "bars" }, ...rows.map(x => h("div", { class: "bar" }, h("span", { class: "lbl" }, x[key]), h("div", { class: "progress info" }, h("div", { style: { width: (100 * x.count / top) + "%" } })), h("span", { class: "n" }, fmt.num(x.count))))); };
      mount(out,
        h("div", { class: "stats mb" },
          statCard("Tracks", fmt.num(o.items), `${fmt.hours(o.hours)} · ~${fmt.bytes(o.est_bytes)}`, "#/library?tab=tracks"),
          statCard("Albums", fmt.num(o.albums), `${fmt.num(o.artists)} artists`, "#/library?tab=artists"),
          statCard("Following", fmt.num(following), following === 1 ? "artist watched for new releases" : "artists watched for new releases", "#/library?tab=artists&view=following"),
          statCard("Lossless", o.lossless_pct + "%", `${fmt.num(o.lossless)} tracks`, "#/library?tab=albums&filter=lossy"),
          statCard("Added", "+" + fmt.num(o.added["7d"]), `this week · +${o.added["24h"]} today · +${fmt.num(o.added["30d"])} this month`, "#/library?tab=albums&filter=recent&sort=added"),
          statCard("Incomplete", fmt.num(o.incomplete_albums), "albums missing tracks", "#/library?tab=albums&filter=incomplete&sort=coverage", o.incomplete_albums ? "warn" : ""),
          statCard("No art", fmt.num(o.no_art_albums), "albums without cover", "#/library?tab=albums&filter=noart", o.no_art_albums ? "warn" : "")),
        h("div", { class: "grid c2 mb" },
          h("div", { class: "card" }, h("h3", {}, "Additions · 30 days"), h("div", { class: "sparkline" }, ...o.timeline.map(d => h("div", { class: d.count ? "" : "zero", title: `${d.day}: ${d.count}`, style: { height: (100 * d.count / max) + "%" } })))),
          h("div", { class: "card" }, h("h3", {}, "Formats"), fmtBar(o.formats, "format"))),
        h("div", { class: "grid c2 mb" },
          h("div", { class: "card" }, h("h3", {}, "Top genres", h("a", { class: "right", href: "#/library?tab=genres" }, "all →")), fmtBar(o.genres.slice(0, 10), "genre")),
          h("div", { class: "card" }, h("h3", {}, "Decades"), fmtBar(o.decades.filter(d => d.decade >= 1900), "decade"))),
        h("div", { class: "section-title" }, "Recently added"),
        h("div", { class: "grid covers" }, ...o.recent.map(a => albumCard({ ...a, has_art: 1 }))));
    }

    async function genres(out, alive) {
      mount(out, h("div", { class: "skel" }, h("div"), h("div")));
      let g; try { g = await api("/api/library/genres?limit=400"); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
      if (!alive()) return;
      const gList = g.genres || [], tList = g.types || [];
      const max = Math.max(1, ...gList.map(x => x.items));
      const tMax = Math.max(1, tList[0]?.count || 1);
      mount(out, h("div", { class: "row mb" }, h("span", { class: "muted small" }, `${gList.length} genres`), can("operator") ? h("a", { class: "btn xs ml-auto", href: "#/beets?tab=scripts" }, "Normalize genres (script)") : null),
        h("div", { class: "grid c2" }, h("div", { class: "card" }, h("h3", {}, "Genres"), gList.length ? h("div", { class: "bars" }, ...gList.map(x => h("div", { class: "bar" }, h("a", { class: "lbl", href: link("library", { tab: "albums", genre: x.genre }) }, x.genre), h("div", { class: "progress info" }, h("div", { style: { width: (100 * x.items / max) + "%" } })), h("span", { class: "n" }, `${fmt.num(x.albums)} / ${fmt.num(x.items)}`)))) : h("div", { class: "empty small" }, "No genres yet.")),
          h("div", { class: "card" }, h("h3", {}, "Album types"), tList.length ? h("div", { class: "bars" }, ...tList.map(x => h("div", { class: "bar" }, h("a", { class: "lbl", href: link("library", { tab: "albums", type: x.type || "__none__" }) }, x.type || "(none)"), h("div", { class: "progress info" }, h("div", { style: { width: (100 * x.count / tMax) + "%" } })), h("span", { class: "n" }, fmt.num(x.count))))) : h("div", { class: "empty small" }, "No albums yet."))));
    }

    async function removed(out, alive) {
      mount(out, h("div", { class: "skel" }, h("div"), h("div")));
      let r; try { r = await api("/api/library/removals"); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
      if (!alive()) return;
      mount(out, h("div", { class: "hint mb" }, "Every removal made from hunter lands here as a run under ", store.status?.quarantine_dir ? h("code", { class: "mono" }, store.status.quarantine_dir + "/library_prune/") : "the quarantine library_prune directory", " with a manifest of files, rows and attributes. Undo puts everything back exactly; files are never deleted by hunter — reclaim space by hand once you are sure."),
        r.removals.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Artists"), h("th", { class: "num" }, "Tracks"), h("th", { class: "num" }, "Albums"), h("th", { class: "num" }, "Size"), h("th", {}, "By"), h("th", {}, "State"), h("th", {}))),
          h("tbody", {}, ...r.removals.map(x => h("tr", { class: "click", onClick: () => x.broken ? toast("manifest unreadable", "bad") : openRemoval(x.id) }, h("td", { class: "muted small nowrap" }, x.broken ? x.id : fmt.datetime(x.ts)), h("td", { class: "b" }, x.broken ? "(broken manifest)" : (x.artists || []).join(", ") || x.id), h("td", { class: "num" }, x.tracks ?? ""), h("td", { class: "num" }, x.albums ?? ""), h("td", { class: "num" }, x.bytes ? fmt.bytes(x.bytes) : ""), h("td", { class: "muted small" }, x.requested_by || ""), h("td", {}, x.broken ? h("span", { class: "badge bad" }, "broken") : x.undone_ts ? h("span", { class: "badge ok" }, "undone") : !x.complete ? h("span", { class: "badge warn" }, "incomplete") : x.errors ? h("span", { class: "badge warn" }, `${x.errors} errors`) : h("span", { class: "badge dim" }, "quarantined"), x.blocked?.length ? h("span", { class: "badge bad", style: { marginLeft: "4px" } }, "blocked") : null),
            h("td", {}, h("div", { class: "actions" }, !x.broken && !x.undone_ts && can("operator") ? h("button", { class: "btn xs", onClick: action(null, async (e) => { e.stopPropagation(); if (!(await confirm({ title: "Undo this removal?", body: `Restores ${x.tracks} files and their beets rows${x.blocked?.length ? ` and unblocks ${x.blocked.join(", ")}` : ""}.`, ok: "Undo" }))) return; const j = await api("/api/library/removal/undo", { id: x.id }); if (!j.ok) throw new Error(j.error); toast("Undo started", "ok"); openJob(j.job.id); }) }, "Undo") : null)))))))
        : h("div", { class: "empty" }, h("div", { class: "big" }, "✓"), "No removals yet. Remove an artist from its shelf, an album from its page, or a selection of tracks."));
    }

    async function health(out, alive) {
      mount(out, h("div", { class: "skel" }, h("div"), h("div")));
      let o; try { o = await api("/api/library/overview"); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
      if (!alive()) return;
      const op = can("operator");
      const card = (title, n, blurb, href, actions) => h("div", { class: "card" + (n ? " warn" : "") }, h("h3", {}, title, h("span", { class: "right" }, fmt.num(n))), h("div", { class: "hint mb" }, blurb), h("div", { class: "row" }, h("a", { class: "btn sm", href }, "Browse"), ...(op && actions ? actions : [])));
      const script = (id, label, apply = false) => h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/beets/scripts/run", { id, apply }); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); }) }, label);
      const hunterProg = (prog, label) => h("button", { class: "btn sm primary", onClick: action(null, async () => { if (await confirm({ title: `Seed ${prog} wants?`, body: "Creates one want per matching album. Pause/clear anytime in Settings → Automation.", ok: "Seed" })) { const r = await api("/api/automation/program", { program: prog, action: "seed" }); toast(`${r.seeded} new wants`, "ok"); refreshStatusSoon(); } }) }, label);
      mount(out, h("div", { class: "grid c2" },
        card("Incomplete albums", o.incomplete_albums, "Fewer tracks than the album's tracktotal. Complete one from its page, or seed them all as gap-fill wants.", "#/library?tab=albums&filter=incomplete&sort=coverage", [hunterProg("gapfill", "Seed gap-fill")]),
        card("Lossy-only albums", o.lossy_albums, "No lossless track at all. Upgrade wants only ever accept lossless candidates; nightly dedup keeps the best master.", "#/library?tab=albums&filter=lossy", [hunterProg("upgrade", "Seed upgrades")]),
        card("Missing cover art", o.no_art_albums, "Albums with no artpath. The backfill script fetches + embeds, time-boxed, never overwriting existing art.", "#/library?tab=albums&filter=noart", [script("fix_missing_art", "Dry-run backfill"), script("fix_missing_art", "Run backfill", true)]),
        card("Albums without MusicBrainz id", o.no_mbid_albums, "Unverified imports — mbsync and gap-fill can't reason about them. Try mbsync on one, or re-import via the resolver.", "#/library?tab=albums&filter=nombid"),
        card("Single-track albums", null, "Usually orphan rows or singletons that slipped past the no-singletons rule.", "#/library?tab=albums&filter=onetrack", [script("cleanup_lonetrack_comps", "Clean lone-track comps (dry)")]),
        card("Mixed-quality albums", null, "Lossless and lossy tracks in one album — dedup_masters collapses duplicate masters.", "#/library?tab=albums&filter=mixed", [script("dedup_masters", "Dry-run dedup")])));
    }
    tabCtl.go();
  },
};

export function albumCard(a) {
  const w = a.wants || {};
  return h("div", { class: "cover-card", onClick: () => openAlbum(a.id) },
    cover(coverArtUrl(a), { size: "xxl" }),
    h("div", { class: "t", title: a.album }, a.album), h("div", { class: "s" }, a.artist, a.year ? ` · ${a.year}` : ""),
    h("div", { class: "m" }, a.tracktotal && a.tracks < a.tracktotal ? h("span", { class: "badge warn" }, `${a.tracks}/${a.tracktotal}`) : null, a.lossless_share !== undefined ? qualityBadge(a) : null, w.gapfill ? wantBadge(w.gapfill) : null, w.upgrade ? wantBadge(w.upgrade) : null));
}

export async function albumsView(out, r, alive = () => out.isConnected) {
  const st = { q: r.q.q || "", sort: r.q.sort || "added", filter: r.q.filter || "", genre: r.q.genre || "", decade: r.q.decade || "", type: r.q.type || "", fmt: r.q.fmt || "", added: r.q.added || "", artist: r.q.artist || "", year: r.q.year || "", label: r.q.label || "", group: r.q.group || "", letter: r.q.letter || "" };
  let view = ls.get("lib_view", "grid"), offset = 0, rows = [], total = 0, loadGen = 0;
  const list = h("div", {});
  const beetSel = (act, label) => ({ label, role: "operator", onClick: async (items) => { const res = await confirmBeetsRun({ command: act, query: items.map(a => `album_id:${a.id}`).join(" , "), apply: true, confirm_count: items.length }, label); if (!res) return; if (res.ok) { toast(`${act} started on ${items.length} albums`, "ok"); openJob(res.job.id); } else toast(res.error, "bad"); } });
  sel.configure("albums", [
    { label: "Download", onClick: async (items) => { await queueDownload({ title: items.length === 1 ? `${items[0].artist} — ${items[0].album}` : `${items.length} albums`, album_ids: items.map(a => a.id) }); sel.clear(); } },
    { label: "Complete", role: "operator", onClick: async (items) => { let n = 0; for (const a of items) { const r = await api("/api/library/want", { album_id: a.id, kind: "gapfill" }); n += r.added ? 1 : 0; } toast(`${n} gap-fill wants`, "ok"); sel.clear(); refreshStatusSoon(); load(); } },
    { label: "Upgrade", role: "operator", onClick: async (items) => { let n = 0; for (const a of items) { const r = await api("/api/library/want", { album_id: a.id, kind: "upgrade" }); n += r.added ? 1 : 0; } toast(`${n} upgrade wants`, "ok"); sel.clear(); refreshStatusSoon(); load(); } },
    beetSel("fetchart", "Fetch art"), beetSel("mbsync", "MB sync"), beetSel("write", "Write tags"),
    { label: "Edit tags…", role: "operator", onClick: async (items) => modifyDialog({ query: items.map(a => `album_id:${a.id}`).join(" , "), album: true, after: load }) },
    { label: "Remove…", cls: "danger", role: "operator", onClick: (items) => removeDialog({ albumIds: items.map(a => a.id), title: `${items.length} selected albums`, after: () => { sel.clear(); load(); } }) },
  ]);
  const bar = facetBar(st, {
    search: "album / artist",
    selects: { genre: true, decade: true, type: true, fmt: true, added: true },
    chips: [["recent", "Recent"], ["incomplete", "Incomplete", ["complete"]], ["complete", "Complete", ["incomplete"]], ["lossless", "Lossless", ["lossy", "mixed"]], ["lossy", "Lossy-only", ["lossless", "mixed"]], ["mixed", "Mixed", ["lossless", "lossy"]], ["hires", "Hi-res"], ["noart", "No art", ["art"]], ["nombid", "No MBID", ["mbid"]], ["onetrack", "1-track"], ["empty", "Empty"], ["comp", "Compilation"], ["multidisc", "Multi-disc"], ["nogenre", "No genre"], ["noyear", "No year"]],
    sorts: [["added", "Recently added"], ["artist", "Artist"], ["album", "Album"], ["year", "Newest"], ["year_asc", "Oldest"], ["genre", "Genre"], ["tracks", "Most tracks"], ["length", "Longest"], ["coverage", "Least complete"], ["quality", "Lowest quality"], ["bitrate", "Highest bitrate"]],
    groups: [["genre", "Group by genre"], ["decade", "Group by decade"], ["year", "Group by year"], ["type", "Group by type"], ["quality", "Group by quality"], ["format", "Group by format"], ["artist", "Group by artist"], ["label", "Group by label"], ["letter", "Group by letter"]],
  }, () => load());
  const ctx = h("div", { class: "row between mb" });
  mount(out, bar.el, ctx, list);
  async function load(more = false) {
    const myGen = ++loadGen;
    bar.paintActive();
    if (st.group) { mount(ctx); await groupView(list, { base: "/api/library/albums", state: st, unit: "albums", onPick: (k) => { if (k !== null) applyGroupPick(st, k); bar.rebuild(); load(); } }); return; }
    const nextOffset = more ? offset + 60 : 0;
    let res;
    try { res = await api(`/api/library/albums${qs({ ...facetParams(st), offset: nextOffset })}`); } catch (e) { if (myGen === loadGen && alive()) mount(list, h("div", { class: "notice bad" }, e.message)); return; }
    if (myGen !== loadGen || !alive()) return;
    offset = nextOffset;
    rows = more ? rows.concat(res.albums) : res.albums; total = res.total; paint();
  }
  function paint() {
    mount(ctx, h("span", { class: "muted small" }, `${fmt.num(rows.length)} of ${fmt.num(total)} albums`), h("span", { class: "ml-auto" }),
      rows.length ? h("button", { class: "btn xs ghost", onClick: () => { rows.forEach(a => sel.set(a.id, a, true)); paint(); } }, "Select page") : null,
      selectAllButton({ total, url: `/api/library/albums/ids${qs(facetParams(st))}`, key: "albums", map: (a) => ({ key: a.id, item: a }) }),
      h("div", { class: "seg sm" }, h("button", { class: view === "grid" ? "active" : "", onClick: () => { view = "grid"; ls.set("lib_view", view); paint(); } }, "▦"), h("button", { class: view === "list" ? "active" : "", onClick: () => { view = "list"; ls.set("lib_view", view); paint(); } }, "☰")));
    const more = rows.length < total ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, `Load more (${fmt.num(total - rows.length)} left)`) : null;
    if (!rows.length) { mount(list, h("div", { class: "empty" }, "No albums match.")); return; }
    if (view === "grid") { mount(list, h("div", { class: "grid covers" }, ...rows.map(a => { const c = albumCard(a); const chk = h("span", { class: "check" + (sel.has(a.id) ? " on" : ""), style: { position: "absolute", top: "6px", left: "6px", background: "rgba(0,0,0,.5)" }, onClick: (e) => { e.stopPropagation(); chk.classList.toggle("on", sel.toggle(a.id, a)); } }, "✓"); c.style.position = "relative"; c.prepend(chk); return c; })), more); return; }
    mount(list, h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}), h("th", {}, ""), h("th", {}, "Album"), h("th", {}, "Artist"), h("th", { class: "num" }, "Year"), h("th", {}, "Tracks"), h("th", {}, "Quality"), h("th", {}, "Genre"), h("th", {}, "Added"), h("th", {}))),
      h("tbody", {}, ...rows.map(a => { const chk = h("span", { class: "check" + (sel.has(a.id) ? " on" : ""), onClick: (e) => { e.stopPropagation(); chk.classList.toggle("on", sel.toggle(a.id, a)); } }, "✓");
        return h("tr", { class: "click", onClick: () => openAlbum(a.id) }, h("td", {}, chk), h("td", {}, cover(coverArtUrl(a), {})), h("td", { class: "b" }, a.album, a.albumtype && a.albumtype !== "album" ? h("span", { class: "badge dim", style: { marginLeft: "6px" } }, a.albumtype) : null),
          h("td", { class: "muted" }, h("a", { href: link("library", {}, "artist", a.artist), onClick: (e) => e.stopPropagation() }, a.artist)), h("td", { class: "num" }, a.year || ""), h("td", {}, covBar(a.tracks, a.tracktotal)), h("td", {}, qualityBadge(a)),
          h("td", { class: "muted small" }, a.genre ? h("a", { href: link("library", { tab: "albums", genre: a.genre }), onClick: (e) => e.stopPropagation() }, a.genre) : ""), h("td", { class: "muted small nowrap" }, fmt.ago(a.added_ts)),
          h("td", {}, h("div", { class: "actions" }, h("button", { class: "btn xs", title: "download album as zip", onClick: action(null, async (e) => { e.stopPropagation(); await queueDownload({ title: `${a.artist} — ${a.album}`, album_ids: [a.id] }); }) }, "⤓")))); })))), more);
  }
  await load();
}

// ---------------- tracks ----------------
async function tracksView(out, r, alive = () => out.isConnected) {
  const st = { q: r.q.q || "", fmt: r.q.fmt || "", sort: r.q.sort || "added", filter: r.q.filter || "", genre: r.q.genre || "", decade: r.q.decade || "", added: r.q.added || "", artist: r.q.artist || "", year: r.q.year || "", min_kbps: r.q.min_kbps || "", max_kbps: r.q.max_kbps || "", group: r.q.group || "" };
  let offset = 0, rows = [], total = 0, loadGen = 0;
  const list = h("div", {});
  sel.configure("tracks", [
    { label: "Download", onClick: async (items) => { await queueDownload({ title: items.length === 1 ? `${items[0].artist} - ${items[0].title}` : `${items.length} tracks`, item_ids: items.map(t => t.id) }); sel.clear(); } },
    { label: "Edit tags…", role: "operator", onClick: async (items) => modifyDialog({ itemIds: items.map(t => t.id), after: load }) },
    { label: "Write tags", role: "operator", onClick: async (items) => { const res = await confirmBeetsRun({ command: "write", query: items.map(t => `id:${t.id}`).join(" , "), apply: true, confirm_count: items.length }, "Write tags"); if (!res) return; if (res.ok) openJob(res.job.id); else toast(res.error, "bad"); } },
    { label: "→ Navidrome playlist", role: "operator", onClick: async (items) => { const name = await promptText({ title: "Navidrome playlist name", value: "hunter picks" }); if (!name) return; const res = await api("/api/make_playlist", { name, tracks: items.map(t => ({ title: t.title, artist: t.artist })) }); toast(`${res.matched}/${res.total} matched → “${res.name || name}”`, "ok"); sel.clear(); } },
    { label: "Remove…", cls: "danger", role: "operator", onClick: (items) => removeDialog({ itemIds: items.map(t => t.id), title: `${items.length} selected tracks`, after: () => { sel.clear(); load(); } }) },
  ]);
  const bar = facetBar(st, {
    search: "artist / title / album",
    selects: { genre: true, decade: true, fmt: true, added: true },
    chips: [["short", "Short (<45s)"], ["long", "Long (>20m)"], ["lowbit", "<128 kbps"], ["hires", "Hi-res"], ["nombid", "No MBID"], ["untitled", "Untitled/unknown"], ["nogenre", "No genre"], ["lone", "Lone track"], ["lone_va", "Lone V/A"], ["noalbum", "No album"], ["nofingerprint", "No fingerprint"], ["nobpm", "No BPM"], ["nolyrics", "No lyrics"], ["notrack", "No track #"]],
    sorts: [["added", "Recently added"], ["artist", "Artist"], ["album", "Album"], ["title", "Title"], ["year", "Year"], ["genre", "Genre"], ["bitrate", "Highest bitrate"], ["bitrate_asc", "Lowest bitrate"], ["length", "Longest"], ["length_asc", "Shortest"], ["bpm", "BPM"]],
    groups: [["genre", "Group by genre"], ["format", "Group by format"], ["kbps", "Group by bitrate"], ["decade", "Group by decade"], ["year", "Group by year"], ["artist", "Group by artist"]],
  }, () => load());
  const ctx = h("div", { class: "row between mb" });
  mount(out, bar.el, ctx, list);
  async function load(more = false) {
    const myGen = ++loadGen;
    bar.paintActive();
    if (st.group) { mount(ctx); await groupView(list, { base: "/api/library/tracks", state: st, unit: "tracks", onPick: (k) => { if (k !== null) applyGroupPick(st, k); bar.rebuild(); load(); } }); return; }
    const nextOffset = more ? offset + 100 : 0;
    let res;
    try { res = await api(`/api/library/tracks${qs({ ...facetParams(st), offset: nextOffset })}`); } catch (e) { if (myGen === loadGen && alive()) mount(list, h("div", { class: "notice bad" }, e.message)); return; }
    if (myGen !== loadGen || !alive()) return;
    offset = nextOffset;
    rows = more ? rows.concat(res.tracks) : res.tracks; total = res.total;
    mount(ctx, h("span", { class: "muted small" }, `${fmt.num(rows.length)} of ${fmt.num(total)} tracks`), h("span", { class: "ml-auto" }),
      rows.length ? h("button", { class: "btn xs ghost", onClick: () => { rows.forEach(t => sel.set(t.id, t, true)); list.querySelectorAll(".check:not([data-local])").forEach(c => c.classList.add("on")); } }, "Select page") : null,
      selectAllButton({ total, url: `/api/library/tracks/ids${qs(facetParams(st))}`, key: "tracks", map: (t) => ({ key: t.id, item: t }) }));
    mount(list, h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}), h("th", {}, "Title"), h("th", {}, "Artist"), h("th", {}, "Album"), h("th", { class: "num" }, "Year"), h("th", {}, "Format"), h("th", { class: "num" }, "kbps"), h("th", { class: "num" }, "Time"), h("th", {}, "Genre"), h("th", {}))),
      h("tbody", {}, ...rows.map(t => { const chk = h("span", { class: "check" + (sel.has(t.id) ? " on" : ""), onClick: (e) => { e.stopPropagation(); chk.classList.toggle("on", sel.toggle(t.id, t)); } }, "✓");
        return h("tr", { class: "click", onClick: () => openTrack(t.id) }, h("td", {}, chk), h("td", { class: "b" }, t.title || h("span", { class: "muted" }, "(untitled)")), h("td", { class: "muted" }, t.artist), h("td", {}, h("a", { href: "#", onClick: (e) => { e.preventDefault(); e.stopPropagation(); openAlbum(t.album_id); } }, t.album)), h("td", { class: "num" }, t.year || ""),
          h("td", {}, h("span", { class: "badge " + (t.lossless ? "ok" : "dim") }, (t.format || "?").toLowerCase(), t.bitdepth > 16 ? ` ${t.bitdepth}b` : "")), h("td", { class: "num" }, t.kbps || ""), h("td", { class: "num" }, fmt.mmss(t.secs)),
          h("td", { class: "muted small" }, t.genre || ""), h("td", {}, h("div", { class: "actions" }, h("a", { class: "btn xs", href: `/api/dl/track?id=${encodeURIComponent(t.id)}`, download: "", title: "download this file", onClick: (e) => e.stopPropagation() }, "⤓")))); })))),
      rows.length < total ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, `Load more (${fmt.num(total - rows.length)} left)`) : null,
      !rows.length ? h("div", { class: "empty" }, "No tracks match.") : null);
  }
  await load();
}

// ---------------- duplicates ----------------
async function duplicatesView(out, r, alive = () => out.isConnected) {
  const st = { q: r.q.q || "", scope: r.q.scope || "all", sort: r.q.sort || "waste" };
  let offset = 0, groups = [], total = 0, extra = 0, loadGen = 0;
  const keep = new Map();      // group key → item id to keep
  const include = new Set();   // group keys whose non-kept copies are queued for removal
  const list = h("div", {}); const ctx = h("div", { class: "row between mb" });
  const input = debouncedInput({ placeholder: "artist / title / album", value: st.q, delay: 320, onInput: (v) => { st.q = v; setQuery({ q: st.q }); load(); } });
  const scopeSel = h("select", { class: "select sm", onChange: (e) => { st.scope = e.target.value; setQuery({ scope: st.scope }); load(); } }, ...[["all", "Everywhere"], ["same_album", "Within one album (true dupes)"], ["cross_album", "Across albums (comps, reissues)"]].map(([v, l]) => h("option", { value: v, selected: st.scope === v }, l)));
  const sortSel = h("select", { class: "select sm", onChange: (e) => { st.sort = e.target.value; setQuery({ sort: st.sort }); load(); } }, ...[["waste", "Most wasted space"], ["count", "Most copies"], ["artist", "Artist"]].map(([v, l]) => h("option", { value: v, selected: st.sort === v }, l)));
  const pending = () => { const ids = []; for (const g of groups) if (include.has(g.key)) for (const t of g.tracks) if (t.id !== keep.get(g.key)) ids.push(t.id); return ids; };
  const bulk = h("div", { class: "row" });
  const paintBulk = () => { const ids = pending(); mount(bulk, h("span", { class: "muted small" }, `${include.size} groups marked · ${fmt.num(ids.length)} copies to remove`),
    h("button", { class: "btn xs ghost", onClick: () => { groups.forEach(g => include.add(g.key)); paint(); } }, "Mark all loaded"), h("button", { class: "btn xs ghost", onClick: () => { include.clear(); paint(); } }, "Unmark"),
    ids.length && can("operator") ? h("button", { class: "btn sm danger", onClick: () => removeDialog({ itemIds: ids, title: `${ids.length} duplicate copies`, after: () => { include.clear(); load(); } }) }, `Remove ${fmt.num(ids.length)} copies…`) : null); };
  mount(out, h("div", { class: "hint mb" }, "Same recording, more than one file. Identity is the MusicBrainz recording id when tagged, otherwise normalized artist + title + length. The best copy (lossless › bit depth › sample rate › bitrate › oldest) is pre-selected as the keeper — change it per group, mark the groups you want cleaned, then remove the other copies through the journaled path (undoable)."),
    h("div", { class: "filterbar" }, input, scopeSel, sortSel), ctx, bulk, list);
  const q = (t) => `${t.format ? t.format.toLowerCase() : "?"}${t.bitdepth > 16 ? ` ${t.bitdepth}b` : ""}${t.samplerate > 48000 ? ` ${(t.samplerate / 1000).toFixed(1)}k` : ""}${!t.lossless && t.kbps ? ` ${t.kbps} kbps` : ""}`;
  async function load(more = false) {
    const myGen = ++loadGen;
    const nextOffset = more ? offset + 40 : 0;
    let res;
    try { res = await api(`/api/library/duplicates${qs({ q: st.q, scope: st.scope, sort: st.sort, offset: nextOffset, limit: 40 })}`); } catch (e) { if (myGen === loadGen && alive()) mount(list, h("div", { class: "notice bad" }, e.message)); return; }
    if (myGen !== loadGen || !alive()) return;
    offset = nextOffset;
    groups = more ? groups.concat(res.groups) : res.groups; total = res.total; extra = res.extra_tracks;
    if (!more) {
      const keys = new Set(groups.map(g => g.key));
      for (const k of [...include]) if (!keys.has(k)) include.delete(k);
    }
    for (const g of groups) if (!keep.has(g.key)) keep.set(g.key, g.suggest_keep);
    paint();
  }
  function paint() {
    mount(ctx, h("span", { class: "muted small" }, `${fmt.num(groups.length)} of ${fmt.num(total)} groups · ${fmt.num(extra)} surplus copies in this scope`));
    paintBulk();
    mount(list, ...groups.map(g => {
      const mark = h("span", { class: "check" + (include.has(g.key) ? " on" : ""), dataset: { local: "1" }, title: "mark this group for cleanup", onClick: () => { include.has(g.key) ? include.delete(g.key) : include.add(g.key); mark.classList.toggle("on", include.has(g.key)); paintBulk(); } }, "✓");
      return h("div", { class: "card tight mb dup-group" },
        h("div", { class: "row" }, mark, h("div", { class: "b ellip", style: { flex: 1 } }, `${g.tracks[0]?.artist || ""} — ${g.tracks[0]?.title || ""}`), h("span", { class: "badge " + (g.by === "mbid" ? "ok" : "dim"), title: g.by === "mbid" ? "matched by MusicBrainz recording id" : "matched by tags" }, g.by), h("span", { class: "badge acc" }, `${g.count} copies`), h("span", { class: "muted xs" }, `~${fmt.bytes(g.waste_bytes)} surplus`)),
        h("div", { class: "list" }, ...g.tracks.map(t => { const isKeep = keep.get(g.key) === t.id;
          return h("div", { class: "item dup-row" + (isKeep ? " keep" : ""), onClick: () => { keep.set(g.key, t.id); paint(); } },
            h("span", { class: "radio" + (isKeep ? " on" : ""), title: "keep this copy" }),
            h("div", { class: "main" }, h("div", { class: "t small" }, h("a", { href: "#", onClick: (e) => { e.preventDefault(); e.stopPropagation(); openAlbum(t.album_id); } }, t.album || "(no album)"), t.year ? h("span", { class: "muted" }, ` · ${t.year}`) : null, t.comp ? h("span", { class: "badge dim", style: { marginLeft: "6px" } }, "comp") : null, t.albumtype && t.albumtype !== "album" ? h("span", { class: "badge dim", style: { marginLeft: "6px" } }, t.albumtype) : null),
              h("div", { class: "s mono xs" }, t.path)),
            h("div", { class: "end" }, h("span", { class: "badge " + (t.lossless ? "ok" : "dim") }, q(t)), h("span", { class: "muted xs" }, fmt.mmss(t.secs)), h("button", { class: "btn xs ghost", onClick: (e) => { e.stopPropagation(); openTrack(t.id); } }, "…"))); })));
    }), groups.length < total ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, `Load more (${fmt.num(total - groups.length)} groups left)`) : null, !groups.length ? h("div", { class: "empty" }, h("div", { class: "big" }, "✓"), "No duplicates in this scope.") : null);
  }
  await load();
}

// ---------------- junk ----------------
async function junkView(out, r, alive = () => out.isConnected) {
  mount(out, h("div", { class: "skel" }, h("div"), h("div"), h("div")));
  let j; try { j = await api("/api/library/junk"); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
  if (!alive()) return;
  const op = can("operator");
  const script = (id, label, apply = false) => h("button", { class: "btn xs", onClick: action(null, async (e) => { e.stopPropagation(); const r = await api("/api/beets/scripts/run", { id, apply }); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); }) }, label);
  const card = (title, n, blurb, href, extra) => h("div", {
    class: "card tight junk-card" + (n ? " warn" : ""),
    onClick: (e) => { if (!e.target.closest("button, a")) navigate(href); },
  },
    h("a", { class: "row between", href }, h("div", { class: "b" }, title), h("span", { class: "badge " + (n ? "warn" : "ok") }, fmt.num(n))),
    blurb ? h("div", { class: "hint" }, blurb) : null,
    op && extra ? h("div", { class: "row", style: { marginTop: "6px" } }, extra) : null);
  const T = (f, sort) => link("library", { tab: "tracks", filter: f, sort: sort || "" }), A = (f, sort) => link("library", { tab: "albums", filter: f, sort: sort || "" }), R = (f) => link("library", { tab: "artists", filter: f });
  mount(out, h("div", { class: "hint mb" }, "Every signal that usually means junk, with a count. Each card opens the matching filtered list where you can select all and remove (journaled, undoable) — nothing here deletes by itself."),
    h("div", { class: "section-title" }, "Duplicates"),
    h("div", { class: "grid auto mb" }, card("Duplicate recordings", j.duplicates.extra_tracks, `${fmt.num(j.duplicates.groups)} groups · surplus copies of the same recording`, link("library", { tab: "duplicates" }))),
    h("div", { class: "section-title" }, "Tracks"),
    h("div", { class: "grid auto mb" },
      card("Untitled / unknown", j.tracks.untitled, "Empty or placeholder title/artist.", T("untitled")),
      card("Very short", j.tracks.short, "Under 45 seconds — interludes, silence, samples.", T("short", "length_asc")),
      card("Low bitrate", j.tracks.lowbit, "Lossy under 128 kbps.", T("lowbit", "bitrate_asc")),
      card("Lone Various-Artists tracks", j.tracks.lone_va, "A 1-track compilation row — the classic resolver artifact.", T("lone_va"), script("cleanup_lonetrack_comps", "Clean (dry-run)")),
      card("No album row", j.tracks.noalbum, "Items whose album row is gone.", T("noalbum"), script("prune_orphan_rows", "Prune orphan rows (dry)")),
      card("No MusicBrainz id", j.tracks.nombid, "Unverified tags — mbsync can't help until matched.", T("nombid")),
      card("No fingerprint", j.tracks.nofingerprint, "Never fingerprinted; duplicate detection relies on tags for these.", T("nofingerprint"))),
    h("div", { class: "section-title" }, "Albums"),
    h("div", { class: "grid auto mb" },
      card("Single-track albums", j.albums.onetrack, "Usually orphan rows or singles that slipped the no-singletons rule.", A("onetrack")),
      card("Empty album rows", j.albums.empty, "Album rows with zero items.", A("empty"), script("prune_orphan_rows", "Prune (dry)")),
      card("Incomplete", j.albums.incomplete, "Fewer tracks than tracktotal.", A("incomplete", "coverage")),
      card("No MusicBrainz id", j.albums.nombid, "Unverified imports.", A("nombid")),
      card("No cover art", j.albums.noart, "No artpath on the row.", A("noart"), script("fix_missing_art", "Backfill (dry)")),
      card("No year", j.albums.noyear, "", A("noyear")), card("No genre", j.albums.nogenre, "", A("nogenre"))),
    h("div", { class: "section-title" }, "Artists"),
    h("div", { class: "grid auto mb" },
      card("One-track artists", j.artists.one_track, "An artist that exists because of a single stray file.", R("one_track")),
      card("No MusicBrainz artist id", j.artists.noartistid, "", R("noartistid"))),
    op ? h("div", { class: "row" }, script("sweep_empty_dirs", "Sweep empty dirs (dry)"), script("dedup_masters", "Collapse duplicate masters (dry)"), h("a", { class: "btn xs ghost", href: "#/beets?tab=scripts" }, "All scripts")) : null);
}

// Library → Artists: everyone in beets, or just the ones hunter watches for
// new releases. The roster used to live only under Artists → Followed, which
// is behind "More" on a phone — nobody found it.
async function artistsTab(out, r, alive = () => out.isConnected) {
  let view = r.q.view === "following" ? "following" : "all";
  const seg = h("div", { class: "seg mb" });
  const body = h("div", {});
  const paint = () => mount(seg, ...[["all", "All artists"], ["following", "Following"]].map(([k, l]) => h("button", { class: k === view ? "active" : "", onClick: () => { view = k; setQuery({ view }); paint(); go(); } }, l)));
  const go = () => (view === "following" ? followedRoster(body, 0, alive) : libraryArtists(body, r, alive));
  mount(out, h("div", { class: "row between mb" }, seg, h("span", { class: "hint" }, "Follow from an artist's shelf, Discover, or Spotify to watch for new releases.")), body);
  paint();
  await go();
}

async function artistShelf(root, name) {
  setCrumbs(["Library", h("a", { class: "muted", href: "#/library?tab=artists" }, "Artists"), name]);
  mount(root, h("div", { class: "skel" }, h("div"), h("div"), h("div")));
  let a; try { a = await api(`/api/library/artist?name=${encodeURIComponent(name)}`); } catch (e) { if (root.isConnected) mount(root, h("div", { class: "notice bad" }, e.message)); return; }
  if (!root.isConnected) return;
  const op = can("operator");
  const owned = a.albums.length, complete = a.albums.filter(x => !x.tracktotal || x.tracks >= x.tracktotal).length, lossless = a.albums.filter(x => x.lossless_share >= 1).length;
  const canonBox = h("div", { class: "card" }, h("h3", {}, "Essential albums", h("span", { class: "right" }, "ListenBrainz popularity")), h("div", { class: "skel" }, h("div"), h("div")));
  const escBeet = (s) => String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  const artistQuery = `albumartist:"${escBeet(name)}"`;
  mount(root,
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, name), h("div", { class: "sub" }, `${owned} albums · ${fmt.num(a.tracks)} tracks · ${fmt.hours(a.hours)} · ${complete}/${owned} complete · ${lossless}/${owned} lossless`, a.genres.length ? ` · ${a.genres.slice(0, 4).join(", ")}` : "")),
      h("div", { class: "actions" },
        a.followed ? h("span", { class: "badge ok" }, "followed") : (op ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/follow_by_name", { name }); toast(r.added ? `Following ${r.name}` : "Already following", "ok"); artistShelf(root, name); }) }, "Follow") : null),
        h("button", { class: "btn sm", onClick: () => a.mbid ? openMbArtist(a.mbid, name) : navigate(link("discover", { q: name, kind: "artist" })) }, "Discography (MB)"),
        op ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/library/beet", { action: "fetchart", artist: name }); if (r.ok) openJob(r.job.id); else toast(r.error, "bad"); }) }, "Fetch missing art") : null,
        op ? h("button", { class: "btn sm", onClick: () => modifyDialog({ query: artistQuery, album: true, current: { albumartist: name }, after: () => artistShelf(root, name) }) }, "Rename artist…") : null,
        op ? h("a", { class: "btn sm ghost", href: link("beets", { tab: "console", cmd: "list", query: artistQuery }) }, "Console") : null,
        h("button", { class: "btn sm", title: "every file by this artist as one zip", onClick: action(null, async () => { await queueDownload({ title: name, artists: [name] }); }) }, "Download all"),
        a.blocked ? h("span", { class: "badge bad", title: "future imports of this albumartist are quarantined" }, "blocked") : null,
        blockButton(name, a.blocked, { after: () => artistShelf(root, name) }),
        op ? h("button", { class: "btn sm danger", onClick: () => removeDialog({ artists: [name], title: name, after: () => { if (safeDecode(location.hash).includes(name)) navigate("#/library?tab=artists"); } }) }, "Remove artist…") : null)),
    h("div", { class: "spread" },
      h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}), h("th", {}, "Album"), h("th", { class: "num" }, "Year"), h("th", {}, "Tracks"), h("th", {}, "Quality"), h("th", {}, "Type"), h("th", {}))),
        h("tbody", {}, ...a.albums.map(x => h("tr", { class: "click", onClick: () => openAlbum(x.id) }, h("td", {}, cover(coverArtUrl(x), {})), h("td", { class: "b" }, x.album), h("td", { class: "num" }, x.year || ""), h("td", {}, covBar(x.tracks, x.tracktotal), x.wants?.gapfill ? wantBadge(x.wants.gapfill) : null), h("td", {}, qualityBadge(x), x.wants?.upgrade ? wantBadge(x.wants.upgrade) : null), h("td", { class: "muted small" }, x.albumtype || ""),
          h("td", {}, h("div", { class: "actions" }, op && x.tracktotal && x.tracks < x.tracktotal && !x.wants?.gapfill ? h("button", { class: "btn xs primary", onClick: action(null, async (e) => { e.stopPropagation(); const r = await api("/api/library/want", { album_id: x.id, kind: "gapfill" }); toast(r.added ? "Hunting missing tracks" : "queued already", "ok"); refreshStatusSoon(); artistShelf(root, name); }) }, "Complete") : null))))))),
      canonBox));
  try {
    const c = await api(`/api/canon?name=${encodeURIComponent(name)}${a.mbid ? "&mbid=" + encodeURIComponent(a.mbid) : ""}`);
    if (!root.isConnected) return;
    mount(canonBox, h("h3", {}, "Essential albums", h("span", { class: "right" }, c.total ? `${c.owned}/${c.total} owned` : c.note || "")),
      c.canon.length ? h("div", { class: "list" }, ...c.canon.slice(0, 25).map(x => h("div", { class: "item click", onClick: () => x.album_id ? openAlbum(x.album_id) : null }, cover(artUrl(x.rgid), {}), h("div", { class: "main" }, h("div", { class: "t small" }, x.album), h("div", { class: "s" }, x.year || "", " · score ", (x.score * 100).toFixed(0))), h("div", { class: "end" }, x.owned ? h("span", { class: "badge ok" }, "owned") : x.want ? wantBadge(x.want) : (op ? h("button", { class: "btn xs primary", onClick: action(null, async (e) => { e.stopPropagation(); const r = await api("/api/request", { rgid: x.rgid, artist: name, album: x.album, year: x.year }); toast(r.added ? "Requested" : "owned / queued", r.added ? "ok" : "warn"); refreshStatusSoon(); artistShelf(root, name); }) }, "Request") : null))))) : h("div", { class: "empty small" }, "no data"));
  } catch (e) { if (root.isConnected) mount(canonBox, h("h3", {}, "Essential albums"), h("div", { class: "hint" }, e.message)); }
}
