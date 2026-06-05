import locale as _locale
import os


def detect_lang() -> str:
    env = os.environ.get("AI_LIMIT_LANG", "")
    if env:
        return "zh" if env.lower().startswith("zh") else "en"
    try:
        loc = _locale.getlocale()[0] or os.environ.get("LANG", "")
        return "zh" if loc.startswith("zh") else "en"
    except Exception:
        return "en"


LANG = detect_lang()


def t(zh: str, en: str) -> str:
    return zh if LANG == "zh" else en
