# Security Policy

If you find a bug that affects the security of a system running FluxCast, please
report it privately instead of opening a public issue.

## Supported versions

_Only_ the most recent release is supported. There are no backports and no LTS
releases. If you package FluxCast, track the latest tag.

## What is not a security issue

- Bugs in ffmpeg, GStreamer, NetworkManager or wpa_supplicant. Report those
  upstream.
- Anything that needs root on the machine already running FluxCast.
- Crashes with no security consequence. Those are ordinary bugs, open an issue.
- The WFD stream not being encrypted. FluxCast does not implement HDCP and says
  so in its Wi-Fi Display information element, so anyone already inside your
  Wi-Fi Direct group can read the stream.
- Missing compiler or linker hardening that nothing can reach.

## What is a security issue

- A machine other than the sink you picked joining, taking over or keeping alive
  an RTSP session.
- Reading your screen off the DLNA or Chromecast HTTP server without the
  per-session URL.
- A sink injecting input through UIBC beyond what you enabled, or reaching
  `/dev/uinput` when UIBC is off.
- Firewall rules that FluxCast opens for a session and leaves open afterwards.
- A remote peer causing FluxCast to run code, read files or exhaust memory.
- Anything that lets a machine other than the sink you picked see your screen.

## How to report

Use the Report a vulnerability button on the Security tab of the repository:

    https://github.com/IlyaP358/fluxcast/security/advisories/new

That opens a private advisory, visible only to you and whoever administers this
repository. If you would rather use mail, write to
[illia@fluxcast.dev](mailto:illia@fluxcast.dev).

Include whatever you have: what an attacker needs, the commands you ran, the
output of `python3 src/main.py --doctor`, your OS, compositor and TV model.

This is spare-time work, so I will not promise a deadline. A fix ships in the
next release and the advisory is published with it.
Reporters are credited in the release notes unless you ask not to be.
