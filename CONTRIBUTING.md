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

Everything lives in one file, `src/i18n/translations.json`. Each entry is keyed
by the English source string:

```json
"Stop Casting": {
    "en": "Stop Casting",
    "ru": "Остановить трансляцию",
    "cs": "Zastavit vysílání"
}
```

To add a language, append your code to every entry. There is no new file to
create and no template to copy.

Four rules. Each one fails silently if you break it, so please read them:

- **Never change the key or the `en` value.** The key has to match the string in
  the source code character for character, trailing spaces included.
- **Keep `{placeholders}` exactly as they are.** Translating `{target}` into
  your own language raises a runtime error, and only for users of that language.
- **Keep `en` listed first** in each entry.
- **Stay consistent inside your language.** If you translate "cast" one way in
  the menu, use the same word in the notifications.

Test without touching your system locale:

```bash
FLUXCAST_LANG=de python3 src/main.py --tray
```

Then add your language to the table in the README.

## Reporting Bugs

Open an issue and include:
- Output of `python3 src/main.py --doctor` and `tail -f /tmp/fluxcast-wfd-latency.jsonl  ` 
- What you ran and what happened
- OS, compositor, TV model
