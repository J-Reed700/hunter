"""Recovery — library <-> disk reconciliation, and re-requesting what is gone."""
from __future__ import annotations

from hunter import domain, recovery, storage
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx

log = common.log
_MAX_REQUEUE = 5000


def _filters(ctx: Ctx) -> dict:
    return dict(
        q=ctx.q("q"),
        sort=ctx.q("sort", "artist"),
        artist=ctx.q("artist"),
        fmt=ctx.q("fmt"),
        album_id=ctx.qi("album_id", 0, 0),
    )


@get("/api/recovery", role="operator")
def list_missing(ctx: Ctx):
    return ctx.json(
        200,
        recovery.query(
            ctx.cfg,
            offset=ctx.qi("offset", 0, 0),
            limit=ctx.qi("limit", 100, 1, 500),
            refresh=ctx.q("refresh") == "1",
            **_filters(ctx),
        ),
    )


@get("/api/recovery/keys", role="operator")
def missing_keys(ctx: Ctx):
    return ctx.json(200, recovery.keys(ctx.cfg, **_filters(ctx)))


@post("/api/recovery/requeue")
def requeue(ctx: Ctx):
    """Re-request the chosen missing tracks with operator priority (-50)."""
    body = ctx.body or {}
    if body.get("all"):
        raw_aid = body.get("album_id")
        if raw_aid is None or raw_aid == "":
            aid = 0
        elif isinstance(raw_aid, bool) or not isinstance(raw_aid, (int, str)):
            return ctx.error(400, "album_id must be an integer")
        else:
            try:
                aid = int(raw_aid)
            except ValueError:
                return ctx.error(400, "album_id must be an integer")
        ids = recovery.keys(
            ctx.cfg,
            q=str(body.get("q") or ""),
            artist=str(body.get("artist") or ""),
            fmt=str(body.get("fmt") or ""),
            album_id=aid,
        )["item_ids"]
    else:
        raw_ids = body.get("item_ids")
        if raw_ids is not None and not isinstance(raw_ids, list):
            return ctx.error(400, "item_ids must be a list")
        ids = []
        for i in raw_ids or ():
            if isinstance(i, int) and not isinstance(i, bool):
                ids.append(i)
            elif isinstance(i, str):
                try:
                    ids.append(int(i))
                except ValueError:
                    return ctx.error(400, "item_ids must contain integers")
            else:
                return ctx.error(400, "item_ids must contain integers")
    if not ids:
        return ctx.error(400, "nothing selected")
    if len(ids) > _MAX_REQUEUE:
        return ctx.error(
            400,
            f"{len(ids):,} tracks selected — the cap is {_MAX_REQUEUE:,} per request",
        )

    st_sum = storage.summary(ctx.cfg)
    if not st_sum["ok"]:
        return ctx.error(409, st_sum.get("error") or "storage is not live")

    snapshot = recovery.scan(ctx.cfg)
    if not snapshot["trusted"]:
        return ctx.error(
            409,
            snapshot["untrusted_reason"]
            or "the library mount is not readable — refusing to re-request",
        )

    still_gone = recovery.verify(ctx.cfg, ids)
    returned = len(ids) - len(still_gone)
    if not still_gone:
        return ctx.json(
            200,
            {
                "ok": True,
                "queued": 0,
                "skipped": 0,
                "returned": returned,
                "note": "every selected file is present on disk again",
            },
        )

    added = skipped = 0
    with common.state(ctx.cfg) as st:
        with st.transaction():
            for alb in recovery.albums_of(still_gone):
                key = f"recovery:{alb['key']}"
                track_total = alb.get("album_track_total") or alb.get("tracks") or None
                try:
                    if st.upsert_want(
                        key,
                        alb["artist"],
                        alb["album"],
                        alb.get("year") or 0,
                        track_total,
                        "recovery",
                        resurrect=True,
                    ):
                        st.set_priority(key, domain.PRIORITY_OPERATOR)
                        st.log_event(
                            "request",
                            alb["artist"],
                            alb["album"],
                            f"recovery: {alb['tracks']} track(s) missing from disk",
                        )
                        added += 1
                    else:
                        skipped += 1
                except Exception as exc:                      # noqa: BLE001
                    log.warning("recovery requeue failed for %s — %s", key, exc)
                    skipped += 1
    if added:
        common.spawn_pass()
    return ctx.json(
        200,
        {
            "ok": True,
            "queued": added,
            "skipped": skipped,
            "tracks": len(still_gone),
            "returned": returned,
        },
    )
