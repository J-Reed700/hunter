import { api, h, mount, toast, action, busy, sel, setQuery, cover, artUrl, qs, refreshStatusSoon, onCleanup, store, can } from "../app.js";
import { requestBtn, ownedOrWant, openMbArtist, candidateRow, field } from "./_shared.js";

export default {
  async render(root, r) {
    const op = can("operator");
    const allowedTabs = op ? ["mb", "soulseek", "manual"] : ["mb"];
    const allowedKinds = ["album", "artist", "song"];
    let kind = allowedKinds.includes(r.q.kind) ? r.q.kind : "album";
    let tab = allowedTabs.includes(r.q.tab) ? r.q.tab : "mb";
    let q = r.q.q || "";
    let results = [], offset = 0, lastHasMore = false, timer = null, retryTimer = null, searchGen = 0, sort = "rel", filt = "all";
    onCleanup(() => { clearTimeout(timer); clearTimeout(retryTimer); });
    const input = h("input", { class: "input", placeholder: "Album, artist, song — or paste a MusicBrainz / Spotify URL", value: q, type: "search", enterkeyhint: "search", autocomplete: "off", autofocus: matchMedia("(pointer: fine)").matches });
    const segK = h("div", { class: "seg" });
    const segT = h("div", { class: "seg" });
    const tools = h("div", { class: "row" });
    const out = h("div", {});
    const tabDefs = [["mb", "MusicBrainz"], ...(op ? [["soulseek", "Soulseek browse"], ["manual", "Free-text add"]] : [])];
    const paintSegs = () => {
      mount(segT, ...tabDefs.map(([k, l]) => h("button", { class: k === tab ? "active" : "", onClick: () => { tab = k; setQuery({ tab }); paintSegs(); go(); } }, l)));
      mount(segK, ...[["album", "Albums"], ["artist", "Artists"], ["song", "Songs"]].map(([k, l]) => h("button", { class: k === kind ? "active" : "", onClick: () => { kind = k; setQuery({ kind }); paintSegs(); go(); } }, l)));
      segK.hidden = tab !== "mb";
    };
    mount(root,
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Discover"), h("div", { class: "sub" }, "Request albums from MusicBrainz, browse Soulseek directly, or add a free-text want. Song requests fetch the whole album — no singletons, ever.")),
        h("div", { class: "actions" }, segT)),
      h("div", { class: "filterbar" }, h("div", { class: "grow", style: { maxWidth: "620px" } }, input), segK, tools),
      out);
    paintSegs();
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { q = input.value.trim(); setQuery({ q }); go(); }, 420); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(timer); q = input.value.trim(); setQuery({ q }); go(); } });
    sel.configure("selected", [{ label: "Request selected", role: "operator", onClick: async (items) => { const r = await api("/api/request_bulk", { items }); toast(`${r.added} queued · ${r.skipped_owned} already owned`, "ok"); sel.clear(); refreshStatusSoon(); } }]);

    const skel = () => mount(out, h("div", { class: "skel" }, h("div"), h("div"), h("div"), h("div")));
    async function go(more = false) {
      clearTimeout(retryTimer); retryTimer = null;
      const myGen = ++searchGen;
      if (tab === "manual") return paintManual();
      if (tab === "soulseek") return paintSoulseek(myGen);
      if (!q || q.length < 2) { mount(out, h("div", { class: "empty" }, h("div", { class: "big" }, "⌕"), "Type at least two characters. ", h("kbd", {}, "⌘K"), " opens this from anywhere.")); return; }
      if (/^https?:\/\//i.test(q)) return resolveUrl(q, myGen);
      const nextOffset = more ? offset + 20 : 0;
      if (!more) skel();
      try {
        const r = await api(`/api/search${qs({ kind, q, offset: nextOffset })}`);
        if (myGen !== searchGen || !out.isConnected) return;
        offset = nextOffset;
        results = more ? results.concat(r.results) : r.results;
        lastHasMore = r.results.length >= 20;
        paintResults(lastHasMore);
      } catch (e) {
        if (myGen !== searchGen || !out.isConnected) return;
        if (e.status === 503) {
          mount(out, h("div", { class: "notice warn" }, h("div", { class: "body" }, "MusicBrainz is busy — retrying in 3 s…")));
          retryTimer = setTimeout(() => { if (myGen === searchGen && out.isConnected && tab === "mb") go(more); }, 3000);
        } else mount(out, h("div", { class: "notice bad" }, e.message));
      }
    }
    function paintTools() {
      if (kind === "artist") { mount(tools); return; }
      mount(tools,
        h("select", { class: "select sm", onChange: (e) => { sort = e.target.value; paintResults(lastHasMore); } }, ...[["rel", "Relevance"], ["year", "Newest"], ["year_asc", "Oldest"], ["artist", "Artist"]].map(([v, l]) => h("option", { value: v, selected: sort === v }, l))),
        h("select", { class: "select sm", onChange: (e) => { filt = e.target.value; paintResults(lastHasMore); } }, ...[["all", "All"], ["missing", "Not owned"], ["partial", "Partial"], ["owned", "Owned"]].map(([v, l]) => h("option", { value: v, selected: filt === v }, l))));
    }
    function paintResults(hasMore = lastHasMore) {
      paintTools();
      let rows = results.slice();
      if (kind !== "artist") {
        if (filt === "missing") rows = rows.filter(x => !x.owned && !x.have); else if (filt === "partial") rows = rows.filter(x => !x.owned && x.have); else if (filt === "owned") rows = rows.filter(x => x.owned);
        if (sort === "year") rows.sort((a, b) => (b.year || 0) - (a.year || 0)); else if (sort === "year_asc") rows.sort((a, b) => (a.year || 9999) - (b.year || 9999)); else if (sort === "artist") rows.sort((a, b) => (a.artist || "").localeCompare(b.artist || ""));
      }
      if (!rows.length) { mount(out, h("div", { class: "empty" }, "No results.")); return; }
      const more = hasMore ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, go(true)) }, "Load more") : null;
      if (kind === "artist") {
        mount(out, h("div", { class: "list card" }, ...rows.map(a => h("div", { class: "item click", onClick: () => openMbArtist(a.id, a.name) },
          h("div", { class: "main" }, h("div", { class: "t" }, a.name, a.disambiguation ? h("span", { class: "muted" }, ` · ${a.disambiguation}`) : null), h("div", { class: "s" }, [a.type, a.country, a.begin].filter(Boolean).join(" · "))),
          h("div", { class: "end" }, op ? h("button", { class: "btn sm", onClick: action(null, async (e) => { e.stopPropagation(); const r = await api("/api/follow", { id: a.id, name: a.name }); toast(r.added ? `Following ${a.name}` : "Already following", "ok"); }) }, "Follow") : null, h("button", { class: "btn sm primary", onClick: (e) => { e.stopPropagation(); openMbArtist(a.id, a.name); } }, "Discography"))))), more);
        return;
      }
      mount(out, h("div", { class: "list card" }, ...rows.map(x => {
        const key = x.rgid;
        const chk = op ? h("span", { class: "check" + (sel.has(key) ? " on" : ""), onClick: (e) => { e.stopPropagation(); if (x.owned) return toast("owned"); const on = sel.toggle(key, { rgid: x.rgid, artist: x.artist, album: x.album, year: x.year }); chk.classList.toggle("on", on); } }, "✓") : null;
        return h("div", { class: "item" }, chk, cover(artUrl(x.rgid), {}),
          h("div", { class: "main" }, h("div", { class: "t" }, kind === "song" ? x.title : x.album, kind === "song" ? h("span", { class: "muted" }, ` · ${x.album}`) : null), h("div", { class: "s" }, h("a", { href: "#", onClick: (e) => { e.preventDefault(); if (x.artist_id) openMbArtist(x.artist_id, x.artist); else { input.value = x.artist; kind = "artist"; q = x.artist; paintSegs(); go(); } } }, x.artist), x.year ? ` · ${x.year}` : "", x.type ? ` · ${x.type}` : "", x.tracks ? ` · ${x.tracks} tracks` : "", x.secondary ? ` · ${x.secondary}` : "")),
          h("div", { class: "end" }, ownedOrWant(x), requestBtn(x)));
      })), more);
    }
    async function resolveUrl(url, myGen) {
      skel();
      try {
        const r = await api("/api/resolve_url", { url });
        if (myGen !== searchGen || !out.isConnected) return;
        if (r.kind === "album") { results = [r.album]; lastHasMore = false; kind = "album"; paintSegs(); paintResults(false); }
        else if (r.kind === "artist") { openMbArtist(r.artist.id, r.artist.name); mount(out, h("div", { class: "empty" }, "Opened ", h("b", {}, r.artist.name))); }
        else mount(out, h("div", { class: "notice info" }, h("div", { class: "body" }, r.detail)));
      } catch (e) { if (myGen === searchGen && out.isConnected) mount(out, h("div", { class: "notice bad" }, e.message)); }
    }
    // ---- Soulseek browse ----
    async function paintSoulseek(myGen) {
      mount(tools);
      if (!store.status?.slskd) { mount(out, h("div", { class: "notice warn" }, h("div", { class: "body" }, "slskd is not configured (SLSKD_API_KEY)."))); return; }
      if (!q || q.length < 2) { mount(out, h("div", { class: "empty" }, "Search Soulseek directly — every candidate dir with quality info; download any. Downloads are tracked as “browse” wants with live progress.")); return; }
      mount(out, h("div", { class: "skel" }, h("div"), h("div"), h("div")), h("div", { class: "hint mt" }, "Searching Soulseek — up to 30 s…"));
      try {
        const r = await api("/api/slskd_search", { q });
        if (myGen !== searchGen || !out.isConnected) return;
        const lbl = h("input", { class: "input sm", placeholder: "label for the queue (artist / album)", value: q, style: { maxWidth: "360px" } });
        mount(out, h("div", { class: "row mb" }, h("span", { class: "muted" }, `${r.candidates.length} dirs`), lbl),
          r.candidates.length ? h("div", { class: "list card" }, ...r.candidates.map(c => candidateRow(c, { label: "Download", onGrab: async (btn) => { await busy(btn, api("/api/slskd_download", { username: c.username, dir: c.dir, files: c.files, label: lbl.value.trim() })); toast(`Queued ${c.files.length} files from ${c.username}`, "ok"); refreshStatusSoon(); } }))) : h("div", { class: "empty" }, "Nothing found."));
      } catch (e) { if (myGen === searchGen && out.isConnected) mount(out, h("div", { class: "notice bad" }, e.message)); }
    }
    // ---- manual ----
    function paintManual() {
      mount(tools);
      const artist = h("input", { class: "input", placeholder: "Artist" }), album = h("input", { class: "input", placeholder: "Album" }), year = h("input", { class: "input", type: "number", placeholder: "Year (opt)" }), tracks = h("input", { class: "input", type: "number", placeholder: "Expected tracks (opt)" });
      let force = false;
      const btn = h("button", { class: "btn primary", onClick: action(null, async () => {
        if (!artist.value.trim() || !album.value.trim()) return toast("artist + album required", "warn");
        const r = await api("/api/add", { artist: artist.value.trim(), album: album.value.trim(), year: +year.value || 0, tracks: +tracks.value || 0, force });
        if (r.added) { toast("Want queued — pass spawned", "ok"); force = false; btn.textContent = "Add want"; refreshStatusSoon(); }
        else if (r.owned) { toast(`Already owned (${r.have}/${r.expected}) — click again to force`, "warn"); force = true; btn.textContent = "Force add"; }
        else toast("Already in the queue");
      }) }, "Add want");
      mount(out, h("div", { class: "card", style: { maxWidth: "640px" } }, h("h3", {}, "Free-text want"), h("div", { class: "hint mb" }, "For things MusicBrainz search can't find. hunter searches Soulseek by artist + album text; the beets import verifies the result. Owned albums are refused unless forced."),
        h("div", { class: "grid c2" }, field("Artist", artist), field("Album", album), field("Year", year), field("Expected tracks", tracks)), h("div", { class: "row mt" }, btn)));
    }
    go();
  },
};
