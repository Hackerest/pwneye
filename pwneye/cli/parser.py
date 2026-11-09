import argparse
from rich_argparse import RichHelpFormatter

from pwneye.core.utils.validators import validate_ip_or_domain, validate_port

# Custom configuration for the parser
RichHelpFormatter.styles.clear()

RichHelpFormatter.styles["argparse.groups"] = "bold"
RichHelpFormatter.styles["argparse.help"] = "default"
RichHelpFormatter.styles["argparse.metavar"] = "grey70"

class PwneyeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, logger, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = logger

    def error(self, message):
        """
        Print argparse error messages using the provided Logger.
        """
        self.print_usage()
        print()
        self.logger.debug(message)
        self.exit(2)

    def exit(self, status=0, message=None):
        """
        Add a blank line before exiting.
        """
        print()
        super().exit(status)

class PwneyeHelpFormatter(RichHelpFormatter):
    @staticmethod
    def group_name_formatter(group_name: str) -> str:
        formatted = group_name.title()
        formatted = formatted.replace("Onvif", "ONVIF")
        formatted = formatted.replace("Rtsp", "RTSP")
        return formatted

    def __init__(self, *args, **kwargs):
        kwargs["max_help_position"] = 40
        kwargs["width"] = 130
        super().__init__(*args, **kwargs)

def argparse_type(fn, *, name: str):
    def wrapper(value):
        try:
            return fn(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"invalid {name}: {exc}"
            )
    return wrapper


def validate_ptz_move(value: str) -> tuple[str, float]:
    """
    Validate a PTZ move payload formatted as DIRECTION,DURATION.
    """
    parts = [segment.strip() for segment in value.split(",")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("expected a move in the form DIRECTION,DURATION")

    direction = parts[0].lower()
    supported_directions = {
        "left",
        "right",
        "up",
        "down",
        "l",
        "r",
        "u",
        "d",
    }
    if direction not in supported_directions:
        raise ValueError("direction must be one of: left, right, up, down, l, r, u, d")

    try:
        duration = float(parts[1])
    except ValueError as exc:
        raise ValueError("duration must be numeric") from exc

    if duration <= 0:
        raise ValueError("duration must be greater than 0")

    normalized_direction = {
        "l": "left",
        "r": "right",
        "u": "up",
        "d": "down",
    }.get(direction, direction)

    return normalized_direction, duration

def parse_args(logger) -> argparse.Namespace:
    parser = PwneyeArgumentParser(
        prog = "pwneye",
        formatter_class=PwneyeHelpFormatter,
        logger=logger
    )

    # Target selection

    targeting = parser.add_argument_group(
        "Target Selection (required)",
        "Choose either a single target or ONVIF discovery on the local network",
    )
    targeting_mode = targeting.add_mutually_exclusive_group(required=False)
    targeting_mode.add_argument(
        "-t", "--target",
        type=argparse_type(
            validate_ip_or_domain,
            name="target"
        ),
        metavar="TARGET",
        help="Target IP address or domain",
    )
    targeting_mode.add_argument(
        "--discover",
        nargs="?",
        const="",
        default=None,
        metavar="INTERFACE|all",
        help="Continuously discover ONVIF cameras using INTERFACE, or cycle through all IPv4 interfaces",
    )

    # ONVIF options

    onvif = parser.add_argument_group("ONVIF (Optional)")
    onvif.add_argument(
        "-so",
        "--skip-onvif",
        action="store_true",
        help="Skip ONVIF detection and probing",
    )
    onvif.add_argument(
        "-oP", "--onvif-port",
        type=argparse_type(validate_port, name="ONVIF port"),
        metavar="PORT",
        help="ONVIF port (if not specified, common ONVIF ports are tested)",
    )
    onvif.add_argument(
        "-ou", "--onvif-username",
        metavar="USER",
        default=None,
        help="ONVIF username or file (omit for common usernames; use '' for an empty username)",
    )
    onvif.add_argument(
        "-op", "--onvif-password",
        metavar="PASS",
        default=None,
        help="ONVIF password or file (omit for common passwords; use '' for an empty password)",
    )
    onvif.add_argument(
        "--reboot",
        action="store_true",
        help="Reboot the camera via ONVIF and skip RTSP probing",
    )
    onvif.add_argument(
        "--reset",
        action="store_true",
        help="Factory-reset the camera via ONVIF and skip RTSP probing",
    )
    onvif.add_argument(
        "--deface",
        nargs="?",
        const="",
        default=None,
        metavar="MESSAGE",
        help="Darken the stream and place MESSAGE at the center via ONVIF, then skip RTSP probing",
    )
    onvif.add_argument(
        "--undeface",
        action="store_true",
        help="Restore the last saved ONVIF deface profile and skip RTSP probing",
    )
    onvif.add_argument(
        "--shell",
        action="store_true",
        help="Open an interactive ONVIF shell and skip RTSP probing",
    )
    onvif.add_argument(
        "--move",
        type=argparse_type(validate_ptz_move, name="PTZ move"),
        action="append",
        metavar="DIRECTION,DURATION",
        help="Move the camera via ONVIF using direction,duration (e.g. right,2). Can be specified multiple times and skips RTSP probing",
    )
    # RTSP options

    rtsp = parser.add_argument_group("RTSP (Optional)")
    rtsp.add_argument(
        "-sr",
        "--skip-rtsp",
        action="store_true",
        help="Skip RTSP detection and probing",
    )
    rtsp.add_argument(
        "-P", "--rtsp-port",
        type=argparse_type(
            validate_port,
            name="RTSP port"
        ),
        default=None,
        metavar="PORT",
        help="RTSP port (if not specified, common RTSP ports are tested)",
    )
    rtsp.add_argument(
        "-u", "--username",
        default=None,
        metavar="USER",
        help="RTSP username or file (omit for common usernames; use '' for an empty username)",
    )
    rtsp.add_argument(
        "-p", "--password",
        default=None,
        metavar="PASS",
        help="RTSP password or file (omit for common passwords; use '' for an empty password)",
    )
    rtsp.add_argument(
        "-cn", "--connection-string",
        default="",
        metavar="PATH",
        help="RTSP connection string or file with one connection string per line",
    )
    rtsp.add_argument(
        "--protocol",
        choices=["tcp", "udp"],
        default="tcp",
        help="Transport protocol for RTSP connections (default: tcp)",
    )
    rtsp.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Override RTSP timeouts (defaults: 5 seconds for port discovery, 10 for stream probes)",
    )
    rtsp.add_argument(
        "--vendor",
        metavar="VENDOR",
        help="Specify the RTSP vendor manually (otherwise automatic identification is attempted)",
    )
    rtsp.add_argument(
        "--banner",
        action="store_true",
        help="Fetch the RTSP banner and exit",
    )
    rtsp.add_argument(
        "--multi-channel",
        action="store_true",
        help="Prefer RTSP multi-channel connection strings when available",
    )
    rtsp.add_argument(
        "--legacy",
        action="store_true",
        help="Open live RTSP previews with ffplay instead of the dedicated client",
    )
    rtsp.add_argument(
        "--record",
        nargs="?",
        const="",
        default=None,
        metavar="FILENAME.mp4",
        help="Record the RTSP stream to FILENAME.mp4 (or auto-generate a timestamped filename)",
    )
    rtsp.add_argument(
        "--snapshot",
        nargs="?",
        const="",
        default=None,
        metavar="FILENAME.jpg",
        help="Save an RTSP snapshot to FILENAME.jpg (or auto-generate a timestamped filename)",
    )
    rtsp.add_argument(
        "--no-video",
        action="store_true",
        help="Do not attempt to fetch or decode video streams",
    )

    # Cache options

    cache = parser.add_argument_group("Cache (Optional)")
    cache.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read from or write to cache",
    )
    cache.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore cached results but update the cache with new findings",
    )
    cache.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete all cached target entries and exit",
    )

    # Misc options

    misc = parser.add_argument_group("Misc (optional)")
    misc.add_argument(
        "-srt", "--skip-reachability",
        action="store_true",
        help="Skip the initial target reachability check",
    )
    misc.add_argument(
        "--threads",
        type=int,
        default=1,
        metavar="N",
        help="Number of concurrent threads",
    )
    misc.add_argument(
        "-lv", "--list-vendors",
        action="store_true",
        help="List supported RTSP vendors and exit",
    )
    misc.add_argument(
        "--check-updates",
        action="store_true",
        help="Check whether a newer pwneye release is available and exit",
    )
    misc.add_argument(
        "--update",
        action="store_true",
        help="Update pwneye to the latest version",
    )

    args = parser.parse_args()
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    args.rtsp_port_timeout = args.timeout if args.timeout is not None else 5
    args.timeout = args.timeout if args.timeout is not None else 10

    if args.update and (args.target or args.discover is not None or args.list_vendors or args.check_updates or args.clear_cache):
        parser.error("--update must be used as a standalone command")

    if not (args.target or args.discover is not None or args.list_vendors or args.check_updates or args.clear_cache or args.update):
        parser.error("one of --target or --discover is required")

    if args.record is not None and args.snapshot is not None:
        parser.error("--record and --snapshot cannot be used together")

    return args
