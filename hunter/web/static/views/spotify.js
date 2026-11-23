import { api, h, mount, fmt, toast, action, busy, sel, setQuery, poll, navigate, link, refreshStatusSoon, setTopActions, modal, promptText, can } from "../app.js";
import { openWant, wantBadge, openAlbum } from "./_shared.js";

export default {
  async render(root, r) {
    const op = can("operator");
    const allowedTabs = new Set(["playlists", "albums", "liked", "artists"]);
    let tab = allowedTabs.has(r.q.tab) ? r.q.tab : "playlists";
    const head = h("div", {});
    const tabs = h("div", { class: "tabs" });
    const out = h("div", {});
    let tabGen = 0;
    const go = () => {
      const myGen = ++tabGen;
      const alive = () => myGen === tabGen && out.isConnected;
      return ({
        playlists: () => playlists(alive),
        albums: () => albums(alive),
        liked: () => liked(alive),
        artists: () => artists(alive),
      })[tab]();
    };
    mount(root, h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Spotify"), h("div", { class: "sub" }, "Every playlist, saved album, liked song and followed artist — read from hunter's local cache, annotated against beets. Pick what to hunt; the pass does the rest."))), head, tabs, out);
    if (op) setTopActions(h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/spotify/sync", { scope: "all" }); toast(r.status === "started" ? "Sync started" : r.status === "cooldown" ? `Cooling down ${fmt.dur(r.cooldown_s)}` : "Sync already running", r.status === "started" ? "ok" : "warn"); }) }, "⟳ Sync all"));

    const paintTabs = (ov) => mount(tabs, ...[["playlists", "Playlists", ov?.counts?.playlists], ["albums", "Saved albums", ov?.counts?.saved_albums], ["liked", "Liked songs", ov?.counts?.liked], ["artists", "Followed artists", ov?.counts?.artists]].map(([k, l, n]) => h("button", { class: k === tab ? "active" : "", onClick: () => { tab = k; setQuery({ tab, id: "" }); paintTabs(ov); go(); } }, l, n !== undefined ? h("span", { class: "n" }, fmt.num(n)) : null)));
    paintTabs();

    async function paintHead() {
      const ov = await api("/api/spotify/overview");
      if (!root.isConnected) return;
      paintTabs(ov);
      const job = ov.job;
      const st = job?.status;
      let bar = null;
      if (!ov.connected) bar = h("div", { class: "notice warn" }, h("div", { class: "body" }, h("b", {}, "Spotify is not connected. "), op ? h("span", {}, "Paste your sp_dc cookie in ", h("a", { href: "#/settings?tab=spotify" }, "Settings → Spotify"), " — the whole library syncs in ~2 minutes.") : "Ask an operator to connect Spotify."));
      else if (st === "running") bar = h("div", { class: "notice info" }, h("span", { class: "dot ok pulse" }), h("div", { class: "body" }, h("b", {}, "Syncing "), `${job.scope} · stage ${job.plan?.[0] || "…"} · ${job.requests} requests · ${job.new_wants} new wants`, job.detail ? ` · ${job.detail}` : ""));
      else if (st === "error") bar = h("div", { class: "notice bad" }, h("div", { class: "body" }, h("b", {}, "Last sync failed: "), job.detail, " ", op ? h("button", { class: "btn xs", onClick: action(null, async () => { await api("/api/spotify/sync", { scope: "all", reset: true, ignore_cooldown: true }); toast("Retrying sync"); }) }, "Retry") : null));
      else if (ov.cooldown_s > 0) bar = h("div", { class: "notice warn" }, h("div", { class: "body" }, h("b", {}, "Rate-limited. "), `Spotify asked us to wait ${fmt.dur(ov.cooldown_s)}. The cache below keeps working.`));
      else if (ov.stale) bar = h("div", { class: "notice info" }, h("div", { class: "body" }, "Cache is older than 24h — a pass will re-crawl", op ? h("span", {}, ", or ", h("button", { class: "btn xs", onClick: action(null, async () => { await api("/api/spotify/sync", { scope: "all" }); toast("Sync started", "ok"); }) }, "sync now")) : "."));
      const w = ov.wants_from_spotify || {};
      mount(head, bar, h("div", { class: "row mb small muted" }, `${ov.playlists.total} playlists · ${ov.playlists.hunting} hunting · ${ov.playlists.mirroring} mirroring · wants from Spotify: ${w.wanted || 0} wanted, ${w.grabbed || 0} downloading, ${w.done || 0} done`));
    }
    poll(paintHead, 6000);

    // ---------------- playlists ----------------
    async function playlists(alive = () => out.isConnected) {
      if (r.q.id) return playlistDetail(r.q.id, alive);
      mount(out, h("div", { class: "skel" }, h("div"), h("div"), h("div")));
      const res = await api("/api/spotify/playlists");
      if (!alive()) return;
      const q = h("input", { class: "input", placeholder: "filter playlists", style: { maxWidth: "280px" } });
      const list = h("div", {});
      const paint = () => {
        const rows = res.playlists.filter(p => p.name.toLowerCase().includes(q.value.toLowerCase()));
        mount(list, h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "Playlist"), h("th", { class: "num" }, "Tracks"), h("th", {}, "Cache"), h("th", {}, "Hunt"), h("th", {}, "Mirror"), h("th", {}, "Wants"), h("th", {}))),
          h("tbody", {}, ...rows.map(p => {
            const w = p.wants || {};
            return h("tr", { class: "click", onClick: () => { setQuery({ id: p.spotify_id }); playlistDetail(p.spotify_id, alive); } },
              h("td", {}, h("div", { class: "b" }, p.name), h("div", { class: "muted xs" }, p.owner || "")),
              h("td", { class: "num" }, p.track_count),
              h("td", {}, h("span", { class: "badge " + (p.tracks_state === "ok" ? "ok" : p.tracks_state ? "warn" : "dim") }, p.tracks_state || "not crawled"), p.tracks_synced_ts ? h("div", { class: "muted xs" }, fmt.ago(p.tracks_synced_ts)) : null),
              h("td", {}, toggle(p, "hunt", "warn")), h("td", {}, toggle(p, "mirror", "good")),
              h("td", { class: "small" }, w.wanted ? h("span", { class: "badge wanted" }, `${w.wanted} wanted`) : null, " ", w.grabbed ? h("span", { class: "badge grabbed" }, `${w.grabbed} dl`) : null, " ", w.done ? h("span", { class: "badge done" }, `${w.done} done`) : null),
              h("td", {}, op ? h("div", { class: "actions" }, h("button", { class: "btn xs", title: "re-crawl this playlist", onClick: action(null, async (e) => { e.stopPropagation(); const r = await api("/api/spotify/sync", { scope: "playlist", id: p.spotify_id }); toast(r.status === "started" ? "Refreshing" : r.status, r.status === "started" ? "ok" : "warn"); }) }, "⟳")) : null));
          })))));
      };
      q.addEventListener("input", paint);
      mount(out, h("div", { class: "hint mb" }, h("b", {}, "Hunt"), " feeds the playlist's albums into the want queue (default off — connecting Spotify can never mass-download). ", h("b", {}, "Mirror"), " recreates it in Navidrome from owned tracks. Open a playlist for track-level control."),
        h("div", { class: "filterbar" }, q, op ? h("button", { class: "btn sm ghost ml-auto", onClick: () => pasteUrl() }, "+ Add playlist by URL") : null), list);
      paint();
    }
    function toggle(p, field, cls) {
      return h("button", { class: `toggle ${cls}` + (p[field] ? " on" : ""), disabled: !op, onClick: op ? action(null, async (e) => { e.stopPropagation(); const btn = e.currentTarget; /* null after the await — capture first */ const on = !btn.classList.contains("on"); const r = await api("/api/playlist_toggle", { id: p.spotify_id, field, value: on }); p[field] = on ? 1 : 0; btn.classList.toggle("on", on); if (field === "hunt") toast(on ? `Hunt on: ${r.added} new wants, ${r.moved} resurrected` : `Hunt off: ${r.moved} wants parked`, on ? "ok" : ""); refreshStatusSoon(); }) : (e) => e.stopPropagation() });
    }
    function pasteUrl() {
      const inp = h("input", { class: "input", placeholder: "https://open.spotify.com/playlist/…" });
      modal({ title: "Register a playlist", body: h("div", { class: "stack" }, h("p", { class: "hint" }, "Any playlist — yours or public. It appears in the roster with Hunt/Mirror off."), inp), actions: [{ label: "Add", cls: "primary", onClick: async () => { const r = await api("/api/resolve_url", { url: inp.value.trim() }); toast(r.detail || "added", "ok"); playlists(); } }] });
    }

    async function playlistDetail(pid, alive = () => out.isConnected) {
      mount(out, h("div", { class: "skel" }, h("div"), h("div"), h("div")));
      let d; try { d = await api(`/api/spotify/playlist_tracks?id=${encodeURIComponent(pid)}`); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
      if (!alive()) return;
      let mode = "tracks", filt = "all";
      const s = d.summary;
      const body = h("div", {});
      const albumKey = (t) => t.want_key || t.album_id || `manual:${t.artist}:${t.album}`;
      sel.configure("albums", [
        { label: "Hunt selected", role: "operator", onClick: async (items) => { const r = await api("/api/spotify/request", { items, source: `playlist:${d.name}` }); toast(`${r.added} queued · ${r.skipped_owned} already owned`, "ok"); sel.clear(); refreshStatusSoon(); playlistDetail(pid, alive); } },
        { label: "Un-want", cls: "danger", role: "operator", onClick: async (items) => { const r = await api("/api/spotify/drop", { keys: items.map(i => i.want_key || i.album_id || i.key) }); toast(`${r.dropped} dropped`); sel.clear(); refreshStatusSoon(); playlistDetail(pid, alive); } },
        { label: "→ Navidrome playlist", cls: "", role: "operator", onClick: async (items) => { const name = await promptText({ title: "Navidrome playlist name", value: d.name + " (picks)" }); if (!name) return; const tracks = d.tracks.filter(t => items.some(i => i.key === albumKey(t))).map(t => ({ title: t.title, artist: t.artist })); const r = await api("/api/make_playlist", { name, tracks }); toast(`${r.matched}/${r.total} matched → “${r.name || name}”`, "ok"); } },
      ]);
      const paint = () => {
        const rows = (mode === "tracks" ? d.tracks : d.albums).filter(x => filt === "all" || (filt === "owned" ? x.owned : filt === "missing" ? (!x.owned && !isOpen(x.want)) : filt === "wanted" ? isOpen(x.want) : filt === "partial" ? (!x.owned && x.have) : true));
        const table = h("table", { class: "table" }, h("thead", {}, h("tr", {}, op ? h("th", {}) : null, mode === "tracks" ? h("th", {}, "Track") : null, h("th", {}, "Album"), h("th", {}, "Artist"), h("th", { class: "num" }, mode === "tracks" ? "Time" : "In playlist"), h("th", {}, "Library"), h("th", {}))),
          h("tbody", {}, ...rows.map(x => {
            const key = mode === "tracks" ? albumKey(x) : (x.want_key || x.key);
            const item = { key, want_key: x.want_key || key, album_id: x.album_id, artist: x.artist, album: x.album, year: x.year, album_total: x.album_total };
            const chk = op ? h("span", { class: "check" + (sel.has(key) ? " on" : ""), onClick: (e) => { e.stopPropagation(); chk.classList.toggle("on", sel.toggle(key, item)); } }, "✓") : null;
            return h("tr", { class: "click", onClick: () => { if (x.beets_album_id) openAlbum(x.beets_album_id); else if (isOpen(x.want)) openWant(x.want.id); } },
              op ? h("td", {}, chk) : null, mode === "tracks" ? h("td", { class: "b" }, x.title, x.liked ? h("span", { class: "acc", title: "liked" }, " ♥") : null) : null,
              h("td", {}, x.album, x.year ? h("span", { class: "muted xs" }, ` ${x.year}`) : null), h("td", { class: "muted" }, x.artist),
              h("td", { class: "num" }, mode === "tracks" ? fmt.mmss(x.duration_s) : `${x.tracks}/${x.album_total || "?"}`),
              h("td", {}, libBadge(x)),
              h("td", {}, op ? h("div", { class: "actions" }, !x.owned && !isOpen(x.want) ? h("button", { class: "btn xs primary", onClick: action(null, async (e) => { e.stopPropagation(); const r = await api("/api/spotify/request", { items: [item], source: `playlist:${d.name}` }); toast(r.added ? "Hunting the album" : "already owned / queued", r.added ? "ok" : "warn"); refreshStatusSoon(); playlistDetail(pid, alive); }) }, x.have ? "Complete" : "Hunt album") : null) : null));
          })));
        mount(body, rows.length ? h("div", { class: "table-wrap" }, table) : h("div", { class: "empty" }, "nothing matches"));
      };
      mount(out,
        h("div", { class: "row mb" }, h("a", { class: "btn sm ghost", href: "#", onClick: (e) => { e.preventDefault(); setQuery({ id: "" }); r.q.id = ""; playlists(alive); } }, "← Playlists"), h("h2", { style: { fontSize: "18px" } }, d.name), h("span", { class: "muted small" }, `${d.track_count} tracks · cache ${d.state || "?"} · ${d.synced_ts ? fmt.ago(d.synced_ts) : "never"}`),
          h("span", { class: "ml-auto" }), h("label", { class: "toggle-row small" }, "Hunt ", toggle({ spotify_id: pid, hunt: d.hunt }, "hunt", "warn")), h("label", { class: "toggle-row small" }, "Mirror ", toggle({ spotify_id: pid, mirror: d.mirror }, "mirror", "good")),
          op ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/spotify/sync", { scope: "playlist", id: pid }); toast(r.status, r.status === "started" ? "ok" : "warn"); }) }, "⟳ Refresh") : null),
        h("div", { class: "stats mb" },
          stat("Tracks", s.tracks, `${s.owned} owned`, () => { filt = "all"; paint(); }), stat("Albums", s.albums, `${s.albums_owned} owned`, () => { mode = "albums"; filt = "all"; paintSeg(); paint(); }),
          stat("Missing albums", s.albums - s.albums_owned - s.albums_wanted, "not owned, not queued", () => { mode = "albums"; filt = "missing"; paintSeg(); paint(); }, "warn"), stat("Queued", s.albums_wanted, "wanted / downloading", () => { mode = "albums"; filt = "wanted"; paintSeg(); paint(); })),
        h("div", { class: "filterbar" }, segEl(), h("div", { class: "chips" }, ...[["all", "All"], ["missing", "Missing"], ["partial", "Partial"], ["wanted", "Queued"], ["owned", "Owned"]].map(([k, l]) => h("span", { class: "chip" + (filt === k ? " active" : ""), onClick: (e) => { filt = k; [...e.currentTarget.parentNode.children].forEach(c => c.classList.remove("active")); e.currentTarget.classList.add("active"); paint(); } }, l))),
          op ? h("button", { class: "btn sm ml-auto", onClick: () => { for (const a of d.albums) if (!a.owned && !isOpen(a.want)) { const k = a.want_key || a.key; sel.set(k, { key: k, want_key: a.want_key || k, album_id: a.album_id, artist: a.artist, album: a.album, year: a.year, album_total: a.album_total }, true); } paint(); toast(`${sel.values().length} albums selected`); } }, "Select all missing") : null),
        body);
      let segHost;
      function segEl() { segHost = h("div", { class: "seg sm" }); paintSeg(); return segHost; }
      function paintSeg() { mount(segHost, ...[["tracks", "Tracks"], ["albums", "Albums"]].map(([k, l]) => h("button", { class: mode === k ? "active" : "", onClick: () => { mode = k; paintSeg(); paint(); } }, l))); }
      paint();
    }

    // ---------------- saved albums ----------------
    async function albums(alive = () => out.isConnected) {
      let q = "", sort = "name", filt = "", offset = 0, rows = [], total = 0, timer;
      const input = h("input", { class: "input", placeholder: "filter", style: { maxWidth: "260px" } });
      const list = h("div", {});
      input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { q = input.value.trim(); load(); }, 300); });
      sel.configure("albums", [
        { label: "Hunt selected", role: "operator", onClick: async (items) => { const r = await api("/api/spotify/request", { items, source: "saved_albums" }); toast(`${r.added} queued · ${r.skipped_owned} owned`, "ok"); sel.clear(); refreshStatusSoon(); load(); } },
        { label: "Un-want", cls: "danger", role: "operator", onClick: async (items) => { const r = await api("/api/spotify/drop", { keys: items.map(i => i.want_key || i.album_id) }); toast(`${r.dropped} dropped`); sel.clear(); load(); } },
      ]);
      mount(out, h("div", { class: "filterbar" }, input,
        h("select", { class: "select sm", onChange: (e) => { sort = e.target.value; load(); } }, ...[["name", "Album"], ["artist", "Artist"], ["year", "Year"], ["added", "Recently saved"]].map(([v, l]) => h("option", { value: v }, l))),
        h("div", { class: "chips" }, ...[["", "All"], ["missing", "Missing"], ["partial", "Partial"], ["wanted", "Queued"], ["owned", "Owned"]].map(([k, l]) => h("span", { class: "chip" + (filt === k ? " active" : ""), onClick: (e) => { filt = k; [...e.currentTarget.parentNode.children].forEach(c => c.classList.remove("active")); e.currentTarget.classList.add("active"); load(); } }, l))),
        op ? h("button", { class: "btn sm ml-auto", onClick: () => { for (const a of rows) if (!a.owned && !isOpen(a.want)) sel.set(a.want_key || a.album_id, item(a), true); toast(`${sel.values().length} selected`); paint(); } }, "Select all missing on page") : null), list);
      const item = (a) => ({ want_key: a.want_key, album_id: a.album_id, artist: a.artist, album: a.name, year: a.year, album_total: a.total_tracks });
      async function load(more = false) {
        offset = more ? offset + 100 : 0;
        const r = await api(`/api/spotify/saved_albums?q=${encodeURIComponent(q)}&sort=${encodeURIComponent(sort)}&filter=${encodeURIComponent(filt)}&offset=${encodeURIComponent(offset)}`);
        if (!alive()) return;
        rows = more ? rows.concat(r.albums) : r.albums; total = r.total; paint();
      }
      function paint() {
        mount(list, h("div", { class: "muted small mb" }, `${fmt.num(rows.length)} of ${fmt.num(total)}`), h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, op ? h("th", {}) : null, h("th", {}, "Album"), h("th", {}, "Artist"), h("th", { class: "num" }, "Year"), h("th", { class: "num" }, "Tracks"), h("th", {}, "Library"), h("th", {}))),
          h("tbody", {}, ...rows.map(a => { const k = a.want_key || a.album_id; const chk = op ? h("span", { class: "check" + (sel.has(k) ? " on" : ""), onClick: (e) => { e.stopPropagation(); chk.classList.toggle("on", sel.toggle(k, item(a))); } }, "✓") : null;
            return h("tr", { class: "click", onClick: () => { if (a.beets_album_id) openAlbum(a.beets_album_id); else if (isOpen(a.want)) openWant(a.want.id); } }, op ? h("td", {}, chk) : null, h("td", { class: "b" }, a.name), h("td", { class: "muted" }, a.artist), h("td", { class: "num" }, a.year || ""), h("td", { class: "num" }, a.total_tracks || ""), h("td", {}, libBadge(a)),
              h("td", {}, op ? h("div", { class: "actions" }, !a.owned && !isOpen(a.want) ? h("button", { class: "btn xs primary", onClick: action(null, async (e) => { e.stopPropagation(); const r = await api("/api/spotify/request", { items: [item(a)], source: "saved_albums" }); toast(r.added ? "Hunting" : "owned / queued", r.added ? "ok" : "warn"); refreshStatusSoon(); load(); }) }, a.have ? "Complete" : "Hunt") : null) : null)); })))),
          rows.length < total ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, "Load more") : null);
      }
      await load();
    }

    // ---------------- liked ----------------
    async function liked(alive = () => out.isConnected) {
      let q = "", filt = "", offset = 0, rows = [], total = 0, timer;
      const input = h("input", { class: "input", placeholder: "filter", style: { maxWidth: "260px" } });
      const list = h("div", {});
      input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { q = input.value.trim(); load(); }, 300); });
      const key = (t) => t.want_key || t.album_id || `manual:${t.artist}:${t.album}`;
      const item = (t) => ({ want_key: t.want_key, album_id: t.album_id, artist: t.artist, album: t.album, year: t.year, album_total: t.album_total });
      sel.configure("albums", [
        { label: "Hunt selected albums", role: "operator", onClick: async (items) => { const r = await api("/api/spotify/request", { items, source: "liked" }); toast(`${r.added} queued · ${r.skipped_owned} owned`, "ok"); sel.clear(); refreshStatusSoon(); load(); } },
        { label: "Un-want", cls: "danger", role: "operator", onClick: async (items) => { const r = await api("/api/spotify/drop", { keys: items.map(i => i.want_key || i.album_id || `manual:${i.artist}:${i.album}`) }); toast(`${r.dropped} dropped`); sel.clear(); load(); } },
        { label: "→ Navidrome playlist", role: "operator", onClick: async (items) => { const name = await promptText({ title: "Navidrome playlist name", value: "Liked picks" }); if (!name) return; const tracks = rows.filter(t => items.some(i => (i.want_key || i.album_id || `manual:${i.artist}:${i.album}`) === key(t))).map(t => ({ title: t.title, artist: t.artist })); const r = await api("/api/make_playlist", { name, tracks }); toast(`${r.matched}/${r.total} matched → “${r.name || name}”`, "ok"); } },
      ]);
      mount(out, h("div", { class: "hint mb" }, "A liked song is hunted as its whole album (the no-singletons rule)."), h("div", { class: "filterbar" }, input,
        h("div", { class: "chips" }, ...[["", "All"], ["missing", "Missing"], ["partial", "Partial"], ["wanted", "Queued"], ["owned", "Owned"]].map(([k, l]) => h("span", { class: "chip" + (filt === k ? " active" : ""), onClick: (e) => { filt = k; [...e.currentTarget.parentNode.children].forEach(c => c.classList.remove("active")); e.currentTarget.classList.add("active"); load(); } }, l)))), list);
      async function load(more = false) {
        offset = more ? offset + 100 : 0;
        const r = await api(`/api/spotify/liked?q=${encodeURIComponent(q)}&filter=${encodeURIComponent(filt)}&offset=${encodeURIComponent(offset)}`);
        if (!alive()) return;
        rows = more ? rows.concat(r.tracks) : r.tracks; total = r.total;
        mount(list, h("div", { class: "muted small mb" }, `${fmt.num(rows.length)} of ${fmt.num(total)}`), h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, op ? h("th", {}) : null, h("th", {}, "Track"), h("th", {}, "Album"), h("th", {}, "Artist"), h("th", { class: "num" }, "Time"), h("th", {}, "Library"), h("th", {}))),
          h("tbody", {}, ...rows.map(t => { const k = key(t); const chk = op ? h("span", { class: "check" + (sel.has(k) ? " on" : ""), onClick: (e) => { e.stopPropagation(); chk.classList.toggle("on", sel.toggle(k, item(t))); } }, "✓") : null;
            return h("tr", { class: "click", onClick: () => { if (t.beets_album_id) openAlbum(t.beets_album_id); else if (isOpen(t.want)) openWant(t.want.id); } }, op ? h("td", {}, chk) : null, h("td", { class: "b" }, t.title), h("td", {}, t.album), h("td", { class: "muted" }, t.artist), h("td", { class: "num" }, fmt.mmss(t.duration_s)), h("td", {}, libBadge(t)),
              h("td", {}, op ? h("div", { class: "actions" }, !t.owned && !isOpen(t.want) ? h("button", { class: "btn xs primary", onClick: action(null, async (e) => { e.stopPropagation(); const r = await api("/api/spotify/request", { items: [item(t)], source: "liked" }); toast(r.added ? "Hunting the album" : "owned / queued", r.added ? "ok" : "warn"); refreshStatusSoon(); load(); }) }, "Hunt album") : null) : null)); })))),
          rows.length < total ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, "Load more") : null);
      }
      await load();
    }

    // ---------------- artists ----------------
    async function artists(alive = () => out.isConnected) {
      const input = h("input", { class: "input", placeholder: "filter", style: { maxWidth: "260px" } });
      const list = h("div", {});
      let timer;
      input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 300); });
      mount(out, h("div", { class: "hint mb" }, "Artists you follow on Spotify. “Follow in hunter” resolves the name on MusicBrainz and turns on release watching."), h("div", { class: "filterbar" }, input), list);
      async function load() {
        const r = await api(`/api/spotify/artists?q=${encodeURIComponent(input.value.trim())}`);
        if (!alive()) return;
        mount(list, h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "Artist"), h("th", {}, "Genres"), h("th", { class: "num" }, "Library albums"), h("th", {}, "hunter"), h("th", {}))),
          h("tbody", {}, ...r.artists.map(a => h("tr", {}, h("td", { class: "b" }, a.name), h("td", { class: "muted small" }, (a.genres || "").split(",").slice(0, 3).join(", ")), h("td", { class: "num" }, a.library_albums ?? ""), h("td", {}, a.hunter_followed ? h("span", { class: "badge ok" }, "following") : h("span", { class: "badge dim" }, "—")),
            h("td", {}, h("div", { class: "actions" }, op && !a.hunter_followed ? h("button", { class: "btn xs primary", onClick: action(null, async () => { const r = await api("/api/follow_by_name", { name: a.name }); toast(r.added ? `Following ${r.name}` : "Already following", "ok"); load(); }) }, "Follow in hunter") : null,
              h("button", { class: "btn xs", onClick: () => navigate(link("discover", { q: a.name, kind: "artist" })) }, "Discography"),
              a.library_albums ? h("button", { class: "btn xs", onClick: () => navigate(link("library", {}, "artist", a.name)) }, "Shelf") : null))))))));
      }
      await load();
    }
    go();
  },
};
const isOpen = (w) => w && ["wanted", "grabbed"].includes(w.status);
function libBadge(x) {
  if (isOpen(x.want)) return wantBadge(x.want);
  if (x.owned) return h("span", { class: "badge ok" }, `owned${x.have ? " · " + x.have : ""}`);
  if (x.have) return h("span", { class: "badge warn" }, `${x.have}/${x.expected || "?"}`);
  if (x.want) return h("span", { class: "badge " + x.want.status }, x.want.status);
  return h("span", { class: "badge dim" }, "missing");
}
function stat(k, v, d, onClick, cls = "") { return h("div", { class: "stat " + cls, onClick }, h("div", { class: "k" }, k), h("div", { class: "v" }, fmt.num(v)), h("div", { class: "d" }, d)); }
