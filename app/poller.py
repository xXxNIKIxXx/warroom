"""Multi-User-Poller. Ein Zyklus:
  1. member-territories EINMAL holen (global, für alle gleich) → Owner-Lookup + Raster
  2. je User: /me (Gang), Footprint (eigene APs, stündlich neu), Owner-Diff → Events,
     Stats-Snapshot — alles mit dem Key DIESES Users (Rate-Limit ist pro Key).
Der Key wird nur hier kurz entschlüsselt und nie persistiert."""
import logging
import time

from . import auth, config, db, grid, push
from .wdg import Wdg, WdgError

log = logging.getLogger("warroom.poller")


def _num(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def refresh_footprint(conn, client: Wdg, user_id: int, glat: float, glng: float) -> int:
    data = client.my_aps()
    aps = data.get("aps", [])
    counts: dict[str, list] = {}
    for a in aps:
        la, lo = a.get("lat"), a.get("lng")
        if la is None or lo is None:
            continue
        i, j = grid.cell_index(la, lo, glat, glng)
        k = grid.key_from_index(i, j)
        c = counts.get(k)
        if c:
            c[2] += 1
        else:
            counts[k] = [i, j, 1]
    conn.execute("DELETE FROM footprint_cells WHERE user_id = ?", (user_id,))
    conn.executemany(
        "INSERT INTO footprint_cells (user_id, cell_key, i, j, my_aps) VALUES (?,?,?,?,?)",
        [(user_id, k, v[0], v[1], v[2]) for k, v in counts.items()],
    )
    conn.execute("UPDATE users SET footprint_at = ? WHERE id = ?", (time.time(), user_id))
    return len(counts)


def _classify(prev_gid, cur_gid, my_gid):
    if prev_gid == cur_gid:
        return None
    if prev_gid == my_gid and cur_gid != my_gid:
        return "lost"
    if cur_gid == my_gid and prev_gid != my_gid:
        return "captured"
    if cur_gid is None:
        return "freed"
    if prev_gid is None:
        return "new_owner"
    return "flipped"


def diff_territory(conn, lookup: dict, glat, glng, user_id: int, my_gid,
                   initialized: bool, ring: int, watch_level: str = "turf") -> int:
    """Turf = eigene AP-Zellen + Ring drumherum. In der territory-Tabelle landen alle
    besetzten Zellen im Ring (jede Gang) PLUS die eigenen unbesetzten Footprint-Zellen.
    Events (Wächter) NUR für Footprint-Zellen (eigener Einsatz) → kein Nachbar-Spam."""
    foot = {}  # cell_key -> my_aps
    fset = set()
    for r in conn.execute(
            "SELECT cell_key, i, j, my_aps FROM footprint_cells WHERE user_id = ?", (user_id,)):
        foot[r["cell_key"]] = r["my_aps"]
        fset.add((r["i"], r["j"]))
    # Anker = Footprint-Zellen mit mind. einem Nachbarn im Ring. Verwirft isolierte
    # Streu-APs (Durchfahrt/GPS-Ausreißer), deren Ring sonst Fremdfelder zieht
    # (z. B. je 1 AP in Eindhoven/Rotterdam). Fallback für sehr dünne User: alle Zellen.
    def _clustered(i, j):
        for di in range(-ring, ring + 1):
            for dj in range(-ring, ring + 1):
                if (di or dj) and (i + di, j + dj) in fset:
                    return True
        return False
    anchors = [(i, j) for (i, j) in fset if _clustered(i, j)] or list(fset)
    turf = set()
    for (i, j) in anchors:
        for di in range(-ring, ring + 1):
            for dj in range(-ring, ring + 1):
                turf.add((i + di, j + dj))

    # Über die Turf-Zellen iterieren statt über ALLE globalen Zellen — bei N Usern
    # sonst N × 150k+ Iterationen pro Zyklus. lookup ist bereits nach cell_key indiziert.
    new: dict[str, dict] = {}
    for (i, j) in turf:
        k = grid.key_from_index(i, j)
        c = lookup.get(k)
        if c is None:
            continue
        cla, clo = grid.center(i, j, glat, glng)
        new[k] = {"i": i, "j": j, "lat": cla, "lng": clo,
                  "gid": _num(c.get("gang_id")), "gang": c.get("gang"),
                  "uid": _num(c.get("user_id")), "count": _num(c.get("count")),
                  "color": c.get("color")}
    # eigene unbesetzte Footprint-Zellen ergänzen — aber NUR wenn sie im Turf liegen
    # (nahe eines Ankers); isolierte Streu-Zellen bleiben so komplett draußen
    for r in conn.execute(
            "SELECT cell_key, i, j FROM footprint_cells WHERE user_id = ?", (user_id,)):
        if (r["i"], r["j"]) in turf and r["cell_key"] not in new:
            cla, clo = grid.center(r["i"], r["j"], glat, glng)
            new[r["cell_key"]] = {"i": r["i"], "j": r["j"], "lat": cla, "lng": clo,
                                  "gid": None, "gang": None, "uid": None, "count": None, "color": None}

    old = {row["cell_key"]: row for row in conn.execute(
        "SELECT * FROM territory WHERE user_id = ?", (user_id,))}
    events = 0
    for k, n in new.items():
        p = old.get(k)
        conn.execute(
            """INSERT INTO territory (user_id, cell_key, i, j, lat, lng, gang_id, gang,
                     owner_user_id, count, color, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(user_id, cell_key) DO UPDATE SET
                 gang_id=excluded.gang_id, gang=excluded.gang,
                 owner_user_id=excluded.owner_user_id, count=excluded.count,
                 color=excluded.color, updated_at=excluded.updated_at""",
            (user_id, k, n["i"], n["j"], n["lat"], n["lng"], n["gid"], n["gang"],
             n["uid"], n["count"], n["color"]),
        )
        if initialized and p:
            prev_gid, cur_gid = _num(p["gang_id"]), n["gid"]
            kind = _classify(prev_gid, cur_gid, my_gid)
            if kind:
                # Nähe: eigene AP-Zelle > eigene Gang beteiligt > nur im Umkreis
                if foot.get(k, 0) > 0:
                    prox = "mine"
                elif prev_gid == my_gid or cur_gid == my_gid:
                    prox = "gang"
                else:
                    prox = "near"
                emit = (watch_level == "near"
                        or (watch_level == "turf" and prox in ("mine", "gang"))
                        or (watch_level == "own" and prox == "mine"))
                if emit:
                    conn.execute(
                        """INSERT INTO events (user_id, cell_key, i, j, lat, lng, kind,
                                 old_gang_id, old_gang, new_gang_id, new_gang, my_aps, proximity)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (user_id, k, n["i"], n["j"], n["lat"], n["lng"], kind,
                         prev_gid, p["gang"], cur_gid, n["gang"], foot.get(k, 0), prox),
                    )
                    events += 1
    # Jungfräulicher Boden: im Ring, aber in KEINEM Feed — dort war noch nie jemand.
    # (new enthält alle besetzten Zellen des Rings + meine eigenen Footprint-Zellen.)
    virgin = [(i, j) for (i, j) in turf if grid.key_from_index(i, j) not in new]
    conn.execute("DELETE FROM virgin_cells WHERE user_id = ?", (user_id,))
    if virgin:
        rows = []
        for (i, j) in virgin:
            cla, clo = grid.center(i, j, glat, glng)
            rows.append((user_id, grid.key_from_index(i, j), i, j, cla, clo))
        conn.executemany(
            "INSERT INTO virgin_cells (user_id, cell_key, i, j, lat, lng) VALUES (?,?,?,?,?,?)",
            rows)

    # Zellen, die nicht mehr im Turf sind, aus territory entfernen
    stale = [k for k in old if k not in new]
    if stale:
        conn.executemany("DELETE FROM territory WHERE user_id = ? AND cell_key = ?",
                         [(user_id, k) for k in stale])
    # Event-Log je User auf die letzten 200 begrenzen (lautere Scopes → mehr Events)
    conn.execute(
        """DELETE FROM events WHERE user_id = ? AND id NOT IN
           (SELECT id FROM events WHERE user_id = ? ORDER BY id DESC LIMIT 200)""",
        (user_id, user_id))
    return events


def snapshot_stats(conn, client: Wdg, user_id: int, me: dict,
                   gctx: dict, team_cache: dict) -> None:
    """Leaderboard/Territories kommen EINMAL pro Zyklus (gctx), team/me EINMAL pro
    Gang (team_cache) — nur /me bleibt zwingend pro User. Hält Upstream-Last und
    Zyklusdauer flach, wenn viele User registriert sind."""
    my_gang = me.get("gang")
    my_gid = _num(me.get("gang_id"))
    if my_gid in team_cache:
        team = team_cache[my_gid]
    else:
        try:
            team = client.team_me()
        except Exception:
            team = {}
        team_cache[my_gid] = team
    rank = points = None
    try:
        for idx, g in enumerate(gctx.get("leaderboard", {}).get("gangs", []), 1):
            if g.get("name") == my_gang or _num(g.get("gang_id")) == my_gid:
                rank = idx
                break
    except Exception:
        pass
    try:
        for t in gctx.get("territories", []):
            if t.get("name") == my_gang:
                points = _num(t.get("points"))
                break
    except Exception:
        pass
    tot = team.get("totals", {}) if isinstance(team, dict) else {}
    cr = me.get("credits", {}) or {}
    conn.execute(
        """INSERT OR REPLACE INTO stats (user_id, ts, wifi, ble, total, recent_today,
               recent_7d, credits, gang_rank, gang_points, team_total, team_captured,
               team_lost, team_reinforced)
           VALUES (?,datetime('now'),?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, _num(me.get("wifi")), _num(me.get("ble")), _num(me.get("total")),
         _num(me.get("recent_today")), _num(me.get("recent_7d")), _num(cr.get("balance")),
         rank, points, _num(tot.get("total")), _num(tot.get("captured")),
         _num(tot.get("lost")), _num(tot.get("reinforced"))),
    )
    conn.execute(
        "UPDATE users SET gang = ?, gang_id = ?, last_poll = datetime('now') WHERE id = ?",
        (my_gang, _num(me.get("gang_id")), user_id),
    )


def run_user(conn, user_row, lookup, glat, glng, gctx, team_cache) -> dict:
    client = Wdg(auth.user_key(user_row))
    uid = user_row["id"]
    me = client.me()
    my_gid = _num(me.get("gang_id"))

    last_fp = float(user_row["footprint_at"] or 0)
    have_fp = conn.execute(
        "SELECT COUNT(*) n FROM footprint_cells WHERE user_id = ?", (uid,)).fetchone()["n"]
    if have_fp == 0 or (time.time() - last_fp) > config.FOOTPRINT_REFRESH_SECONDS:
        refresh_footprint(conn, client, uid, glat, glng)

    initialized = bool(user_row["terr_init"])
    wl = user_row["watch_level"] if "watch_level" in user_row.keys() else "near"
    n_events = diff_territory(conn, lookup, glat, glng, uid, my_gid, initialized,
                              config.TURF_RING, wl)
    if not initialized:
        conn.execute("UPDATE users SET terr_init = 1 WHERE id = ?", (uid,))
    if n_events:
        # Der Rabe bringt Kunde: die frisch eingefügten Events gebündelt pushen
        fresh = conn.execute(
            "SELECT * FROM events WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (uid, n_events)).fetchall()
        try:
            push.notify_user(conn, uid, list(reversed(fresh)))
        except Exception:
            log.exception("push für %s fehlgeschlagen", user_row["wdg_username"])
    snapshot_stats(conn, client, uid, me, gctx, team_cache)
    return {"user": user_row["wdg_username"], "events": n_events}


def _fetch_global(conn, users) -> tuple[list, float, float, dict]:
    """Global identische Daten EINMAL pro Zyklus holen (member-territories,
    leaderboard, territories) — mit dem erstbesten funktionierenden Key."""
    for u in users:
        try:
            client = Wdg(auth.user_key(u))
            mt = client.member_territories()
            glat = float(mt.get("grid_lat") or 0.02)
            glng = float(mt.get("grid_lng") or 0.02)
            db.kv_set(conn, "grid_lat", glat)
            db.kv_set(conn, "grid_lng", glng)
            gctx = {}
            try:
                gctx["leaderboard"] = client.leaderboard()
            except WdgError:
                gctx["leaderboard"] = {}
            try:
                gctx["territories"] = client.territories()
            except WdgError:
                gctx["territories"] = []
            return mt.get("cells", []), glat, glng, gctx
        except WdgError as e:
            log.warning("member-territories via %s fehlgeschlagen: %s", u["wdg_username"], e)
    return [], 0.02, 0.02, {}


def poll_all(conn) -> dict:
    users = conn.execute("SELECT * FROM users").fetchall()
    if not users:
        return {"users": 0}
    t0 = time.monotonic()
    cells, glat, glng, gctx = _fetch_global(conn, users)
    if not cells:
        return {"users": len(users), "error": "no global data"}
    lookup = {}
    for c in cells:
        la, lo = c.get("lat"), c.get("lng")
        if la is not None and lo is not None:
            lookup[grid.cell_key(la, lo, glat, glng)] = c
    total_events = 0
    team_cache: dict = {}
    for k, u in enumerate(users):
        if k:
            time.sleep(0.15)  # sanftes Rinnsal statt Request-Burst Richtung wdgwars
        try:
            res = run_user(conn, u, lookup, glat, glng, gctx, team_cache)
            total_events += res["events"]
        except Exception:
            log.exception("poll für %s fehlgeschlagen", u["wdg_username"])
    db.kv_set(conn, "last_poll", time.time())
    return {"users": len(users), "global_cells": len(cells), "events": total_events,
            "secs": round(time.monotonic() - t0, 1)}
