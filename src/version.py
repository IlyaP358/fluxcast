"""
FluxCast version reporting script
"""

import functools
import os
import subprocess

# Rewritten at build time; keep the literal on one line so `sed` can match it.
__installed_version__ = "dev"


def _git_version(project_root: str) -> str:
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=project_root, stderr=subprocess.DEVNULL,
    ).decode("utf-8").strip()

    described = subprocess.check_output(
        ["git", "describe", "--long", "--tags", "--always", "--abbrev=7"],
        cwd=project_root, stderr=subprocess.DEVNULL,
    ).decode("utf-8").strip().lstrip("v")

    return f"{described} (git branch: {branch})"


@functools.cache
def get_fluxcast_version() -> str:
    """Cached: every caller would otherwise fork two git processes."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    if os.path.isdir(os.path.join(project_root, ".git")):
        try:
            return _git_version(project_root)
        except (OSError, subprocess.SubprocessError):
            pass

    if __installed_version__ != "dev":
        return __installed_version__

    return "unknown"
