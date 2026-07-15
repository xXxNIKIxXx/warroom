"""Web-Push (VAPID). Keys liegen wie der master.key in data/ — MIT sichern!
Eine Subscription = ein Gerät; lang wird beim Abonnieren mitgespeichert, damit
der Poller in der Sprache des Geräts melden kann. Gesendet wird EINE gebündelte
Meldung pro User und Poll-Zyklus (kein Firehose)."""
import ipaddress
import json
import logging
import os
import socket

from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02, b64urlencode
from pywebpush import WebPushException, webpush

from . import config, i18n

log = logging.getLogger("warroom.push")

VAPID_PATH = config.DATA_DIR / "vapid.pem"
# Self-Hoster setzen ihre eigene Adresse — Push-Dienste kontaktieren die bei Missbrauch.
VAPID_SUB = os.environ.get("WARROOM_VAPID_SUB", f"mailto:{config.CONTACT_MAIL}")


def _endpoint_ok(ep: str) -> bool:
    """Der Endpoint kommt vom Client und der Server POSTet dorthin — ohne Check wäre
    das eine SSRF-Tür ins interne Netz. Nur https auf öffentlich geroutete Hosts."""
    try:
        u = urlparse(ep)
        if u.scheme != "https" or not u.hostname:
            return False
        infos = socket.getaddrinfo(u.hostname, 443, proto=socket.IPPROTO_TCP)
        return bool(infos) and all(
            ipaddress.ip_address(i[4][0]).is_global for i in infos)
    except (ValueError, OSError):
        return False

_vapid: Vapid02 | None = None


def _get_vapid() -> Vapid02:
    global _vapid
    if _vapid is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if VAPID_PATH.exists():
            _vapid = Vapid02.from_file(str(VAPID_PATH))
        else:
            v = Vapid02()
            v.generate_keys()
            v.save_key(str(VAPID_PATH))
            VAPID_PATH.chmod(0o600)  # privater Key — wie master.key
            _vapid = v
            log.info("VAPID-Keypair erzeugt: %s", VAPID_PATH)
    return _vapid


def public_key_b64() -> str:
    """applicationServerKey für pushManager.subscribe (b64url, uncompressed point)."""
    raw = _get_vapid().public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64urlencode(raw)


def subscribe(conn, user_id: int, sub: dict, lang: str) -> bool:
    ep = (sub or {}).get("endpoint")
    keys = (sub or {}).get("keys") or {}
    if not ep or not keys.get("p256dh") or not keys.get("auth") or not _endpoint_ok(ep):
        return False
    conn.execute(
        """INSERT INTO push_subs (endpoint, user_id, p256dh, auth, lang) VALUES (?,?,?,?,?)
           ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,
             p256dh=excluded.p256dh, auth=excluded.auth, lang=excluded.lang""",
        (ep, user_id, keys["p256dh"], keys["auth"], i18n.norm(lang)))
    return True


def unsubscribe(conn, user_id: int, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subs WHERE endpoint = ? AND user_id = ?",
                 (endpoint, user_id))


def send_raw(sub_row, title: str, body: str, tag: str = "warroom") -> bool:
    """Eine Nachricht an EIN Gerät. Wirft nicht — meldet nur Erfolg/Misserfolg."""
    try:
        webpush(
            subscription_info={"endpoint": sub_row["endpoint"],
                               "keys": {"p256dh": sub_row["p256dh"], "auth": sub_row["auth"]}},
            data=json.dumps({"title": title, "body": body, "tag": tag,
                             "url": "/?tab=waechter"}),
            vapid_private_key=str(VAPID_PATH),
            vapid_claims={"sub": VAPID_SUB},
            ttl=600,
        )
        return True
    except Exception:
        log.exception("Push an %s fehlgeschlagen", sub_row["endpoint"][:40])
        return False


def send_welcome(conn, user_id: int, endpoint: str) -> bool:
    """Direkt nach dem Abo: beweist die Kette Ende-zu-Ende auf dem Gerät."""
    row = conn.execute("SELECT * FROM push_subs WHERE endpoint = ? AND user_id = ?",
                       (endpoint, user_id)).fetchone()
    if not row:
        return False
    lang = i18n.norm(row["lang"])
    return send_raw(row, i18n.t(lang, "push_welcome_title"),
                    i18n.t(lang, "push_welcome_body"), tag="warroom-welcome")


def _motto(lang: str, ev) -> str:
    """Gleiche Motto-Logik wie im Wächter-Tab, inkl. Varianten-Pool."""
    kind, prox = ev["kind"], ev["proximity"] or "near"
    if prox in ("mine", "gang"):
        base = {"lost": "watch_step", "captured": "watch_reclaim",
                "freed": "watch_empty"}.get(kind, "watch_skirmish")
    else:
        base = "watch_skirmish" if kind == "flipped" else "watch_near"
    variant = ("", "2", "3")[ev["id"] % 3]
    return i18n.t(lang, base + variant)


def _detail(lang: str, ev) -> str:
    kind = ev["kind"]
    word = i18n.t(lang, "ev_" + kind)
    if kind == "lost":
        rest = i18n.t(lang, "ev_lost_txt", g=ev["new_gang"] or "?")
    elif kind == "captured":
        rest = i18n.t(lang, "ev_captured_txt", g=ev["old_gang"] or i18n.t(lang, "nobody"))
    elif kind == "flipped":
        rest = i18n.t(lang, "ev_flipped_txt", a=ev["old_gang"] or "?", b=ev["new_gang"] or "?")
    else:
        rest = ev["new_gang"] or ev["old_gang"] or ""
    return f"{word} {rest}".replace("<b>", "").replace("</b>", "").strip()


_SEVERITY = {"mine": 0, "gang": 1, "near": 2}


def notify_user(conn, user_id: int, events: list) -> int:
    """Bündelt die Events EINES Poll-Zyklus zu einer Nachricht pro Gerät.
    Tote Endpoints (404/410) werden dabei ausgetragen."""
    if not events:
        return 0
    subs = conn.execute("SELECT * FROM push_subs WHERE user_id = ?", (user_id,)).fetchall()
    if not subs:
        return 0
    lead = sorted(events, key=lambda e: (_SEVERITY.get(e["proximity"] or "near", 2), -e["id"]))[0]
    sent = 0
    for s in subs:
        lang = i18n.norm(s["lang"])
        lines = [_detail(lang, e) for e in events[:3]]
        if len(events) > 3:
            lines.append(i18n.t(lang, "push_more", n=len(events) - 3))
        payload = json.dumps({
            "title": _motto(lang, lead),
            "body": "\n".join(lines),
            "tag": "warroom-watch",
            "url": "/?tab=waechter",
        })
        try:
            webpush(
                subscription_info={"endpoint": s["endpoint"],
                                   "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                data=payload,
                vapid_private_key=str(VAPID_PATH),
                vapid_claims={"sub": VAPID_SUB},
                ttl=1800,
            )
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                conn.execute("DELETE FROM push_subs WHERE endpoint = ?", (s["endpoint"],))
                log.info("Push-Endpoint tot (%s) — ausgetragen", code)
            else:
                log.warning("Push fehlgeschlagen (%s): %s", code, e)
        except Exception:
            log.exception("Push-Versand unerwartet fehlgeschlagen")
    return sent
