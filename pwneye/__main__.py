from rich.console import Console
from pwneye.cli.tui import print_banner, TUI
from pwneye.cli.parser import parse_args
from pwneye.core import engine
from pwneye.core.network import github
from pwneye.core.storage import motd
from pwneye.core.types import ExitCode


def _run_update_check(tui: TUI) -> ExitCode:
    tui.info("Checking for updates...")

    current_version, latest_version, update_available = github.get_update_status()
    if latest_version is None:
        tui.warning("Unable to check for updates")
        return ExitCode.FAILURE

    if update_available:
        tui.warning(
            f"You are running version [bold]{current_version}[/bold], "
            f"but version [bold]{latest_version}[/bold] is available"
        )
        return ExitCode.SUCCESS

    tui.info2(
        f"You are already running the latest version [bold]{current_version}[/bold]"
    )
    return ExitCode.SUCCESS


def main() -> int:
    console = Console(highlight=False)
    tui = TUI(console)

    print_banner(console)

    try:
        args = parse_args(tui)
        try:
            message = motd.get_random_message()
        except motd.MotdError:
            message = None

        if message:
            tui.motd(message)

        if args.check_updates:
            exit_code = _run_update_check(tui)
            console.print()
            return exit_code

        available_update = github.get_available_update()
        if available_update is not None:
            current_version, latest_version = available_update
            tui.warning(
                f"You are running version [bold]{current_version}[/bold], "
                f"but version [bold]{latest_version}[/bold] is available"
            )

        exit_code = engine.run(args, tui)
        console.print()
        return exit_code
    except KeyboardInterrupt:
        tui.interrupted()
        return ExitCode.USER_ABORT

if __name__ == "__main__":
    raise SystemExit(main())
