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
