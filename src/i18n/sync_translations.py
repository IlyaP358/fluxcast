"""Sync translation files against en.json.

Adds missing keys to existing language files (English text as placeholder),
or creates a new file from scratch. Keys are kept sorted for clean diffs.

Usage:
    python3 src/i18n/sync_translations.py         # sync all existing languages
    python3 src/i18n/sync_translations.py de fr   # sync specific languages
    python3 src/i18n/sync_translations.py uk      # create uk.json if absent
"""

import json
import os
import sys

_TRANS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def sync(lang, en):
    path = os.path.join(_TRANS_DIR, f"{lang}.json")
    is_new = not os.path.isfile(path)
    existing = _load(path) if not is_new else {}
    missing = {k: en[k] for k in en if k not in existing}
    if not missing and not is_new:
        print(f"  {lang}: up to date")
        return
    existing.update(missing)
    _save(path, existing)
    suffix = " (created)" if is_new else ""
    print(f"  {lang}: added {len(missing)} key(s){suffix}")


def main():
    en = _load(os.path.join(_TRANS_DIR, "en.json"))
    langs = sys.argv[1:] or [
        f[:-5] for f in os.listdir(_TRANS_DIR)
        if f.endswith(".json") and f != "en.json"
    ]
    for lang in sorted(langs):
        sync(lang, en)


if __name__ == "__main__":
    main()
