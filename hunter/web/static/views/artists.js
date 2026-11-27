import { api, h, mount, fmt, toast, action, busy, navigate, link, cover, artUrl, sel, qs, can } from "../app.js";
import { openMbArtist, requestBtn, ownedOrWant, removeDialog, blockButton, queueDownload, createTabs } from "./_shared.js";
import { facetBar, facetParams, groupView, applyGroupPick, selectAllButton } from "./_filters.js";

export default {
  async render(root, r) {
    const defs = [["followed", "Followed"], ["library", "Library artists"], ["calendar", "Release calendar"]];
    const tabCtl = createTabs(defs, r.q.tab || "followed", (tab, { alive, out }) => ({
      followed: () => followedRoster(out, 0, alive),
      library: () => libraryArtists(out, r, alive),
      calendar: () => calendar(out, alive),
    })[tab]());
    mount(root, h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Artists"), h("div", { class: "sub" }, "Followed artists auto-want new releases (Lidarr's roster, replaced). Library artists are sourced from beets — the truth."))), tabCtl.el, tabCtl.out);

    async function calendar(out, alive) {
      mount(out, h("div", { class: "skel" }, h("div"), h("div")));
      let res;
      try { res = await api("/api/calendar"); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
      if (!alive()) return;
      const section = (title, rows) => h("div", { class: "card mb" }, h("h3", {}, title, h("span", { class: "right" }, `${rows.length}`)), rows.length ? h("div", { class: "list" }, ...rows.map(x => h("div", { class: "item" }, cover(artUrl(x.rgid), {}), h("div", { class: "main" }, h("div", { class: "t" }, `${x.artist} — ${x.album}`), h("div", { class: "s" }, x.date, x.rtype ? ` · ${x.rtype}` : "")), h("div", { class: "end" }, ownedOrWant(x), requestBtn(x))))) : h("div", { class: "empty small" }, "nothing"));
      mount(out, h("div", { class: "hint mb" }, "Releases from followed artists, discovered by the release watch. Subscribe to ", h("a", { href: "/calendar.ics", target: "_blank", rel: "noopener noreferrer" }, "/calendar.ics"), " from any calendar app."), section("Upcoming", res.upcoming), section("Recent (120 days)", res.recent));
    }
    tabCtl.go();
  },
};

export function openLibArtist(name) { navigate(link("library", {}, "artist", name)); }

export async function libraryArtists(out, r = { q: {} }, alive = () => out.isConnected) {
  // Shared by Artists → Library artists and Library → Artists. Filters live in
  // the URL; multi-select feeds removal / blocklist / download.
  const st = { q: r.q.q || "", filter: r.q.filter || "", genre: r.q.genre || "", decade: r.q.decade || "", added: r.q.added || "", sort: r.q.sort || "name", letter: r.q.letter || "", group: r.q.group || "", min_albums: r.q.min_albums || "", max_albums: r.q.max_albums || "" };
  let offset = 0, rows = [], total = 0, loadGen = 0;
  sel.configure("artists", [
    { label: "Download", onClick: async (items) => { await queueDownload({ title: items.length === 1 ? items[0].name : `${items.length} artists`, artists: items.map(a => a.name) }); sel.clear(); } },
    { label: "Follow", role: "operator", onClick: async (items) => { let n = 0; for (const a of items) { const r = await api("/api/follow_by_name", { name: a.name }).catch(() => ({})); n += r.added ? 1 : 0; } toast(`${n} followed`, "ok"); sel.clear(); load(); } },
    { label: "Block", role: "operator", onClick: async (items) => { const r = await api("/api/library/blocklist", { add: items.map(a => a.name) }); toast(`${(r.added || []).length} blocked · ${r.wants_parked || 0} wants parked`, "ok"); sel.clear(); load(); } },
    { label: "Remove…", cls: "danger", role: "operator", onClick: (items) => removeDialog({ artists: items.map(a => a.name), title: `${items.length} artists`, after: () => { sel.clear(); load(); } }) },
  ]);
  const list = h("div", {});
  const bar = facetBar(st, {
    search: "filter artists",
    selects: { genre: true, decade: true, added: true },
    chips: [["lossless", "Lossless", ["lossy", "mixed"]], ["lossy", "Lossy-only", ["lossless", "mixed"]], ["mixed", "Mixed", ["lossless", "lossy"]], ["incomplete", "Has incomplete"], ["one_album", "1 album"], ["one_track", "1 track"], ["nombid", "No MBID"], ["noartistid", "No artist MBID"], ["nogenre", "No genre"], ["followed", "Followed", ["unfollowed"]], ["unfollowed", "Not followed", ["followed"]], ["blocked", "Blocked", ["unblocked"]], ["unblocked", "Not blocked", ["blocked"]]],
    sorts: [["name", "Name"], ["albums", "Most albums"], ["tracks", "Most tracks"], ["length", "Most hours"], ["added", "Recently added"], ["year", "Earliest year"], ["newest", "Latest year"], ["quality", "Lowest quality"], ["genre", "Genre"]],
    groups: [["genre", "Group by genre"], ["decade", "Group by decade"], ["quality", "Group by quality"], ["size", "Group by size"], ["letter", "Group by letter"]],
    letters: true,
  }, () => load());
  const ctx = h("div", { class: "row between mb" });
  mount(out, bar.el, ctx, list);
  const params = () => facetParams(st);
  const withChecks = () => {
    const th = list.querySelector("table.table thead tr"); if (th && !th.dataset.chk) { th.dataset.chk = "1"; th.prepend(h("th", {})); }
    list.querySelectorAll("table.table tbody tr").forEach((tr, i) => {
      const a = rows[i]; if (!a || tr.querySelector(".check")) return;
      const chk = h("span", { class: "check" + (sel.has(a.name) ? " on" : ""), onClick: (e) => { e.stopPropagation(); chk.classList.toggle("on", sel.toggle(a.name, a)); tr.classList.toggle("sel", sel.has(a.name)); } }, "✓");
      tr.prepend(h("td", {}, chk)); tr.classList.toggle("sel", sel.has(a.name));
    });
  };
  async function load(more = false) {
    const myGen = ++loadGen;
    bar.paintActive();
    if (st.group) { mount(ctx); await groupView(list, { base: "/api/library/artists", state: st, unit: "artists", onPick: (k) => { if (k !== null) applyGroupPick(st, k); bar.rebuild(); load(); } }); return; }
    const nextOffset = more ? offset + 100 : 0;
    let res;
    try { res = await api(`/api/library/artists${qs({ ...params(), offset: nextOffset })}`); } catch (e) { if (myGen === loadGen && alive()) mount(list, h("div", { class: "notice bad" }, e.message)); return; }
    if (myGen !== loadGen || !alive()) return;
    offset = nextOffset;
    rows = more ? rows.concat(res.artists) : res.artists; total = res.total;
    mount(ctx, h("span", { class: "muted small" }, `${fmt.num(rows.length)} of ${fmt.num(total)} artists`), h("span", { class: "ml-auto" }),
      rows.length ? h("button", { class: "btn xs ghost", onClick: () => { rows.forEach(a => sel.set(a.name, a, true)); withChecksOn(); } }, "Select page") : null,
      selectAllButton({ total, url: `/api/library/artists/names${qs(params())}`, key: "artists", map: (a) => ({ key: a.name, item: a }) }));
    const op = can("operator");
    mount(list, h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "Artist"), h("th", {}, "Genre"), h("th", { class: "num" }, "Albums"), h("th", { class: "num" }, "Tracks"), h("th", {}, "Quality"), h("th", {}, "Years"), h("th", {}, "Added"), h("th", {}))),
      h("tbody", {}, ...rows.map(a => h("tr", { class: "click", onClick: () => openLibArtist(a.name) },
        h("td", { class: "b" }, a.name, a.followed ? h("span", { class: "badge info", style: { marginLeft: "6px" } }, "followed") : null, a.blocked ? h("span", { class: "badge bad", style: { marginLeft: "6px" } }, "blocked") : null),
        h("td", { class: "muted small" }, a.genre ? h("a", { href: link("library", { tab: "artists", genre: a.genre }), onClick: (e) => e.stopPropagation() }, a.genre) : ""),
        h("td", { class: "num" }, a.albums), h("td", { class: "num" }, fmt.num(a.tracks)),
        h("td", {}, h("span", { class: "cov" }, h("span", { class: "progress " + (a.lossless_share >= 1 ? "good" : a.lossless_share > 0 ? "warn" : "bad") }, h("div", { style: { width: (a.lossless_share * 100) + "%" } })), Math.round(a.lossless_share * 100) + "%")),
        h("td", { class: "muted small" }, a.first_year && a.last_year ? (a.first_year === a.last_year ? String(a.first_year) : `${a.first_year}–${a.last_year}`) : ""), h("td", { class: "muted small" }, a.last_added ? fmt.ago(a.last_added) : ""),
        h("td", {}, h("div", { class: "actions" },
          h("button", { class: "btn xs", title: "download every file by this artist as a zip", onClick: action(null, async (e) => { e.stopPropagation(); await queueDownload({ title: a.name, artists: [a.name] }); }) }, "⤓"),
          op && !a.followed ? h("button", { class: "btn xs", onClick: action(null, async (e) => { e.stopPropagation(); const r = await api("/api/follow_by_name", { name: a.name }); toast(r.added ? `Following ${r.name}` : "Already following", "ok"); a.followed = true; load(); }) }, "Follow") : null,
          h("button", { class: "btn xs", onClick: (e) => { e.stopPropagation(); navigate(link("discover", { q: a.name, kind: "artist" })); } }, "MB"),
          blockButton(a.name, a.blocked, { cls: "btn xs", after: (b) => { a.blocked = b; load(); } }),
          op ? h("button", { class: "btn xs danger", onClick: (e) => { e.stopPropagation(); removeDialog({ artists: [a.name], title: a.name, after: () => load() }); } }, "Remove…") : null))))))),
      rows.length < total ? h("button", { class: "btn mt", onClick: (e) => busy(e.currentTarget, load(true)) }, `Load more (${fmt.num(total - rows.length)} left)`) : null,
      !rows.length ? h("div", { class: "empty" }, "No artists match.") : null);
    withChecks();
  }
  const withChecksOn = () => list.querySelectorAll(".check:not([data-local])").forEach(c => c.classList.add("on"));
  await load();
}

export async function followedRoster(out, _gen, isAlive = () => out.isConnected) {
  // Followed-artist management. Shown on Artists → Followed AND Library → Artists → Following.
  mount(out, h("div", { class: "skel" }, h("div"), h("div")));
  let r;
  try { r = await api("/api/artists"); } catch (e) { if (isAlive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
  if (!isAlive()) return;
  const q = h("input", { class: "input", placeholder: "filter", style: { maxWidth: "280px" } });
  const list = h("div", {});
  const op = can("operator");
  const paint = () => {
    const rows = r.artists.filter(a => a.name.toLowerCase().includes(q.value.toLowerCase()));
    mount(list, rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "Artist"), h("th", {}, "Monitored"), h("th", {}, "Wants"), h("th", {}, "Followed"), h("th", {}, "Last check"), h("th", { class: "num" }, "Open"), h("th", {}))),
      h("tbody", {}, ...rows.map(a => {
        const types = (a.want_types || "album,ep").split(",").map(s => s.trim()).filter(Boolean);
        const chip = (t) => h("span", {
          class: "chip" + (types.includes(t) ? " active" : ""),
          onClick: op ? action(null, async () => {
            const set = new Set(types);
            set.has(t) ? set.delete(t) : set.add(t);
            const next = [...set].join(",") || "album,ep";
            const res = await api("/api/artist_option", { mbid: a.mbid, want_types: next });
            a.want_types = res.want_types || next;
            paint();
          }) : null,
        }, t);
        return h("tr", {}, h("td", {}, h("a", { class: "b", href: "#", onClick: (e) => { e.preventDefault(); openMbArtist(a.mbid, a.name); } }, a.name)),
          h("td", {}, h("button", { class: "toggle good" + (a.monitored !== 0 ? " on" : ""), disabled: !op, onClick: op ? action(null, async (e) => { const on = !e.currentTarget.classList.contains("on"); await api("/api/artist_option", { mbid: a.mbid, monitored: on }); a.monitored = on ? 1 : 0; paint(); }) : null })),
          h("td", {}, h("div", { class: "chips" }, chip("album"), chip("ep"), chip("single"), chip("live"))),
          h("td", { class: "muted small" }, fmt.date(a.followed_ts)), h("td", { class: "muted small" }, a.last_check_ts ? fmt.ago(a.last_check_ts) : "never"),
          h("td", { class: "num" }, a.open_wants ? h("a", { href: link("queue", { q: a.name }) }, a.open_wants) : "0"),
          h("td", {}, h("div", { class: "actions" }, op ? h("button", { class: "btn xs", title: "check for releases on the next pass", onClick: action(null, async () => { await api("/api/artist_check", { mbid: a.mbid }); toast("Check queued — pass spawned", "ok"); }) }, "Check now") : null,
            h("button", { class: "btn xs", onClick: () => openLibArtist(a.name) }, "Shelf"),
            op ? h("button", { class: "btn xs danger", onClick: action(null, async () => { await api("/api/unfollow", { id: a.mbid }); r.artists = r.artists.filter(x => x !== a); paint(); }) }, "Unfollow") : null)));
      })))) : h("div", { class: "empty" }, h("div", { class: "big" }, "♫"), "Nobody followed yet. Follow from Discover → Artists, from the Spotify tab, or from a library artist's shelf."));
  };
  q.addEventListener("input", paint);
  mount(out, h("div", { class: "filterbar" }, q, h("span", { class: "muted small ml-auto" }, `${r.artists.length} followed · checked every ${r.check_hours}h · `, h("a", { href: "/calendar.ics", target: "_blank", rel: "noopener noreferrer" }, "calendar.ics"))), list);
  paint();
}
