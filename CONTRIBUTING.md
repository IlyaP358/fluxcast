# Contributing to FluxCast

Thanks for your interest! The project is actively developed, so contributions are very welcome.

## Before You Start

Run `--doctor` and make sure your environment is ready:

```bash
python3 src/main.py --doctor
```

Check open issues, maybe someone is already working on 
the same thing.

## How to Contribute

Fork the repo, make your changes, open a PR against `dev`.

For now the project is tested only on Hyprland/Samsung. 
If you're adding support for a different TV or compositor, 
please attach a session log or short video showing it works. 
I don't have the hardware to verify it myself.

## Pull Request Size

Two rules, both about keeping review honest rather than keeping the tree tidy.

**One feature per pull request.** A branch that does several unrelated things
cannot be reviewed as a whole, only spot-checked, and it cannot be reverted
later without taking the good parts with it. If yours has grown past one
change, split it. Three small PRs get read the same week; one large one sits
for weeks and then gets declined for its shape rather than its content.

**No file may exceed 500 lines after your change, unless it was already over
500 before it.** A file you can read start to finish in one sitting is one you
can reason about as a whole; past that, review turns into reading the diff and
trusting the rest. The handful of files already over the limit are
grandfathered, so this never asks you to split something you did not write.
Files under `tests/` are exempt.

Both rules have real exceptions. If yours is one, say so in the pull request
description rather than hoping it goes unnoticed.

## What's Most Needed Right Now

- Testing on non-Samsung TVs (LG, Sony, Philips)
- Screen capture backends for KDE/GNOME Wayland and X11
- Translations into more languages
- Bug reports with `--doctor` output and session logs

## Translations

Each language is one file in `src/i18n/translations/`, named by its code:
`de.json`, `fr.json`. `en.json` is the source of truth; its keys are the exact
English strings the code asks for.

**To add a language**, create the file with every key already in it:

```bash
python3 src/i18n/sync_translations.py uk
```

Then translate the values in `src/i18n/translations/uk.json`. The script leaves
the English text in place as the placeholder, so anything you have not reached
yet still reads English.

**To update an existing language** after new strings land, run the same command.
It adds only the keys your file is missing and does not touch what you already
translated:

```bash
python3 src/i18n/sync_translations.py de
```

Four rules. Each one fails silently if you break it, so please read them:

- **Never change a key, and never edit `en.json`.** A key has to match the string
  in the source character for character, trailing spaces included. One of them
  really does end in three spaces:
  `"Cast via Miracast (WFD, portal dialog)   "`.
- **Keep `{placeholders}` exactly as they are.** Four strings carry them:
  `{target}`, `{rc}` and `{path}`. Translating the name inside the braces raises a
  runtime error, and only for users of your language.
- **Do not reformat the file.** The script writes it sorted, two-space indent,
  non-ASCII left as-is. Keeping that shape is what makes the diffs readable.
- **Stay consistent inside your language.** If you translate "cast" one way in the
  menu, use the same word in the notifications.

You do not have to finish. A key with no translation falls back to English, so a
partial file is a perfectly good pull request.

Check your work:

```bash
FLUXCAST_LANG=uk python3 src/main.py --tray   # see it without changing your locale
python3 -m unittest discover -s tests
```

`tests/test_i18n.py` compares every language against `en.json` and warns about
missing keys instead of failing, so an incomplete translation will not block CI.

Finally, add your language to the table in the README.

## Reporting Bugs

Open an issue and include:
- Output of `python3 src/main.py --doctor` and `tail -f /tmp/fluxcast-wfd-latency.jsonl  ` 
- What you ran and what happened
- OS, compositor, TV model
