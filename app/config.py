"""Konfiguration. Es gibt KEINEN globalen API-Key mehr — jeder User bringt seinen
eigenen mit, verschlüsselt in der DB (siehe crypto.py). Hier nur Pfade + Kadenz."""
import os
from pathlib import Path

BASE_URL = "https://wdgwars.pl"
USER_AGENT = "warroom-companion/0.1 (+https://warroom.mechanics-toolbox.org)"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WARROOM_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "warroom.sqlite"
MASTER_KEY_PATH = DATA_DIR / "master.key"

# Deckel für Neuanmeldungen (jeder User = 1 fremder API-Key in unserer Obhut +
# Poll-Last) — bewusst klein starten, per Env anhebbar ohne Rebuild.
MAX_USERS = int(os.environ.get("WARROOM_MAX_USERS", "30"))
CONTACT_MAIL = "st4bleground@proton.me"

# member-territories wird serverseitig nur alle 5 min per Cron neu berechnet.
POLL_SECONDS = 300
# Footprint (eigene APs) je User seltener voll neu einlesen.
FOOTPRINT_REFRESH_SECONDS = 3600
REINFORCE_BUFFER = 3
# Turf = eigene AP-Zellen + Ring von TURF_RING Zellen drumherum (Chebyshev).
# 4 Zellen ≈ 6–9 km bei ~50°N (0.02° ≈ 2,2 km lat / ~1,4 km lng).
TURF_RING = 4
