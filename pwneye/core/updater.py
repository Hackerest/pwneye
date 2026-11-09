import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pwneye import __file__ as package_file
from pwneye.core.types import ExitCode, TUI


PIPX_GUIDE = "https://github.com/Hackerest/pwneye#pipx"


def _pipx_package(prefix: Path) -> dict:
    try:
        metadata = json.loads((prefix / "pipx_metadata.json").read_text(encoding="utf-8"))
        package = metadata["main_package"]
        return package if package["package"] == "pwneye" else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _installed_version(prefix: Path) -> str | None:
    version = _pipx_package(prefix).get("package_version")
    if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return version
    return None


def _upgrade_command(pipx: str, prefix: Path) -> list[str] | None:
    for scope, variable in (([], "PIPX_HOME"), (["--global"], "PIPX_GLOBAL_HOME")):
        try:
            result = subprocess.run(
                [pipx, "environment", *scope, "--value", variable],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        home = result.stdout.strip()
        if result.returncode == 0 and home and Path(home).is_absolute():
            if (Path(home) / "venvs" / prefix.name).resolve() == prefix:
                return [pipx, "upgrade", *scope, "--", prefix.name]
    return None


def is_pipx_installation() -> bool:
    prefix = Path(sys.prefix).resolve()
    return Path(package_file).resolve().is_relative_to(prefix) and bool(_pipx_package(prefix))


def run(tui: TUI) -> ExitCode:
    prefix = Path(sys.prefix).resolve()
    if not is_pipx_installation():
        tui.warning("This copy of pwneye was not installed via pipx. See {url}", url=PIPX_GUIDE)
        return ExitCode.FAILURE

    pipx = shutil.which("pipx")
    if not pipx:
        tui.warning("pipx is not available on PATH. See {url}", url=PIPX_GUIDE)
        return ExitCode.FAILURE

    command = _upgrade_command(pipx, prefix)
    if command is None:
        tui.warning("pipx could not locate the current installation. See {url}", url=PIPX_GUIDE)
        return ExitCode.FAILURE

    previous = _installed_version(prefix)
    if previous is None:
        tui.error("Unable to determine the installed pwneye version")
        return ExitCode.FAILURE

    tui.info("Updating pwneye via pipx...")
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError as exc:
        tui.error("Unable to run pipx: {error}", error=str(exc))
        return ExitCode.FAILURE
    if result.returncode != 0:
        tui.error("pipx upgrade failed (exit status {status})", status=result.returncode)
        return ExitCode.FAILURE

    current = _installed_version(prefix)
    if current is None:
        tui.error("Unable to verify the installed pwneye version after the upgrade")
        return ExitCode.FAILURE
    if tuple(map(int, current.split("."))) <= tuple(map(int, previous.split("."))):
        tui.error("pipx did not install a newer version of pwneye (before: {previous}, after: {current})",
                  previous=previous, current=current)
        return ExitCode.FAILURE
    tui.success("pwneye updated from {previous} to {current}", previous=previous, current=current)
    return ExitCode.SUCCESS
