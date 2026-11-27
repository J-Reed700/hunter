/* Recovery — reconciliation between the beets library and the files on disk.
   Every row here is a library item whose file is gone. Pick one, pick a
   handful, or take everything that matches the filter. A re-request is
   album-shaped because that is what Soulseek serves, but selection stays
   per-track so you can see exactly what was lost. */
import { api, h, mount, fmt, toast, action, confirm, busy, sel, setQuery, qs, can } from "../app.js";
import { openAlbum, openTrack, debouncedInput } from "./_shared.js";

const SORTS = [["artist", "Artist · album · track"], ["album", "Album"], ["title", "Title"],
               ["added", "Recently added first"], ["size", "Largest first"]];

export default {
  async render(root, r) {
    const state = { q: r.q.q || "", sort: r.q.sort || "artist", fmt: r.q.fmt || "",
                    artist: r.q.artist || "", album_id: +(r.q.album_id || 0),
                    offset: 0, limit: 100 };
    const head = h("div", {}), filterHost = h("div", { class: "facets" }), body = h("div", {});
    const searchInp = debouncedInput({
      placeholder: "Search artist, album or title",
      value: state.q,
      delay: 320,
      onInput: (val) => { state.q = val; state.offset = 0; setQuery({ q: state.q }); load(); },
    });
    const selectsHost = h("div", { class: "facet-selects" });
    const activeHost = h("div", { class: "row facet-active" });
    mount(filterHost, h("div", { class: "facet-row" }, searchInp, selectsHost), activeHost);
    mount(root, h("div", { class: "page-head" }, h("div", {},
      h("h1", {}, "Recovery"),
      h("div", { class: "sub" }, "Tracks beets has in the library whose file is no longer on disk. This is the exact loss — not a download-history guess. Select any number and re-request them."))), head, filterHost, body);

    const params = () => ({ q: state.q, sort: state.sort, fmt: state.fmt, artist: state.artist,
                            album_id: state.album_id || "" });
    let res = null, loadGen = 0;

    const requeue = async (payload) => {
      const r2 = await api("/api/recovery/requeue", payload);
      if (r2.returned) toast(`${fmt.num(r2.returned)} of those files are back on disk — skipped`, "warn", 5000);
      if (r2.queued) toast(`Queued ${fmt.num(r2.queued)} album${r2.queued === 1 ? "" : "s"} covering ${fmt.num(r2.tracks || 0)} track${r2.tracks === 1 ? "" : "s"}`, "ok", 5000);
      else if (!r2.returned) toast(r2.note || "Nothing queued — already wanted or blocked", "warn", 5000);
      sel.clear();
      await load();
    };

    sel.configure("tracks", [
      { label: "Re-request", cls: "primary", onClick: (items) =>
          requeue({ item_ids: items.map(t => t.id) }) },
    ]);

    function paintHead() {
      const healthy = res.trusted && !res.missing;
      mount(head,
        !res.trusted ? h("div", { class: "notice bad mb" },
          h("b", {}, "Scan not trusted — "), res.untrusted_reason || "the library mount is unreadable.",
          h("div", { class: "xs", style: { marginTop: "4px" } },
            `Nothing is listed and re-requests are blocked until ${res.music_root} reads normally again. An unmounted disk makes the whole library look deleted; that is a mount failure, not a loss.`)) : null,
        h("div", { class: "row between mb", style: { flexWrap: "wrap", gap: "8px" } },
          h("span", { class: "muted small" },
            `${fmt.num(res.library_items)} items in the library · `,
            healthy ? h("span", { class: "good" }, "all files present")
                    : h("b", { class: res.trusted ? "bad" : "" }, `${fmt.num(res.missing)} missing (${fmt.bytes(res.library_bytes)})`),
            res.scanned_ts ? ` · checked ${fmt.ago(res.scanned_ts)}` : ""),
          h("span", { class: "row", style: { gap: "6px" } },
            h("button", { class: "btn xs ghost", onClick: action(null, () => load(true)) }, "Re-check now"),
            res.trusted && res.total && can("operator")
              ? h("button", { class: "btn xs danger", onClick: action(null, async () => {
                  if (!await confirm({ title: `Re-request all ${fmt.num(res.total)} missing tracks?`,
                    body: `This queues every album covering them (${fmt.num(res.albums_total)} albums, about ${fmt.bytes(res.bytes)}). Existing wants and the blocklist still apply.`,
                    ok: "Re-request all", danger: true })) return;
                  await requeue({ all: true, ...params() });
                }) }, `Re-request all ${fmt.num(res.total)}`) : null)));
    }

    function paintFilterControls() {
      const select = (key, opts, first) => {
        const s = h("select", { class: "select sm", "aria-label": key }, ...(first ? [first] : []).concat(opts).map(([v, l]) => h("option", { value: v, selected: String(state[key] || "") === v }, l)));
        s.addEventListener("change", () => { state[key] = s.value; state.offset = 0; setQuery({ [key]: s.value }); load(); });
        return s;
      };
      mount(selectsHost,
        select("fmt", (res.formats || []).map(f => [f, f]), ["", "Any format"]),
        select("sort", SORTS));
      mount(activeHost, state.artist || state.album_id ? h("span", { class: "chip active", onClick: () => { state.artist = ""; state.album_id = 0; setQuery({ artist: "", album_id: "" }); load(); } },
        state.artist ? `artist: ${state.artist}` : `album #${state.album_id}`, " ✕") : null);
    }

    function albumCards() {
      if (!res.albums.length || state.album_id) return null;
      return h("details", { class: "card tight mb", open: res.albums.length <= 12 },
        h("summary", {}, `${fmt.num(res.albums_total)} album${res.albums_total === 1 ? "" : "s"} affected`),
        h("div", { class: "grid auto", style: { marginTop: "8px" } }, ...res.albums.map(a =>
          h("div", { class: "card tight" },
            h("div", { class: "b ellip", title: a.album }, a.album),
            h("div", { class: "muted xs ellip", title: a.artist }, a.artist, a.year ? ` · ${a.year}` : ""),
            h("div", { class: "row between", style: { marginTop: "6px", gap: "6px" } },
              h("span", { class: "badge warn" }, `${fmt.num(a.tracks)} missing`),
              h("span", { class: "row", style: { gap: "4px" } },
                a.album_id ? h("button", { class: "btn xs ghost", onClick: () => openAlbum(a.album_id) }, "Open") : null,
                can("operator") ? h("button", { class: "btn xs primary", onClick: action(null, () => requeue({ item_ids: a.item_ids })) }, "Recover") : null))))));
    }

    function trackTable() {
      if (!res.tracks.length) return h("div", { class: "empty" },
        h("div", { class: "big" }, res.trusted && !res.missing ? "✓" : "—"),
        res.trusted && !res.missing
          ? "Every file in the library is present on disk. Nothing to recover."
          : "Nothing matches this filter.");
      const rows = res.tracks.map(t => {
        const key = "t" + t.id;
        const tr = h("tr", { class: sel.has(key) ? "sel" : "" });
        const check = h("span", { class: "check" + (sel.has(key) ? " on" : ""), role: "checkbox", tabindex: 0,
          onClick: (e) => { e.stopPropagation(); const on = sel.toggle(key, t); e.currentTarget.classList.toggle("on", on); tr.classList.toggle("sel", on); } }, "✓");
        mount(tr,
          h("td", {}, can("operator") ? check : null),
          h("td", {}, h("button", { class: "lnk", type: "button", onClick: () => openTrack(t.id) }, t.title),
                      t.track ? h("span", { class: "muted xs" }, ` · #${t.track}`) : null),
          h("td", { class: "ellip" }, t.albumartist),
          h("td", { class: "ellip" }, t.album_id
              ? h("button", { class: "lnk", type: "button", onClick: () => openAlbum(t.album_id) }, t.album || "—")
              : (t.album || "—")),
          h("td", {}, t.format || "—"),
          h("td", {}, t.length ? fmt.dur(t.length) : "—"),
          h("td", {}, t.bytes ? fmt.bytes(t.bytes) : "—"),
          h("td", {}, can("operator")
            ? h("button", { class: "btn xs primary", onClick: action(null, () => requeue({ item_ids: [t.id] })) }, "Recover")
            : null));
        return tr;
      });
      return h("div", { class: "card tight" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "Title"), h("th", {}, "Artist"),
          h("th", {}, "Album"), h("th", {}, "Format"), h("th", {}, "Length"), h("th", {}, "Size"), h("th", {}, ""))),
        h("tbody", {}, ...rows)));
    }

    function pager() {
      const pages = Math.ceil(res.total / state.limit);
      if (pages <= 1) return null;
      const page = Math.floor(state.offset / state.limit) + 1;
      const go = (n) => { state.offset = (n - 1) * state.limit; load(); window.scrollTo({ top: 0, behavior: "smooth" }); };
      return h("div", { class: "row between mt" },
        h("button", { class: "btn sm ghost", disabled: page <= 1, onClick: () => go(page - 1) }, "‹ Prev"),
        h("span", { class: "muted small" }, `Page ${page} of ${fmt.num(pages)} · ${fmt.num(res.total)} tracks`),
        h("button", { class: "btn sm ghost", disabled: page >= pages, onClick: () => go(page + 1) }, "Next ›"));
    }

    function selectAll() {
      if (!res.total || res.total <= res.tracks.length || !can("operator")) return null;
      return h("button", { class: "btn xs ghost", onClick: async (ev) => {
        const k = await busy(ev.currentTarget, api("/api/recovery/keys" + qs(params())));
        const byId = new Map(res.tracks.map(t => [t.id, t]));
        k.item_ids.forEach(id => sel.set("t" + id, byId.get(id) || { id }, true));
        body.querySelectorAll(".check").forEach(c => c.classList.add("on"));
        body.querySelectorAll("tbody tr").forEach(tr => tr.classList.add("sel"));
        toast(k.capped ? `Selected the first ${fmt.num(k.item_ids.length)} of ${fmt.num(k.total)}` : `Selected all ${fmt.num(k.item_ids.length)}`, k.capped ? "warn" : "ok");
      } }, `Select all ${fmt.num(res.total)} matching`);
    }

    async function load(refresh = false) {
      const myGen = ++loadGen;
      if (!res) mount(body, h("div", { class: "skel" }, h("div"), h("div"), h("div")));
      let next;
      try { next = await api("/api/recovery" + qs({ ...params(), offset: state.offset, limit: state.limit, refresh: refresh ? 1 : "" })); }
      catch (e) { if (myGen === loadGen && root.isConnected) mount(body, h("div", { class: "notice bad" }, e.message)); return; }
      if (myGen !== loadGen || !root.isConnected) return;
      res = next;
      paintHead();
      paintFilterControls();
      mount(body, albumCards(),
        h("div", { class: "row between mb" }, h("span", { class: "muted small" },
          res.total ? `${fmt.num(res.total)} track${res.total === 1 ? "" : "s"} · ${fmt.bytes(res.bytes)}` : ""), selectAll()),
        trackTable(), pager());
    }
    await load();
  },
};
