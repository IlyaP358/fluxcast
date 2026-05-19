# Contributing to FluxCast

Thanks for your interest! The project is in early alpha and 
actively developed, so contributions are very welcome.

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
- Bug reports with `--doctor` output and session logs

## Reporting Bugs

Open an issue and include:
- Output of `python3 src/main.py --doctor` and `tail -f /tmp/fluxcast-wfd-latency.jsonl  ` 
- What you ran and what happened
- OS, compositor, TV model
