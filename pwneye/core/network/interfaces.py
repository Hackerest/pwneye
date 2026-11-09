import socket
from ipaddress import IPv4Interface
from dataclasses import dataclass

import ifaddr


@dataclass(frozen=True)
class InterfaceSelection:
    name: str | None
    ipv4: str | None
    prefixlen: int | None


def list_interface_names() -> list[str]:
    """
    Return the system network interface names currently visible to Python.
    """
    return [adapter.name for adapter in ifaddr.get_adapters(include_unconfigured=True)]


def list_discovery_interfaces() -> list[InterfaceSelection]:
    """Return one configured non-loopback IPv4 address per network interface."""
    selections = []
    seen = set()
    for adapter in ifaddr.get_adapters():
        if adapter.name in seen:
            continue
        for address in adapter.ips:
            ipv4 = _normalize_ipv4(address.ip)
            if ipv4 is None or ipv4 == "0.0.0.0":
                continue
            selections.append(InterfaceSelection(
                adapter.name, ipv4, _normalize_prefixlen(address.network_prefix),
            ))
            seen.add(adapter.name)
            break
    return selections


def get_interface_ipv4(name: str) -> str | None:
    """
    Resolve the primary IPv4 address associated with a network interface name.
    """
    adapter = _find_adapter(name)
    if adapter is None:
        return None

    for ip_address in adapter.ips:
        candidate = _normalize_ipv4(ip_address.ip)
        if candidate is not None:
            return candidate

    return None


def get_default_interface() -> InterfaceSelection:
    """
    Return the default outbound interface and IPv4 address when detectable.
    """
    local_ip = _get_default_local_ipv4()
    if local_ip is None:
        return InterfaceSelection(name=None, ipv4=None, prefixlen=None)

    for adapter in ifaddr.get_adapters(include_unconfigured=True):
        for ip_address in adapter.ips:
            if _normalize_ipv4(ip_address.ip) == local_ip:
                return InterfaceSelection(
                    name=adapter.name,
                    ipv4=local_ip,
                    prefixlen=_normalize_prefixlen(getattr(ip_address, "network_prefix", None)),
                )

    return InterfaceSelection(name=None, ipv4=local_ip, prefixlen=None)


def resolve_interface_selection(name: str) -> InterfaceSelection:
    """
    Resolve a user-specified interface name to an interface selection.
    """
    adapter = _find_adapter(name)
    if adapter is None:
        return InterfaceSelection(name=name, ipv4=None, prefixlen=None)

    for ip_address in adapter.ips:
        candidate = _normalize_ipv4(ip_address.ip)
        if candidate is None:
            continue

        return InterfaceSelection(
            name=adapter.name,
            ipv4=candidate,
            prefixlen=_normalize_prefixlen(getattr(ip_address, "network_prefix", None)),
        )

    return InterfaceSelection(name=adapter.name, ipv4=None, prefixlen=None)


def _get_default_local_ipv4() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
            return str(local_ip) if local_ip else None
    except OSError:
        return None


def _find_adapter(name: str) -> ifaddr.Adapter | None:
    expected_name = name.strip().lower()
    for adapter in ifaddr.get_adapters(include_unconfigured=True):
        if adapter.name.lower() == expected_name:
            return adapter
        if adapter.nice_name and adapter.nice_name.lower() == expected_name:
            return adapter
    return None


def _normalize_ipv4(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if ":" in value or value.startswith("127."):
        return None
    return value


def format_interface_address(selection: InterfaceSelection) -> str | None:
    if selection.ipv4 is None:
        return None
    if selection.prefixlen is None:
        return selection.ipv4
    return f"{selection.ipv4}/{selection.prefixlen}"


def format_interface_subnet(selection: InterfaceSelection) -> str | None:
    if selection.ipv4 is None or selection.prefixlen is None:
        return None

    try:
        return str(IPv4Interface(f"{selection.ipv4}/{selection.prefixlen}").network)
    except ValueError:
        return None


def _normalize_prefixlen(value: object) -> int | None:
    if isinstance(value, int) and 0 <= value <= 32:
        return value
    return None
