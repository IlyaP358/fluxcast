import subprocess


def _run(
    args: list[str],
    timeout: float = 5.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": env,
    }
    if args[:2] == ["sudo", "-A"]:
        kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run(args, **kwargs)
