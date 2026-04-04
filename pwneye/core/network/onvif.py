import http.client
import queue
import ssl
import threading
import time

from itertools import product
from typing import Optional, List, Dict, Any, Callable

from onvif import ONVIFClient, ONVIFDiscovery
from pwneye.core.network import common as netcomm

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

    for task in tasks:
        task_queue.put(task)

    def worker() -> None:
        nonlocal result

        while not stop_event.is_set():
            try:
                port, username, password = task_queue.get_nowait()
            except queue.Empty:
                return

            try:
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
) -> List[Dict[str, Any]]:
    """
    Discover ONVIF devices on the local network via WS-Discovery.

    Returns:
        List of discovered devices with host, port, scopes, types and XAddrs.
    """
    for attempt in range(max(1, attempts)):
        try:
            discovery = ONVIFDiscovery(timeout=timeout)
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
