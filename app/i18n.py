"""Lightweight PT-PT / EN-US translations for templates, JS, and API messages."""

from __future__ import annotations

import json
from contextvars import ContextVar, Token
from pathlib import Path

from starlette.requests import Request

LOCALES = ("pt-PT", "en-US")
DEFAULT_LOCALE = "pt-PT"
COOKIE_NAME = "lang"
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

_locale: ContextVar[str] = ContextVar("locale", default=DEFAULT_LOCALE)
_catalogs: dict[str, dict[str, str]] | None = None

JS_MESSAGES = (
    "A carregar os gateways…",
    "Não foi possível ler os gateways deste cloud.",
    "Escolhe um cloud para ver a timezone.",
    "A carregar os domínios…",
    "A carregar os servidores…",
    "A carregar os domínios e os servidores…",
    "Não foi possível ler os domínios deste cloud.",
    "Não foi possível ler os servidores deste cloud.",
    "A pesquisar…",
    "A carregar a página…",
    "A procurar chamadas relacionadas…",
    "A ler…",
    "Não foi possível ler a CDR deste cloud.",
)


def _load_catalogs() -> dict[str, dict[str, str]]:
    global _catalogs
    if _catalogs is not None:
        return _catalogs
    root = Path(__file__).resolve().parent / "locales"
    catalogs: dict[str, dict[str, str]] = {DEFAULT_LOCALE: {}}
    for code in LOCALES:
        if code == DEFAULT_LOCALE:
            continue
        path = root / f"{code}.json"
        if path.is_file():
            catalogs[code] = json.loads(path.read_text(encoding="utf-8"))
        else:
            catalogs[code] = {}
    _catalogs = catalogs
    return catalogs


def get_locale() -> str:
    return _locale.get()


def set_locale(code: str) -> Token:
    locale = code if code in LOCALES else DEFAULT_LOCALE
    return _locale.set(locale)


def reset_locale(token: Token) -> None:
    _locale.reset(token)


def html_lang(locale: str | None = None) -> str:
    code = locale or get_locale()
    return "en" if code.startswith("en") else "pt"


def _(msgid: str) -> str:
    if not msgid:
        return msgid
    locale = get_locale()
    if locale == DEFAULT_LOCALE:
        return msgid
    return _load_catalogs().get(locale, {}).get(msgid, msgid)


def js_messages() -> dict[str, str]:
    return {msgid: _(msgid) for msgid in JS_MESSAGES}


def resolve_locale(request: Request) -> str:
    cookie = (request.cookies.get(COOKIE_NAME) or "").strip()
    if cookie in LOCALES:
        return cookie
    header = request.headers.get("accept-language") or ""
    for part in header.split(","):
        tag = part.split(";")[0].strip().lower().replace("_", "-")
        if not tag:
            continue
        if tag == "en-us" or tag.startswith("en"):
            return "en-US"
        if tag == "pt-pt" or tag.startswith("pt"):
            return "pt-PT"
    return DEFAULT_LOCALE
