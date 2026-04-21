from threading import RLock
from typing import Iterable, Mapping, Any

from rich.console import Console
from rich.status import Status
from rich.text import Text
from rich.markup import escape

from pwneye.config import DEVELOPER, VERSION, CODENAME, REPO
from pwneye.core.types import PromptInterrupt

def print_banner(console: Console) -> None:
    """
    Prints the application banner to the console.

    :param console: A `rich.console.Console` instance used for rendering the banner.
    """
    r = "[bold red]⣿[/]"
    banner = f"""
⠀⠀⠀⣸⣏⠛⠻⠿⣿⣶⣤⣄⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⣿⣿⣿⣷⣦⣤⣈⠙⠛⠿⣿⣷⣶⣤⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⢸⣿⣿⣿⣿⣿⣿⣿⣿⣶⣦⣄⣈⠙⠻⠿⣿⣷⣶⣤⣀⡀⠀⠀⠀⠀⠀⠀
⠀⠀⣾⣿{r}⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣦⣄⡉⠛⠻⢿⣿⣷⣶⣤⣀⠀⠀  [bold red]pwneye[/bold red] [grey62]v{VERSION}_{CODENAME}[/grey62]
⠀⠀⠀⠉⠙⠛⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣾⢻⣍⡉⠉⣿⠇⠀  {REPO}
⠀⠀⠀⠀⠀⠀⠀⢹⡏⢹⣿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠇⣰⣿⣿⣾⠏⠀⠀  Coded by {DEVELOPER}
⠀⠀⠀⠀⠀⠀⠀⠘⣿⠈⣿⠸⣯⠉⠛⠿⢿⣿⣿⣿⣿⡏⠀⠻⠿⣿⠇⠀⠀⠀  
⠀⠀⠀⠀⠀⠀⠀⠀⢿⡆⢻⡄⣿⡀⠀⠀⠀⠈⠙⠛⠿⠿⠿⠿⠛⠋      Sponsored by Hackerest ([link=hackerest.com][dodger_blue2][underline]hackerest.com[/][/][/])
⠀⠀⠀⠀⠀⠀ ⣀⣀⣿⣴⣿⢾⣿
    """
    console.print(Text.from_markup(banner))

class TUI:
    """
    Terminal UI helper for pwneye.
    Handles styled output, prompts, and user interaction.
    """

    def __init__(self, console: Console):
        self.console = console
        self._lock = RLock()
        self._live: Status | None = None
        self._live_message = ""
        self._live_spinner = "dots"
        self._interrupted = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format_message(self, message: str, **kwargs) -> str:
        """
        Format message using str.format().
        Auto-colors placeholders by wrapping them in cyan (escaped to avoid markup injection).
        """
        safe = {}
        for k, v in kwargs.items():
            # escape markup inside values, then colorize
            safe[k] = f"[grey70]{escape(str(v))}[/]"

        try:
            return message.format(**safe)
        except KeyError as exc:
            missing = exc.args[0]
            return f"{message} [red](missing key: {missing})[/red]"

    def _print(
        self,
        prefix: str,
        message: str,
        *,
        style: str | None = None,
        end: str = "",
        **kwargs,
    ) -> None:
        """
        Centralized print method.
        """
        with self._lock:
            formatted = self._format_message(message, **kwargs)

            if style:
                formatted = f"[{style}]{formatted}[/{style}]"

            if self._live is not None:
                self._live.stop()
                self.console.print(f"{prefix} {formatted}{end}")
                self._live = self.console.status(
                    self._live_message,
                    spinner=self._live_spinner,
                )
                self._live.start()
                return

            self.console.print(f"{prefix} {formatted}{end}")

    # ------------------------------------------------------------------
    # Log-style methods
    # ------------------------------------------------------------------

    def info(self, message: str, end: str = "", **kwargs) -> None:
        self._print(
            "[[dodger_blue2]info[/]]",
            message,
            end=end,
            **kwargs,
        )

    def info2(self, message: str, end: str = "", **kwargs) -> None:
        self._print(
            "[bold][[/bold][bold dodger_blue2]info[/bold dodger_blue2][bold]][/bold]",
            message,
            style="bold",
            end=end,
            **kwargs,
        )

    def success(self, message: str, end: str = "", **kwargs) -> None:
        self._print(
            "[bold][[/bold][bold green]success[/bold green][bold]][/bold]",
            message,
            style="bold",
            end=end,
            **kwargs,
        )

    def warning(self, message: str, end: str = "", **kwargs) -> None:
        self._print(
            "[[yellow]warning[/]]",
            message,
            end=end,
            **kwargs,
        )

    def error(self, message: str, end: str = "", **kwargs) -> None:
        self._print(
            "[[red]error[/]]",
            message,
            end=end,
            **kwargs,
        )

    def debug(self, message: str, end: str = "", **kwargs) -> None:
        self._print(
            "[[dark_violet]debug[/]]",
            message,
            end=end,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Blocks & prompts
    # ------------------------------------------------------------------

    def block(
        self,
        content: Iterable[str] | Mapping[str, Any],
        indent: int = 3,
    ) -> None:
        """
        Print an indented block of text, surrounded by blank lines.

        - If content is a list/iterable of strings, each line is printed as-is.
        - If content is a dict, it is printed as `key: value`.
        """
        prefix = " " * indent

        self.console.print()

        if isinstance(content, dict):
            for key, value in content.items():
                value = "(empty)" if not value else value
                self.console.print(f"{prefix}[bold]{key}[/]: [grey70]{value}[/]")
        else:
            for line in content:
                self.console.print(f"{prefix}{line}")

        self.console.print()

    # ------------------------------------------------------------------
    # Live output
    # ------------------------------------------------------------------

    def start_live(self, message: str, spinner: str = "dots") -> None:
        """
        Start a live status line that can be updated in-place.
        """
        with self._lock:
            self.stop_live()
            self._live_message = message
            self._live_spinner = spinner
            self._live = self.console.status(message, spinner=spinner)
            self._live.start()

    def update_live(self, message: str) -> None:
        """
        Update the active live status line.
        """
        with self._lock:
            if self._live is None:
                return

            self._live_message = message
            self._live.update(message)

    def stop_live(self) -> None:
        """
        Stop the active live status line, if any.
        """
        with self._lock:
            if self._live is not None:
                self._live.stop()
                self._live = None
                self._live_message = ""

    def interrupted(self, message: str = "CTRL-C detected. Aborting execution...") -> None:
        """
        Stop live output and print a single interruption message.
        """
        with self._lock:
            if self._interrupted:
                self.stop_live()
                return

            self._interrupted = True
            self.stop_live()
            self.console.file.write("\r\033[2K")
            self.console.file.flush()
            self.warning(message)
            self.console.print()

    def confirm(
        self,
        prompt: str,
        default: bool = True,
        interrupt_message: str | None = "CTRL-C detected. Aborting execution...",
    ) -> bool:
        """
        One-line Y/N prompt.
        Accepts: y/yes/Y/YES/n/no/N/NO (case-insensitive).
        Returns: True for yes, False for no.
        CTRL-C exits gracefully unless the caller wants to handle it silently.
        """
        while True:
            try:
                # One-line prompt, only the letters are colored (parentheses stay plain)
                default_hint = "y" if default is True else "n"
                self.console.print(
                    f"[[cyan]>[/]] {prompt} "
                    f"[([green]y[/green])es/([red]n[/red])o] (default: {default_hint}): ",
                    end=""
                )

                choice = input().strip().lower()

                if choice.startswith("y"):
                    return True
                if choice.startswith("n"):
                    return False
                if choice == "":
                    return default

                self.console.print(
                    "[[yellow]warning[/]] Invalid input. Use (y)es, (n)o, or press ENTER for default"
                )

            except KeyboardInterrupt:
                if interrupt_message is None:
                    self.stop_live()
                    self.console.file.write("\r\033[2K")
                    self.console.file.flush()
                else:
                    self.interrupted(interrupt_message)
                raise PromptInterrupt from None

    def select(
        self,
        title: str,
        items: list[str],
        prompt: str = "Choice",
        indent: int = 3,
    ):
        """
        Display a numbered selection menu and wait for valid user input.
        """
        if not items:
            raise ValueError("No items to select from")

        # Header
        self.console.print(f"[[cyan]>[/]] {title}")

        # Build numbered block with colored numbers only
        lines = []
        for idx, item in enumerate(items, start=1):
            lines.append(f"[[cyan]{idx}[/cyan]] {item}")

        self.block(lines, indent=indent)

        while True:
            try:
                # Bold prompt
                self.console.print(f"[bold]{prompt}[/bold]: ", end="")
                choice = input().strip()

                if not choice.isdigit():
                    raise ValueError

                index = int(choice)
                if not 1 <= index <= len(items):
                    raise ValueError

                return items[index - 1]

            except KeyboardInterrupt:
                self.interrupted()
                raise PromptInterrupt from None

            except ValueError:
                self.warning(
                    f"Invalid choice. Select a number between 1 and {len(items)}"
                )
