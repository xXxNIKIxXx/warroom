"""Verschlüsselung der wdgwars-Keys at-rest (Fernet = AES-128-CBC + HMAC).
Master-Key aus Env WARROOM_MASTER_KEY, sonst aus data/master.key (wird beim ersten
Start erzeugt, 0600). Ohne Master-Key lässt sich kein gespeicherter Key entschlüsseln
— Backup von master.key gehört also zum DB-Backup dazu."""
import os

from cryptography.fernet import Fernet

from . import config

_fernet: Fernet | None = None


def _load() -> Fernet:
    global _fernet
    if _fernet:
        return _fernet
    key = os.environ.get("WARROOM_MASTER_KEY")
    if not key:
        p = config.MASTER_KEY_PATH
        if p.exists():
            key = p.read_text(encoding="utf-8").strip()
        else:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key().decode()
            p.write_text(key, encoding="utf-8")
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _load().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _load().decrypt(token.encode()).decode()
