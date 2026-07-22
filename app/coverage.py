"""Coverage brush: GPS breadcrumb points logged while wardriving. Each point carries
the operator's expected reception radius, so the union of the discs is the ground truly
covered — not just cells that held an AP. Point-based (not polygons) so the radius stays
honest per point (it may change between drives) and the same table later absorbs the
wdgwars-AP backfill as src='ap'."""
import sqlite3

MAX_BATCH = 500          # per request — offline buffers flush in chunks, not all at once
MIN_R, MAX_R = 10, 2000  # metres; clamp GPS/UI garbage into a sane disc size


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def add_points(conn: sqlite3.Connection, user_id: int, pts: list) -> int:
    """Insert a batch of {lat,lng,r,t?} dicts. Returns the count actually stored.
    Invalid points are skipped, never fatal — an offline batch must not be rejected
    wholesale because one buffered breadcrumb is garbage. `t` is the client capture
    time (ISO); absent → server time, so replay of a buffered drive stays honest."""
    rows = []
    for p in pts[:MAX_BATCH]:
        if not isinstance(p, dict):
            continue
        try:
            lat = float(p["lat"]); lng = float(p["lng"])
            r = int(p.get("r") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0) or r <= 0:
            continue
        t = p.get("t")
        rows.append((user_id, lat, lng, _clamp(r, MIN_R, MAX_R), str(t) if t else None))
    if not rows:
        return 0
    # OR IGNORE + the (user_id,lat,lng,ts) unique index make re-sends idempotent, so the
    # unload safety net can fire-and-forget without ever duplicating a disc.
    conn.executemany(
        "INSERT OR IGNORE INTO coverage_pts (user_id, lat, lng, radius_m, src, ts) "
        "VALUES (?, ?, ?, ?, 'gps', COALESCE(?, datetime('now')))", rows)
    return len(rows)


def points(conn: sqlite3.Connection, user_id: int) -> list:
    """Every stored disc for this user, oldest first (draw order = drive order)."""
    rows = conn.execute(
        "SELECT lat, lng, radius_m AS r, src FROM coverage_pts "
        "WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    return [{"lat": r["lat"], "lng": r["lng"], "r": r["r"], "src": r["src"]} for r in rows]


def clear(conn: sqlite3.Connection, user_id: int) -> int:
    """Wipe this user's coverage (the ↺ in the layer popover). Returns rows removed."""
    return conn.execute("DELETE FROM coverage_pts WHERE user_id = ?", (user_id,)).rowcount
