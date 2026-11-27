import { api, h, mount, fmt, toast, action, confirm, refreshStatusSoon, session, can, isAdmin, logout, modal } from "../app.js";
import { field, blockButton, createTabs, debouncedInput } from "./_shared.js";

export default {
  async render(root, r) {
    const op = can("operator");
    const defs = [
      ...(op ? [["automation", "Automation"], ["spotify", "Spotify"], ["peers", "Peers"], ["blocklist", "Blocklist"]] : []),
      ["account", "Account"],
      ...(isAdmin() ? [["users", "Users"]] : []),
      ...(op ? [["system", "System"]] : []),
    ];
    const tabCtl = createTabs(defs, r.q.tab || (op ? "automation" : "account"), (tab, { alive, out }) => ({
      automation, spotify, peers, blocklist, account, users, system,
    })[tab](out, alive));
    mount(root, h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Settings"), h("div", { class: "sub" }, "Programs, tunables, connections, the import blocklist, your account and (admins) everyone else's."))), tabCtl.el, tabCtl.out);

    async function automation(out, alive = () => out.isConnected) {
      mount(out, h("div", { class: "skel" }, h("div"), h("div"), h("div")));
      let a; try { a = await api("/api/automation?count=1"); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
      if (!alive()) return;
      const p = a.programs, s = a.settings, lim = a.limits;
      const prog = (title, blurb, body) => h("div", { class: "card" }, h("h3", {}, title), h("div", { class: "hint mb" }, blurb), body);
      const progCtl = (name, st, cand) => { const open = (st.wanted || 0) + (st.grabbed || 0); const parked = st.skipped || 0; return h("div", { class: "stack" },
        h("div", { class: "row" }, h("span", { class: "badge " + (open ? "ok" : parked ? "warn" : "dim") }, open ? `${open} open` : parked ? "paused" : "not started"), h("span", { class: "muted small" }, `${st.done || 0} done · ${parked} parked · ${st.exhausted || 0} exhausted`, cand !== null && cand !== undefined ? ` · ${cand} candidates in beets` : "")),
        h("div", { class: "row" },
          h("button", { class: "btn sm primary", onClick: action(null, async () => { if (await confirm({ title: `Seed ${name}?`, body: `Creates a want for every candidate album (${cand ?? "?"}). Re-running picks up new ones; existing wants are untouched.`, ok: "Seed" })) { const r = await api("/api/automation/program", { program: name, action: "seed" }); toast(`${r.seeded} new wants`, "ok"); refreshStatusSoon(); automation(out, alive); } }) }, "Seed / rescan"),
          open ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/automation/program", { program: name, action: "pause" }); toast(`${r.paused} parked`); automation(out, alive); refreshStatusSoon(); }) }, "Pause") : null,
          parked ? h("button", { class: "btn sm", onClick: action(null, async () => { const r = await api("/api/automation/program", { program: name, action: "resume" }); toast(`${r.resumed} resumed`, "ok"); automation(out, alive); refreshStatusSoon(); }) }, "Resume") : null,
          (open || parked) ? h("button", { class: "btn sm danger", onClick: action(null, async () => { if (await confirm({ title: `Clear all ${name} wants?`, body: "Done wants stay. Everything open or parked becomes skipped.", ok: "Clear", danger: true, typed: name })) { const r = await api("/api/automation/program", { program: name, action: "clear" }); toast(`${r.cleared} cleared`); automation(out, alive); refreshStatusSoon(); } }) }, "Clear") : null)); };
      const knob = (key, label, hint) => { const spec = lim[key]; const inp = h("input", { class: "input sm", type: "number", value: s[key], min: spec?.[0], max: spec?.[1], style: { maxWidth: "120px" } }); inp.addEventListener("change", action(null, async () => { const r = await api("/api/automation/settings", { [key]: +inp.value }); toast(`${label} → ${r.applied[key]}`, "ok"); })); return field(label, inp, hint + (spec ? ` (${spec[0]}–${spec[1]})` : "")); };
      const boolKnob = (key, label, hint) => { const t = h("button", { class: "toggle" + (s[key] ? " on" : ""), onClick: action(null, async (e) => { const btn = e.currentTarget; const on = !btn.classList.contains("on"); await api("/api/automation/settings", { [key]: on }); btn.classList.toggle("on", on); toast(`${label} ${on ? "on" : "off"}`, "ok"); }) }); return h("div", { class: "toggle-row" }, t, h("div", {}, h("div", { class: "lbl" }, label), h("div", { class: "sub" }, hint))); };
      mount(out,
        h("div", { class: "notice " + (a.apply ? "good" : "warn") + " mb" }, h("div", { class: "body" }, h("b", {}, a.apply ? "LIVE. " : "SHADOW. "), a.apply ? "Passes enqueue downloads and write playlists." : "Passes search and log but enqueue nothing. Flip the pill in the top bar to go live.")),
        h("div", { class: "grid c2 mb" },
          prog("Gap-fill", "Partial albums in beets (below coverage) become wants carrying the expected track count; a landed download that raises coverage retires them. Requests from album pages ride the same lifecycle at higher priority.", progCtl("gapfill", p.gapfill, p.gapfill_candidates)),
          prog("Lossless upgrades", "Lossy-only albums become lowest-priority wants that only accept lossless candidates. Retire when the same beets album row holds lossless files; nightly dedup keeps the best master.", progCtl("upgrade", p.upgrade, p.upgrade_candidates)),
          prog("Release watch", `${p.release_watch.followed} followed artists checked every ${p.release_watch.check_hours}h on MusicBrainz; new releases matching each artist's type profile auto-want.`, h("a", { class: "btn sm", href: "#/artists" }, "Manage artists")),
          prog("Spotify hunts", `${p.playlist_hunts} playlists hunting. Saved albums ${p.spotify_sync.want_sources.includes("saved_albums") ? "auto-want" : "do not auto-want"} (HUNTER_WANT_SOURCES).`, h("a", { class: "btn sm", href: "#/spotify" }, "Spotify tab")),
          prog("Prowlarr fallback", `After ${s.prowlarr_after_misses} straight Soulseek misses, search Prowlarr's audio indexers (torrent + usenet); the grab lands in your configured downloads directory for the watcher. ${p.prowlarr.configured ? "" : "PROWLARR_API_KEY not set."}`, h("div", { class: "stack" }, boolKnob("prowlarr_fallback", "Enabled", "Soulseek is always tried first"), knob("prowlarr_after_misses", "After misses", "consecutive Soulseek misses before asking Prowlarr"))),
          prog("Housekeeping", `Weekly in-pass reseed keeps gapfill/upgrade fresh once started (last ${p.last_reseed_age_days ?? "—"} d ago). hunter.db is snapshotted daily to /state/backups.`, h("a", { class: "btn sm", href: "#/settings?tab=system" }, "Backups"))),
        h("div", { class: "section-title" }, "Tuning"),
        h("div", { class: "card" }, h("div", { class: "grid c3" },
          knob("interval", "Pass interval (s)", "scheduler cadence"), knob("max_grabs_per_pass", "Max grabs / pass", "avalanche guard"), knob("min_bitrate", "Min bitrate (kbps)", "lossy floor; 0 = off"),
          knob("max_attempts", "Max grab attempts", "before exhausted"), knob("search_miss_cap", "Search-miss cap", "candidate-less searches before 'unfindable'")),
          h("div", { class: "mt" }, boolKnob("prefer_lossless", "Prefer lossless", "rank lossless candidates first")),
          h("details", { class: "acc mt" }, h("summary", {}, "Env-only settings (read-only)"), h("div", { class: "acc-body" }, h("dl", { class: "kv small" }, ...Object.entries(a.readonly).flatMap(([k, v]) => [h("dt", {}, k), h("dd", { class: "mono" }, String(v))]))))));
    }

    async function spotify(out, alive = () => out.isConnected) {
      let st; try { st = await api("/api/settings"); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
      if (!alive()) return;
      const sp = st.spotify;
      const spdc = h("input", { class: "input mono", placeholder: "sp_dc cookie value", autocomplete: "off" });
      const cid = h("input", { class: "input", placeholder: "Client ID" }), csec = h("input", { class: "input", type: "password", placeholder: "Client Secret" }), paste = h("input", { class: "input", placeholder: "Paste the 127.0.0.1/callback?code=… URL" });
      const status = sp.connected ? (sp.last_sync?.error ? h("div", { class: "notice bad mb" }, h("div", { class: "body" }, h("b", {}, "Connected, but the last sync failed: "), sp.last_sync.error)) : h("div", { class: "notice good mb" }, h("div", { class: "body" }, h("b", {}, "Connected "), `via ${sp.cookie_auth ? "sp_dc cookie" : "developer app"}`, sp.last_sync ? ` · last sync ${sp.last_sync.status}${sp.last_sync.requests ? ` (${sp.last_sync.requests} requests)` : ""}` : ""))) : h("div", { class: "notice warn mb" }, h("div", { class: "body" }, h("b", {}, "Not connected.")));
      mount(out, status,
        h("div", { class: "grid c2" },
          h("div", { class: "card accent" }, h("h3", {}, "Browser cookie (recommended)"), h("div", { class: "hint mb" }, "No dev app, no Premium gate. On a computer logged into ", h("b", {}, "open.spotify.com"), ": DevTools (F12) → Application → Cookies → https://open.spotify.com → copy ", h("b", {}, "sp_dc"), ". Lasts about a year. ", sp.proxy ? "Spotify traffic egresses through the VPN proxy." : ""),
            h("div", { class: "row nowrap" }, spdc, h("button", { class: "btn primary", onClick: action(null, async () => { const r = await api("/api/spotify/spdc", { sp_dc: spdc.value.trim() }); toast(`Connected as ${r.user} — syncing`, "ok"); spotify(out, alive); refreshStatusSoon(); }) }, "Connect"))),
          h("div", { class: "card" }, h("h3", {}, "Developer app"), h("div", { class: "hint mb" }, "Create an app at developer.spotify.com/dashboard with redirect URI exactly ", h("code", { class: "mono" }, "http://127.0.0.1:8888/callback"), " (Spotify rejects localhost / https / trailing slash)."),
            h("div", { class: "stack" }, h("div", { class: "row nowrap" }, cid, csec, h("button", { class: "btn", onClick: action(null, async () => { await api("/api/spotify/creds", { client_id: cid.value.trim(), client_secret: csec.value.trim() }); toast("Saved", "ok"); }) }, "Save")),
              h("button", { class: "btn ghost", onClick: action(null, async () => { const r = await api("/api/spotify/auth_url"); window.open(r.url, "_blank", "noopener,noreferrer"); toast(h("span", {}, "Opened Spotify approval — ", h("a", { href: r.url, target: "_blank", rel: "noopener noreferrer", class: "acc" }, "open link if blocked")), "ok", 10000); }) }, "Open Spotify approval…"),
              h("div", { class: "row nowrap" }, paste, h("button", { class: "btn", onClick: action(null, async () => { await api("/api/spotify/exchange", { url: paste.value.trim() }); toast("Connected", "ok"); spotify(out, alive); }) }, "Connect"))))),
        sp.connected ? h("div", { class: "row mt" }, h("button", { class: "btn sm danger", onClick: action(null, async () => { if (await confirm({ title: "Disconnect Spotify?", body: "Removes the stored token/cookie. The cache stays.", ok: "Disconnect", danger: true })) { await api("/api/spotify/disconnect", {}); spotify(out, alive); } }) }, "Disconnect")) : null);
    }

    async function peers(out, alive = () => out.isConnected) {
      let p; try { p = await api("/api/peers"); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
      if (!alive()) return;
      const banInp = h("input", { class: "input sm", placeholder: "username", style: { maxWidth: "220px" } });
      mount(out, h("div", { class: "hint mb" }, `Peer reputation from grab outcomes. A peer whose grabs all fail ${p.strikes} times is auto-banned. Banned peers are excluded from every hunt and every picker.`),
        h("div", { class: "grid c2" },
          h("div", { class: "card" }, h("h3", {}, "Banned", h("span", { class: "right" }, p.banned.length)), h("div", { class: "row mb" }, banInp, h("button", { class: "btn sm danger", onClick: action(null, async () => { await api("/api/ban", { username: banInp.value.trim(), reason: "manual" }); banInp.value = ""; peers(out, alive); }) }, "Ban")),
            p.banned.length ? h("div", { class: "list" }, ...p.banned.map(b => h("div", { class: "item" }, h("div", { class: "main" }, h("div", { class: "t mono small" }, b.username), h("div", { class: "s" }, b.reason, " · ", b.at || "")), h("div", { class: "end" }, h("button", { class: "btn xs", onClick: action(null, async () => { await api("/api/unban", { username: b.username }); peers(out, alive); }) }, "Unban"))))) : h("div", { class: "hint" }, "none")),
          h("div", { class: "card" }, h("h3", {}, "Reputation", h("span", { class: "right" }, "top peers by grabs")), h("div", { class: "table-wrap", style: { border: 0 } }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "Peer"), h("th", { class: "num" }, "Grabs"), h("th", { class: "num" }, "OK"), h("th", { class: "num" }, "Failed"), h("th", { class: "num" }, "Rep"), h("th", {}))),
            h("tbody", {}, ...p.stats.slice(0, 60).map(s => h("tr", {}, h("td", { class: "mono small" }, s.username, s.banned ? h("span", { class: "badge bad", style: { marginLeft: "6px" } }, "banned") : null), h("td", { class: "num" }, s.grabs), h("td", { class: "num good" }, s.completed), h("td", { class: "num " + (s.failed ? "bad" : "") }, s.failed), h("td", { class: "num" }, s.reputation), h("td", {}, h("div", { class: "actions" }, !s.banned ? h("button", { class: "btn xs danger", onClick: action(null, async () => { await api("/api/ban", { username: s.username, reason: "manual" }); peers(out, alive); }) }, "Ban") : null))))))))));
    }

    // ---- import blocklist ------------------------------------------------------
    async function blocklist(out, alive) {
      let q = "", loadGen = 0;
      const input = debouncedInput({ placeholder: "filter", delay: 250, style: { maxWidth: "260px" }, onInput: (v) => { q = v; load(); } });
      const addInp = h("textarea", { class: "input", rows: 2, placeholder: "exact albumartist to block — one per line for many", style: { maxWidth: "460px", resize: "vertical" } });
      const list = h("div", {});
      const head = h("div", { class: "row mb" });
      const addBtn = h("button", { class: "btn sm danger", onClick: action(null, async () => {
        const names = addInp.value.split(/\n/).map(s => s.trim()).filter(Boolean);
        if (!names.length) return;
        if (!(await confirm({ title: `Block ${names.length === 1 ? names[0] : names.length + " artists"}?`, body: "Future downloads with this albumartist route to the quarantine directory instead of the library; open hunter wants for them are parked. Existing files are untouched — remove them from the artist's shelf if you want them gone.", ok: "Block", danger: true }))) return;
        const r = await api("/api/library/blocklist", { add: names });
        toast(`${r.added.length} blocked${r.skipped.length ? ` · ${r.skipped.length} already listed` : ""}${r.wants_parked ? ` · ${r.wants_parked} wants parked` : ""}`, "ok");
        addInp.value = ""; load();
      }) }, "Block");
      mount(out, h("div", { class: "hint mb" }, "The blocklist is the configured artist blocklist file — the watcher checks every download's albumartist against it (case, punctuation and accent-insensitive; \"the\" ignored) and quarantines matches before they ever reach beets. hunter also refuses to create wants for blocked artists. Edits here go through the beets container and take effect on the next import."),
        can("operator") ? h("div", { class: "card mb" }, h("h3", {}, "Block an artist"), h("div", { class: "row nowrap", style: { alignItems: "flex-start" } }, addInp, addBtn), h("div", { class: "hint mt" }, "Tip: from an artist's shelf, ", h("b", {}, "Remove artist…"), " can block and quarantine in one go.")) : null,
        head, h("div", { class: "filterbar" }, input), list);
      async function load() {
        const myGen = ++loadGen;
        let r;
        try { r = await api(`/api/library/blocklist?q=${encodeURIComponent(q)}`); } catch (e) { if (myGen === loadGen && alive()) mount(list, h("div", { class: "notice bad" }, e.message)); return; }
        if (myGen !== loadGen || !alive()) return;
        mount(head, h("span", { class: "badge " + (r.available ? "dim" : "bad") }, r.available ? `${fmt.num(r.count)} blocked artists` : "blocklist file not readable"), h("a", { class: "btn xs ghost ml-auto", href: "#/library?tab=removed" }, "Removals →"));
        mount(list, r.entries.length ? h("div", { class: "card tight" }, h("div", { class: "list" }, ...r.entries.map(n => h("div", { class: "item" }, h("div", { class: "main" }, h("a", { class: "t", href: `#/library/artist/${encodeURIComponent(n)}` }, n)), h("div", { class: "end" }, blockButton(n, true, { cls: "btn xs", after: load })))))) : h("div", { class: "empty" }, q ? "No entries match." : "Nothing blocked."));
      }
      await load();
    }

    // ---- own account -----------------------------------------------------------
    async function account(out, alive) {
      const me = session.me;
      const cur = h("input", { class: "input", type: "password", placeholder: "current password", autocomplete: "current-password" });
      const pw = h("input", { class: "input", type: "password", placeholder: "new password (8+ chars)", autocomplete: "new-password" });
      const pw2 = h("input", { class: "input", type: "password", placeholder: "repeat new password", autocomplete: "new-password" });
      const sessBox = h("div", { class: "card" }, h("h3", {}, "Sessions"), h("div", { class: "skel" }, h("div")));
      const paintSessions = async () => {
        let s; try { s = await api("/api/auth/sessions"); } catch (e) { if (alive()) mount(sessBox, h("h3", {}, "Sessions"), h("div", { class: "notice bad" }, e.message)); return; }
        if (!alive()) return;
        mount(sessBox, h("h3", {}, "Sessions", h("span", { class: "right" }, `${s.sessions.length} active`)),
          h("div", { class: "hint mb" }, "Every browser you are signed in on. Sessions slide with use; 'remember me' ones last 30 days."),
          h("div", { class: "list" }, ...s.sessions.map(x => h("div", { class: "item" }, h("div", { class: "main" }, h("div", { class: "t small" }, x.current ? h("span", { class: "badge ok", style: { marginRight: "6px" } }, "this browser") : null, (x.ua || "unknown client").slice(0, 70)), h("div", { class: "s" }, `${x.ip || "?"} · signed in ${fmt.datetime(x.created_ts)} · last seen ${fmt.ago(x.last_seen_ts)}${x.remember ? " · remembered" : ""}`)), h("div", { class: "end" }, !x.current ? h("button", { class: "btn xs danger", onClick: action(null, async () => { await api("/api/auth/session/revoke", { id: x.id }); toast("Session revoked"); paintSessions(); }) }, "Revoke") : null)))),
          s.sessions.length > 1 ? h("div", { class: "row mt" }, h("button", { class: "btn sm danger", onClick: action(null, async () => { const r = await api("/api/auth/logout_all", {}); toast(`${r.revoked} other sessions signed out`, "ok"); paintSessions(); }) }, "Sign out everywhere else")) : null);
      };
      mount(out, h("div", { class: "grid c2" },
        h("div", { class: "stack", style: { gap: "12px" } },
          h("div", { class: "card" }, h("h3", {}, "You"), h("div", { class: "row mb" }, h("span", { class: "avatar", style: { width: "40px", height: "40px", borderRadius: "50%", background: "var(--acc-dim)", color: "var(--acc2)", display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: "700" } }, (me.display || me.username).slice(0, 1).toUpperCase()), h("div", {}, h("div", { class: "b" }, me.display || me.username), h("div", { class: "muted small" }, me.username, " · ", h("span", { class: "badge " + (me.role === "admin" ? "acc" : me.role === "operator" ? "info" : "dim") }, me.role), me.via === "token" ? " · API token" : ""))),
            h("div", { class: "hint" }, me.role === "admin" ? "Administrator: everything, plus user management and the audit trail." : me.role === "operator" ? "Operator: every action in the console. Ask an admin for user management." : "Viewer: read-only, plus ZIP downloads and managing your own sessions."),
            h("div", { class: "row mt" }, h("button", { class: "btn sm", onClick: logout }, "Sign out"))),
          me.via === "cookie" ? h("div", { class: "card" }, h("h3", {}, "Change password"), h("div", { class: "stack" }, cur, pw, pw2, h("div", { class: "row" }, h("button", { class: "btn sm primary", onClick: action(null, async () => { if (pw.value !== pw2.value) throw new Error("passwords do not match"); await api("/api/auth/password", { current: cur.value, new: pw.value }); toast("Password changed · other sessions signed out", "ok"); cur.value = pw.value = pw2.value = ""; paintSessions(); }) }, "Change password")))) : null),
        sessBox));
      paintSessions();
    }

    // ---- user management (admin) ----------------------------------------------
    async function users(out, alive) {
      if (!isAdmin()) { mount(out, h("div", { class: "notice warn" }, h("div", { class: "body" }, "Admins only."))); return; }
      const list = h("div", {});
      const roleBadge = (r) => h("span", { class: "badge " + (r === "admin" ? "acc" : r === "operator" ? "info" : "dim") }, r);
      const addUser = () => {
        const u = h("input", { class: "input", placeholder: "username", autocapitalize: "none" }), d = h("input", { class: "input", placeholder: "display name (optional)" });
        const p = h("input", { class: "input", type: "password", placeholder: "temporary password (8+ chars)", autocomplete: "new-password" });
        const role = h("select", { class: "select" }, ...session.roles.map(r => h("option", { value: r, selected: r === "operator" }, r)));
        const must = h("input", { type: "checkbox", checked: true });
        modal({ title: "Add user", body: h("div", { class: "stack" }, field("Username", u), field("Display name", d), field("Role", role, "viewer = read-only · operator = every action · admin = + users"), field("Password", p), h("label", { class: "row small" }, must, " must change password at first sign-in")),
          actions: [{ label: "Create", cls: "primary", onClick: async () => { await api("/api/users/create", { username: u.value.trim(), display: d.value.trim(), password: p.value, role: role.value, must_change_pw: must.checked }); toast(`Created ${u.value.trim()}`, "ok"); load(); } }] });
        setTimeout(() => u.focus(), 40);
      };
      const resetPw = (x) => {
        const p = h("input", { class: "input", type: "password", placeholder: "new temporary password", autocomplete: "new-password" });
        modal({ title: `Reset password · ${x.username}`, body: h("div", { class: "stack" }, h("div", { class: "hint" }, "All their sessions are revoked and they must pick a new password at next sign-in."), p),
          actions: [{ label: "Reset", cls: "danger", onClick: async () => { await api("/api/users/password", { id: x.id, new: p.value }); toast("Password reset", "ok"); load(); } }] });
        setTimeout(() => p.focus(), 40);
      };
      mount(out, h("div", { class: "row mb" }, h("div", { class: "hint", style: { flex: 1 } }, "Roles: ", h("b", {}, "viewer"), " reads everything; ", h("b", {}, "operator"), " runs everything (wants, jobs, beets, removals); ", h("b", {}, "admin"), " also manages users and sees the audit trail. The last active admin cannot be demoted, disabled or deleted. Lost admin password → ", h("code", { class: "mono" }, "docker exec -it hunter hunter user passwd <name>"), "."), h("button", { class: "btn sm primary", onClick: addUser }, "Add user")), list);
      async function load() {
        let r; try { r = await api("/api/users"); } catch (e) { if (alive()) mount(list, h("div", { class: "notice bad" }, e.message)); return; }
        if (!alive()) return;
        mount(list, h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "User"), h("th", {}, "Role"), h("th", {}, "Status"), h("th", {}, "Last sign-in"), h("th", { class: "num" }, "Sessions"), h("th", {}, "Created"), h("th", {}))),
          h("tbody", {}, ...r.users.map(x => { const me = x.id === session.me.id;
            const roleSel = h("select", { class: "select sm", onChange: action(null, async (e) => { const v = e.target.value; if (!(await confirm({ title: `Make ${x.username} ${v}?`, ok: "Change" }))) { e.target.value = x.role; return; } await api("/api/users/update", { id: x.id, role: v }); toast(`${x.username} → ${v}`, "ok"); load(); }) }, ...r.roles.map(rr => h("option", { value: rr, selected: rr === x.role }, rr)));
            return h("tr", { class: x.disabled ? "bad" : "" },
              h("td", {}, h("div", { class: "b" }, x.display || x.username, me ? h("span", { class: "badge ok", style: { marginLeft: "6px" } }, "you") : null), h("div", { class: "muted small mono" }, x.username)),
              h("td", {}, me ? roleBadge(x.role) : roleSel),
              h("td", {}, x.disabled ? h("span", { class: "badge bad" }, "disabled") : h("span", { class: "badge ok" }, "active"), x.must_change_pw ? h("span", { class: "badge warn", style: { marginLeft: "4px" } }, "must change pw") : null),
              h("td", { class: "muted small" }, x.last_login_ts ? fmt.ago(x.last_login_ts) : "never"),
              h("td", { class: "num" }, x.sessions),
              h("td", { class: "muted small" }, fmt.date(x.created_ts), x.created_by ? ` by ${x.created_by}` : ""),
              h("td", {}, h("div", { class: "actions" },
                !me ? h("button", { class: "btn xs", onClick: () => resetPw(x) }, "Reset pw") : null,
                x.sessions ? h("button", { class: "btn xs", onClick: action(null, async () => { const j = await api("/api/users/revoke", { id: x.id }); toast(`${j.revoked} sessions revoked`); load(); }) }, "Sign out") : null,
                !me ? h("button", { class: "btn xs " + (x.disabled ? "good" : ""), onClick: action(null, async () => { await api("/api/users/update", { id: x.id, disabled: !x.disabled }); toast(x.disabled ? "Enabled" : "Disabled"); load(); }) }, x.disabled ? "Enable" : "Disable") : null,
                !me ? h("button", { class: "btn xs danger", onClick: action(null, async () => { if (!(await confirm({ title: `Delete ${x.username}?`, body: "Their sessions end immediately. The audit trail keeps their name.", ok: "Delete", danger: true, typed: x.username }))) return; await api("/api/users/delete", { id: x.id }); toast("Deleted"); load(); }) }, "Delete") : null))); })))));
      }
      await load();
    }

    async function system(out, alive = () => out.isConnected) {
      let st, b, svc, db, me;
      try { [st, b, svc, db, me] = await Promise.all([api("/api/settings"), api("/api/backups"), api("/api/services").catch(() => null), api("/api/beets/db_stats").catch(() => ({})), api("/api/auth/me").catch(() => ({}))]); } catch (e) { if (alive()) mount(out, h("div", { class: "notice bad" }, e.message)); return; }
      if (!alive()) return;
      const conn = (name, ok, detail) => h("div", { class: "svc" }, h("span", { class: "dot " + (ok ? "ok" : "bad") }), h("span", { class: "nm" }, name), h("span", { class: "st" }, detail || (ok ? "configured" : "not configured")));
      mount(out, h("div", { class: "grid c2" },
        h("div", { class: "card" }, h("h3", {}, "Connections"),
          conn("Spotify", st.spotify.connected, st.spotify.connected ? (st.spotify.cookie_auth ? "sp_dc cookie" : "dev app") : ""), conn("slskd", st.slskd.configured, st.slskd.url), conn("Navidrome", st.navidrome.configured, st.navidrome.url), conn("Prowlarr", st.prowlarr.configured), conn("AudioMuse", st.audiomuse.configured), conn("Webhook", st.webhook), conn("beets console", st.console, st.console ? "docker socket mounted" : "socket missing"),
          h("div", { class: "section-title" }, "Paths"), h("dl", { class: "kv small" }, ...Object.entries(st.paths).flatMap(([k, v]) => [h("dt", {}, k), h("dd", { class: "mono" }, v)])),
          h("div", { class: "section-title" }, "beets DB"), h("dl", { class: "kv small" }, h("dt", {}, "musiclibrary.db"), h("dd", { class: "mono" }, fmt.bytes(db.db)), h("dt", {}, "WAL"), h("dd", { class: "mono" }, fmt.bytes(db.wal)), h("dt", {}, "import.log"), h("dd", { class: "mono" }, fmt.bytes(db.import_log), db.import_log_mtime ? ` · ${fmt.ago(db.import_log_mtime)}` : ""))),
        h("div", { class: "stack", style: { gap: "12px" } },
          h("div", { class: "card" }, h("h3", {}, "Access"), h("div", { class: "hint" }, "Browser access is by account (Settings → Account / Users). Scripts and curl can use ", h("code", { class: "mono" }, "X-Hunter-Token"), " with ", h("code", { class: "mono" }, "HUNTER_UI_TOKEN"), " from .env — it acts as an admin named ", h("i", {}, "token"), ". ", me.token_configured ? h("span", { class: "badge ok" }, "token configured") : h("span", { class: "badge dim" }, "no API token set"))),
          h("div", { class: "card" }, h("h3", {}, "Backups", h("button", { class: "btn xs right", onClick: action(null, async () => { const r = await api("/api/backups/hunter", {}); toast(`Snapshot ${r.backup}`, "ok"); system(out, alive); }) }, "Snapshot hunter.db now")),
            h("div", { class: "hint mb" }, `hunter.db ${fmt.bytes(b.state_db_size)} · beets DB ${fmt.bytes(b.beets_db_size)}. Daily hunter snapshots keep 14 days; beets backups happen at the start of nightly maintenance. Accounts and sessions live in hunter.db too.`),
            h("div", { class: "grid c2" }, h("div", {}, h("div", { class: "section-title" }, "hunter"), ...b.hunter.slice(0, 8).map(f => h("div", { class: "row small mono" }, h("span", { class: "ellip" }, f.name), h("span", { class: "muted ml-auto" }, fmt.bytes(f.size))))), h("div", {}, h("div", { class: "section-title" }, "beets"), ...b.beets.slice(0, 8).map(f => h("div", { class: "row small mono" }, h("span", { class: "ellip" }, f.name), h("span", { class: "muted ml-auto" }, fmt.bytes(f.size))))))),
          svc?.available ? h("div", { class: "card" }, h("h3", {}, "Containers"), ...svc.services.map(s => h("div", { class: "svc" }, h("span", { class: "dot " + (!s.exists ? "" : s.running ? (s.health === "unhealthy" ? "warn" : "ok") : "bad") }), h("span", { class: "nm" }, s.name), h("span", { class: "st" }, s.exists ? `${s.status}${s.health ? " · " + s.health : ""}` : "absent")))) : null)));
    }
    tabCtl.go();
  },
};
