from rich.console import Console
from pwneye.cli.tui import print_banner, TUI
from pwneye.cli.parser import parse_args
from pwneye.config import REPO
from pwneye.core import engine, updater
from pwneye.core.network import github
from pwneye.core.storage import cache as cachedata
from pwneye.core.storage import motd
from pwneye.core.types import ExitCode

RELEASES_URL = f"{REPO}/releases"


def _run_update_check(tui: TUI, *, upgrade: bool = False) -> ExitCode:
    tui.info("Checking for updates...")

    current_version, latest_version, update_available = github.get_update_status()
    if latest_version is None:
        tui.warning("Unable to check for updates")
        return ExitCode.FAILURE

    if update_available:
        tui.warning(
            f"You are running version [bold]{current_version}[/bold], "
            f"but version [bold]{latest_version}[/bold] is available "
            f"({RELEASES_URL})"
        )
        return updater.run(tui) if upgrade else ExitCode.SUCCESS

    tui.info2(
        f"You are already running the latest version [bold]{current_version}[/bold]"
    )
    return ExitCode.SUCCESS


def _run_clear_cache(tui: TUI) -> ExitCode:
    entry_count = cachedata.count_entries()
    if entry_count == 0:
        tui.info("No cache entries were found")
        return ExitCode.SUCCESS

    label = "entry" if entry_count == 1 else "entries"
    if not tui.confirm(
        "Do you really want to delete {count} cached {label}?".format(
            count=entry_count,
            label=label,
        ),
        default=False,
    ):
        tui.info("Cache cleanup aborted at user request")
        return ExitCode.SUCCESS

    deleted = cachedata.clear_all()
    deleted_label = "entry" if deleted == 1 else "entries"
    tui.success("Deleted {count} cached {label}", count=deleted, label=deleted_label)
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

        if args.clear_cache:
            exit_code = _run_clear_cache(tui)
            console.print()
            return exit_code

        if args.check_updates or args.update:
            exit_code = _run_update_check(tui, upgrade=args.update)
            console.print()
            return exit_code

        available_update = github.get_available_update()
        if available_update is not None:
            current_version, latest_version = available_update
            upgrade_hint = ". Run [bold]pwneye --update[/bold] to upgrade" if updater.is_pipx_installation() else ""
            tui.warning(
                f"You are running version [bold]{current_version}[/bold], "
                f"but version [bold]{latest_version}[/bold] is available "
                f"({RELEASES_URL}){upgrade_hint}"
            )

        exit_code = engine.run(args, tui)
        console.print()
        return exit_code
    except KeyboardInterrupt:
        tui.interrupted()
        return ExitCode.USER_ABORT

if __name__ == "__main__":
    raise SystemExit(main())
