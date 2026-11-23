/* Reusable library filtering: a facet bar (search · selects · quick-filter
   chips · A–Z · sort · group-by) whose state lives in the URL, a group-by
   drill-in view over the server's /groups endpoints, and "select all N
   matching" across the whole filtered set. Every list view (artists, albums,
   tracks) is built from these so they behave identically. */
import { api, h, mount, fmt, toast, busy, sel, setQuery, qs } from "../app.js";

const FACET_CACHE_TTL_MS = 5 * 60 * 1000;
let _facetCache = null;
let _facetInflight = null;
async function loadFacets(force = false) {
  if (!force && _facetCache && (Date.now() - _facetCache.ts) < FACET_CACHE_TTL_MS) return _facetCache;
  if (_facetInflight) return _facetInflight;
  _facetInflight = api("/api/library/genres?limit=600")
    .then((g) => {
      _facetCache = {
        genres: (g.genres || []).map(x => [x.genre, `${x.genre} (${fmt.num(x.albums)})`]),
        types: (g.types || []).filter(t => t.type).map(t => [t.type, `${t.type} (${fmt.num(t.count)})`]),
        ts: Date.now(),
      };
      return _facetCache;
    })
    .catch(() => _facetCache || { genres: [], types: [], ts: 0 })
    .finally(() => { _facetInflight = null; });
  return _facetInflight;
}
export async function genreOptions(force = false) { return (await loadFacets(force)).genres; }
export async function typeOptions(force = false) { return (await loadFacets(force)).types; }
export function invalidateFacetCache() { _facetCache = null; }
export const DECADES = [["", "Any decade"], ...[2020, 2010, 2000, 1990, 1980, 1970, 1960, 1950, 1940, 1930].map(d => [String(d), `${d}s`])];
export const ADDED = [["", "Added any time"], ["1d", "Added today"], ["7d", "Added this week"], ["30d", "Added this month"], ["90d", "Added 90 days"], ["365d", "Added this year"]];
export const FORMATS = [["", "Any format"], ["LOSSLESS", "Lossless"], ["LOSSY", "Lossy"], ["FLAC", "FLAC"], ["MP3", "MP3"], ["AAC", "AAC"], ["M4A", "M4A"], ["OPUS", "Opus"], ["OGG", "Ogg"], ["WAV", "WAV"]];

// state helpers — `filter` is a comma list of boolean quick-filters
export const hasFlag = (state, k) => (state.filter || "").split(",").filter(Boolean).includes(k);
export function toggleFlag(state, k, exclusive = []) {
  let set = new Set((state.filter || "").split(",").filter(Boolean));
  if (set.has(k)) set.delete(k); else { exclusive.forEach(x => set.delete(x)); set.add(k); }
  state.filter = [...set].join(",");
}
export function facetParams(state) {
  const p = {};
  for (const k of ["q", "filter", "genre", "decade", "type", "fmt", "added", "sort", "letter", "artist", "group", "scope", "year", "min_kbps", "max_kbps", "min_albums", "max_albums"]) if (state[k]) p[k] = state[k];
  return p;
}

/** Facet bar. spec: { search, chips:[[k,l,exclusiveWith[]]], selects:{genre,decade,type,fmt,added}, sorts:[[v,l]], groups:[[v,l]], letters } */
export function facetBar(state, spec, onChange) {
  const bar = h("div", { class: "facets" });
  let timer = null;
  const input = spec.search ? h("input", { class: "input", type: "search", placeholder: spec.search, value: state.q || "", enterkeyhint: "search" }) : null;
  if (input) input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.q = input.value.trim(); setQuery({ q: state.q }); onChange(); }, 320); });
  const selects = h("div", { class: "facet-selects" });
  const select = (key, opts, first) => {
    const s = h("select", { class: "select sm", "aria-label": key }, ...(first ? [first] : []).concat(opts).map(([v, l]) => h("option", { value: v, selected: (state[key] || "") === v }, l)));
    s.addEventListener("change", () => { state[key] = s.value; setQuery({ [key]: s.value }); onChange(); });
    return s;
  };
  const build = async () => {
    const kids = [];
    if (spec.selects?.genre) kids.push(select("genre", [["__none__", "(no genre)"], ...await genreOptions()], ["", "Any genre"]));
    if (spec.selects?.decade) kids.push(select("decade", DECADES.slice(1), DECADES[0]));
    if (spec.selects?.type) kids.push(select("type", [["__none__", "(untyped)"], ...await typeOptions()], ["", "Any type"]));
    if (spec.selects?.fmt) kids.push(select("fmt", FORMATS.slice(1), FORMATS[0]));
    if (spec.selects?.added) kids.push(select("added", ADDED.slice(1), ADDED[0]));
    if (spec.sorts) kids.push(select("sort", spec.sorts));
    if (spec.groups) kids.push(select("group", spec.groups, ["", "No grouping"]));
    mount(selects, ...kids);
  };
  build();
  const chips = spec.chips ? h("div", { class: "chips" }, ...spec.chips.map(([k, l, ex]) => {
    const c = h("span", { class: "chip" + (hasFlag(state, k) ? " active" : ""), onClick: () => { toggleFlag(state, k, ex || []); setQuery({ filter: state.filter }); paintChips(); onChange(); } }, l);
    c.dataset.k = k; return c;
  })) : null;
  const paintChips = () => { if (chips) [...chips.children].forEach(c => c.classList.toggle("active", hasFlag(state, c.dataset.k))); };
  const letters = spec.letters ? h("div", { class: "chips letters" }, ...["", ..."ABCDEFGHIJKLMNOPQRSTUVWXYZ#"].map(L => h("span", { class: "chip" + ((state.letter || "") === L ? " active" : ""), onClick: (e) => { state.letter = L; setQuery({ letter: L }); [...letters.children].forEach(c => c.classList.remove("active")); e.currentTarget.classList.add("active"); onChange(); } }, L || "All"))) : null;
  const active = h("div", { class: "row facet-active" });
  const paintActive = () => {
    const pills = [];
    const pill = (label, clear) => h("span", { class: "chip active", onClick: () => { clear(); onChange(); } }, label, " ✕");
    if (state.genre) pills.push(pill(`genre: ${state.genre === "__none__" ? "none" : state.genre}`, () => { state.genre = ""; setQuery({ genre: "" }); build(); }));
    if (state.decade) pills.push(pill(`${state.decade}s`, () => { state.decade = ""; setQuery({ decade: "" }); build(); }));
    if (state.type) pills.push(pill(`type: ${state.type}`, () => { state.type = ""; setQuery({ type: "" }); build(); }));
    if (state.fmt) pills.push(pill(`format: ${state.fmt}`, () => { state.fmt = ""; setQuery({ fmt: "" }); build(); }));
    if (state.added) pills.push(pill(`added: ${state.added}`, () => { state.added = ""; setQuery({ added: "" }); build(); }));
    if (state.artist) pills.push(pill(`artist: ${state.artist}`, () => { state.artist = ""; setQuery({ artist: "" }); }));
    if (state.year) pills.push(pill(`year: ${state.year}`, () => { state.year = ""; setQuery({ year: "" }); }));
    if (state.min_albums || state.max_albums) {
      const lbl = state.min_albums && state.max_albums
        ? (String(state.min_albums) === String(state.max_albums) ? `albums: ${state.min_albums}` : `albums: ${state.min_albums}–${state.max_albums}`)
        : state.min_albums ? `albums: ≥${state.min_albums}` : `albums: ≤${state.max_albums}`;
      pills.push(pill(lbl, () => { state.min_albums = ""; state.max_albums = ""; setQuery({ min_albums: "", max_albums: "" }); }));
    }
    if (state.min_kbps || state.max_kbps) {
      const lbl = state.min_kbps && state.max_kbps ? `kbps: ${state.min_kbps}–${state.max_kbps}` : state.min_kbps ? `kbps: ≥${state.min_kbps}` : `kbps: ≤${state.max_kbps}`;
      pills.push(pill(lbl, () => { state.min_kbps = ""; state.max_kbps = ""; setQuery({ min_kbps: "", max_kbps: "" }); }));
    }
    mount(active, ...pills, pills.length || state.filter || state.q || state.letter ? h("button", { class: "btn xs ghost", onClick: () => { for (const k of ["q", "filter", "genre", "decade", "type", "fmt", "added", "artist", "year", "letter", "min_albums", "max_albums", "min_kbps", "max_kbps"]) { state[k] = ""; } setQuery({ q: "", filter: "", genre: "", decade: "", type: "", fmt: "", added: "", artist: "", year: "", letter: "", min_albums: "", max_albums: "", min_kbps: "", max_kbps: "" }); if (input) input.value = ""; paintChips(); if (letters) [...letters.children].forEach((c, i) => c.classList.toggle("active", i === 0)); build(); onChange(); } }, "Clear all") : null);
  };
  mount(bar, h("div", { class: "facet-row" }, input, selects), chips, letters, active);
  return { el: bar, paintActive, paintChips, rebuild: build };
}

/** Group-by drill-in: fetches `${base}/groups?by=…` with the current filters and renders cards. */
export async function groupView(out, { base, state, unit, onPick }) {
  mount(out, h("div", { class: "skel" }, h("div"), h("div"), h("div")));
  const by = state.group;
  const p = { ...facetParams(state), by }; delete p.group; delete p.sort;
  let res;
  try { res = await api(`${base}/groups${qs(p)}`); } catch (e) { mount(out, h("div", { class: "notice bad" }, e.message)); return; }
  const rows = res.groups || [];
  const max = Math.max(1, ...rows.map(g => g[unit] || 0));
  const label = (k) => by === "decade" || by === "year" ? (k === "0" ? "(no year)" : (by === "decade" ? k + "s" : k)) : k;
  mount(out, h("div", { class: "row between mb" }, h("span", { class: "muted small" }, `${fmt.num(rows.length)} groups by ${by}`), h("button", { class: "btn xs ghost", onClick: () => { state.group = ""; setQuery({ group: "" }); onPick(null); } }, "Back to list")),
    rows.length ? h("div", { class: "grid auto" }, ...rows.map(g => h("div", { class: "card tight click group-card", tabindex: 0, role: "button", onClick: () => onPick(g.key) },
      h("div", { class: "row between" }, h("div", { class: "b ellip" }, label(g.key)), h("span", { class: "badge acc" }, fmt.num(g[unit]))),
      h("div", { class: "progress info mt", style: { marginTop: "6px" } }, h("div", { style: { width: (100 * (g[unit] || 0) / max) + "%" } })),
      h("div", { class: "muted xs", style: { marginTop: "4px" } }, [g.artists !== undefined && unit !== "artists" ? `${fmt.num(g.artists)} artists` : null, g.albums !== undefined && unit !== "albums" ? `${fmt.num(g.albums)} albums` : null, g.tracks !== undefined && unit !== "tracks" ? `${fmt.num(g.tracks)} tracks` : null, g.hours ? `${g.hours} h` : null, g.lossless_share !== undefined ? `${Math.round(g.lossless_share * 100)}% lossless` : null].filter(Boolean).join(" · "))))) : h("div", { class: "empty" }, "Nothing matches."));
}

/** Applies a picked group value to the state as the matching filter. */
export function applyGroupPick(state, key) {
  const by = state.group;
  const map = { genre: "genre", decade: "decade", type: "type", format: "fmt", artist: "artist", year: "year" };
  if (key === null) return;
  if (by === "quality") toggleFlag(state, key, ["lossless", "lossy", "mixed"]);
  else if (by === "letter") state.letter = key;
  else if (by === "kbps") { state.fmt = key === "lossless" ? "LOSSLESS" : "LOSSY"; }
  else if (by === "size") { const m = { "1 album": [1, 1], "2-3 albums": [2, 3], "4-9 albums": [4, 9], "10+ albums": [10, ""] }[key]; if (m) { state.min_albums = m[0]; state.max_albums = m[1]; } }
  else if (map[by]) state[map[by]] = key === "(no genre)" ? "__none__" : key === "(untyped)" ? "__none__" : key;
  state.group = "";
  setQuery({ ...facetParams(state), group: "", filter: state.filter || "", min_albums: state.min_albums || "", max_albums: state.max_albums || "" });
}

/** "Select all N matching" — pulls ids for the whole filtered set (server cap) into the selection. */
export function selectAllButton({ total, url, key, map, label }) {
  if (!total) return null;
  return h("button", { class: "btn xs ghost", onClick: async (ev) => {
    const r = await busy(ev.currentTarget, api(url));
    const rows = r[key] || [];
    rows.forEach(x => { const it = map(x); sel.set(it.key, it.item, true); });
    document.querySelectorAll(".check").forEach(c => c.classList.add("on"));
    toast(r.capped ? `Selected the first ${fmt.num(rows.length)} of ${fmt.num(r.total)} (cap)` : `Selected all ${fmt.num(rows.length)}`, r.capped ? "warn" : "ok");
  } }, `Select all ${fmt.num(total)}`);
}

/** Queue a download bundle for a selection and point at the Downloads view. */
export async function queueDownload({ title, item_ids = [], album_ids = [], artists = [] }) {
  const est = await api("/api/downloads/estimate", { item_ids, album_ids, artists });
  if (!est.count) { toast("Nothing on disk to download", "warn"); return null; }
  if (est.bytes > est.max_bundle_bytes) { toast(`${fmt.bytes(est.bytes)} exceeds the ${fmt.bytes(est.max_bundle_bytes)} bundle cap — split the selection`, "bad", 6000); return null; }
  const r = await api("/api/downloads", { title, item_ids, album_ids, artists });
  if (!r.ok) { toast(r.error, "bad"); return null; }
  toast(h("span", {}, `Bundling ${fmt.num(est.count)} files · ${fmt.bytes(est.bytes)} — `, h("a", { href: "#/downloads", class: "acc" }, "open Downloads")), "ok", 6000);
  return r.bundle;
}
