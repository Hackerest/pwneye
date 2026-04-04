import argparse
from rich_argparse import RichHelpFormatter, ArgumentDefaultsRichHelpFormatter

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

class PwneyeHelpFormatter(ArgumentDefaultsRichHelpFormatter):
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
    targeting_mode = targeting.add_mutually_exclusive_group(required=True)
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
        action="store_true",
        help="Discover ONVIF cameras on the local network",
    )

    # ONVIF options

    onvif = parser.add_argument_group("ONVIF (optional)")
    onvif.add_argument(
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
        default="",
        help="ONVIF username or file with one username per line",
    )
    onvif.add_argument(
        "-op", "--onvif-password",
        metavar="PASS",
        default="",
        help="ONVIF password or file with one password per line",
    )
    onvif.add_argument(
        "--reboot",
        action="store_true",
        help="Reboot the camera via ONVIF and skip RTSP probing",
    )
    # RTSP options

    rtsp = parser.add_argument_group("RTSP (optional)")
    rtsp.add_argument(
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
        help="RTSP port",
    )
    rtsp.add_argument(
        "-u", "--username",
        default="",
        metavar="USER",
        help="RTSP username or file with one username per line",
    )
    rtsp.add_argument(
        "-p", "--password",
        default="",
        metavar="PASS",
        help="RTSP password or file with one password per line",
    )
    rtsp.add_argument(
        "--protocol",
        choices=["tcp", "udp"],
        default="tcp",
        help="Transport protocol for RTSP connections",
    )
    rtsp.add_argument(
        "--timeout",
        type=int,
        default=10,
        metavar="SECONDS",
        help="RTSP connection timeout",
    )
    rtsp.add_argument(
        "--vendor",
        metavar="VENDOR",
        help="Specify the vendor manually (e.g. hikvision)",
    )
    rtsp.add_argument(
        "--record",
        nargs="?",
        const="",
        default=None,
        metavar="OUTPUT.mp4",
        help="Record the RTSP stream to OUTPUT.mp4 (or auto-generate a timestamped filename)",
    )
    rtsp.add_argument(
        "--no-video",
        action="store_true",
        help="Do not attempt to fetch or decode video streams",
    )

    # Misc options

    misc = parser.add_argument_group("Misc (optional)")
    misc.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read from or write to cache",
    )
    misc.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore cached results but update the cache with new findings",
    )
    misc.add_argument(
        "--threads",
        type=int,
        default=1,
        metavar="N",
        help="Number of concurrent threads",
    )

    return parser.parse_args()
