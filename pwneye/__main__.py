from rich.console import Console
from pwneye.cli.tui import print_banner, TUI
from pwneye.cli.parser import parse_args
from pwneye.core import engine
from pwneye.core.types import ExitCode

def main() -> int:
    console = Console(highlight=False)
    tui = TUI(console)

    print_banner(console)

    try:
        args = parse_args(tui)
        exit_code = engine.run(args, tui)
        console.print()
        return exit_code
    except KeyboardInterrupt:
        tui.interrupted()
        return ExitCode.USER_ABORT

if __name__ == "__main__":
    raise SystemExit(main())
