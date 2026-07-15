"""Zellmittelpunkte auf eine Straße ziehen.

Der geometrische Mittelpunkt einer Zelle liegt gern im Wald, auf dem Acker oder
im Fluss — Google Maps macht daraus unfahrbare Routen. Wir suchen deshalb per
Overpass (OpenStreetMap) die nächstgelegene befahrbare Straße INNERHALB der Zelle.

Zwei Dinge sind wichtig:
  * Der Punkt muss in der Zelle bleiben — sonst bricht die Auto-Weiterschaltung
    der In-App-Führung (die erkennt das Ziel am Zell-Schlüssel).
  * Das Ergebnis ist für eine Zelle für immer gleich → global gecacht. Jede Zelle
    wird genau einmal nachgeschlagen, danach nie wieder.
"""
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, db, grid

log = logging.getLogger("warroom.roads")

# Öffentliche Overpass-Instanzen. Die Hauptinstanz wirft unter Last gern 504 —
# dann einfach die nächste fragen. Schlägt alles fehl, bleibt es beim Mittelpunkt
# und wird NICHT gecacht (beim nächsten Anlauf neu versucht).
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
BATCH = 8           # Zellen pro Anfrage
TIMEOUT = 30

# Nur befahrbares: keine Fuß-/Radwege, Treppen, Trampelpfade.
DRIVABLE = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential"
            "|living_street|service|motorway_link|trunk_link|primary_link"
            "|secondary_link|tertiary_link|road")


def _grid(conn) -> tuple[float, float]:
    return (float(db.kv_get(conn, "grid_lat", 0.02) or 0.02),
            float(db.kv_get(conn, "grid_lng", 0.02) or 0.02))


def _ensure_grid(conn, glat: float, glng: float) -> None:
    """Ändert sich das Raster, sind alle gecachten Punkte wertlos."""
    tag = f"{glat}_{glng}"
    if db.kv_get(conn, "roads_grid") != tag:
        conn.execute("DELETE FROM cell_roads")
        db.kv_set(conn, "roads_grid", tag)


def _query(bboxes: list[tuple[float, float, float, float]]) -> list[dict]:
    parts = "".join(
        f'way["highway"~"^({DRIVABLE})$"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});'
        for (s, w, n, e) in bboxes)
    # `skel` = ohne Tags: wir brauchen nur die Stützpunkte. In einer Stadt sind das
    # schnell tausende Wege — mit Tags wäre die Antwort ein Vielfaches groß.
    ql = f"[out:json][timeout:{TIMEOUT}];({parts});out skel geom;"
    data = urllib.parse.urlencode({"data": ql}).encode()
    last = None
    for url in OVERPASS_MIRRORS:
        req = urllib.request.Request(
            url, data=data,
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT + 10) as r:
                return json.loads(r.read().decode("utf-8")).get("elements", [])
        except Exception as ex:   # 504/429/Timeout/DNS — nächste Instanz probieren
            last = ex
            log.info("Overpass %s: %s — nächste Instanz", urllib.parse.urlparse(url).netloc, ex)
    raise OSError(f"alle Overpass-Instanzen fehlgeschlagen: {last}")


def _nearest_in_cell(ways: list[dict], s, w, n, e, clat, clng):
    """Nächster Straßen-Stützpunkt zum Zellmittelpunkt — aber nur Punkte IN der Zelle."""
    best = None
    best_d = float("inf")
    coslat = math.cos(math.radians(clat))
    for el in ways:
        for p in el.get("geometry") or []:
            la, lo = p.get("lat"), p.get("lon")
            if la is None or lo is None:
                continue
            if not (s <= la <= n and w <= lo <= e):
                continue
            dy = la - clat
            dx = (lo - clng) * coslat
            d = dy * dy + dx * dx
            if d < best_d:
                best_d = d
                best = (la, lo)
    return best


def snap_cells(conn, cells: list[tuple[int, int]]) -> dict[str, list | None]:
    """cell_key → [lat, lng] auf der Straße, oder None wenn dort nachweislich keine ist.

    Zellen, deren Abfrage fehlschlug, FEHLEN im Ergebnis — der Aufrufer muss sie
    später erneut fragen. Ein Netzaussetzer ist kein Befund.
    """
    glat, glng = _grid(conn)
    _ensure_grid(conn, glat, glng)

    out: dict[str, list | None] = {}
    todo: list[tuple[int, int]] = []
    for (i, j) in cells:
        k = grid.key_from_index(i, j)
        if k in out:
            continue
        row = conn.execute(
            "SELECT lat, lng, found FROM cell_roads WHERE cell_key = ?", (k,)).fetchone()
        if row:
            out[k] = [row["lat"], row["lng"]] if row["found"] else None
        else:
            todo.append((i, j))

    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        boxes = []
        for (i, j) in chunk:
            (s, w), (n, e) = grid.bounds(i, j, glat, glng)
            boxes.append((s, w, n, e))
        t0 = time.monotonic()
        try:
            ways = _query(boxes)
            log.info("Overpass: %d Zellen, %d Wege, %.1f s",
                     len(chunk), len(ways), time.monotonic() - t0)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as ex:
            # WICHTIG: Fehlgeschlagene Abfrage ist KEIN Befund. Diese Zellen tauchen
            # gar nicht erst in der Antwort auf (nicht als null!) — sonst würde ein
            # kurzer Overpass-Aussetzer dauerhaft als "hier gibt es keine Straße"
            # festgeschrieben. Nicht cachen, der Client fragt später erneut.
            log.warning("Overpass nicht erreichbar (%s) — %d Zellen bleiben offen",
                        ex, len(chunk))
            continue

        for idx, (i, j) in enumerate(chunk):
            s, w, n, e = boxes[idx]
            clat, clng = grid.center(i, j, glat, glng)
            hit = _nearest_in_cell(ways, s, w, n, e, clat, clng)
            k = grid.key_from_index(i, j)
            if hit:
                conn.execute(
                    "INSERT OR REPLACE INTO cell_roads (cell_key, lat, lng, found) "
                    "VALUES (?,?,?,1)", (k, hit[0], hit[1]))
                out[k] = [hit[0], hit[1]]
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO cell_roads (cell_key, lat, lng, found) "
                    "VALUES (?,NULL,NULL,0)", (k,))
                out[k] = None
                log.info("keine Straße in Zelle %s", k)
    return out
