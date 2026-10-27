import json
import os


def _lang_of(value: str) -> str:
    return value.split(".")[0].split("@")[0].split("_")[0].strip().lower()


def _get_system_lang() -> str:
    forced = os.environ.get("FLUXCAST_LANG", "").strip()
    if forced:
        return _lang_of(forced)

    locale_lang = ""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "").strip()
        if value:
            locale_lang = _lang_of(value)
            break

    if locale_lang in ("c", "posix"):
        return "en"

    for entry in os.environ.get("LANGUAGE", "").split(":"):
        lang = _lang_of(entry)
        if lang:
            return lang

    return locale_lang or "en"


_USER_LANG = _get_system_lang()

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRANS_DIR = os.path.join(_CURRENT_DIR, "translations")
_TRANS_FILE = os.path.join(_CURRENT_DIR, "translations.json")


def _load_json_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[i18n] Error loading {os.path.basename(path)}: {e}", flush=True)
    return {}


_TRANSLATIONS = {}
if os.path.isdir(_TRANS_DIR):
    for filename in sorted(os.listdir(_TRANS_DIR)):
        if not filename.endswith(".json"):
            continue
        lang = filename[:-5].lower()
        lang_map = _load_json_file(os.path.join(_TRANS_DIR, filename))
        if isinstance(lang_map, dict):
            _TRANSLATIONS[lang] = lang_map
elif os.path.exists(_TRANS_FILE):
    legacy_data = _load_json_file(_TRANS_FILE)
    if isinstance(legacy_data, dict):
        for text, mapped in legacy_data.items():
            if not isinstance(mapped, dict):
                continue
            for lang, value in mapped.items():
                _TRANSLATIONS.setdefault(lang, {})[text] = value


if "en" not in _TRANSLATIONS:
    _TRANSLATIONS["en"] = {}


def _l(text: str) -> str:
    current_lang = _TRANSLATIONS.get(_USER_LANG, _TRANSLATIONS.get("en", {}))
    if text in current_lang:
        return current_lang[text]
    if text in _TRANSLATIONS.get("en", {}):
        return _TRANSLATIONS["en"][text]
    return text
