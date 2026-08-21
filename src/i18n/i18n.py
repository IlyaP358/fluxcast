import json
import os


def _lang_of(value: str) -> str:
    return value.split(".")[0].split("@")[0].split("_")[0].strip().lower()

def _get_system_lang() -> str:
    forced = os.environ.get("FLUXCAST_LANG")
    if forced and forced.strip():
        return _lang_of(forced)
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value and _lang_of(value) not in ("", "c", "posix"):
            return _lang_of(value)
    return "en"

_USER_LANG = _get_system_lang()

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRANS_FILE = os.path.join(_CURRENT_DIR, "translations.json")

_TRANSLATIONS = {}
if os.path.exists(_TRANS_FILE):
    try:
        with open(_TRANS_FILE, "r", encoding="utf-8") as f:
            _TRANSLATIONS = json.load(f)
    except Exception as e:
        print(f"[i18n] Error loading translations.json: {e}", flush=True)

def _l(text: str) -> str:
    if text in _TRANSLATIONS:
        return _TRANSLATIONS[text].get(_USER_LANG, _TRANSLATIONS[text].get('en', text))
    return text
