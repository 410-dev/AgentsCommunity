"""Minimal UI localization.

Source strings are English and double as translation keys. A string may contain
str.format placeholders ("Message sent to {n} machine(s)."); parameters are applied
after lookup so translations can reorder them. Missing translations fall back to English.
"""

from fastapi import Request

from .i18n_ko import KO

LANGUAGES = {"en": "English", "ko": "한국어"}
DEFAULT_LANGUAGE = "en"
COOKIE = "lang"

_TABLES = {"en": {}, "ko": KO}


def detect_language(request: Request) -> str:
    chosen = request.cookies.get(COOKIE)
    if chosen in LANGUAGES:
        return chosen
    for part in request.headers.get("accept-language", "").split(","):
        tag = part.split(";")[0].strip().lower()
        primary = tag.split("-")[0]
        if primary in LANGUAGES:
            return primary
    return DEFAULT_LANGUAGE


def translator(lang: str):
    table = _TABLES.get(lang, {})

    def _(text: str, **params) -> str:
        if not text:
            return text
        out = table.get(text, text)
        if params:
            try:
                return out.format(**params)
            except (KeyError, IndexError, ValueError):
                return text.format(**params)
        return out

    return _
