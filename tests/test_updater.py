import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from pwneye.core import updater
from pwneye.core.types import ExitCode


class UpdaterTests(unittest.TestCase):
    def test_checkout_does_not_upgrade_an_unrelated_pipx_installation(self):
        tui = Mock()
        with patch.object(updater, "package_file", "/checkout/pwneye/__init__.py"), \
             patch.object(updater.subprocess, "run") as command:
            self.assertEqual(updater.run(tui), ExitCode.FAILURE)
        command.assert_not_called()
        self.assertEqual(tui.warning.call_args.kwargs["url"], updater.PIPX_GUIDE)

    def test_upgrade_targets_current_environment_and_propagates_failure(self):
        with TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            prefix = home / "venvs" / "pwneye-test"
            prefix.mkdir(parents=True)
            metadata = prefix / "pipx_metadata.json"

            def write_version(version):
                metadata.write_text(json.dumps({"main_package": {
                    "package": "pwneye", "package_version": version,
                }}), encoding="utf-8")

            for code, version, expected in (
                (0, "1.3.10", ExitCode.SUCCESS),
                (0, "1.3.3", ExitCode.FAILURE),
                (0, "1.3.2", ExitCode.FAILURE),
                (0, None, ExitCode.FAILURE),
                (1, "1.3.4", ExitCode.FAILURE),
            ):
                write_version("1.3.3")
                tui = Mock()

                def run_command(args, **kwargs):
                    if args[1] == "environment":
                        return subprocess.CompletedProcess(args, 0, stdout=str(home))
                    write_version(version)
                    return subprocess.CompletedProcess(args, code)

                with patch.object(updater.sys, "prefix", str(prefix)), \
                     patch.object(updater, "package_file", str(prefix / "pwneye/__init__.py")), \
                     patch.object(updater.shutil, "which", return_value="/bin/pipx"), \
                     patch.object(updater.subprocess, "run", side_effect=run_command) as command:
                    self.assertEqual(updater.run(tui), expected)
                command.assert_called_with(["/bin/pipx", "upgrade", "--", "pwneye-test"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if expected == ExitCode.SUCCESS:
                    tui.success.assert_called_once_with("pwneye updated from {previous} to {current}",
                                                        previous="1.3.3", current=version)
                else:
                    tui.success.assert_not_called()
                    tui.error.assert_called_once()

    def test_global_home_matches_only_in_global_scope(self):
        prefix = Path("/opt/pipx/venvs/pwneye").resolve()
        with patch.object(updater.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, stdout="/different/home"),
            subprocess.CompletedProcess([], 0, stdout=str(prefix.parent.parent)),
        ]):
            self.assertEqual(updater._upgrade_command("/bin/pipx", prefix),
                             ["/bin/pipx", "upgrade", "--global", "--", "pwneye"])
