"""Jinja-Setup + i18n-Kontext + Anzeige-Helfer."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import config, i18n

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

LANG_COOKIE = "wr_lang"

# Cache-Buster: höchste mtime der eigenen Assets. Ändert sich bei jedem Edit →
# neue ?v=-URL → Cloudflare/Browser holen frisch (CF cached /static/* sonst 4 h).
ASSET_V = 0
for _p in (STATIC_DIR / "style.css", STATIC_DIR / "sw.js"):
    try:
        ASSET_V = max(ASSET_V, int(_p.stat().st_mtime))
    except OSError:
        pass
templates.env.globals["asset_v"] = ASSET_V
templates.env.globals["contact_mail"] = config.CONTACT_MAIL
templates.env.globals["max_users"] = config.MAX_USERS


def fmt_n(v):
    try:
        return f"{int(v):,}".replace(",", ".")
    except (TypeError, ValueError):
        return v if v is not None else "—"


templates.env.filters["n"] = fmt_n


def lang_of(request: Request) -> str:
    return i18n.norm(request.cookies.get(LANG_COOKIE))


def render(request: Request, template: str, ctx: dict | None = None):
    lang = lang_of(request)
    base = {
        "lang": lang,
        "t": lambda key, **kw: i18n.t(lang, key, **kw),
        "js": i18n.js_bundle(lang),
    }
    if ctx:
        base.update(ctx)
    return templates.TemplateResponse(request, template, base)
