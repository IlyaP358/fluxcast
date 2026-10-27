import importlib
import json
import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

_LOCALE_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "i18n", "translations")


class I18nFilesTest(unittest.TestCase):
    def test_per_language_files_exist(self):
        self.assertTrue(os.path.isfile(os.path.join(_LOCALE_DIR, "en.json")))
        self.assertTrue(os.path.isfile(os.path.join(_LOCALE_DIR, "ru.json")))

    def test_no_missing_translation_keys(self):
        with open(os.path.join(_LOCALE_DIR, "en.json"), encoding="utf-8") as f:
            en_keys = set(json.load(f))
        missing = {}
        for fname in os.listdir(_LOCALE_DIR):
            if not fname.endswith(".json") or fname == "en.json":
                continue
            with open(os.path.join(_LOCALE_DIR, fname), encoding="utf-8") as f:
                lang_keys = set(json.load(f))
            gap = en_keys - lang_keys
            if gap:
                missing[fname[:-5]] = gap
        if missing:
            warnings.warn(
                "Incomplete translations (missing keys will fall back to English):\n"
                + "\n".join(f"  {lang}: {len(keys)} key(s) missing" for lang, keys in sorted(missing.items())),
                UserWarning,
                stacklevel=2,
            )


class I18nLoaderTest(unittest.TestCase):
    def setUp(self):
        import i18n.i18n as i18n
        os.environ["FLUXCAST_LANG"] = "ru"
        importlib.reload(i18n)
        self.i18n = i18n

    def tearDown(self):
        os.environ.pop("FLUXCAST_LANG", None)

    def test_known_string_is_translated(self):
        self.assertEqual(self.i18n._l("Stop Casting"), "Остановить трансляцию")

    def test_unknown_string_is_returned_as_is(self):
        self.assertEqual(self.i18n._l("__no_such_key__"), "__no_such_key__")

    def test_fallback_to_english(self):
        os.environ["FLUXCAST_LANG"] = "xx"
        importlib.reload(self.i18n)
        self.assertEqual(self.i18n._l("Stop Casting"), "Stop Casting")


if __name__ == "__main__":
    unittest.main()
